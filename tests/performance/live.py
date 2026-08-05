"""Guarded real-AKS application-path replay prerequisite (issue #186 task 8.2).

`run_live_replay` drives the *real* production stack (`KubeClient` ->
`WatchManager` -> `ResourceStore` -> `MeasuredKorvidApp`) against an
already-seeded AKS cluster (20 namespaces / 1,000 Pods, created by
`tests.performance.manifests.build_seed_manifests` and the `seed-manifests`
CLI subcommand). This module never provisions, tears down, or discovers a
cluster on its own - it only replays churn against a topology a human has
already created and identified explicitly via `--context`,
`--expected-cluster-id`, and `--run-id`.

Every mutation is fail-closed:

1. **Cluster identity gate** (`_verify_cluster_identity`): the active
   kubeconfig context, its resolved API server hostname, and an independent
   `az aks show --ids <id>` lookup must all agree before any client connects.
2. **Ownership gate** (`_verify_ownership`): every expected namespace and
   every expected Pod must already carry both ownership labels
   (`manifests.MANAGED_BY_LABEL`/`manifests.RUN_LABEL`) before any churn is
   attempted.
3. **Guarded churn** (`drive_live_churn`): each mutation is a JSON-Patch that
   `test`s the target Pod's UID and both ownership labels before `replace`ing
   `status.phase`. A failed `test` op aborts the *entire* run - there is no
   unguarded fallback.

Two separate `KubeReadClient` connections are used deliberately: a
non-instrumented *harness* client (`LiveDependencies.harness_kube_client_factory`)
performs the ownership gate, the post-churn independent re-read, and nothing
else, while a single telemetry-wired *application-path* client
(`LiveDependencies.kube_client_factory`) is only ever handed to
`make_live_watch_source`. This keeps `ReplayReport.api` reporting exactly the
production application read path (the real `KubeClient.watch_pods` LIST+WATCH
telemetry) instead of being diluted by the harness's own bookkeeping reads.

Unit tests substitute every external boundary via `LiveDependencies`; none of
them may contact Azure, a real kubeconfig, or a real cluster.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.core.watch import WatchManager, WatchSource
from korvid.k8s.client import KubeClient, resolve_context_name
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.telemetry import ReadTelemetry
from korvid.ui.widgets.resource_table import ResourceTable
from tests.performance import manifests
from tests.performance.metrics import BenchmarkRecorder, ProcessSampler
from tests.performance.profile import WorkloadProfile
from tests.performance.replay import MeasuredKorvidApp, ReplayOptions, ReplayReport, _build_manifest
from tests.performance.workload import ScheduledEvent, scheduled_events, summary_digest
from tests.ui.waits import until

#: Namespace-scoped read for the ownership gate; not exposed via `PODS_META`
#: because Namespaces are cluster-scoped (`namespaced=False`).
_NAMESPACES_META = ResourceMeta("Namespace", "namespaces", "", "v1", False)

#: Live topology is pinned to the aks-1k profile shape (issue #186 Task 8):
#: exactly 20 namespaces of 50 Pods each, matching `seed-manifests`'s output.
_REQUIRED_OBJECT_COUNT = 1000
_REQUIRED_NAMESPACE_COUNT = 20


async def _sleep_default(delay: float) -> None:
    await asyncio.sleep(delay)


@dataclass(frozen=True)
class CommandResult:
    """Captured outcome of a subprocess invocation (e.g. `az aks show`)."""

    exit_code: int
    stdout: str
    stderr: str


#: Runs an argv list and captures its result; the identity gate's only
#: subprocess seam. Production uses `_default_command_runner`; tests inject
#: a fake that never touches a real `az` binary.
CommandRunner = Callable[[list[str]], Awaitable[CommandResult]]


class KubeReadClient(Protocol):
    """Structural subset of `KubeClient` the live read path needs.

    Deliberately narrow (vs. requiring the full `KubeClient`) so unit tests
    can implement it with a plain in-memory fake instead of subclassing the
    real, network-talking client.
    """

    async def connect(self, context: str | None = None) -> None: ...

    async def close(self) -> None: ...

    async def list_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> list[GenericSummary]: ...

    async def list_pods(self, namespace: str) -> list[PodSummary]: ...

    def watch_pods(self, namespace: str | None) -> AsyncIterator[tuple[str, PodSummary]]: ...


class MutationClient(Protocol):
    """Issues one guarded status mutation; production talks JSON-Patch to a
    real API server, tests mutate an in-memory fake cluster the same way."""

    async def patch_pod_status_guarded(
        self, namespace: str, name: str, *, uid: str, phase: str
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class LiveDependencies:
    """Every external boundary `run_live_replay` crosses, as an injectable
    seam. Production callers get real wiring from `_default_dependencies`;
    tests always construct this explicitly with fakes, so no unit test can
    reach Azure, a real kubeconfig, or a real cluster.

    `kube_client_factory` and `harness_kube_client_factory` are deliberately
    two separate seams (constructing two separate `KubeReadClient`
    connections at runtime): the former is wired with `recorder.record_api`
    and used *only* for the real application read path
    (`make_live_watch_source`'s `watch_pods`), so `ReplayReport.api` reports
    exactly the production LIST+WATCH telemetry an operator would see. The
    latter is never wired to telemetry and is used *only* for the harness's
    own bookkeeping reads (the ownership gate and the post-churn independent
    re-read) - those reads must never dilute the application-path signal.
    """

    command_runner: CommandRunner
    active_context: Callable[[], str | None]
    context_host: Callable[[str], Awaitable[str]]
    kube_client_factory: Callable[[ReadTelemetry], KubeReadClient]
    harness_kube_client_factory: Callable[[], KubeReadClient]
    mutation_client_factory: Callable[[str], MutationClient]


async def _default_command_runner(args: list[str]) -> CommandResult:
    """Run *args* as a subprocess and capture stdout/stderr/exit code.

    A missing executable (e.g. `az` not on PATH) surfaces as a `ValueError`
    so it fails the identity gate cleanly instead of raising an uncaught
    `FileNotFoundError` deep inside `run_live_replay`.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ValueError(f"executable not found: {args[0]!r} ({exc})") from exc
    stdout_bytes, stderr_bytes = await proc.communicate()
    return CommandResult(
        proc.returncode if proc.returncode is not None else 1,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _default_active_context() -> str | None:
    return resolve_context_name()


async def _default_context_host(context: str) -> str:
    """Resolve the API server hostname *context* would dial.

    Loads the kubeconfig into a private `Configuration` (never persisted,
    never the global default) - mirroring `KubeClient.probe_context`'s
    isolation pattern - so resolving identity never disturbs any live
    connection.
    """
    configuration = k8s_client.Configuration()
    await k8s_config.load_kube_config(
        context=context, client_configuration=configuration, persist_config=False
    )
    hostname = urlparse(configuration.host or "").hostname
    if not hostname:
        raise ValueError(f"could not resolve API server hostname for context {context!r}")
    return hostname


def _json_pointer_escape(segment: str) -> str:
    """Escape one JSON-Pointer (RFC 6901) reference token."""
    return segment.replace("~", "~0").replace("/", "~1")


def build_guarded_status_patch(*, uid: str, run_id: str, phase: str) -> list[dict[str, Any]]:
    """The exact JSON-Patch op list a guarded status mutation issues.

    `test`s the target Pod's UID and both ownership labels before
    `replace`-ing `status.phase`, so a stale, foreign, or replaced Pod aborts
    the whole patch server-side - there is no unguarded fallback.
    """
    managed_by_path = f"/metadata/labels/{_json_pointer_escape(manifests.MANAGED_BY_LABEL)}"
    run_path = f"/metadata/labels/{_json_pointer_escape(manifests.RUN_LABEL)}"
    return [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": managed_by_path, "value": manifests.MANAGED_BY_VALUE},
        {"op": "test", "path": run_path, "value": run_id},
        {"op": "replace", "path": "/status/phase", "value": phase},
    ]


class _KubeMutationClient:
    """Production `MutationClient`: issues a guarded JSON-Patch against the
    real `pods/status` subresource of *context*."""

    def __init__(self, context: str, run_id: str) -> None:
        self._context = context
        self._run_id = run_id
        self._api: k8s_client.ApiClient | None = None
        self._core_v1: k8s_client.CoreV1Api | None = None

    async def _ensure_connected(self) -> k8s_client.CoreV1Api:
        if self._core_v1 is None:
            configuration = k8s_client.Configuration()
            await k8s_config.load_kube_config(
                context=self._context, client_configuration=configuration, persist_config=False
            )
            self._api = k8s_client.ApiClient(configuration)
            self._core_v1 = k8s_client.CoreV1Api(self._api)
        return self._core_v1

    async def patch_pod_status_guarded(
        self, namespace: str, name: str, *, uid: str, phase: str
    ) -> None:
        core_v1 = await self._ensure_connected()
        ops = build_guarded_status_patch(uid=uid, run_id=self._run_id, phase=phase)
        try:
            await core_v1.patch_namespaced_pod_status(
                name,
                namespace,
                ops,
                _content_type="application/json-patch+json",  # type: ignore[call-arg]  # kubernetes_asyncio's .pyi stub omits _content_type; accepted via **kwargs at runtime
            )
        except k8s_client.exceptions.ApiException as exc:
            raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc

    async def close(self) -> None:
        if self._api is not None:
            await self._api.close()


def _default_dependencies(context: str) -> LiveDependencies:
    """Real production wiring: subprocess `az`, real kubeconfig, real `KubeClient`.

    Two independent `KubeClient` connections are constructed: the
    telemetry-wired one `run_live_replay` hands to `make_live_watch_source`,
    and a plain, non-instrumented one (`read_telemetry=None`, so
    `KubeClient._observe_read` is a no-op) for the harness's own ownership
    and post-churn reads.
    """
    return LiveDependencies(
        command_runner=_default_command_runner,
        active_context=_default_active_context,
        context_host=_default_context_host,
        kube_client_factory=lambda read_telemetry: KubeClient(read_telemetry=read_telemetry),
        harness_kube_client_factory=lambda: KubeClient(),
        mutation_client_factory=lambda run_id: _KubeMutationClient(context, run_id),
    )


def live_object_identity(run_id: str, namespace_count: int, index: int) -> tuple[str, str]:
    """Map a synthetic churn-schedule Pod index onto the exact seeded
    `(namespace, name)` `manifests.build_seed_manifests` created for it -
    guaranteeing seeding and live churn always agree on identity."""
    namespace = manifests.namespace_name(run_id, index % namespace_count)
    name = manifests.pod_name(namespace_count, index)
    return namespace, name


def _owns(labels: Iterable[tuple[str, str]], run_id: str) -> bool:
    mapping = dict(labels)
    return (
        mapping.get(manifests.MANAGED_BY_LABEL) == manifests.MANAGED_BY_VALUE
        and mapping.get(manifests.RUN_LABEL) == run_id
    )


def _validate_time_scale(options: ReplayOptions) -> None:
    if options.time_scale != 1.0:
        raise ValueError(
            f"run_live_replay requires options.time_scale == 1.0 (real wall time), "
            f"got {options.time_scale!r}"
        )


def _validate_topology(profile: WorkloadProfile) -> None:
    if profile.object_count != _REQUIRED_OBJECT_COUNT:
        raise ValueError(
            f"live replay requires object_count == {_REQUIRED_OBJECT_COUNT}, "
            f"got {profile.object_count}"
        )
    if profile.namespace_count != _REQUIRED_NAMESPACE_COUNT:
        raise ValueError(
            f"live replay requires namespace_count == {_REQUIRED_NAMESPACE_COUNT}, "
            f"got {profile.namespace_count}"
        )


async def _verify_cluster_identity(
    *, context: str, expected_cluster_id: str, deps: LiveDependencies
) -> None:
    """Fail-closed 5-step cluster identity gate; every failure raises
    `ValueError` before any client is constructed or any mutation attempted.
    """
    active = deps.active_context()
    if active != context:
        raise ValueError(
            f"active kubeconfig context {active!r} does not match required context {context!r}"
        )

    hostname = await deps.context_host(context)

    result = await deps.command_runner(
        ["az", "aks", "show", "--ids", expected_cluster_id, "-o", "json"]
    )
    if result.exit_code != 0:
        raise ValueError(f"az aks show failed (exit {result.exit_code}): {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"az aks show returned malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("az aks show returned malformed JSON: expected a JSON object")

    resource_id = payload.get("id")
    if resource_id != expected_cluster_id:
        raise ValueError(
            f"az aks show returned id {resource_id!r}, expected {expected_cluster_id!r}"
        )

    fqdn = payload.get("fqdn") or ""
    private_fqdn = payload.get("privateFqdn") or ""
    expected_hostname = fqdn or private_fqdn
    if not expected_hostname:
        raise ValueError("az aks show returned neither fqdn nor privateFqdn")
    if hostname != expected_hostname:
        raise ValueError(
            f"context {context!r} API hostname {hostname!r} does not match "
            f"cluster hostname {expected_hostname!r}"
        )


async def _verify_ownership(
    kube: KubeReadClient, *, run_id: str, namespace_count: int, object_count: int
) -> dict[tuple[str, str], PodSummary]:
    """Ownership gate: every expected namespace and every expected Pod must
    already exist with both ownership labels, checked before any churn.

    Collects every mismatch across all namespaces/Pods before raising, so a
    single failed run surfaces the full blast radius at once instead of
    stopping at the first namespace.

    Returns the validated `(namespace, name) -> PodSummary` snapshot so
    callers can reuse it (e.g. as the pre-churn uid snapshot) instead of
    immediately re-listing the same Pods a second time.
    """
    expected_namespaces = [manifests.namespace_name(run_id, i) for i in range(namespace_count)]
    actual_namespaces = {obj.name: obj for obj in await kube.list_objects(_NAMESPACES_META, None)}

    missing_namespaces = [name for name in expected_namespaces if name not in actual_namespaces]
    if missing_namespaces:
        raise ValueError(f"missing expected namespaces: {', '.join(missing_namespaces)}")

    mismatched_namespaces = [
        name for name in expected_namespaces if not _owns(actual_namespaces[name].labels, run_id)
    ]
    if mismatched_namespaces:
        raise ValueError(
            f"namespaces missing/mismatched ownership labels: {', '.join(mismatched_namespaces)}"
        )

    pods_per_namespace = object_count // namespace_count
    expected_pod_names = [
        manifests.pod_name(namespace_count, local_index * namespace_count)
        for local_index in range(pods_per_namespace)
    ]
    missing_pods: list[str] = []
    mismatched_pods: list[str] = []
    validated_pods: dict[tuple[str, str], PodSummary] = {}
    for namespace in expected_namespaces:
        pods_by_name = {pod.name: pod for pod in await kube.list_pods(namespace)}
        for name in expected_pod_names:
            pod = pods_by_name.get(name)
            if pod is None:
                missing_pods.append(f"{namespace}/{name}")
            elif not _owns(pod.labels, run_id):
                mismatched_pods.append(f"{namespace}/{name}")
            else:
                validated_pods[(namespace, name)] = pod
    if missing_pods:
        raise ValueError(f"missing expected pods: {', '.join(missing_pods)}")
    if mismatched_pods:
        raise ValueError(f"pods with mismatched ownership labels: {', '.join(mismatched_pods)}")
    return validated_pods


def make_live_watch_source(
    kube: KubeReadClient, expected_namespaces: frozenset[str]
) -> WatchSource:
    """Filter the real cluster-wide Pod watch to exactly the expected,
    seeded namespaces - unrelated cluster Pods (or namespaces sharing the
    cluster with this run) must never enter the benchmark store."""

    async def _source(kind: str, _scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        if kind != "pods":
            raise ValueError(f"run_live_replay only watches pods, got kind={kind!r}")
        async for event_type, pod in kube.watch_pods(None):
            if pod.namespace in expected_namespaces:
                yield (event_type, pod)

    return _source


async def drive_live_churn(
    events: Iterable[ScheduledEvent],
    *,
    run_id: str,
    namespace_count: int,
    live_state: dict[tuple[str, str], PodSummary],
    mutation_client: MutationClient,
    recorder: BenchmarkRecorder,
    options: ReplayOptions,
) -> None:
    """Drive guarded churn at wall-clock time (matching `_ReplaySource`'s
    inter-event delay math). Any guard failure (`ApiStatusError`) propagates
    immediately and unconditionally aborts the run - there is no unguarded
    fallback and no attempt to continue past a failed `test` op.
    """
    now = options.monotonic_fn if options.monotonic_fn is not None else monotonic
    sleep = options.async_sleep if options.async_sleep is not None else _sleep_default
    start = now()
    for event in events:
        elapsed = now() - start
        delay = event.offset_seconds * options.time_scale - elapsed
        if delay > 0:
            await sleep(delay)

        index = int(event.summary.name.removeprefix("pod-"))
        namespace, name = live_object_identity(run_id, namespace_count, index)
        current = live_state.get((namespace, name))
        if current is None:
            raise ValueError(f"live churn target {namespace}/{name} is not in the seeded state")

        await mutation_client.patch_pod_status_guarded(
            namespace, name, uid=current.uid, phase=event.summary.phase
        )
        live_state[(namespace, name)] = replace(current, phase=event.summary.phase)
        recorder.record_event(event.sequence, now())


async def run_live_replay(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
    deps: LiveDependencies | None = None,
) -> ReplayReport:
    """Replay churn against an already-seeded real AKS cluster and return metrics.

    Fail-closed order: `time_scale`/`run_id`/topology validation, the cluster
    identity gate, the ownership gate - all *before* any mutation - then the
    real application-path wiring (`KubeClient` -> `WatchManager` ->
    `ResourceStore` -> `MeasuredKorvidApp`), guarded churn, and digest parity
    against an independent post-churn re-read of the cluster.

    Two `KubeReadClient` connections are used: `harness_kube` (never wired to
    telemetry) performs the ownership gate and the post-churn independent
    re-read; `kube` (wired to `recorder.record_api`) is only ever handed to
    `make_live_watch_source`, so `ReplayReport.api` reports exactly the
    production application read path. The ownership gate's validated Pod
    snapshot is reused as the pre-churn uid snapshot - it is never re-listed.
    """
    _validate_time_scale(options)
    manifests.validate_run_id(run_id)
    _validate_topology(profile)

    active_deps = deps if deps is not None else _default_dependencies(context)
    await _verify_cluster_identity(
        context=context, expected_cluster_id=expected_cluster_id, deps=active_deps
    )

    store = ResourceStore()
    recorder = BenchmarkRecorder()
    sampler = ProcessSampler(options.sample_interval)

    harness_kube = active_deps.harness_kube_client_factory()
    await harness_kube.connect(context)
    try:
        # Reused directly as the pre-churn uid snapshot below - no second,
        # redundant listing pass over the same Pods.
        live_state = await _verify_ownership(
            harness_kube,
            run_id=run_id,
            namespace_count=profile.namespace_count,
            object_count=profile.object_count,
        )

        expected_namespaces = frozenset(
            manifests.namespace_name(run_id, i) for i in range(profile.namespace_count)
        )

        kube = active_deps.kube_client_factory(recorder.record_api)
        await kube.connect(context)
        try:
            source = make_live_watch_source(kube, expected_namespaces)
            watch_manager = WatchManager(store, source, retry_delay=0.0)
            manifest = _build_manifest(profile)
            mutation_client = active_deps.mutation_client_factory(run_id)

            app = MeasuredKorvidApp(
                config=KorvidConfig(namespace=ALL_NAMESPACES),
                store=store,
                watch_manager=watch_manager,
                recorder=recorder,
            )

            sampler.start()
            churn_started_before_input = False
            try:
                async with app.run_test() as pilot:
                    table = app.query_one(ResourceTable)

                    await until(
                        pilot,
                        lambda: table.row_count == profile.object_count,
                        timeout=60.0,
                        label="initial owned pods rendered",
                    )

                    events = scheduled_events(profile)
                    churn_task = asyncio.create_task(
                        drive_live_churn(
                            events,
                            run_id=run_id,
                            namespace_count=profile.namespace_count,
                            live_state=live_state,
                            mutation_client=mutation_client,
                            recorder=recorder,
                            options=options,
                        )
                    )
                    churn_started_before_input = True

                    t0 = monotonic()
                    await pilot.press("down")
                    recorder.record_input(monotonic() - t0)
                    t0 = monotonic()
                    await pilot.press("up")
                    recorder.record_input(monotonic() - t0)

                    await churn_task

                    await until(
                        pilot,
                        lambda: not recorder._pending_events,
                        timeout=60.0,
                        label="churn complete and all events rendered",
                    )
            finally:
                process_samples = await sampler.stop()
                await watch_manager.stop_all()
                await mutation_client.close()

            # Independently re-read the cluster's actual Pods for ground-truth
            # digest parity via the non-instrumented harness client, rather
            # than trusting the driver's own bookkeeping or polluting the
            # application-path telemetry with this harness-only read.
            final_pods: list[PodSummary] = []
            for namespace in expected_namespaces:
                final_pods.extend(await harness_kube.list_pods(namespace))
            expected_digest = summary_digest(final_pods)
            final_digest = summary_digest(
                cast(Iterable[PodSummary], store.get("pods", ALL_NAMESPACES))
            )
        finally:
            await kube.close()
    finally:
        await harness_kube.close()

    benchmark = recorder.report(manifest, process_samples, final_digest=final_digest)

    return ReplayReport(
        object_count=profile.object_count,
        expected_digest=expected_digest,
        final_digest=final_digest,
        dropped_updates=benchmark.dropped_updates,
        rendered_updates=benchmark.rendered_updates,
        render_passes=benchmark.render_passes,
        coalesced_updates=benchmark.coalesced_updates,
        event_to_render=benchmark.event_to_render,
        input_latency=benchmark.input_latency,
        churn_started_before_input=churn_started_before_input,
        process=benchmark.process,
        api=benchmark.api,
        manifest=benchmark.manifest,
    )
