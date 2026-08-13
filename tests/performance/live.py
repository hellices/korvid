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
   `az aks show` lookup for the fixed dedicated-test resource group/name must
   all agree before any client connects.
2. **Ownership gate** (`_verify_ownership`): every expected namespace and
   every expected Pod must already carry both ownership labels
   (`manifests.MANAGED_BY_LABEL`/`manifests.RUN_LABEL`), and no *unexpected*
   Pod may exist in an owned namespace, before any churn is attempted.
3. **Guarded churn** (`drive_live_churn`): each mutation is a JSON-Patch that
   `test`s the target Pod's UID and both ownership labels before writing a
   dedicated, non-ownership `manifests.TICK_LABEL` on the Pod's *own*
   metadata. A failed `test` op aborts the *entire* run - there is no
   unguarded fallback, and no ownership label, `status`, or spec field is ever
   written.
4. **Post-churn revalidation** (`read_and_validate_owned_pods`): the
   ground-truth cluster read re-checks the exact identity set, every UID, and
   both ownership labels before its digest is trusted.

Churn is metadata-only by design. The seeded Pods are real
`registry.k8s.io/pause:3.10` Pods whose `status` subresource is owned by the
kubelet: patching `status.phase` would be reverted on the next node sync and
would contradict the design doc's "metadata-only updates create real watch
traffic without restarting containers or changing the workload's resource
demand". `TICK_LABEL` is user-owned, is part of `PodSummary.labels` (so it
really does change the store digest and produce a watch event), and no
controller reconciles it away.

Event-to-render latency is recorded where the *application* first sees an
event - `make_live_watch_source` records at watch receipt, exactly like the
deterministic `_ReplaySource` - never at patch acknowledgement. The watch
event and the patch response race over independent connections, so recording
at ack both misreports the interval (it would include the write round-trip)
and can append an event *after* its own render.

Two separate `KubeReadClient` connections are used deliberately: a
non-instrumented *harness* client (`LiveDependencies.harness_kube_client_factory`)
performs the ownership gate, the ground-truth re-read, and nothing else, while
a single telemetry-wired *application-path* client
(`LiveDependencies.kube_client_factory`) is only ever handed to
`make_live_watch_source`. This keeps `ReplayReport.api` reporting exactly the
production application read path (the real `KubeClient.watch_pods` LIST+WATCH
telemetry) instead of being diluted by the harness's own bookkeeping reads.
Mutation traffic is likewise never reported as application read telemetry: its
throttles are counted separately in `ChurnSummary.mutation_throttles`.

Unit tests substitute every external boundary via `LiveDependencies`; none of
them may contact Azure, a real kubeconfig, or a real cluster.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from time import monotonic
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.parse import urlparse

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.core.watch import WatchManager, WatchSource
from korvid.k8s.client import KubeClient, load_refreshable_kube_config, resolve_context_name
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.telemetry import ReadTelemetry
from korvid.ui.widgets.describe_screen import DescribeScreen
from korvid.ui.widgets.resource_table import ResourceTable
from tests.performance import manifests
from tests.performance.metrics import (
    BenchmarkRecorder,
    ChurnSummary,
    NodePoolInfo,
    ProcessSampler,
)
from tests.performance.profile import WorkloadProfile, burst_end_offsets, validate_profile
from tests.performance.replay import (
    MeasuredKorvidApp,
    ReplayOptions,
    ReplayReport,
    build_manifest,
    check_rendered_rows,
    measure_cursor_input,
    resolve_korvid_sha,
    wait_for,
)
from tests.performance.workload import ScheduledEvent, scheduled_events, summary_digest

#: Namespace-scoped read for the ownership gate; not exposed via `PODS_META`
#: because Namespaces are cluster-scoped (`namespaced=False`).
_NAMESPACES_META = ResourceMeta("Namespace", "namespaces", "", "v1", False)

#: Pods, for the read-only `describe` provider the UI-at-scale scenarios need.
_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True)

#: Live topology is pinned to the aks-1k profile shape (issue #186 Task 8):
#: exactly 20 namespaces of 50 Pods each, matching `seed-manifests`'s output.
_REQUIRED_OBJECT_COUNT = 1000
_REQUIRED_NAMESPACE_COUNT = 20

#: The immutable dedicated-test target contract (design doc "Fixed target and
#: capacity"). The identity gate fails closed unless `az aks show` reports
#: exactly this resource group, cluster name, and both required tags, so a
#: production cluster (or its ARM id) can never satisfy the gate even if the
#: operator supplies a matching, valid ARM id and kubeconfig context.
_REQUIRED_RESOURCE_GROUP = "rg-korvid-contract-test"
_REQUIRED_CLUSTER_NAME = "aks-korvid-contract-test"
_REQUIRED_TAGS: Mapping[str, str] = MappingProxyType(
    {"purpose": "korvid-contract-testing", "production-use": "prohibited"}
)

#: HTTP status the mutation path may retry. 429 (API Priority and Fairness)
#: is the only one: it is a "come back later" answer to a well-formed,
#: fully guarded request, and the retry re-issues the *identical* guarded
#: patch. Every other status - including a failed `test` op - aborts the run.
_RETRYABLE_MUTATION_STATUS = 429

#: How many times teardown re-attempts draining the churn task when its own
#: await is interrupted by a cancellation aimed at the caller.
_CANCEL_DRAIN_ATTEMPTS = 3


async def _sleep_default(delay: float) -> None:
    await asyncio.sleep(delay)


