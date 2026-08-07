"""Async Kubernetes client wrapper. The only module that talks to the API server."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, cast
from urllib.parse import quote, urlencode

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio import watch as k8s_watch
from kubernetes_asyncio.stream import WsApiClient

from korvid.k8s.columns import CustomColumn, evaluate_all
from korvid.k8s.components import ComponentRef, manifest_components
from korvid.k8s.csp import ProviderInfo, detect_provider
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.drain import DrainPlan, build_drain_plan
from korvid.k8s.dryrun import diff_manifests
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import (
    HELM_SECRET_TYPE,
    HelmReleaseSummary,
    HelmRevisionSummary,
    ReleaseTracker,
    decode_release,
    release_detail,
    release_from_secret,
    revision_from_secret,
)
from korvid.k8s.logs import LogLine
from korvid.k8s.metrics import PodMetrics, parse_pod_metrics_list
from korvid.k8s.models import GenericSummary, PodSummary, summary_for
from korvid.k8s.reads import ReadOps
from korvid.k8s.telemetry import ReadOperation, ReadTelemetry, ReadTelemetryEvent
from korvid.k8s.writes import WriteOps

logger = logging.getLogger(__name__)

#: Re-LIST cadence for kinds without a watch endpoint (OLM's packageserver,
#: issue #141): catalog-ish content changes rarely, so a slow poll keeps the
#: view fresh without hammering an aggregated API.
LIST_POLL_INTERVAL = 30.0


def _path_segment(value: str) -> str:
    """Percent-encode *value* for safe use as a single URL path segment.

    Namespaces and names arrive from user and agent input; encoding prevents
    ``/`` from altering the request path. Empty and dot segments are rejected
    outright: quote() leaves ``.`` intact, so ``.`` or ``..`` would survive as
    a literal traversal segment that an HTTP stack can normalize away from
    the intended (and, for writes, approved) object path.
    """
    if value in ("", ".", ".."):
        raise ValueError(f"invalid URL path segment: {value!r}")
    return quote(value, safe="")


_DNS1123_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
_UID_RE = re.compile(r"^[a-fA-F0-9-]+$")


def _parse_log_line(pod: str, container: str, text: str) -> LogLine:
    """Split the kubelet ``timestamps=true`` RFC3339 prefix off a log line.

    ``datetime.fromisoformat`` (3.11+) accepts the RFC3339Nano form kubelet
    emits, truncating nanoseconds to microseconds. Unparsable prefixes leave
    the line untouched with ``timestamp=None``.
    """
    ts_str, _, rest = text.partition(" ")
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return LogLine(pod=pod, container=container, text=text)
    return LogLine(pod=pod, container=container, text=rest, timestamp=ts)


def resolve_context_name(context: str | None = None, config_file: str | None = None) -> str | None:
    """Return the effective kubeconfig context name, or None if unresolvable.

    Used to pin kubectl subprocesses (shell/debug) with ``--context`` so a
    ``kubectl config use-context`` in another terminal cannot retarget them
    at a different cluster mid-session.
    """
    if context:
        return context
    try:
        _, active = k8s_config.list_kube_config_contexts(config_file=config_file)
    except Exception:
        return None
    if not active:
        return None
    name = active.get("name")
    return str(name) if name else None


def resolve_context_namespace(
    context: str | None = None, config_file: str | None = None
) -> str | None:
    """Return the kubeconfig context's default namespace, or None if unset.

    RBAC-limited fallback (issue #49): when cluster-wide `list namespaces` is
    forbidden, the context's own namespace is the one namespace the user
    demonstrably works in — seed the picker/watch fallback with it.
    """
    try:
        contexts, active = k8s_config.list_kube_config_contexts(config_file=config_file)
    except Exception:
        return None
    entry: Any = active
    if context:
        entry = next((c for c in contexts if c.get("name") == context), None)
    if not entry:
        return None
    namespace = (entry.get("context") or {}).get("namespace")
    return str(namespace) if namespace else None


def list_context_names(config_file: str | None = None) -> tuple[list[str], str | None]:
    """Return all kubeconfig context names plus the active one (issue #36).

    Feeds the `:ctx` picker. An unreadable kubeconfig yields `([], None)`
    rather than raising — the picker then explains there is nothing to
    switch to.
    """
    try:
        contexts, active = k8s_config.list_kube_config_contexts(config_file=config_file)
    except Exception:
        return [], None
    names = [str(c["name"]) for c in contexts if c.get("name")]
    active_name = str(active["name"]) if active and active.get("name") else None
    return names, active_name


#: Bound on the `:ctx` auth probe round trip (issue #36): a wedged target
#: cluster must fail the switch quickly instead of hanging the flow.
_PROBE_TIMEOUT = 10.0
_EXEC_CREDENTIAL_REFRESH_SKEW = timedelta(minutes=5)
_EXEC_CREDENTIAL_GENERATION_ATTR = "_korvid_exec_credential_generation"


def _exec_credential_expires_soon(loader: object) -> bool:
    expiry = getattr(loader, "exec_plugin_expiry", None)
    if not isinstance(expiry, datetime):
        return False
    return expiry - _EXEC_CREDENTIAL_REFRESH_SKEW <= datetime.now(tz=expiry.tzinfo)


async def load_refreshable_kube_config(
    *,
    context: str | None,
    client_configuration: k8s_client.Configuration,
    persist_config: bool,
) -> None:
    """Load kubeconfig and refresh expiring exec credentials before API calls."""
    loader = await k8s_config.load_kube_config(
        context=context,
        client_configuration=client_configuration,
        persist_config=persist_config,
    )
    if not isinstance(getattr(loader, "exec_plugin_expiry", None), datetime):
        return

    refresh_lock = asyncio.Lock()
    refresh_generation = 0
    setattr(client_configuration, _EXEC_CREDENTIAL_GENERATION_ATTR, refresh_generation)

    async def _refresh(configuration: k8s_client.Configuration) -> None:
        nonlocal refresh_generation
        configuration_generation = getattr(configuration, _EXEC_CREDENTIAL_GENERATION_ATTR, 0)
        if configuration_generation == refresh_generation and not _exec_credential_expires_soon(
            loader
        ):
            return
        async with refresh_lock:
            configuration_generation = getattr(configuration, _EXEC_CREDENTIAL_GENERATION_ATTR, 0)
            if configuration_generation < refresh_generation:
                await asyncio.wait_for(loader.load_and_set(configuration), _PROBE_TIMEOUT)
                setattr(configuration, _EXEC_CREDENTIAL_GENERATION_ATTR, refresh_generation)
            elif _exec_credential_expires_soon(loader):
                await asyncio.wait_for(loader.load_and_set(configuration), _PROBE_TIMEOUT)
                refresh_generation += 1
                setattr(configuration, _EXEC_CREDENTIAL_GENERATION_ATTR, refresh_generation)

    client_configuration.refresh_api_key_hook = _refresh


class KubeClient(ReadOps, WriteOps):
    """Thin wrapper over kubernetes_asyncio; returns typed summaries."""

    def __init__(
        self,
        custom_columns: Mapping[str, tuple[CustomColumn, ...]] | None = None,
        *,
        read_telemetry: ReadTelemetry | None = None,
    ) -> None:
        self._api: k8s_client.ApiClient | None = None
        self._core_v1: k8s_client.CoreV1Api | None = None
        self._ssar_warned = False
        #: User-configured extra table columns (issue #45), keyed by plural
        #: kind; values are extracted from the raw manifest at summary time
        #: (the manifest is discarded afterwards). Secrets are dropped as
        #: defense in depth: their values only render through the masking
        #: pipeline, never through raw-manifest extraction.
        self._custom_columns: Mapping[str, tuple[CustomColumn, ...]] = {
            kind: columns for kind, columns in (custom_columns or {}).items() if kind != "secrets"
        }
        #: pods/resize discovery result; None until the first successful check.
        self._pod_resize_supported: bool | None = None
        #: cloud provider detection result; None until the first lookup.
        self._provider_info: ProviderInfo | None = None
        self._read_telemetry = read_telemetry

    @staticmethod
    def _namespaces_path() -> str:
        return "/api/v1/namespaces"

    @staticmethod
    def _pods_path(namespace: str | None) -> str:
        if namespace is None:
            return "/api/v1/pods"
        return f"/api/v1/namespaces/{_path_segment(namespace)}/pods"

    def _observe_read(
        self,
        operation: ReadOperation,
        path: str,
        *,
        payload: object | None = None,
        object_count: int = 0,
        status: int | None = None,
    ) -> None:
        """Report one read to the optional telemetry seam (issue #186).

        `decoded_bytes` is an exact canonical-JSON byte count, not an estimate:
        the benchmark's API-load accounting is compared across runs and
        profiles, so an approximation (e.g. `len(str(payload))`, which counts
        Python `repr` characters and misencodes non-ASCII, `True`, and `None`)
        would silently change the reported number without removing the work.
        The whole method is inert unless a caller opted into telemetry, and the
        measured cost of the exact count is ~8.8 ms for a 1,000-Pod
        cluster-wide LIST (0.4% of the 2 s LIST-to-render budget) and ~9 us per
        watch event (0.004% of the 250 ms event-to-render budget).
        """
        if self._read_telemetry is None:
            return
        decoded_bytes = 0
        if payload is not None:
            decoded_bytes = len(
                json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
            )
        self._read_telemetry(
            ReadTelemetryEvent(
                operation=operation,
                path=path,
                decoded_bytes=decoded_bytes,
                object_count=object_count,
                status=status,
            )
        )

    def _observe_read_error(
        self,
        path: str,
        exc: ApiStatusError | k8s_client.exceptions.ApiException,
    ) -> None:
        self._observe_read("error", path, status=int(getattr(exc, "status", 0) or 0))

    def _pod_summary(self, manifest: dict[str, Any]) -> PodSummary:
        """PodSummary + configured custom column values (issue #45)."""
        summary = PodSummary.from_manifest(manifest)
        columns = self._custom_columns.get("pods")
        if not columns:
            return summary
        return dataclasses.replace(summary, custom=evaluate_all(columns, manifest))

    def _object_summary(self, meta: ResourceMeta, manifest: dict[str, Any]) -> GenericSummary:
        """summary_for + configured custom column values (issue #45)."""
        summary = summary_for(meta.kind, manifest)
        columns = self._custom_columns.get(meta.plural)
        if not columns:
            return summary
        return dataclasses.replace(summary, custom=evaluate_all(columns, manifest))

    async def connect(self, context: str | None = None) -> None:
        configuration = k8s_client.Configuration()
        await load_refreshable_kube_config(
            context=context,
            client_configuration=configuration,
            persist_config=True,
        )
        k8s_client.Configuration.set_default(configuration)
        self._api = k8s_client.ApiClient(configuration)
        self._core_v1 = k8s_client.CoreV1Api(self._api)
        # A new connection may target a different cluster; discard any
        # capability discovered against the previous one.
        self._pod_resize_supported = None
        self._provider_info = None

    async def probe_context(self, context: str) -> None:
        """Validate that *context* resolves and authenticates (issue #36).

        Loads the kubeconfig into a private ``Configuration`` (never
        persisted, never the global default) and creates a
        SelfSubjectAccessReview — granted to every authenticated principal
        via ``system:basic-user`` but *not* to ``system:anonymous``, unlike
        `/version` which ``system:public-info-viewer`` serves without
        credentials. Expired or missing credentials therefore fail here,
        and the live connection is untouched either way — which is what
        lets a failed `:ctx` switch leave the old context fully usable.

        The timeout bounds the whole probe, including credential loading:
        ``load_kube_config`` may invoke an exec credential plugin, and a
        stalled plugin must not hang the switch forever.
        """

        async def _probe() -> None:
            probe_configuration = k8s_client.Configuration()
            await k8s_config.load_kube_config(
                context=context,
                client_configuration=probe_configuration,
                persist_config=False,
            )
            api = k8s_client.ApiClient(probe_configuration)
            try:
                review = k8s_client.V1SelfSubjectAccessReview(
                    spec=k8s_client.V1SelfSubjectAccessReviewSpec(
                        resource_attributes=k8s_client.V1ResourceAttributes(
                            verb="get", resource="pods"
                        )
                    )
                )
                await k8s_client.AuthorizationV1Api(api).create_self_subject_access_review(review)
            finally:
                await api.close()

        await asyncio.wait_for(_probe(), timeout=_PROBE_TIMEOUT)

    async def switch_context(self, context: str | None) -> None:
        """Retarget the live connection at *context* (issue #36).

        ``None`` means the kubeconfig's current-context — the recovery path
        needs it because the original startup context may itself have been
        the default.
        Call only after ``probe_context`` succeeded and all consumers of the
        old connection (watches, pollers, log streams) are stopped: the old
        ``ApiClient`` is closed here, which would otherwise kill their
        streams mid-read. The kubeconfig is loaded as the global default so
        per-session ``WsApiClient`` instances (exec/transfer) follow along.
        The load is bounded like the probe: an exec credential plugin that
        succeeded during the probe but stalls on this second invocation must
        surface as a timeout into the caller's recovery path, not hang the
        already-torn-down session forever.
        """
        old_api = self._api
        configuration = k8s_client.Configuration()
        await asyncio.wait_for(
            load_refreshable_kube_config(
                context=context,
                client_configuration=configuration,
                persist_config=True,
            ),
            _PROBE_TIMEOUT,
        )
        k8s_client.Configuration.set_default(configuration)
        self._api = k8s_client.ApiClient(configuration)
        self._core_v1 = k8s_client.CoreV1Api(self._api)
        # Per-connection caches describe the previous cluster.
        self._pod_resize_supported = None
        self._provider_info = None
        if old_api is not None:
            await old_api.close()

    async def list_namespaces(self) -> list[str]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        path = self._namespaces_path()
        try:
            resp = await self._core_v1.list_namespace(_preload_content=False)
            data = await _to_dict(resp)
        except ApiStatusError as exc:
            self._observe_read_error(path, exc)
            raise
        except k8s_client.exceptions.ApiException as exc:
            self._observe_read_error(path, exc)
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        items = data.get("items", [])
        self._observe_read("list", path, payload=data, object_count=len(items))
        return [item["metadata"]["name"] for item in items]

    async def detect_cloud_provider(self) -> ProviderInfo:
        """Detect the cluster's cloud provider from a few nodes (issue #30).

        Cached per connection; connect() discards the cache. Any API failure
        (RBAC-limited users often cannot list nodes cluster-wide) yields
        "unknown", also cached — detection is a hint, never worth retry churn.
        """
        if self._provider_info is not None:
            return self._provider_info
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._core_v1.list_node(limit=5, _preload_content=False)
            data = await _to_dict(resp)
        except Exception as exc:
            # Best-effort probe: RBAC denials arrive as ApiException, but
            # transport failures (DNS, TLS, connection reset) surface as
            # ordinary aiohttp/OS errors — none of them may abort startup.
            # Cancellation (BaseException) still propagates.
            logger.debug("cloud provider detection failed: %s", exc)
            self._provider_info = detect_provider([])
            return self._provider_info
        self._provider_info = detect_provider(data.get("items", []))
        return self._provider_info

    def open_pod_exec(
        self,
        namespace: str,
        pod: str,
        container: str | None,
        command: list[str],
        *,
        stdin: bool,
    ) -> AbstractAsyncContextManager[Any]:
        """Open an exec websocket against a pod for streaming I/O (issue #47).

        Yields the raw connection (channel-framed messages, ``send_bytes``);
        `korvid.core.transfer` speaks the frame protocol on top of it. A
        dedicated ``WsApiClient`` is created per session — it shares the
        kubeconfig ``connect()`` loaded — and closed with the session.
        """
        if self._core_v1 is None or self._api is None:
            raise RuntimeError("connect() first")
        configuration = self._api.configuration

        @asynccontextmanager
        async def _session() -> AsyncIterator[Any]:
            ws_api = WsApiClient(configuration)
            try:
                core = k8s_client.CoreV1Api(ws_api)
                kwargs: dict[str, Any] = {
                    "command": command,
                    "stderr": True,
                    "stdin": stdin,
                    "stdout": True,
                    "tty": False,
                    "_preload_content": False,
                }
                if container is not None:
                    kwargs["container"] = container
                # The generated stubs type exec responses as `str` (the
                # preloaded form); with _preload_content=False the WsApiClient
                # returns the websocket context manager instead.
                ws_ctx = cast(
                    AbstractAsyncContextManager[Any],
                    await core.connect_get_namespaced_pod_exec(pod, namespace, **kwargs),
                )
                async with ws_ctx as ws:
                    yield ws
            finally:
                await ws_api.close()

        return _session()

    async def list_pods(self, namespace: str) -> list[PodSummary]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        path = self._pods_path(namespace)
        try:
            resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
            data = await _to_dict(resp)
        except ApiStatusError as exc:
            self._observe_read_error(path, exc)
            raise
        except k8s_client.exceptions.ApiException as exc:
            self._observe_read_error(path, exc)
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        items = data.get("items", [])
        self._observe_read("list", path, payload=data, object_count=len(items))
        return [self._pod_summary(item) for item in items]

    async def watch_pods(self, namespace: str | None) -> AsyncIterator[tuple[str, PodSummary]]:
        """LIST then watch pods; namespace=None watches cluster-wide."""
        if namespace is not None:
            async for item in self._watch_pods_namespaced(namespace):
                yield item
        else:
            async for item in self._watch_pods_cluster():
                yield item

    async def _watch_pods_namespaced(self, namespace: str) -> AsyncIterator[tuple[str, PodSummary]]:
        """Per-namespace pod watch via CoreV1Api (LIST then stream)."""
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        path = self._pods_path(namespace)

        # LIST first: yield pre-existing pods as ADDED and anchor the watch at
        # the snapshot's resourceVersion so no events are missed between LIST
        # and Watch.  A 410-Gone mid-stream propagates to WatchManager, which
        # retries by calling watch_pods again — the fresh call re-LISTs,
        # which is the intended relist fallback; no separate 410 handling needed.
        try:
            resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
            data = await _to_dict(resp)
        except ApiStatusError as exc:
            self._observe_read_error(path, exc)
            raise
        except k8s_client.exceptions.ApiException as exc:
            self._observe_read_error(path, exc)
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

        items = data.get("items", [])
        self._observe_read("list", path, payload=data, object_count=len(items))
        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in items:
            yield ("ADDED", self._pod_summary(item))

        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        w = k8s_watch.Watch()
        self._observe_read("watch_open", path)
        try:
            async with w.stream(
                self._core_v1.list_namespaced_pod, namespace, **watch_kwargs
            ) as stream:
                async for event in stream:
                    raw_object = event["raw_object"]
                    self._observe_read("watch_event", path, payload=raw_object, object_count=1)
                    yield (
                        str(event["type"]),
                        self._pod_summary(raw_object),
                    )
        except ApiStatusError as exc:
            self._observe_read_error(path, exc)
            raise
        except k8s_client.exceptions.ApiException as exc:
            self._observe_read_error(path, exc)
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def _watch_pods_cluster(self) -> AsyncIterator[tuple[str, PodSummary]]:
        """Cluster-wide pod watch via raw /api/v1/pods path (LIST then stream)."""
        if self._api is None:
            raise RuntimeError("connect() first")

        path = self._pods_path(None)
        try:
            data = await self._request_json(path)
        except ApiStatusError as exc:
            self._observe_read_error(path, exc)
            raise

        items = data.get("items", [])
        self._observe_read("list", path, payload=data, object_count=len(items))
        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in items:
            yield ("ADDED", self._pod_summary(item))

        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        watch_func = self._make_raw_watch_callable(path)
        w = k8s_watch.Watch()
        self._observe_read("watch_open", path)
        try:
            async with w.stream(watch_func, **watch_kwargs) as stream:
                async for event in stream:
                    raw_object = event["raw_object"]
                    self._observe_read("watch_event", path, payload=raw_object, object_count=1)
                    yield (
                        str(event["type"]),
                        self._pod_summary(raw_object),
                    )
        except ApiStatusError as exc:
            self._observe_read_error(path, exc)
            raise
        except k8s_client.exceptions.ApiException as exc:
            self._observe_read_error(path, exc)
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    def _list_path(self, meta: ResourceMeta, namespace: str | None) -> str:
        """LIST/WATCH path for a kind; cluster-scoped kinds have no
        namespaced path regardless of scope."""
        if namespace is not None and meta.namespaced:
            return f"{meta.api_base}/namespaces/{_path_segment(namespace)}/{meta.plural}"
        return f"{meta.api_base}/{meta.plural}"

    async def _initial_object_snapshot(
        self, meta: ResourceMeta, namespace: str | None
    ) -> tuple[str, str | None, list[GenericSummary], dict[str, GenericSummary]]:
        list_path = self._list_path(meta, namespace)
        try:
            data = await self._request_json(list_path)
        except ApiStatusError as exc:
            self._observe_read_error(list_path, exc)
            raise

        items = data.get("items", [])
        self._observe_read("list", list_path, payload=data, object_count=len(items))
        resource_version = (data.get("metadata") or {}).get("resourceVersion")
        summaries: list[GenericSummary] = []
        known: dict[str, GenericSummary] = {}
        for item in items:
            summary = self._object_summary(meta, item)
            summaries.append(summary)
            known[f"{summary.namespace}/{summary.name}"] = summary
        return list_path, resource_version, summaries, known

    def _watch_objects_requires_poll_fallback(
        self,
        list_path: str,
        exc: ApiStatusError | k8s_client.exceptions.ApiException,
    ) -> bool:
        status = int(getattr(exc, "status", 0) or 0)
        self._observe_read_error(list_path, exc)
        if status == 405:
            return True
        raise ApiStatusError(
            status,
            str(getattr(exc, "reason", "") or ""),
            str(getattr(exc, "body", "") or ""),
        ) from exc

    async def watch_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> AsyncIterator[tuple[str, GenericSummary]]:
        """LIST then watch any resource kind; None namespace = all namespaces.

        Contract mirrors watch_pods: pre-existing items are yielded as ADDED
        first, then live watch events from the snapshot resourceVersion.
        ApiException is wrapped as ApiStatusError at both the LIST and watch phases.

        Kinds whose server offers no watch (``meta.watchable`` False, or a
        server that advertises watch and then rejects it with 405 - OLM's
        packageserver, issue #141) degrade to periodic re-LIST diffing: the
        stream stays alive and incremental, so the view keeps rendering
        without the clear/retry/die loop.
        """
        if self._api is None:
            raise RuntimeError("connect() first")

        # LIST phase --------------------------------------------------------
        list_path, resource_version, initial_summaries, known = await self._initial_object_snapshot(
            meta, namespace
        )
        for summary in initial_summaries:
            yield ("ADDED", summary)

        if not meta.watchable:
            async for event in self._poll_objects(meta, list_path, known):
                yield event
            return

        # Watch phase -------------------------------------------------------
        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        watch_func = self._make_raw_watch_callable(list_path)

        w = k8s_watch.Watch()
        self._observe_read("watch_open", list_path)
        try:
            async with w.stream(watch_func, **watch_kwargs) as stream:
                async for event in stream:
                    raw_object = event["raw_object"]
                    self._observe_read("watch_event", list_path, payload=raw_object, object_count=1)
                    yield (
                        str(event["type"]),
                        self._object_summary(meta, raw_object),
                    )
        except (k8s_client.exceptions.ApiException, ApiStatusError) as exc:
            # The raw-watch adapter surfaces HTTP errors as ApiStatusError
            # (via _raise_for_status); the kubernetes client's own paths
            # raise ApiException - both carry .status/.reason, and the 405
            # fallback must catch both.
            if self._watch_objects_requires_poll_fallback(list_path, exc):
                # Discovery advertised watch but the server refuses it: as
                # deterministic as it gets - poll instead of letting the
                # manager burn retries clearing and re-seeding the store.
                logger.info("%s rejects watch (405); falling back to LIST polling", meta.plural)
                async for event in self._poll_objects(meta, list_path, known):
                    yield event

    async def _poll_objects(
        self, meta: ResourceMeta, list_path: str, known: dict[str, GenericSummary]
    ) -> AsyncIterator[tuple[str, GenericSummary]]:
        """Endless re-LIST diff stream for kinds without a watch endpoint.

        Each round upserts every present row (ADDED doubles as MODIFIED in
        the store) and emits DELETED for rows that vanished since the last
        round, so the table stays incremental - never cleared. *known* is
        seeded with the initial LIST's rows.
        """
        while True:
            await asyncio.sleep(LIST_POLL_INTERVAL)
            try:
                data = await self._request_json(list_path)
            except ApiStatusError as exc:
                self._observe_read_error(list_path, exc)
                raise
            items = data.get("items", [])
            self._observe_read("list", list_path, payload=data, object_count=len(items))
            current: dict[str, GenericSummary] = {}
            for item in items:
                summary = self._object_summary(meta, item)
                current[f"{summary.namespace}/{summary.name}"] = summary
                yield ("ADDED", summary)
            for key, old in known.items():
                if key not in current:
                    yield ("DELETED", old)
            known = current

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        """LIST any resource kind and return GenericSummary items.

        Reuses the path logic of watch_objects' LIST phase.
        ApiException is wrapped as ApiStatusError.
        """
        if self._api is None:
            raise RuntimeError("connect() first")
        path = self._list_path(meta, namespace)
        try:
            data = await self._request_json(path)
        except ApiStatusError as exc:
            self._observe_read_error(path, exc)
            raise
        items = data.get("items", [])
        self._observe_read("list", path, payload=data, object_count=len(items))
        return [self._object_summary(meta, item) for item in items]

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        """Fetch the raw manifest for a single object. ApiException → ApiStatusError."""
        path = self._object_path(meta, namespace, name)
        try:
            result = await self._request_json(path)
        except ApiStatusError as exc:
            # Every other read path records the failure before propagating;
            # without this a denied or throttled GET is a hole in the telemetry.
            self._observe_read_error(path, exc)
            raise
        self._observe_read("get", path, payload=result, object_count=1)
        return result

    # Helm release browsing (issue #28) ----------------------------------
    # Releases are Secrets of type helm.sh/release.v1; the synthetic kinds
    # "helmreleases"/"helmrevisions" adapt the Secret stream - no helm binary.

    def _helm_secrets_query(self, *, name: str | None = None) -> list[tuple[str, str]]:
        """Selectors restricting the Secret stream to helm-owned release
        Secrets (type + owner label; a non-helm Secret reusing the type must
        not surface as a release), optionally pinned to one release name."""
        label = "owner=helm" if name is None else f"owner=helm,name={name}"
        return [("fieldSelector", f"type={HELM_SECRET_TYPE}"), ("labelSelector", label)]

    @staticmethod
    def _helm_secrets_base(namespace: str | None) -> str:
        return (
            f"/api/v1/namespaces/{_path_segment(namespace)}/secrets"
            if namespace is not None
            else "/api/v1/secrets"
        )

    async def _watch_helm_secrets(
        self, namespace: str | None
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """LIST then watch helm release Secrets; same contract as watch_objects."""
        if self._api is None:
            raise RuntimeError("connect() first")
        base = self._helm_secrets_base(namespace)
        params = self._helm_secrets_query()
        data = await self._request_json(f"{base}?{urlencode(params)}")
        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in data.get("items", []):
            yield ("ADDED", item)
        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version
        # The watch adapter appends its own query params: hand it the bare
        # path plus the selectors, never a path with the query pre-embedded.
        watch_func = self._make_raw_watch_callable(base, extra_query=params)
        w = k8s_watch.Watch()
        try:
            async with w.stream(watch_func, **watch_kwargs) as stream:
                async for event in stream:
                    yield (str(event["type"]), event["raw_object"])
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def watch_helm_releases(
        self, namespace: str | None
    ) -> AsyncIterator[tuple[str, HelmReleaseSummary]]:
        """Release rows (latest revision per release) from the Secret stream."""
        tracker = ReleaseTracker()
        async for event_type, secret in self._watch_helm_secrets(namespace):
            for out in tracker.apply(event_type, release_from_secret(secret)):
                yield out

    async def list_helm_releases(self, namespace: str | None) -> list[HelmReleaseSummary]:
        """Latest revision per release, LIST-only (the helm_list_releases
        tool, issue #161): same Secret parsing as the browser's synthetic
        kind — no helm binary involved."""
        if self._api is None:
            raise RuntimeError("connect() first")
        base = self._helm_secrets_base(namespace)
        data = await self._request_json(f"{base}?{urlencode(self._helm_secrets_query())}")
        latest: dict[tuple[str, str], HelmReleaseSummary] = {}
        for item in data.get("items", []):
            release = release_from_secret(item)
            key = (release.namespace, release.name)
            current = latest.get(key)
            if current is None or release.revision > current.revision:
                latest[key] = release
        return sorted(latest.values(), key=lambda r: (r.namespace, r.name))

    async def watch_helm_revisions(
        self, namespace: str | None
    ) -> AsyncIterator[tuple[str, HelmRevisionSummary]]:
        """One row per revision Secret (drill-down history under a release)."""
        async for event_type, secret in self._watch_helm_secrets(namespace):
            yield (event_type, revision_from_secret(secret))

    @staticmethod
    def _helm_revision(secret: dict[str, Any]) -> int:
        labels = (secret.get("metadata") or {}).get("labels") or {}
        try:
            return int(labels.get("version") or 0)
        except ValueError:
            return 0

    async def _helm_release_secret(
        self, namespace: str, name: str, revision: int | None = None
    ) -> dict[str, Any]:
        """The release's revision Secret (latest, or the requested revision).

        Raises ApiStatusError(404) when no matching revision Secret exists.
        """
        base = self._helm_secrets_base(namespace)
        path = f"{base}?{urlencode(self._helm_secrets_query(name=name))}"
        data = await self._request_json(path)
        items = list(data.get("items", []))
        if revision is not None:
            items = [s for s in items if self._helm_revision(s) == revision]
        if not items:
            raise ApiStatusError(404, f"helm release {name!r} not found in {namespace!r}")
        return max(items, key=self._helm_revision)

    async def get_helm_release_components(self, namespace: str, name: str) -> list[ComponentRef]:
        """Component refs from the latest revision's rendered manifest.

        Raises ApiStatusError(404) when the release does not exist; an
        undecodable payload degrades to an empty component list (the tree
        then shows only the root, matching the browser's label fallback).
        """
        chosen = await self._helm_release_secret(namespace, name)
        try:
            payload = decode_release(chosen)
        except ValueError:
            return []
        return manifest_components(payload.get("manifest"))

    async def get_helm_release(
        self, namespace: str, name: str, revision: int | None = None
    ) -> dict[str, Any]:
        """Decoded release detail for describe: metadata plus user-supplied
        values; the rendered manifest is deliberately dropped (it is the
        full template output and drowns the describe view).

        Raises ApiStatusError(404) when no matching revision Secret exists;
        an undecodable payload degrades to label-only detail with a
        ``warning`` key (the browser lists such releases via the same
        fallback, so describe must not fail where the row still shows).
        """
        chosen = await self._helm_release_secret(namespace, name, revision)
        _rev = self._helm_revision
        labels = (chosen.get("metadata") or {}).get("labels") or {}
        try:
            payload = decode_release(chosen)
        except ValueError:
            # The browser lists this release via the label fallback; describe
            # must degrade the same way, not error where the row still shows.
            detail = release_detail({}, name=name, namespace=namespace, revision=_rev(chosen))
            detail["status"] = str(labels.get("status") or "")
            detail["warning"] = "release payload could not be decoded; label-only detail"
            return detail
        detail = release_detail(payload, name=name, namespace=namespace, revision=_rev(chosen))
        if not detail["status"]:
            # A payload can decode while ``info`` is missing or malformed;
            # the row shows the Secret's status label, so describe must too.
            detail["status"] = str(labels.get("status") or "")
        return detail

    async def list_pod_metrics(self, namespace: str | None) -> list[PodMetrics]:
        """Current pod usage from metrics.k8s.io; None lists all namespaces.

        Raises ApiStatusError (404 when metrics-server is not installed,
        403 without RBAC) so callers can degrade gracefully.
        """
        if namespace is not None:
            path = f"/apis/metrics.k8s.io/v1beta1/namespaces/{_path_segment(namespace)}/pods"
        else:
            path = "/apis/metrics.k8s.io/v1beta1/pods"
        return parse_pod_metrics_list(await self._request_json(path))

    @staticmethod
    def _object_path(meta: ResourceMeta, namespace: str | None, name: str) -> str:
        if meta.namespaced and namespace is not None:
            return (
                f"{meta.api_base}/namespaces/{_path_segment(namespace)}"
                f"/{meta.plural}/{_path_segment(name)}"
            )
        return f"{meta.api_base}/{meta.plural}/{_path_segment(name)}"

    async def _request_write(
        self,
        path: str,
        method: str,
        body: dict[str, Any] | None = None,
        content_type: str | None = None,
        query_params: list[tuple[str, str]] | None = None,
    ) -> bytes:
        """Mutating request through the ApiClient; wraps ApiException as
        ApiStatusError. Returns the raw response body (dry-run previews parse
        the would-be object out of it; plain writes ignore it)."""
        if self._api is None:
            raise RuntimeError("connect() first")
        header_params: dict[str, str] = {}
        if content_type is not None:
            header_params["Content-Type"] = content_type
        try:
            resp = await self._api.call_api(
                path,
                method,
                auth_settings=["BearerToken"],
                header_params=header_params,
                body=body,
                query_params=query_params or [],
                _preload_content=False,
            )
            # Drain the body so the pooled HTTP connection is released; with
            # _preload_content=False the caller owns the response. Writes may
            # return empty or non-JSON bodies, so no decode is attempted here.
            raw: bytes = await resp.read()
            _raise_for_status(resp, raw)
            return raw
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(
                int(exc.status or 0),
                str(exc.reason or ""),
                body=str(getattr(exc, "body", "") or ""),
            ) from exc

    async def can_i(
        self,
        verb: str,
        resource: str,
        subresource: str,
        namespace: str | None,
        group: str = "",
        name: str = "",
    ) -> bool:
        """SelfSubjectAccessReview permission pre-check (spec: RBAC check at
        the approval gate). Fails open on infrastructure errors: the write is
        still approval-gated and audited, and SSAR itself may be forbidden."""
        if self._api is None:
            raise RuntimeError("connect() first")
        attrs: dict[str, Any] = {"verb": verb, "resource": resource}
        if group:
            attrs["group"] = group
        if name:
            attrs["name"] = name
        if subresource:
            attrs["subresource"] = subresource
        if namespace:
            attrs["namespace"] = namespace
        body = {
            "apiVersion": "authorization.k8s.io/v1",
            "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": attrs},
        }
        try:
            resp = await self._api.call_api(
                "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                "POST",
                auth_settings=["BearerToken"],
                header_params={"Content-Type": "application/json"},
                body=body,
                _preload_content=False,
            )
            data = await _to_dict(resp)
        except Exception:
            # Fail-open, but make the silently disabled pre-check visible to
            # operators: warn on the first failure, debug afterwards.
            if self._ssar_warned:
                logger.debug("SelfSubjectAccessReview failed; allowing (fail-open)", exc_info=True)
            else:
                self._ssar_warned = True
                logger.warning(
                    "SelfSubjectAccessReview failed; permission pre-checks are"
                    " disabled (fail-open) - writes remain approval-gated and audited",
                    exc_info=True,
                )
            return True
        return bool((data.get("status") or {}).get("allowed", False))

    @staticmethod
    def _delete_body(uid: str | None) -> dict[str, Any]:
        """DeleteOptions body for a delete. propagationPolicy is stated
        explicitly: an omitted policy lets existing finalizers or the
        resource-specific default pick something else (e.g. orphan), which
        would contradict the cascade note preview_delete shows the user.
        A ``uid`` precondition pins the exact object incarnation being
        approved. Keep preview_delete's note in sync with this body."""
        body: dict[str, Any] = {"propagationPolicy": "Background"}
        if uid:
            body["preconditions"] = {"uid": uid}
        return body

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        """DELETE a single object. A ``uid`` precondition pins the exact object
        incarnation that was approved: if the object was deleted and recreated
        under the same name meanwhile, the API server refuses with 409 instead
        of deleting the replacement. ApiException → ApiStatusError."""
        await self._request_write(
            self._object_path(meta, namespace, name),
            "DELETE",
            body=self._delete_body(uid),
            content_type="application/json",
        )

    @staticmethod
    def _scale_patch(replicas: int, uid: str | None) -> dict[str, Any]:
        """Merge-patch body for the /scale subresource. A ``uid`` in the
        patched metadata is an apiserver precondition: the patch is rejected
        with 409 when the object was recreated. Shared by the real write and
        its dry-run preview so the two can never drift apart."""
        body: dict[str, Any] = {"spec": {"replicas": replicas}}
        if uid:
            body["metadata"] = {"uid": uid}
        return body

    @staticmethod
    def _restart_patch(uid: str | None, restarted_at: str | None) -> dict[str, Any]:
        """Strategic-merge-patch body for a rolling restart, the way kubectl
        does it: stamp the pod template with a restartedAt annotation. Shared
        by the real write and its dry-run preview; the caller passes the same
        ``restarted_at`` (from writes.restart_stamp) to both so the previewed
        request is byte-identical to the executed one. A missing stamp falls
        back to now() for direct callers outside the approval flow."""
        stamp = restarted_at or datetime.now().astimezone().isoformat()
        body: dict[str, Any] = {
            "spec": {
                "template": {
                    "metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": stamp}}
                }
            }
        }
        if uid:
            body["metadata"] = {"uid": uid}
        return body

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        """Set spec.replicas via the /scale subresource (merge patch)."""
        await self._request_write(
            f"{self._object_path(meta, namespace, name)}/scale",
            "PATCH",
            body=self._scale_patch(replicas, uid),
            content_type="application/merge-patch+json",
        )

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        """Trigger a rolling restart by patching the pod template."""
        await self.rollout_restart_with_stamp(meta, namespace, name, uid=uid)

    async def rollout_restart_with_stamp(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> None:
        """Restart whose patch body carries the caller-provided stamp, so the
        approved write is byte-identical to the previewed dry run."""
        await self._request_write(
            self._object_path(meta, namespace, name),
            "PATCH",
            body=self._restart_patch(uid, restarted_at),
            content_type="application/strategic-merge-patch+json",
        )

    @staticmethod
    def _resize_patch(
        resources: dict[str, dict[str, dict[str, str]]], uid: str | None
    ) -> dict[str, Any]:
        """Strategic-merge-patch body for the pods/resize subresource
        (issue #27): per-container resources keyed by name, so quantities not
        named are kept. A ``uid`` in metadata is the same apiserver
        precondition the other writes use. Shared by the real write and its
        dry-run preview so the two can never drift apart."""
        containers = [{"name": container, "resources": res} for container, res in resources.items()]
        body: dict[str, Any] = {"spec": {"containers": containers}}
        if uid:
            body["metadata"] = {"uid": uid}
        return body

    def _pod_resize_path(self, namespace: str, name: str) -> str:
        return f"{self._object_path(PODS_META, namespace, name)}/resize"

    async def resize_pod(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> None:
        """In-place resize of a running pod (1.35 GA): PATCH the
        ``pods/resize`` subresource with new requests/limits."""
        await self._request_write(
            self._pod_resize_path(namespace, name),
            "PATCH",
            body=self._resize_patch(resources, uid),
            content_type="application/strategic-merge-patch+json",
        )

    async def supports_pod_resize(self) -> bool:
        """Whether this cluster exposes the ``pods/resize`` subresource
        (stable in 1.35). Cached per connection once discovery succeeds; a
        transient discovery failure answers False without caching, so the
        feature is not permanently disabled by one bad round trip."""
        if self._pod_resize_supported is None:
            try:
                core = await self._request_json("/api/v1")
            except Exception:
                logger.debug("pods/resize discovery failed", exc_info=True)
                return False
            # The subresource must both exist and advertise patch: an
            # aggregated or restricted apiserver may list it without the one
            # verb resize_pod() needs, and offering the feature there would
            # make every attempt fail.
            self._pod_resize_supported = any(
                r.get("name") == "pods/resize" and "patch" in (r.get("verbs") or [])
                for r in core.get("resources", [])
            )
        return self._pod_resize_supported

    async def preview_resize(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        """Diff of the pod before vs after a dry-run resize. ``uid``
        semantics match ``preview_scale``; the dry run is pinned to the GET
        snapshot's resourceVersion (see ``_pin_revision``). None on any
        failure: a preview must never block the approval flow."""
        path = self._pod_resize_path(namespace, name)
        try:
            current = await self._request_json(self._object_path(PODS_META, namespace, name))
            proposed = await self._dry_run(
                path,
                "PATCH",
                self._pin_revision(self._resize_patch(resources, uid), current),
                "application/strategic-merge-patch+json",
            )
        except Exception:
            logger.debug("resize dry-run preview failed", exc_info=True)
            return None
        return diff_manifests(current, proposed)

    @staticmethod
    def _cordon_patch(unschedulable: bool, uid: str | None) -> dict[str, Any]:
        """Strategic-merge-patch body for cordon/uncordon. A ``uid`` in the
        patched metadata is an apiserver precondition (409 when the node
        object was recreated). Shared by the real write and its dry-run
        preview so the two can never drift apart."""
        body: dict[str, Any] = {"spec": {"unschedulable": unschedulable}}
        if uid:
            body["metadata"] = {"uid": uid}
        return body

    @staticmethod
    def _node_path(name: str) -> str:
        return f"/api/v1/nodes/{_path_segment(name)}"

    async def cordon_node(self, name: str, unschedulable: bool, *, uid: str | None = None) -> None:
        """Cordon (or uncordon) a node by patching ``spec.unschedulable``,
        the way kubectl does it. A ``uid`` precondition pins the exact node
        object incarnation that was approved."""
        await self._request_write(
            self._node_path(name),
            "PATCH",
            body=self._cordon_patch(unschedulable, uid),
            content_type="application/strategic-merge-patch+json",
        )

    async def preview_cordon(
        self, name: str, unschedulable: bool, *, uid: str | None = None
    ) -> list[str] | None:
        """Diff of the node before vs after a dry-run cordon/uncordon.
        ``uid`` semantics match ``preview_scale``; the dry run is pinned to
        the GET snapshot's resourceVersion (see ``_pin_revision``). None on
        any failure: a preview must never block the approval flow."""
        path = self._node_path(name)
        try:
            current = await self._request_json(path)
            proposed = await self._dry_run(
                path,
                "PATCH",
                self._pin_revision(self._cordon_patch(unschedulable, uid), current),
                "application/strategic-merge-patch+json",
            )
        except Exception:
            logger.debug("cordon dry-run preview failed", exc_info=True)
            return None
        return diff_manifests(current, proposed)

    async def evict_pod(self, namespace: str, name: str, *, uid: str | None = None) -> None:
        """Evict one pod through the Eviction API (policy/v1) - the
        PDB-respecting path kubectl drain uses. The server answers 429 when
        a PodDisruptionBudget has no disruptions left (surfaced as
        ApiStatusError, never retried here). A ``uid`` precondition pins the
        pod incarnation captured in the drain plan."""
        delete_options: dict[str, Any] = {}
        if uid:
            delete_options["preconditions"] = {"uid": uid}
        body = {
            "apiVersion": "policy/v1",
            "kind": "Eviction",
            "metadata": {"name": name, "namespace": namespace},
            "deleteOptions": delete_options,
        }
        await self._request_write(
            f"{self._object_path(PODS_META, namespace, name)}/eviction",
            "POST",
            body=body,
            content_type="application/json",
        )

    async def drain_plan(self, node_name: str) -> DrainPlan:
        """Impact plan for draining *node_name*: every pod scheduled there,
        classified against the cluster's PodDisruptionBudgets (see
        ``korvid.k8s.drain``). A missing policy/v1 API (404) degrades to an
        empty budget list - blocked evictions then surface as 429s during
        execution; an RBAC-denied cluster-wide list (403) falls back to
        per-namespace PDB queries so namespace-scoped users still get
        up-front warnings; any other failure propagates so the UI aborts
        instead of showing a falsely PDB-aware plan."""
        pods = await self._request_json(
            "/api/v1/pods",
            query_params=[("fieldSelector", f"spec.nodeName={node_name}")],
        )
        pod_items = pods.get("items") or []
        try:
            pdbs = await self._request_json("/apis/policy/v1/poddisruptionbudgets")
            pdb_items = pdbs.get("items") or []
        except ApiStatusError as exc:
            if exc.status == 404:
                logger.debug("policy/v1 PDB API absent (404); drain plan proceeds without budgets")
                pdb_items = []
            elif exc.status == 403:
                pdb_items = await self._pdbs_by_namespace(pod_items)
            else:
                # Auth failure or transport trouble must abort the drain
                # rather than present a falsely PDB-aware plan.
                raise
        return build_drain_plan(pod_items, pdb_items)

    async def pods_on_node(self, node_name: str) -> tuple[str, ...]:
        """Lightweight presence probe for the post-drain termination poll:
        one pods list filtered by node, no PDB queries."""
        pods = await self._request_json(
            "/api/v1/pods",
            query_params=[("fieldSelector", f"spec.nodeName={node_name}")],
        )
        keys = []
        for pod in pods.get("items") or []:
            metadata = pod.get("metadata") or {}
            uid = metadata.get("uid")
            ref = f"{metadata.get('namespace', '')}/{metadata.get('name', '')}"
            keys.append(str(uid) if uid else ref)
        return tuple(keys)

    async def _pdbs_by_namespace(self, pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Cluster-wide PDB list was RBAC-denied: retry per namespace of the
        pods being drained, which namespace-scoped users can usually read.
        404 means no PDBs in that namespace's view; anything else (including
        a per-namespace 403) propagates - see ``drain_plan``."""
        namespaces = sorted(
            {str((p.get("metadata") or {}).get("namespace", "")) for p in pods} - {""}
        )
        items: list[dict[str, Any]] = []
        for namespace in namespaces:
            try:
                got = await self._request_json(
                    f"/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets"
                )
            except ApiStatusError as exc:
                if exc.status == 404:
                    continue
                raise
            items.extend(got.get("items") or [])
        return items

    async def _dry_run(
        self,
        path: str,
        method: str,
        body: dict[str, Any] | None,
        content_type: str | None,
    ) -> dict[str, Any]:
        """Replay a write with ``dryRun=All`` and parse the would-be result.
        Admission webhooks and validation run server-side; nothing persists.

        DELETE is special-cased: when a request body is present the apiserver
        decodes DeleteOptions from the *body only* and ignores URL query
        parameters entirely (apiserver ``delete.go``), so a query-only
        ``dryRun`` would silently execute the real delete. The flag therefore
        also rides inside a deep copy of the options body — the caller's body
        (and the real delete built from it), nested dicts included, is never
        mutated."""
        if method == "DELETE" and body is not None:
            body = {**copy.deepcopy(body), "dryRun": ["All"]}
        raw = await self._request_write(
            path,
            method,
            body=body,
            content_type=content_type,
            query_params=[("dryRun", "All")],
        )
        result: dict[str, Any] = json.loads(raw)
        return result

    @staticmethod
    def _pin_revision(body: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        """Bind a dry-run patch to the GET snapshot it will be diffed against:
        metadata.resourceVersion is an apiserver optimistic-concurrency
        precondition, so a concurrent update between the two requests turns
        into a 409 (preview degrades to None) instead of a diff that mixes
        two revisions the server never evaluated together. Preview-only: the
        approved write is pinned by uid, not frozen to this revision."""
        rv = (current.get("metadata") or {}).get("resourceVersion")
        if rv:
            body.setdefault("metadata", {})["resourceVersion"] = str(rv)
        return body

    async def preview_scale(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        """Diff of the /scale subresource before vs after a dry-run scale.
        The captured ``uid`` rides along as the same precondition the real
        write carries, so the dry run replays the exact request being
        approved - a same-named replacement fails here (409 -> None) instead
        of previewing a diff the approved write can never apply. The dry run
        is additionally pinned to the GET snapshot's resourceVersion (see
        ``_pin_revision``). None on any failure: a preview must never block
        the approval flow."""
        path = f"{self._object_path(meta, namespace, name)}/scale"
        try:
            current = await self._request_json(path)
            proposed = await self._dry_run(
                path,
                "PATCH",
                self._pin_revision(self._scale_patch(replicas, uid), current),
                "application/merge-patch+json",
            )
        except Exception:
            logger.debug("scale dry-run preview failed", exc_info=True)
            return None
        return diff_manifests(current, proposed)

    async def preview_rollout_restart(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> list[str] | None:
        """Diff of the object before vs after a dry-run rollout restart.
        ``uid`` semantics match ``preview_scale``; ``restarted_at`` is the
        per-approval stamp shared with the executed write; the dry run is
        pinned to the GET snapshot's resourceVersion (see ``_pin_revision``).
        None on any failure: a preview must never block the approval flow."""
        path = self._object_path(meta, namespace, name)
        try:
            current = await self._request_json(path)
            proposed = await self._dry_run(
                path,
                "PATCH",
                self._pin_revision(self._restart_patch(uid, restarted_at), current),
                "application/strategic-merge-patch+json",
            )
        except Exception:
            logger.debug("rollout restart dry-run preview failed", exc_info=True)
            return None
        return diff_manifests(current, proposed)

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        """Summary of the exact object a delete would remove, after the
        server accepted a dry-run DELETE (admission webhooks included). The
        dry run carries the same DeleteOptions body as the real delete, so a
        captured ``uid`` precondition rejects a same-named replacement here
        (409 -> None) instead of summarizing the wrong incarnation. A diff is
        meaningless for a removal, so the useful preview is identity plus
        cascading behaviour (the note mirrors _delete_body: explicit
        Background propagation). The dry run is pinned to the GET snapshot
        via a preconditions.resourceVersion so the summary always describes
        the revision the server validated. None on any failure."""
        path = self._object_path(meta, namespace, name)
        body = self._delete_body(uid)
        try:
            manifest = await self._request_json(path)
            rv = (manifest.get("metadata") or {}).get("resourceVersion")
            if rv:
                body.setdefault("preconditions", {})["resourceVersion"] = str(rv)
            await self._dry_run(path, "DELETE", body, "application/json")
        except Exception:
            logger.debug("delete dry-run preview failed", exc_info=True)
            return None
        md = manifest.get("metadata") or {}
        uid = md.get("uid") or "?"
        created = md.get("creationTimestamp") or "?"
        return [
            f"- {meta.plural}/{name} (uid {uid}, created {created})",
            "delete accepted by server dry-run;"
            " dependents are deleted in the background (propagationPolicy: Background)",
        ]

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        """PUT-replace the object with an edited manifest (kubectl edit).
        A ``uid`` injected into metadata is an apiserver precondition: the
        replace is rejected with 409 when the live object is a different
        incarnation than the one that was approved.  The manifest should
        carry the fetched ``resourceVersion`` so concurrent modifications
        surface as 409 conflicts instead of being clobbered."""
        body = manifest
        if uid:
            body = {**manifest, "metadata": {**(manifest.get("metadata") or {}), "uid": uid}}
        await self._request_write(
            self._object_path(meta, namespace, name),
            "PUT",
            body=body,
            content_type="application/json",
        )

    async def create_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        manifest: dict[str, Any],
    ) -> None:
        """POST a new object onto the collection (OLM install, issue #29)."""
        if meta.namespaced:
            if namespace is None:
                # Kubernetes allows a cluster-wide LIST on a namespaced
                # collection but never a POST: reject locally instead of
                # sending a guaranteed-invalid request.
                raise ValueError(f"creating a {meta.kind} requires a namespace")
            path = f"{meta.api_base}/namespaces/{_path_segment(namespace)}/{meta.plural}"
        else:
            path = f"{meta.api_base}/{meta.plural}"
        await self._request_write(path, "POST", body=manifest, content_type="application/json")

    async def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[LogLine]:
        """Stream log lines from a pod container; yields LogLine one per line.

        previous=True forces follow=False (terminated containers can't be followed).
        An empty ``container`` omits the parameter (single-container pods).
        Lines are requested with ``timestamps=true``; the RFC3339 prefix is
        parsed into ``LogLine.timestamp`` and stripped from ``LogLine.text``
        so callers can deduplicate the ~tail_lines replay on reconnect.
        ApiException is wrapped as ApiStatusError both at call time and mid-stream.
        """
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        if previous:
            follow = False
        kwargs: dict[str, Any] = {
            "name": pod,
            "namespace": namespace,
            "follow": follow,
            "previous": previous,
            "tail_lines": tail_lines,
            "timestamps": True,
            "_preload_content": False,
        }
        # An empty container name would 400 on multi-container pods; omitting
        # it lets the API server pick the default for single-container pods.
        if container:
            kwargs["container"] = container
        try:
            resp: Any = await self._core_v1.read_namespaced_pod_log(**kwargs)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        try:
            # Raw path never raises for HTTP errors (see _raise_for_status):
            # check up front so an error Status body isn't streamed as logs.
            if not 200 <= int(getattr(resp, "status", 0) or 0) <= 299:
                _raise_for_status(resp, await resp.read())
            async for raw in resp.content:
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                yield _parse_log_line(pod, container, text)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        finally:
            # Hard-close the connection: release() would try to drain an
            # infinite follow stream. Guards against leaks on cancel/error.
            resp.close()

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return core v1 Events for the involved object.

        ``kind``/``uid`` narrow the field selector so same-named objects of a
        different kind (or an earlier incarnation of a recreated object) are
        excluded.
        """
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        # fieldSelector has no escaping mechanism; a name with "," would
        # inject extra selectors. Valid k8s names can't fail this check.
        if not _DNS1123_NAME.match(name):
            return []
        selector = f"involvedObject.name={name}"
        if kind and kind.isalnum():
            selector += f",involvedObject.kind={kind}"
        if uid and _UID_RE.match(uid):
            selector += f",involvedObject.uid={uid}"
        try:
            resp = await self._core_v1.list_namespaced_event(
                namespace,
                field_selector=selector,
                _preload_content=False,
            )
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        result: list[dict[str, Any]] = list(data.get("items", []))
        return result

    def _make_raw_watch_callable(
        self, path: str, extra_query: Sequence[tuple[str, str]] = ()
    ) -> Any:
        """Return an async callable compatible with k8s_watch.Watch.stream.

        Watch.stream injects ``watch=True``, ``_preload_content=False``, and
        ``resource_version`` as keyword arguments. This adapter translates those
        to the raw ``call_api`` contract so the correct path is used for **both**
        core-group (group=="", api_base="/api/v1") and extension-group resources,
        eliminating the broken ``/apis//v1/...`` URL that CustomObjectsApi would
        produce when ``group`` is empty.

        ``extra_query`` carries selectors that must ride along with the watch
        params; *path* must be bare (call_api appends ``?`` + query itself,
        so a pre-embedded query string would be silently broken).
        """
        api = self._api
        if api is None:
            raise RuntimeError("connect() first")

        async def _watch_call(
            watch: bool = False,
            _preload_content: bool = True,
            resource_version: str | None = None,
            **_rest: Any,
        ) -> Any:
            # watch/_preload_content are injected by Watch.stream; _rest absorbs
            # any future kwargs it may add.
            query_params: list[tuple[str, Any]] = [*extra_query, ("watch", "true")]
            if resource_version is not None:
                query_params.append(("resourceVersion", resource_version))
            resp = await api.call_api(
                path,
                "GET",
                auth_settings=["BearerToken"],
                query_params=query_params,
                _preload_content=False,
            )
            # Watch never inspects resp.status: a non-2xx response would be
            # retried forever (empty body) or surfaced as malformed events.
            if not 200 <= int(getattr(resp, "status", 0) or 0) <= 299:
                try:
                    _raise_for_status(resp, await resp.read())
                finally:
                    resp.close()
            return resp

        return _watch_call

    async def _request_json(
        self, path: str, query_params: list[tuple[str, str]] | None = None
    ) -> dict[str, Any]:
        """Raw GET through the ApiClient; wraps ApiException as ApiStatusError."""
        if self._api is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._api.call_api(
                path,
                "GET",
                auth_settings=["BearerToken"],
                query_params=query_params or [],
                _preload_content=False,
            )
            body = await resp.read()
            _raise_for_status(resp, body)
            result: dict[str, Any] = json.loads(body)
            return result
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def discover_resources(self) -> list[ResourceMeta]:
        """Return every LIST-able resource from /api/v1 and /apis.

        Kinds without a watch verb (aggregated APIs like OLM's
        packageserver) are included with ``watchable=False`` — the watch
        source keeps them fresh by polling (issue #141).

        Group version lists are fetched concurrently — sequential fetching adds
        one RTT per API group and dominates startup on clusters with many CRDs.
        """
        metas: list[ResourceMeta] = []
        core = await self._request_json("/api/v1")
        metas += _parse_resource_list(core, group="", version="v1")
        groups = await self._request_json("/apis")

        async def _fetch(name: str, version: str) -> list[ResourceMeta]:
            try:
                rl = await self._request_json(f"/apis/{name}/{version}")
            except ApiStatusError:
                return []  # a broken aggregated API must not kill discovery
            return _parse_resource_list(rl, group=name, version=version)

        tasks = []
        for g in groups.get("groups", []):
            name = g.get("name")
            version = (g.get("preferredVersion") or {}).get("version")
            if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
                continue  # malformed group must not kill discovery
            tasks.append(_fetch(name, version))
        for group_metas in await asyncio.gather(*tasks):
            metas += group_metas
        return metas

    async def close(self) -> None:
        if self._api is not None:
            await self._api.close()


async def _to_dict(resp: Any) -> dict[str, Any]:
    """Normalize aiohttp response or dict into a plain dict.

    Raises ApiStatusError on a non-2xx status: with ``_preload_content=False``
    kubernetes_asyncio never raises for HTTP errors, so an unchecked error
    Status body would otherwise be parsed as if it were the requested object.
    """
    if isinstance(resp, dict):
        return resp
    body = await resp.read()
    _raise_for_status(resp, body)
    result: dict[str, Any] = json.loads(body)
    return result


def _raise_for_status(resp: Any, body: bytes) -> None:
    """Raise ApiStatusError for a non-2xx raw (``_preload_content=False``)
    response. kubernetes_asyncio's rest layer only raises ApiException when
    it preloads the body, so raw-path callers must check the status
    themselves — otherwise refused writes (409 uid precondition, 429 PDB
    denial) silently look like successes (caught live by the contract
    suite, issue #109)."""
    status = int(getattr(resp, "status", 0) or 0)
    if not 200 <= status <= 299:
        raise ApiStatusError(
            status,
            str(getattr(resp, "reason", "") or ""),
            body=body.decode("utf-8", errors="replace"),
        )


def _parse_resource_list(data: dict[str, Any], *, group: str, version: str) -> list[ResourceMeta]:
    out = []
    for r in data.get("resources", []):
        name = r.get("name")
        kind = r.get("kind")
        namespaced = r.get("namespaced")
        verbs: list[str] = r.get("verbs", [])
        if not isinstance(name, str) or not isinstance(kind, str) or namespaced is None:
            continue  # malformed entry must not kill discovery
        if "/" in name or "list" not in verbs:
            continue
        out.append(
            ResourceMeta(
                kind,
                name,
                group,
                version,
                bool(namespaced),
                tuple(r.get("shortNames") or ()),
                # list-only aggregated APIs (OLM's packageserver) stay
                # discoverable; the watch source polls them (issue #141).
                watchable="watch" in verbs,
            )
        )
    return out
