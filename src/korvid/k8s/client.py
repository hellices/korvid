"""Async Kubernetes client wrapper. The only module that talks to the API server."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from urllib.parse import quote

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio import watch as k8s_watch

from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.dryrun import diff_manifests
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.metrics import PodMetrics, parse_pod_metrics_list
from korvid.k8s.models import GenericSummary, PodSummary, summary_for
from korvid.k8s.writes import WriteOps

logger = logging.getLogger(__name__)


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
    at a different cluster mid-session (k9s parity).
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


class KubeClient(WriteOps):
    """Thin wrapper over kubernetes_asyncio; returns typed summaries."""

    def __init__(self) -> None:
        self._api: k8s_client.ApiClient | None = None
        self._core_v1: k8s_client.CoreV1Api | None = None
        self._ssar_warned = False
        #: pods/resize discovery result; None until the first successful check.
        self._pod_resize_supported: bool | None = None

    async def connect(self, context: str | None = None) -> None:
        await k8s_config.load_kube_config(context=context)
        self._api = k8s_client.ApiClient()
        self._core_v1 = k8s_client.CoreV1Api(self._api)

    async def list_namespaces(self) -> list[str]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._core_v1.list_namespace(_preload_content=False)
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        return [item["metadata"]["name"] for item in data.get("items", [])]

    async def list_pods(self, namespace: str) -> list[PodSummary]:
        if self._core_v1 is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
        return [PodSummary.from_manifest(item) for item in data.get("items", [])]

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

        # LIST first: yield pre-existing pods as ADDED and anchor the watch at
        # the snapshot's resourceVersion so no events are missed between LIST
        # and Watch.  A 410-Gone mid-stream propagates to WatchManager, which
        # retries by calling watch_pods again — the fresh call re-LISTs,
        # which is the intended relist fallback; no separate 410 handling needed.
        try:
            resp = await self._core_v1.list_namespaced_pod(namespace, _preload_content=False)
            data = await _to_dict(resp)
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in data.get("items", []):
            yield ("ADDED", PodSummary.from_manifest(item))

        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        w = k8s_watch.Watch()
        try:
            async with w.stream(
                self._core_v1.list_namespaced_pod, namespace, **watch_kwargs
            ) as stream:
                async for event in stream:
                    yield (
                        str(event["type"]),
                        PodSummary.from_manifest(event["raw_object"]),
                    )
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def _watch_pods_cluster(self) -> AsyncIterator[tuple[str, PodSummary]]:
        """Cluster-wide pod watch via raw /api/v1/pods path (LIST then stream)."""
        if self._api is None:
            raise RuntimeError("connect() first")

        path = "/api/v1/pods"
        data = await self._request_json(path)

        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in data.get("items", []):
            yield ("ADDED", PodSummary.from_manifest(item))

        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        watch_func = self._make_raw_watch_callable(path)
        w = k8s_watch.Watch()
        try:
            async with w.stream(watch_func, **watch_kwargs) as stream:
                async for event in stream:
                    yield (
                        str(event["type"]),
                        PodSummary.from_manifest(event["raw_object"]),
                    )
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def watch_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> AsyncIterator[tuple[str, GenericSummary]]:
        """LIST then watch any resource kind; None namespace = all namespaces.

        Contract mirrors watch_pods: pre-existing items are yielded as ADDED
        first, then live watch events from the snapshot resourceVersion.
        ApiException is wrapped as ApiStatusError at both the LIST and watch phases.
        """
        if self._api is None:
            raise RuntimeError("connect() first")

        # LIST phase --------------------------------------------------------
        # Cluster-scoped kinds have no namespaced path regardless of scope.
        if namespace is not None and meta.namespaced:
            list_path = f"{meta.api_base}/namespaces/{_path_segment(namespace)}/{meta.plural}"
        else:
            list_path = f"{meta.api_base}/{meta.plural}"

        data = await self._request_json(list_path)

        resource_version: str | None = (data.get("metadata") or {}).get("resourceVersion")
        for item in data.get("items", []):
            yield ("ADDED", summary_for(meta.kind, item))

        # Watch phase -------------------------------------------------------
        watch_kwargs: dict[str, Any] = {}
        if resource_version is not None:
            watch_kwargs["resource_version"] = resource_version

        watch_func = self._make_raw_watch_callable(list_path)

        w = k8s_watch.Watch()
        try:
            async with w.stream(watch_func, **watch_kwargs) as stream:
                async for event in stream:
                    yield (
                        str(event["type"]),
                        summary_for(meta.kind, event["raw_object"]),
                    )
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        """LIST any resource kind and return GenericSummary items.

        Reuses the path logic of watch_objects' LIST phase.
        ApiException is wrapped as ApiStatusError.
        """
        if self._api is None:
            raise RuntimeError("connect() first")
        if namespace is not None and meta.namespaced:
            list_path = f"{meta.api_base}/namespaces/{_path_segment(namespace)}/{meta.plural}"
        else:
            list_path = f"{meta.api_base}/{meta.plural}"
        data = await self._request_json(list_path)
        return [summary_for(meta.kind, item) for item in data.get("items", [])]

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        """Fetch the raw manifest for a single object. ApiException → ApiStatusError."""
        return await self._request_json(self._object_path(meta, namespace, name))

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
            return raw
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

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
            self._pod_resize_supported = any(
                r.get("name") == "pods/resize" for r in core.get("resources", [])
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

    async def _dry_run(
        self,
        path: str,
        method: str,
        body: dict[str, Any] | None,
        content_type: str | None,
    ) -> dict[str, Any]:
        """Replay a write with ``dryRun=All`` and parse the would-be result.
        Admission webhooks and validation run server-side; nothing persists."""
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

    def _make_raw_watch_callable(self, path: str) -> Any:
        """Return an async callable compatible with k8s_watch.Watch.stream.

        Watch.stream injects ``watch=True``, ``_preload_content=False``, and
        ``resource_version`` as keyword arguments. This adapter translates those
        to the raw ``call_api`` contract so the correct path is used for **both**
        core-group (group=="", api_base="/api/v1") and extension-group resources,
        eliminating the broken ``/apis//v1/...`` URL that CustomObjectsApi would
        produce when ``group`` is empty.
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
            query_params: list[tuple[str, Any]] = [("watch", "true")]
            if resource_version is not None:
                query_params.append(("resourceVersion", resource_version))
            return await api.call_api(
                path,
                "GET",
                auth_settings=["BearerToken"],
                query_params=query_params,
                _preload_content=False,
            )

        return _watch_call

    async def _request_json(self, path: str) -> dict[str, Any]:
        """Raw GET through the ApiClient; wraps ApiException as ApiStatusError."""
        if self._api is None:
            raise RuntimeError("connect() first")
        try:
            resp = await self._api.call_api(
                path,
                "GET",
                auth_settings=["BearerToken"],
                _preload_content=False,
            )
            body = await resp.read()
            result: dict[str, Any] = json.loads(body)
            return result
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def discover_resources(self) -> list[ResourceMeta]:
        """Return every list+watch-able resource from /api/v1 and /apis.

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
    """Normalize aiohttp response or dict into a plain dict."""
    if isinstance(resp, dict):
        return resp
    body = await resp.read()
    result: dict[str, Any] = json.loads(body)
    return result


def _parse_resource_list(data: dict[str, Any], *, group: str, version: str) -> list[ResourceMeta]:
    out = []
    for r in data.get("resources", []):
        name = r.get("name")
        kind = r.get("kind")
        namespaced = r.get("namespaced")
        verbs: list[str] = r.get("verbs", [])
        if not isinstance(name, str) or not isinstance(kind, str) or namespaced is None:
            continue  # malformed entry must not kill discovery
        if "/" in name or "list" not in verbs or "watch" not in verbs:
            continue
        out.append(
            ResourceMeta(
                kind,
                name,
                group,
                version,
                bool(namespaced),
                tuple(r.get("shortNames") or ()),
            )
        )
    return out