@dataclass(frozen=True)
class LiveLimits:
    """Every explicit bound the live run enforces.

    Nothing in a live run may be unbounded: an in-flight patch, the number of
    concurrent patches, the wait for churn to finish, the wait for the store to
    converge, and even the initial kubeconfig/credential-plugin connection all
    have a stated ceiling. Tests inject small values; production uses defaults
    sized for the published 30-minute live profile.

    Args:
        churn_concurrency: Maximum simultaneously in-flight guarded patches.
            Serial patching cannot approach the scheduled rate (one round trip
            per event), so concurrency is explicit rather than implicit.
        mutation_timeout_seconds: Ceiling for one guarded patch attempt.
        mutation_throttle_retries: Bounded retries of the *identical* guarded
            patch after HTTP 429, and only after 429.
        mutation_retry_base_delay_seconds: First backoff delay; doubles per
            retry.
        mutation_retry_max_delay_seconds: Hard ceiling for exponential backoff,
            deterministic jitter, and a server-provided `Retry-After` hint.
        mutation_connect_timeout_seconds: Ceiling for connecting the mutation
            client (a kubeconfig exec credential plugin can block).
        read_connect_timeout_seconds: Ceiling for external read-path setup that
            a kubeconfig exec credential plugin can block indefinitely - the
            identity/context-host lookup, the harness read client connect, and
            the application read client connect.
        initial_render_timeout_seconds: Ceiling for the initial 1,000-row
            render.
        churn_grace_seconds: Allowance added to the profile's own scheduled
            duration when bounding the churn wait.
        convergence_timeout_seconds: Ceiling for the store digest to converge
            with the independently read cluster digest.
    """

    churn_concurrency: int = 32
    mutation_timeout_seconds: float = 30.0
    mutation_throttle_retries: int = 5
    mutation_retry_base_delay_seconds: float = 0.5
    mutation_retry_max_delay_seconds: float = 30.0
    mutation_connect_timeout_seconds: float = 60.0
    read_connect_timeout_seconds: float = 60.0
    initial_render_timeout_seconds: float = 60.0
    churn_grace_seconds: float = 300.0
    convergence_timeout_seconds: float = 120.0


@dataclass
class ChurnProgress:
    """Live counters for the churn phase, observable while it runs.

    Kept separate from `ChurnSummary` (the frozen report value) because
    `run_live_replay` reads `started` *during* the run to decide whether churn
    was really under way when input latency was measured.
    """

    requested_events: int = 0
    started: int = 0
    completed: int = 0
    mutation_throttles: int = 0
    first_started_at: float | None = None
    last_completed_at: float | None = None

    def record_started(self, at: float) -> None:
        if self.first_started_at is None:
            self.first_started_at = at
        self.started += 1

    def record_completed(self, at: float) -> None:
        self.completed += 1
        self.last_completed_at = at

    def wall_seconds(self) -> float | None:
        if self.first_started_at is None or self.last_completed_at is None:
            return None
        return self.last_completed_at - self.first_started_at

    def summary(self, *, requested_duration_seconds: int) -> ChurnSummary:
        return ChurnSummary.from_observations(
            requested_events=self.requested_events,
            requested_duration_seconds=requested_duration_seconds,
            observed_events=self.completed,
            wall_seconds=self.wall_seconds(),
            mutation_throttles=self.mutation_throttles,
        )


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

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]: ...

    def stream_logs(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]: ...


class MutationClient(Protocol):
    """Issues one guarded, metadata-only mutation; production talks JSON-Patch
    to a real API server, tests mutate an in-memory fake cluster the same way.

    There is deliberately no method that can write `status`, `spec`, an
    ownership label, or delete anything.
    """

    async def connect(self) -> None: ...

    async def patch_pod_labels_guarded(
        self, namespace: str, name: str, *, uid: str, tick: str
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
    own bookkeeping reads (the ownership gate and the ground-truth re-read) -
    those reads must never dilute the application-path signal.
    """

    command_runner: CommandRunner
    active_context: Callable[[], str | None]
    context_host: Callable[[str], Awaitable[str]]
    kube_client_factory: Callable[[ReadTelemetry], KubeReadClient]
    harness_kube_client_factory: Callable[[], KubeReadClient]
    mutation_client_factory: Callable[[str], MutationClient]
    #: Resolves the immutable korvid commit for the run manifest. A live
    #: evidence run fails closed when this returns `None` rather than publish
    #: an untraceable artifact. Injected in tests for determinism.
    resolve_sha: Callable[[], str | None] = resolve_korvid_sha


@dataclass(frozen=True)
class LiveClusterFacts:
    """Verified, immutable facts about the live target, extracted from the
    identity gate's `az aks show` payload and recorded in the live manifest so
    retained evidence establishes exactly which cluster matrix was qualified.
    """

    context: str
    cluster_id: str
    kubernetes_version: str | None
    node_pools: tuple[NodePoolInfo, ...]


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


def build_guarded_label_patch(*, uid: str, run_id: str, tick: str) -> list[dict[str, Any]]:
    """The exact JSON-Patch op list a guarded churn mutation issues.

    `test`s the target Pod's UID and *both* ownership labels, then `add`s the
    dedicated non-ownership `manifests.TICK_LABEL`, so a stale, foreign, or
    replaced Pod aborts the whole patch server-side - there is no unguarded
    fallback. `add` (not `replace`) is used for the tick because RFC 6902
    `replace` requires the member to exist already, while `add` on an object
    member creates or overwrites it; the seeded Pods start without it.

    Nothing outside `metadata.labels` is written: no `status`, no `spec`, and
    neither ownership label is ever a target of a write op.
    """
    managed_by_path = f"/metadata/labels/{_json_pointer_escape(manifests.MANAGED_BY_LABEL)}"
    run_path = f"/metadata/labels/{_json_pointer_escape(manifests.RUN_LABEL)}"
    tick_path = f"/metadata/labels/{_json_pointer_escape(manifests.TICK_LABEL)}"
    return [
        {"op": "test", "path": "/metadata/uid", "value": uid},
        {"op": "test", "path": managed_by_path, "value": manifests.MANAGED_BY_VALUE},
        {"op": "test", "path": run_path, "value": run_id},
        {"op": "add", "path": tick_path, "value": tick},
    ]


class _KubeMutationClient:
    """Production `MutationClient`: issues a guarded, metadata-only JSON-Patch
    against the Pod resource itself (never the `pods/status` subresource) of
    *context*."""

    def __init__(self, context: str, run_id: str) -> None:
        self._context = context
        self._run_id = run_id
        self._api: k8s_client.ApiClient | None = None
        self._core_v1: k8s_client.CoreV1Api | None = None

    async def connect(self) -> None:
        """Load the kubeconfig and build the API client.

        Called eagerly (under `LiveLimits.mutation_connect_timeout_seconds`)
        before the measured window opens: `load_kube_config` can invoke an
        exec credential plugin, and that latency must not be charged to the
        first churn mutation - `KubeClient.probe_context` bounds exactly the
        same call for exactly the same reason.
        """
        if self._core_v1 is not None:
            return
        configuration = k8s_client.Configuration()
        await load_refreshable_kube_config(
            context=self._context,
            client_configuration=configuration,
            persist_config=False,
        )
        self._api = k8s_client.ApiClient(configuration)
        self._core_v1 = k8s_client.CoreV1Api(self._api)

    async def patch_pod_labels_guarded(
        self, namespace: str, name: str, *, uid: str, tick: str
    ) -> None:
        await self.connect()
        core_v1 = self._core_v1
        if core_v1 is None:  # pragma: no cover - connect() always sets it
            raise RuntimeError("mutation client is not connected")
        ops = build_guarded_label_patch(uid=uid, run_id=self._run_id, tick=tick)
        try:
            await core_v1.patch_namespaced_pod(
                name,
                namespace,
                ops,
                _content_type="application/json-patch+json",  # type: ignore[call-arg]  # kubernetes_asyncio's .pyi stub omits _content_type; accepted via **kwargs at runtime
            )
        except k8s_client.exceptions.ApiException as exc:
            raw_body = getattr(exc, "body", "") or ""
            body = (
                raw_body.decode("utf-8", errors="replace")
                if isinstance(raw_body, bytes)
                else str(raw_body)
            )
            raise ApiStatusError(
                int(exc.status or 0),
                str(exc.reason or ""),
                body=body,
                retry_after_seconds=_api_retry_after_seconds(exc, body),
            ) from exc

    async def close(self) -> None:
        if self._api is not None:
            await self._api.close()


def _default_dependencies(context: str) -> LiveDependencies:
    """Real production wiring: subprocess `az`, real kubeconfig, real `KubeClient`.

    Two independent `KubeClient` connections are constructed: the
    telemetry-wired one `run_live_replay` hands to `make_live_watch_source`,
    and a plain, non-instrumented one (`read_telemetry=None`, so
    `KubeClient._observe_read` is a no-op) for the harness's own ownership
    and ground-truth reads.
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


def _is_running_ready(pod: PodSummary) -> bool:
    """Whether *pod* is phase `Running` with every container ready.

    The published live protocol requires exactly 1,000 Running, Ready Pods
    before measuring. `ready` is the kubectl-style `<ready>/<total>` string, so
    a Pod is ready only when both counts are equal and non-zero (e.g. `1/1`).
    """
    if pod.phase != "Running":
        return False
    parts = pod.ready.split("/")
    if len(parts) != 2:
        return False
    try:
        ready, total = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return total > 0 and ready == total


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
    *, context: str, expected_cluster_id: str, deps: LiveDependencies, limits: LiveLimits
) -> LiveClusterFacts:
    """Fail-closed 5-step cluster identity gate; every failure raises
    `ValueError` before any client is constructed or any mutation attempted.

    Returns the verified, immutable cluster facts (context, ARM id, Kubernetes
    version, node-pool topology) so the caller can record them in the live
    manifest without a second, unbounded metadata lookup.
    """
    active = deps.active_context()
    if active != context:
        raise ValueError(
            f"active kubeconfig context {active!r} does not match required context {context!r}"
        )

    async with asyncio.timeout(limits.read_connect_timeout_seconds):
        hostname = await deps.context_host(context)

    async with asyncio.timeout(limits.read_connect_timeout_seconds):
        result = await deps.command_runner(
            [
                "az",
                "aks",
                "show",
                "--resource-group",
                _REQUIRED_RESOURCE_GROUP,
                "--name",
                _REQUIRED_CLUSTER_NAME,
                "-o",
                "json",
            ]
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

    _verify_immutable_target(payload)

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

    return LiveClusterFacts(
        context=context,
        cluster_id=expected_cluster_id,
        kubernetes_version=_kubernetes_version(payload),
        node_pools=_node_pools(payload),
    )


def _kubernetes_version(payload: dict[str, Any]) -> str | None:
    """The control-plane Kubernetes version AKS reports for the cluster."""
    for key in ("currentKubernetesVersion", "kubernetesVersion"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _node_pools(payload: dict[str, Any]) -> tuple[NodePoolInfo, ...]:
    """Node-pool name/version/count topology from the `az aks show` payload."""
    profiles = payload.get("agentPoolProfiles")
    if not isinstance(profiles, list):
        return ()
    pools: list[NodePoolInfo] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        name = profile.get("name")
        if not isinstance(name, str):
            continue
        version = (
            profile.get("currentOrchestratorVersion") or profile.get("orchestratorVersion") or ""
        )
        count = profile.get("count")
        pools.append(
            NodePoolInfo(
                name=name,
                kubernetes_version=str(version),
                node_count=int(count) if isinstance(count, int) else 0,
            )
        )
    return tuple(pools)


def _verify_immutable_target(payload: dict[str, Any]) -> None:
    """Fail closed unless *payload* describes the fixed dedicated-test target.

    Validating only that `az` and the kubeconfig agree on the operator-supplied
    ARM id is not enough: a production cluster plus its own matching production
    id would pass. The dedicated-test contract is immutable - a fixed resource
    group, cluster name, and both required test-only tags - so those attributes
    are checked directly from the `az aks show` payload before any client is
    constructed or any mutation is attempted.
    """
    resource_group = payload.get("resourceGroup")
    if resource_group != _REQUIRED_RESOURCE_GROUP:
        raise ValueError(
            f"az aks show resource group {resource_group!r} is not the required "
            f"dedicated-test resource group {_REQUIRED_RESOURCE_GROUP!r}"
        )
    name = payload.get("name")
    if name != _REQUIRED_CLUSTER_NAME:
        raise ValueError(
            f"az aks show cluster name {name!r} is not the required dedicated-test "
            f"cluster name {_REQUIRED_CLUSTER_NAME!r}"
        )
    tags = payload.get("tags")
    if not isinstance(tags, dict):
        raise ValueError("az aks show returned no tags; required test-only tags are missing")
    for key, expected_value in _REQUIRED_TAGS.items():
        if tags.get(key) != expected_value:
            raise ValueError(
                f"az aks show is missing required tag {key}={expected_value!r} "
                f"(got {tags.get(key)!r}); refusing to target a cluster not marked "
                f"as korvid contract-testing"
            )


def _expected_pod_names(namespace_count: int, object_count: int) -> tuple[str, ...]:
    """The exact Pod names `seed-manifests` creates in *every* namespace."""
    pods_per_namespace = object_count // namespace_count
    return tuple(
        manifests.pod_name(namespace_count, local_index * namespace_count)
        for local_index in range(pods_per_namespace)
    )


@dataclass
class _PodOwnershipProblems:
    """Accumulated ownership-gate failures across all namespaces/Pods."""

    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    not_ready: list[str] = field(default_factory=list)


async def _collect_owned_pods(
    kube: KubeReadClient,
    *,
    run_id: str,
    expected_namespaces: list[str],
    wanted: tuple[str, ...],
) -> tuple[dict[tuple[str, str], PodSummary], _PodOwnershipProblems]:
    """List every expected namespace and classify its Pods against the contract."""
    problems = _PodOwnershipProblems()
    validated: dict[tuple[str, str], PodSummary] = {}
    for namespace in expected_namespaces:
        pods_by_name = {pod.name: pod for pod in await kube.list_pods(namespace)}
        problems.unexpected.extend(
            f"{namespace}/{name}" for name in sorted(set(pods_by_name) - set(wanted))
        )
        for name in wanted:
            pod = pods_by_name.get(name)
            if pod is None:
                problems.missing.append(f"{namespace}/{name}")
            elif not _owns(pod.labels, run_id):
                problems.mismatched.append(f"{namespace}/{name}")
            elif not _is_running_ready(pod):
                problems.not_ready.append(
                    f"{namespace}/{name} (phase={pod.phase}, ready={pod.ready})"
                )
            else:
                validated[(namespace, name)] = pod
    return validated, problems


async def _verify_ownership(
    kube: KubeReadClient, *, run_id: str, namespace_count: int, object_count: int
) -> dict[tuple[str, str], PodSummary]:
    """Ownership gate: *exactly* the expected namespaces and Pods must exist,
    each with both ownership labels *and* phase Running/Ready, before any churn.

    An unexpected Pod inside an owned namespace is rejected too: the
    application watch filters by namespace, so a foreign Pod would enter the
    benchmark store and turn a precise ownership violation into a generic
    "1,000 rows never rendered" timeout minutes later. A labelled but
    non-Running/non-Ready Pod is rejected as well, since the published protocol
    requires exactly `object_count` Running, Ready owned Pods before measuring
    and the later table-row check cannot distinguish a Pending Pod's row.

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

    wanted = _expected_pod_names(namespace_count, object_count)
    validated_pods, problems = await _collect_owned_pods(
        kube, run_id=run_id, expected_namespaces=expected_namespaces, wanted=wanted
    )
    if problems.missing:
        raise ValueError(f"missing expected pods: {', '.join(problems.missing)}")
    if problems.mismatched:
        raise ValueError(f"pods with mismatched ownership labels: {', '.join(problems.mismatched)}")
    if problems.unexpected:
        raise ValueError(f"unexpected pods in owned namespaces: {', '.join(problems.unexpected)}")
    if problems.not_ready:
        raise ValueError(
            f"owned pods not Running or not Ready: {', '.join(problems.not_ready)}; "
            f"the live run requires exactly {object_count} Running, Ready owned pods"
        )
    return validated_pods


async def read_and_validate_owned_pods(
    kube: KubeReadClient,
    *,
    run_id: str,
    expected: Mapping[tuple[str, str], PodSummary],
) -> list[PodSummary]:
    """Independently re-read the owned Pods and revalidate them before use.

    The ground-truth digest may only be computed from Pods that still are what
    the ownership gate validated: same `(namespace, name)` identity set, same
    UID (a delete/recreate produces a new one), and both ownership labels
    intact. A Pod that lost either label, was replaced, disappeared, or a Pod
    that appeared unexpectedly, is named in the raised error instead of being
    silently folded into the digest.
    """
    namespaces = sorted({namespace for namespace, _name in expected})
    found: dict[tuple[str, str], PodSummary] = {}
    seen: set[tuple[str, str]] = set()
    problems: list[str] = []
    for namespace in namespaces:
        for pod in await kube.list_pods(namespace):
            key = (namespace, pod.name)
            baseline = expected.get(key)
            if baseline is None:
                problems.append(f"{namespace}/{pod.name} (unexpected pod)")
                continue
            seen.add(key)
            if pod.uid != baseline.uid:
                problems.append(
                    f"{namespace}/{pod.name} (uid changed {baseline.uid!r} -> {pod.uid!r})"
                )
            elif not _owns(pod.labels, run_id):
                problems.append(f"{namespace}/{pod.name} (lost ownership labels)")
            else:
                found[key] = pod
    problems.extend(
        f"{namespace}/{name} (missing)"
        for namespace, name in expected
        if (namespace, name) not in seen
    )
    if problems:
        raise ValueError(f"post-churn ownership revalidation failed: {', '.join(sorted(problems))}")
    return list(found.values())


def make_live_watch_source(
    kube: KubeReadClient,
    expected_namespaces: frozenset[str],
    *,
    run_id: str,
    recorder: BenchmarkRecorder,
    now: Callable[[], float] = monotonic,
) -> WatchSource:
    """Filter the real cluster-wide Pod watch to exactly the expected, seeded
    namespaces - unrelated cluster Pods (or namespaces sharing the cluster with
    this run) must never enter the benchmark store.

    Event-to-render measurement starts *here*, at watch receipt of an owned
    `MODIFIED` event, exactly where the deterministic `_ReplaySource` starts
    it. Recording at patch acknowledgement instead would race the watch event
    it is meant to measure (the two arrive over independent connections) and
    would silently fold the write round-trip into a read-path latency metric.

    The event counter is shared across reconnects, so a re-LIST after a dropped
    watch continues the same sequence.
    """
    sequence = 0

    async def _source(kind: str, _scope: str) -> AsyncIterator[tuple[str, PodSummary]]:
        nonlocal sequence
        if kind != "pods":
            raise ValueError(f"run_live_replay only watches pods, got kind={kind!r}")
        async for event_type, pod in kube.watch_pods(None):
            if pod.namespace not in expected_namespaces:
                continue
            if event_type == "MODIFIED" and _owns(pod.labels, run_id):
                sequence += 1
                recorder.record_event(sequence, now())
            yield (event_type, pod)

    return _source


def _first_error(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The most informative leaf of a `TaskGroup` failure.

    A guard failure that aborts the run is reported to the caller as the
    `ApiStatusError` it really is, not as an `ExceptionGroup` wrapper. Sibling
    tasks cancelled *because* of that failure contribute `CancelledError`
    leaves, which are only returned when there is nothing else.
    """
    leaves: list[BaseException] = []
    pending: list[BaseException] = list(group.exceptions)
    while pending:
        exc = pending.pop(0)
        if isinstance(exc, BaseExceptionGroup):
            pending.extend(exc.exceptions)
        else:
            leaves.append(exc)
    for exc in leaves:
        if not isinstance(exc, asyncio.CancelledError):
            return exc
    return leaves[0] if leaves else group


def _parse_retry_seconds(value: object) -> float | None:
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def _api_retry_after_seconds(exc: BaseException, body: str) -> float | None:
    headers = getattr(exc, "headers", None)
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                parsed = _parse_retry_seconds(value)
                if parsed is not None:
                    return parsed

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    details = payload.get("details")
    if not isinstance(details, dict):
        return None
    return _parse_retry_seconds(details.get("retryAfterSeconds"))


def _mutation_retry_delay_seconds(
    exc: ApiStatusError,
    *,
    namespace: str,
    name: str,
    uid: str,
    tick: str,
    attempt: int,
    limits: LiveLimits,
) -> float:
    backoff = limits.mutation_retry_base_delay_seconds * (2 ** (attempt - 1))
    digest = hashlib.sha256(f"{namespace}\0{name}\0{uid}\0{tick}\0{attempt}".encode()).digest()
    jitter_fraction = int.from_bytes(digest[:8], "big") / (1 << 64)
    jittered_backoff = backoff * jitter_fraction
    server_hint = exc.retry_after_seconds or 0.0
    return float(
        min(
            server_hint + jittered_backoff,
            limits.mutation_retry_max_delay_seconds,
        )
    )


async def _mutate_once(
    mutation_client: MutationClient,
    *,
    namespace: str,
    name: str,
    uid: str,
    tick: str,
    progress: ChurnProgress,
    limits: LiveLimits,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], float],
) -> None:
    """One guarded patch, bounded in time and retried only on HTTP 429.

    The retry re-issues the *identical* guarded patch, so it is still atomic
    and still fail-closed: a Pod that lost its identity or labels between
    attempts fails the `test` ops exactly as it would on the first attempt.
    A server-provided delay is treated as a floor, then target-specific jitter
    is added without exceeding the configured retry-delay ceiling.
    Every other status - including a failed `test` (422) - propagates
    immediately and aborts the whole run.
    """
    progress.record_started(now())
    attempt = 0
    while True:
        try:
            async with asyncio.timeout(limits.mutation_timeout_seconds):
                await mutation_client.patch_pod_labels_guarded(namespace, name, uid=uid, tick=tick)
        except ApiStatusError as exc:
            if (
                exc.status != _RETRYABLE_MUTATION_STATUS
                or attempt >= limits.mutation_throttle_retries
            ):
                raise
            progress.mutation_throttles += 1
            attempt += 1
            await sleep(
                _mutation_retry_delay_seconds(
                    exc,
                    namespace=namespace,
                    name=name,
                    uid=uid,
                    tick=tick,
                    attempt=attempt,
                    limits=limits,
                )
            )
            continue
        progress.record_completed(now())
        return


async def drive_live_churn(
    events: Iterable[ScheduledEvent],
    *,
    run_id: str,
    namespace_count: int,
    live_state: Mapping[tuple[str, str], PodSummary],
    mutation_client: MutationClient,
    options: ReplayOptions,
    progress: ChurnProgress,
    limits: LiveLimits,
    profile: WorkloadProfile,
    recorder: BenchmarkRecorder,
) -> None:
    """Drive guarded churn at wall-clock time with explicit bounded concurrency.

    The schedule is followed exactly as `_ReplaySource` follows it (absolute
    offsets converted to inter-event delays), but each mutation is dispatched
    to a task instead of being awaited inline: one round trip per event caps a
    serial driver far below the profile's scheduled rate, which would silently
    understate the load the report claims to have applied.

    Concurrency is bounded by `LiveLimits.churn_concurrency` and every single
    attempt by `LiveLimits.mutation_timeout_seconds`; nothing here is
    unbounded. Any guard failure (`ApiStatusError`) cancels every sibling task
    and propagates unchanged - there is no unguarded fallback and no attempt to
    continue past a failed `test` op.

    This function never records event timing: event-to-render measurement
    starts at watch receipt (`make_live_watch_source`), not at patch ack.
    """
    now = options.monotonic_fn if options.monotonic_fn is not None else monotonic
    sleep = options.async_sleep if options.async_sleep is not None else _sleep_default
    semaphore = asyncio.Semaphore(limits.churn_concurrency)
    start = now()

    async def _run(namespace: str, name: str, uid: str, tick: str) -> None:
        try:
            await _mutate_once(
                mutation_client,
                namespace=namespace,
                name=name,
                uid=uid,
                tick=tick,
                progress=progress,
                limits=limits,
                sleep=sleep,
                now=now,
            )
        finally:
            semaphore.release()

    try:
        async with asyncio.TaskGroup() as group:
            # Burst-end offsets (absolute seconds) mark the moment each burst's
            # window closes, so the post-burst backlog drain can be timed on the
            # same real-clock axis the render pass records on — exactly as the
            # deterministic replay driver does. Without this the live report
            # leaves `post_burst_drain_seconds` empty and the published
            # burst-drain budget cannot be evaluated.
            burst_ends = burst_end_offsets(profile)
            next_burst = 0
            for event in events:
                elapsed = now() - start
                delay = event.offset_seconds * options.time_scale - elapsed
                if delay > 0:
                    await sleep(delay)

                while (
                    next_burst < len(burst_ends) and event.offset_seconds >= burst_ends[next_burst]
                ):
                    recorder.mark_burst_end(monotonic())
                    next_burst += 1

                namespace, name = live_object_identity(run_id, namespace_count, event.object_index)
                current = live_state.get((namespace, name))
                if current is None:
                    raise ValueError(
                        f"live churn target {namespace}/{name} is not in the seeded state"
                    )
                # Bound in-flight work *before* creating the task, so a slow
                # API server throttles the driver instead of accumulating an
                # unbounded backlog of pending patches.
                await semaphore.acquire()
                group.create_task(_run(namespace, name, current.uid, str(event.sequence)))
    except BaseExceptionGroup as group_error:
        raise _first_error(group_error) from None


async def _cancel_and_drain(task: asyncio.Task[None]) -> None:
    """Cancel *task* and wait for it to finish before any client is closed.

    Every in-flight mutation lives inside the churn task's `TaskGroup`, so
    awaiting the churn task is exactly what guarantees no patch is still in
    flight when `mutation_client.close()` runs - and that no mutation outlives
    teardown. A cancellation delivered to *this* coroutine while it drains is
    absorbed and retried a bounded number of times: the caller's own
    `CancelledError` is already propagating out of the enclosing `try`, and
    skipping the drain is precisely the failure mode this exists to prevent.
    """
    task.cancel()
    for _ in range(_CANCEL_DRAIN_ATTEMPTS):
        if task.done():
            break
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            continue
    if task.done() and not task.cancelled():
        # Retrieve any exception so it is never reported as "never retrieved";
        # the error that triggered teardown is the one the caller sees.
        task.exception()


def _store_digest(store: ResourceStore) -> str:
    return summary_digest(cast(Iterable[PodSummary], store.get("pods", ALL_NAMESPACES)))


#: The scoped UI-at-scale scenarios issue #186 requires, each a real Textual
#: pilot key sequence driven during active churn. Sequences are self-restoring:
#: they return the workspace to a single pane showing all owned rows so the
#: post-churn row-count and digest-convergence checks still see 1,000 rows.
#:
#: Every step declares the observable state it must reach. `pilot.press`
#: returning is not evidence: `describe` and `multi_log` bail out with an
#: "unavailable" warning when no manifest/log provider is wired, and the
#: all-namespaces toggle is a no-op while the configured namespace already is
#: `ALL_NAMESPACES` — none of which raises. Without the state check a run would
#: report UI-at-scale evidence it never gathered.


@dataclass(frozen=True)
class _UIStep:
    """One key sequence plus the observable app state it must reach."""

    keys: tuple[str, ...]
    label: str
    reached: Callable[[Any], bool]


@dataclass(frozen=True)
class _UIScenario:
    """One named UI-at-scale scenario as an ordered list of verified steps."""

    name: str
    steps: tuple[_UIStep, ...]


#: Bound on one step reaching its target state. Generous on purpose: the step
#: competes with full-rate churn on the same event loop.
_UI_STEP_TIMEOUT = 30.0


def _ui_scenarios(scoped_namespace: str) -> tuple[_UIScenario, ...]:
    """Build the scenario table; the namespace scenario needs a real seeded
    namespace to scope down to (and back out of)."""
    return (
        # Filter to a substring present in every seeded Pod name ("bench-*"), so
        # the filter exercises the real path without dropping any rows, then
        # clear it so the bar releases focus and the workspace is restored.
        _UIScenario(
            "filter",
            (
                _UIStep(
                    ("slash", "b", "e", "n", "c", "h"),
                    "filter applied",
                    lambda app: app.filter_pattern == "bench",
                ),
                _UIStep(
                    ("escape",),
                    "filter cleared",
                    lambda app: app.filter_pattern == "",
                ),
            ),
        ),
        # Sorting by age (a metrics-free column) keeps every row visible.
        _UIScenario(
            "sort",
            (
                _UIStep(
                    ("A",),
                    "age sort active",
                    lambda app: app._sorts.get("pods") is not None,
                ),
            ),
        ),
        # Scope to one seeded namespace (favorite key 1), then back to all
        # namespaces so the full 1,000-row topology is restored.
        _UIScenario(
            "namespace_switch",
            (
                _UIStep(
                    ("1",),
                    "scoped to one namespace",
                    lambda app: app.current_scope == scoped_namespace,
                ),
                _UIStep(
                    ("0",),
                    "restored to all namespaces",
                    lambda app: app.current_scope == ALL_NAMESPACES,
                ),
            ),
        ),
        _UIScenario(
            "split_pane",
            (
                _UIStep(("ctrl+w", "v"), "second pane open", lambda app: len(app._panes) == 2),
                _UIStep(("ctrl+w", "q"), "back to one pane", lambda app: len(app._panes) == 1),
            ),
        ),
        _UIScenario(
            "describe",
            (
                _UIStep(
                    ("d",),
                    "describe screen open",
                    lambda app: isinstance(app.screen, DescribeScreen),
                ),
                _UIStep(
                    ("escape",),
                    "describe dismissed",
                    lambda app: not isinstance(app.screen, DescribeScreen),
                ),
            ),
        ),
        _UIScenario(
            "multi_log",
            (
                _UIStep(("L",), "log pane open", lambda app: bool(app._log_pane.display)),
                _UIStep(("escape",), "log pane closed", lambda app: not app._log_pane.display),
            ),
        ),
    )


def _describe_provider(
    kube: KubeReadClient,
) -> Callable[[str, str | None, str], Awaitable[dict[str, Any]]]:
    """Read-only manifest fetcher for the `describe` UI-at-scale scenario.

    Only Pods are reachable in the live workspace, so anything else is a
    programmer error rather than a cluster read.
    """

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        if kind != _PODS_META.plural:
            raise ValueError(f"live qualification only describes pods, not {kind!r}")
        return await kube.get_object(_PODS_META, namespace, name)

    return get_manifest


async def drive_ui_scenarios(
    pilot: Any,
    recorder: BenchmarkRecorder,
    *,
    now: Callable[[], float],
    app: Any,
    scoped_namespace: str,
) -> None:
    """Drive the scoped UI-at-scale scenarios through the real Textual pilot.

    Each scenario is a real key sequence issued against the running app during
    active churn; after every step the app must reach the observable state the
    step names, or the scenario is recorded `ok=False`. Every scenario is
    read-only navigation - no scenario writes, deletes, or drains - so this
    never weakens live safety. A scenario that raises or that fails to reach a
    target state is recorded as `ok=False` and never aborts the safety-critical
    run; the remaining steps still run so the sequences restore a single-pane,
    all-rows workspace and later convergence checks stay intact.
    """
    for scenario in _ui_scenarios(scoped_namespace):
        started = now()
        ok = True
        for step in scenario.steps:
            try:
                for key in step.keys:
                    await pilot.press(key)
                await wait_for(
                    pilot,
                    lambda step=step: step.reached(app),  # type: ignore[misc]  # bind per step
                    timeout=_UI_STEP_TIMEOUT,
                    label=f"{scenario.name}: {step.label}",
                    recorder=recorder,
                )
            except Exception:
                ok = False
        recorder.record_scenario(scenario.name, now() - started, ok)


def _check_row_count(row_count: int, expected: int) -> None:
    """Re-assert the exact rendered row count before teardown.

    A late `WatchManager` reconnect clears and re-seeds the store, so a digest
    that matched a moment ago can be recomputed over a partially re-listed
    store. Checking the rendered row count at the same instant turns that into
    an explicit, named failure instead of a plausible-looking report.
    """
    if row_count != expected:
        raise ValueError(
            f"rendered row count regressed before teardown: expected {expected}, got {row_count}"
        )


@dataclass
class _LiveRunState:
    """Values produced inside the measured window and consumed after it."""

    expected_digest: str = ""
    final_digest: str = ""
    churn_started_before_input: bool = False
    progress: ChurnProgress = field(default_factory=ChurnProgress)


async def _run_measured_window(
    *,
    app: MeasuredKorvidApp,
    store: ResourceStore,
    harness_kube: KubeReadClient,
    mutation_client: MutationClient,
    recorder: BenchmarkRecorder,
    profile: WorkloadProfile,
    options: ReplayOptions,
    limits: LiveLimits,
    run_id: str,
    live_state: dict[tuple[str, str], PodSummary],
    state: _LiveRunState,
) -> None:
    """Drive the app: initial render, churn, ground-truth read, convergence.

    Everything that must observe a *live* watch happens inside this function,
    including the independent cluster read and the digest convergence wait.
    Reading ground truth after the watch had already been stopped would compare
    a frozen store against a cluster that was still changing.
    """
    now = options.monotonic_fn if options.monotonic_fn is not None else monotonic
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)

        await wait_for(
            pilot,
            lambda: table.row_count == profile.object_count,
            timeout=limits.initial_render_timeout_seconds,
            label="initial owned pods rendered",
            recorder=recorder,
        )
        # The table is fully populated: mark the interactive boundary that
        # closes the startup and LIST-to-populated-table phases.
        recorder.mark_interactive(monotonic())

        events = scheduled_events(profile)
        state.progress.requested_events = len(events)
        churn_task = asyncio.create_task(
            drive_live_churn(
                events,
                run_id=run_id,
                namespace_count=profile.namespace_count,
                live_state=live_state,
                mutation_client=mutation_client,
                options=options,
                progress=state.progress,
                limits=limits,
                profile=profile,
                recorder=recorder,
            )
        )
        try:
            if events:
                # Real ordering signal: input latency is only meaningful if
                # churn was actually under way, so wait for the first mutation
                # to be dispatched (or for churn to fail) before measuring.
                await wait_for(
                    pilot,
                    lambda: state.progress.started > 0 or churn_task.done(),
                    timeout=limits.initial_render_timeout_seconds,
                    label="first churn mutation started",
                    recorder=recorder,
                )
            state.churn_started_before_input = state.progress.started > 0

            if not (
                churn_task.done() and (churn_task.cancelled() or churn_task.exception() is not None)
            ):
                recorder.record_input(await measure_cursor_input(pilot, table, "down", now=now))
            if not (
                churn_task.done() and (churn_task.cancelled() or churn_task.exception() is not None)
            ):
                recorder.record_input(await measure_cursor_input(pilot, table, "up", now=now))

            # UI-at-scale evidence: drive the scoped scenarios (filter, sort,
            # namespace switch, split pane, describe, multi-log) through the
            # real pilot while churn is still active. Read-only navigation only.
            await drive_ui_scenarios(
                pilot,
                recorder,
                now=now,
                app=app,
                scoped_namespace=manifests.namespace_name(run_id, 0),
            )

            churn_timeout = (
                profile.duration_seconds * options.time_scale + limits.churn_grace_seconds
            )
            async with asyncio.timeout(churn_timeout):
                await churn_task

            # Ground truth, read independently *while the watch is still live*
            # via the non-instrumented harness client (never polluting the
            # application-path telemetry), and revalidated for ownership.
            final_pods = await read_and_validate_owned_pods(
                harness_kube, run_id=run_id, expected=live_state
            )
            state.expected_digest = summary_digest(final_pods)

            await wait_for(
                pilot,
                lambda: (
                    recorder.pending_count() == 0 and _store_digest(store) == state.expected_digest
                ),
                timeout=limits.convergence_timeout_seconds,
                label=(
                    "store digest converged with the independently read cluster digest "
                    f"({state.expected_digest})"
                ),
                recorder=recorder,
            )
            _check_row_count(table.row_count, profile.object_count)
            # Independent of the digest above, which compares a store digest
            # with a store digest and would accept a table full of stale cells.
            check_rendered_rows(table, final_pods)
            state.final_digest = _store_digest(store)
        finally:
            await _cancel_and_drain(churn_task)


async def run_live_replay(
    profile: WorkloadProfile,
    options: ReplayOptions,
    *,
    context: str,
    expected_cluster_id: str,
    run_id: str,
    deps: LiveDependencies | None = None,
    limits: LiveLimits | None = None,
) -> ReplayReport:
    """Replay churn against an already-seeded real AKS cluster and return metrics.

    Fail-closed order: `time_scale`/`run_id`/topology/profile validation, the
    cluster identity gate, the ownership gate - all *before* any mutation
    client is constructed - then the real application-path wiring
    (`KubeClient` -> `WatchManager` -> `ResourceStore` -> `MeasuredKorvidApp`),
    guarded metadata-only churn, and digest parity against an independent,
    revalidated re-read of the cluster taken while the watch is still live.

    Two `KubeReadClient` connections are used: `harness_kube` (never wired to
    telemetry) performs the ownership gate and the ground-truth re-read;
    `kube` (wired to `recorder.record_api`) is only ever handed to
    `make_live_watch_source`, so `ReplayReport.api` reports exactly the
    production application read path. The ownership gate's validated Pod
    snapshot is reused as the pre-churn uid snapshot - it is never re-listed.
    """
    _validate_time_scale(options)
    manifests.validate_run_id(run_id)
    _validate_topology(profile)
    # Re-check duration-dependent invariants: a caller may hand us a profile
    # rewritten with `dataclasses.replace` (the CLI's `--duration`), which
    # bypasses `load_profile` entirely.
    validate_profile(profile)

    active_limits = limits if limits is not None else LiveLimits()
    active_deps = deps if deps is not None else _default_dependencies(context)
    facts = await _verify_cluster_identity(
        context=context,
        expected_cluster_id=expected_cluster_id,
        deps=active_deps,
        limits=active_limits,
    )

    # Fail closed on an untraceable evidence run: a live report must be tied to
    # an immutable korvid commit, resolved *before* any client is constructed.
    korvid_sha = active_deps.resolve_sha()
    if korvid_sha is None:
        raise ValueError(
            "cannot resolve an immutable korvid SHA for the live evidence run; "
            "set GITHUB_SHA in CI or run from a git checkout with a resolvable HEAD"
        )

    store = ResourceStore()
    recorder = BenchmarkRecorder()
    sampler = ProcessSampler(options.sample_interval)
    state = _LiveRunState()

    harness_kube = active_deps.harness_kube_client_factory()
    try:
        async with asyncio.timeout(active_limits.read_connect_timeout_seconds):
            await harness_kube.connect(context)
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

        # Constructed only after the ownership gate passes, then connected
        # eagerly under an explicit bound so credential-plugin latency is paid
        # before the measured window rather than inside the first mutation.
        mutation_client = active_deps.mutation_client_factory(run_id)
        try:
            async with asyncio.timeout(active_limits.mutation_connect_timeout_seconds):
                await mutation_client.connect()

            kube = active_deps.kube_client_factory(recorder.record_api)
            try:
                async with asyncio.timeout(active_limits.read_connect_timeout_seconds):
                    await kube.connect(context)
                source = make_live_watch_source(
                    kube,
                    expected_namespaces,
                    run_id=run_id,
                    recorder=recorder,
                )
                watch_manager = WatchManager(store, source, retry_delay=0.0)
                manifest = build_manifest(
                    profile,
                    korvid_sha=korvid_sha,
                    context=facts.context,
                    cluster_id=facts.cluster_id,
                    kubernetes_version=facts.kubernetes_version,
                    node_pools=facts.node_pools,
                )

                app = MeasuredKorvidApp(
                    config=KorvidConfig(
                        namespace=ALL_NAMESPACES,
                        # Key 1 scopes to a real seeded namespace; key 0 returns
                        # to all namespaces. Without a favorite the toggle is a
                        # no-op (the configured namespace already is
                        # ALL_NAMESPACES) and the scenario proves nothing.
                        favorite_namespaces=(manifests.namespace_name(run_id, 0),),
                    ),
                    store=store,
                    watch_manager=watch_manager,
                    recorder=recorder,
                    # Read-only providers: `describe` and `multi_log` bail out
                    # with an "unavailable" warning when these are None, so the
                    # scenarios would report success having done nothing. The
                    # harness connection is used so these reads stay out of the
                    # measured application read path.
                    aliases={"pods": _PODS_META},
                    get_manifest=_describe_provider(harness_kube),
                    stream_logs=harness_kube.stream_logs,
                )

                sampler.start()
                recorder.mark_process_start(monotonic())
                try:
                    await _run_measured_window(
                        app=app,
                        store=store,
                        harness_kube=harness_kube,
                        mutation_client=mutation_client,
                        recorder=recorder,
                        profile=profile,
                        options=options,
                        limits=active_limits,
                        run_id=run_id,
                        live_state=live_state,
                        state=state,
                    )
                finally:
                    process_samples = await sampler.stop()
                    await watch_manager.stop_all()
            finally:
                await kube.close()
        finally:
            await mutation_client.close()
    finally:
        await harness_kube.close()

    churn = state.progress.summary(requested_duration_seconds=profile.duration_seconds)
    benchmark = recorder.report(
        manifest,
        process_samples,
        final_digest=state.final_digest,
        expected_digest=state.expected_digest,
        churn=churn,
    )

    return ReplayReport(
        object_count=profile.object_count,
        expected_digest=state.expected_digest,
        final_digest=state.final_digest,
        dropped_updates=benchmark.dropped_updates,
        rendered_updates=benchmark.rendered_updates,
        render_passes=benchmark.render_passes,
        coalesced_updates=benchmark.coalesced_updates,
        event_to_render=benchmark.event_to_render,
        input_latency=benchmark.input_latency,
        churn_started_before_input=state.churn_started_before_input,
        process=benchmark.process,
        api=benchmark.api,
        phases=benchmark.phases,
        manifest=benchmark.manifest,
        failures_injected=benchmark.failures_injected,
        ui_scenarios=benchmark.ui_scenarios,
        churn=churn,
    )
