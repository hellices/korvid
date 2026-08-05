"""Guarded real-AKS application-path replay prerequisite tests (issue #186 task 8.2).

Every test substitutes the identity/kubeconfig/subprocess/KubeClient seams
(`LiveDependencies`) with fakes: no test here may contact Azure, a real
kubeconfig, or a real cluster (see `live.py`'s module docstring).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
from collections import Counter
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from korvid.core.store import Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.telemetry import ReadTelemetry, ReadTelemetryEvent
from tests.performance import live, manifests
from tests.performance.live import (
    CommandResult,
    LiveDependencies,
    build_guarded_status_patch,
    drive_live_churn,
    live_object_identity,
    make_live_watch_source,
    run_live_replay,
)
from tests.performance.manifests import build_seed_manifests
from tests.performance.metrics import BenchmarkRecorder
from tests.performance.profile import WorkloadProfile
from tests.performance.replay import ReplayOptions
from tests.performance.workload import scheduled_events, summary_digest

RUN_ID = "aks186"
CONTEXT = "aks-korvid-perf"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
CLUSTER_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg"
    "/providers/Microsoft.ContainerService/managedClusters/aks-korvid-perf"
)
FQDN = "aks-korvid-perf-dns-abc123.hcp.eastus.azmk8s.io"

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _labels(run_id: str) -> tuple[tuple[str, str], ...]:
    return (
        (manifests.MANAGED_BY_LABEL, manifests.MANAGED_BY_VALUE),
        (manifests.RUN_LABEL, run_id),
    )


def _build_fake_topology(
    run_id: str, namespace_count: int, object_count: int
) -> tuple[dict[str, GenericSummary], dict[tuple[str, str], PodSummary]]:
    namespaces = {
        manifests.namespace_name(run_id, i): GenericSummary(
            name=manifests.namespace_name(run_id, i),
            namespace="",
            kind="Namespace",
            created="2024-01-01T00:00:00Z",
            uid=f"ns-uid-{i}",
            labels=_labels(run_id),
        )
        for i in range(namespace_count)
    }
    pods: dict[tuple[str, str], PodSummary] = {}
    for index in range(object_count):
        namespace, name = live_object_identity(run_id, namespace_count, index)
        pods[(namespace, name)] = PodSummary(
            name=name,
            namespace=namespace,
            phase="Running",
            ready="1/1",
            restarts=0,
            node="node-0",
            uid=f"pod-uid-{index}",
            labels=_labels(run_id),
        )
    return namespaces, pods


class _FakeKubeClient:
    """Fake `KubeReadClient`: an in-memory cluster the fake mutation client
    mutates directly, so the watch stream really observes guarded patches -
    exactly like a real API server + watch, with zero network I/O.

    Mirrors the real `KubeClient`'s telemetry behavior exactly: `list_objects`
    and `list_pods` (used only by the harness's ownership/final reads) emit a
    "list" event, and `watch_pods` (the real application read path) emits
    "list" then "watch_open"/"watch_event" - all conditional on
    `read_telemetry` being wired, exactly like `KubeClient._observe_read`
    being a no-op when `read_telemetry is None`. Two `_FakeKubeClient`
    instances constructed over the *same* `namespaces`/`pods` dict objects
    model two independent connections to one shared cluster, exactly like
    `run_live_replay`'s real harness/app-path `KubeClient` pair.
    """

    def __init__(
        self,
        namespaces: dict[str, GenericSummary],
        pods: dict[tuple[str, str], PodSummary],
        *,
        distractor_pods: tuple[PodSummary, ...] = (),
    ) -> None:
        self.read_telemetry: ReadTelemetry | None = None
        self.namespaces = namespaces
        self.pods = pods
        self.distractor_pods = distractor_pods
        self.connect_context: str | None = "__not_connected__"
        self.closed = False
        self.events: asyncio.Queue[tuple[str, PodSummary]] = asyncio.Queue()
        #: Per-namespace `list_pods` call log; asserts the ownership gate's
        #: validated snapshot is reused rather than immediately re-listed.
        self.list_pods_calls: list[str] = []
        self.list_objects_calls = 0
        self.watch_pods_calls = 0

    async def connect(self, context: str | None = None) -> None:
        self.connect_context = context

    async def close(self) -> None:
        self.closed = True

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        assert meta.kind == "Namespace"
        assert namespace is None
        self.list_objects_calls += 1
        if self.read_telemetry is not None:
            self.read_telemetry(ReadTelemetryEvent("list", "/api/v1/namespaces"))
        return list(self.namespaces.values())

    async def list_pods(self, namespace: str) -> list[PodSummary]:
        self.list_pods_calls.append(namespace)
        if self.read_telemetry is not None:
            self.read_telemetry(ReadTelemetryEvent("list", f"/api/v1/namespaces/{namespace}/pods"))
        return [pod for (ns, _name), pod in self.pods.items() if ns == namespace]

    async def watch_pods(self, namespace: str | None) -> AsyncIterator[tuple[str, PodSummary]]:
        assert namespace is None
        self.watch_pods_calls += 1
        if self.read_telemetry is not None:
            self.read_telemetry(ReadTelemetryEvent("list", "/api/v1/pods"))
        for pod in self.pods.values():
            yield ("ADDED", pod)
        for pod in self.distractor_pods:
            yield ("ADDED", pod)
        if self.read_telemetry is not None:
            self.read_telemetry(ReadTelemetryEvent("watch_open", "/api/v1/pods"))
        while True:
            event = await self.events.get()
            if self.read_telemetry is not None:
                self.read_telemetry(ReadTelemetryEvent("watch_event", "/api/v1/pods"))
            yield event


class _FakeMutationClient:
    """Fake `MutationClient`: applies the guard checks a real JSON-Patch
    `test` op would enforce, then mutates the shared fake cluster and wakes
    the fake watch - so a guard failure here is exactly as fatal as a real
    412/422 from the API server."""

    def __init__(self, kube: _FakeKubeClient, run_id: str) -> None:
        self._kube = kube
        self._run_id = run_id
        self.calls: list[tuple[str, str, str]] = []
        self.closed = False

    async def patch_pod_status_guarded(
        self, namespace: str, name: str, *, uid: str, phase: str
    ) -> None:
        self.calls.append((namespace, name, phase))
        current = self._kube.pods.get((namespace, name))
        labels = dict(current.labels) if current is not None else {}
        guard_ok = (
            current is not None
            and current.uid == uid
            and labels.get(manifests.MANAGED_BY_LABEL) == manifests.MANAGED_BY_VALUE
            and labels.get(manifests.RUN_LABEL) == self._run_id
        )
        if not guard_ok or current is None:
            raise ApiStatusError(422, "test operation failed for guarded patch")
        updated = dataclasses.replace(current, phase=phase)
        self._kube.pods[(namespace, name)] = updated
        self._kube.events.put_nowait(("MODIFIED", updated))

    async def close(self) -> None:
        self.closed = True


def _never_called(label: str) -> Callable[..., Any]:
    def _fail(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError(f"{label} must not be called")

    return _fail


def _ok_command_runner(
    *, cluster_id: str = CLUSTER_ID, fqdn: str = FQDN, private_fqdn: str = ""
) -> Callable[[Any], Awaitable[CommandResult]]:
    async def _run(_args: Any) -> CommandResult:
        payload = {"id": cluster_id, "fqdn": fqdn, "privateFqdn": private_fqdn}
        return CommandResult(0, json.dumps(payload), "")

    return _run


def _happy_deps(
    namespaces: dict[str, GenericSummary],
    pods: dict[tuple[str, str], PodSummary],
    run_id: str,
    *,
    distractor_pods: tuple[PodSummary, ...] = (),
    mutation_clients: list[_FakeMutationClient] | None = None,
    mutation_client_factory: Callable[[str], Any] | None = None,
    harness_clients: list[_FakeKubeClient] | None = None,
    app_clients: list[_FakeKubeClient] | None = None,
) -> LiveDependencies:
    """Build a `LiveDependencies` whose harness and application-path
    `KubeReadClient`s are two *separate* `_FakeKubeClient` instances sharing
    the same underlying `namespaces`/`pods` dicts by reference (so a guarded
    mutation issued against the app-path instance is immediately visible to
    the harness's later independent re-read) - exactly mirroring
    `run_live_replay`'s real harness/app-path `KubeClient` pair. Pass
    `harness_clients`/`app_clients` to capture the constructed instances for
    assertions (telemetry exclusion, call counts, connect/close)."""

    async def context_host(context: str) -> str:
        assert context == CONTEXT
        return FQDN

    # The most recently constructed app-path client: `default_mutation_factory`
    # is only ever invoked (by `run_live_replay`) after `kube_client_factory`,
    # so this always resolves to the live watch's own instance/queue.
    app_holder: list[_FakeKubeClient] = []

    def default_mutation_factory(run_id_arg: str) -> _FakeMutationClient:
        client = _FakeMutationClient(app_holder[-1], run_id_arg)
        if mutation_clients is not None:
            mutation_clients.append(client)
        return client

    def kube_factory(read_telemetry: ReadTelemetry) -> _FakeKubeClient:
        client = _FakeKubeClient(namespaces, pods, distractor_pods=distractor_pods)
        client.read_telemetry = read_telemetry
        app_holder.append(client)
        if app_clients is not None:
            app_clients.append(client)
        return client

    def harness_factory() -> _FakeKubeClient:
        client = _FakeKubeClient(namespaces, pods, distractor_pods=distractor_pods)
        if harness_clients is not None:
            harness_clients.append(client)
        return client

    return LiveDependencies(
        command_runner=_ok_command_runner(),
        active_context=lambda: CONTEXT,
        context_host=context_host,
        kube_client_factory=kube_factory,
        harness_kube_client_factory=harness_factory,
        mutation_client_factory=mutation_client_factory or default_mutation_factory,
    )


def _virtual_clock() -> tuple[Callable[[], float], Callable[[float], Awaitable[None]]]:
    virtual_time = [0.0]

    def monotonic_fn() -> float:
        return virtual_time[0]

    async def async_sleep(delay: float) -> None:
        virtual_time[0] += delay
        await asyncio.sleep(0)

    return monotonic_fn, async_sleep


def _tiny_live_profile(*, seed: int = 1) -> WorkloadProfile:
    """A profile satisfying the mandatory 1,000/20 live topology with a
    minimal churn schedule (2 events), kept fast via the virtual clock."""
    return WorkloadProfile(
        schema_version=1,
        id="live-test",
        seed=seed,
        object_count=1000,
        namespace_count=20,
        steady_events_per_second=2,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )


# ---------------------------------------------------------------------------
# Deterministic mapping
# ---------------------------------------------------------------------------


def test_live_object_identity_matches_seed_manifest_layout() -> None:
    run_id = "run1"
    namespace_count = 4
    pods_per_namespace = 3
    manifest_docs = build_seed_manifests(
        run_id=run_id,
        namespace_count=namespace_count,
        pods_per_namespace=pods_per_namespace,
        node_selector="korvid.dev/pool=perftest",
    )
    pod_docs = [doc for doc in manifest_docs if doc["kind"] == "Pod"]
    expected_by_index = {}
    for index, doc in enumerate(pod_docs):
        metadata = doc["metadata"]
        assert isinstance(metadata, dict)
        expected_by_index[index] = (metadata["namespace"], metadata["name"])

    for index in range(namespace_count * pods_per_namespace):
        assert live_object_identity(run_id, namespace_count, index) == expected_by_index[index]


def test_live_object_identity_examples() -> None:
    assert live_object_identity("aks186", 20, 0) == ("korvid-perf-aks186-0", "bench-0")
    assert live_object_identity("aks186", 20, 19) == ("korvid-perf-aks186-19", "bench-0")
    assert live_object_identity("aks186", 20, 20) == ("korvid-perf-aks186-0", "bench-1")
    assert live_object_identity("aks186", 20, 999) == ("korvid-perf-aks186-19", "bench-49")


# ---------------------------------------------------------------------------
# Guarded patch construction
# ---------------------------------------------------------------------------


def test_build_guarded_status_patch_tests_uid_and_both_ownership_labels() -> None:
    ops = build_guarded_status_patch(uid="uid-1", run_id="run1", phase="Pending")
    assert ops == [
        {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
        {
            "op": "test",
            "path": "/metadata/labels/app.kubernetes.io~1managed-by",
            "value": "korvid-performance",
        },
        {
            "op": "test",
            "path": "/metadata/labels/korvid.dev~1performance-run",
            "value": "run1",
        },
        {"op": "replace", "path": "/status/phase", "value": "Pending"},
    ]


# ---------------------------------------------------------------------------
# Guarded churn
# ---------------------------------------------------------------------------


async def test_drive_live_churn_sends_guarded_patches_for_every_event() -> None:
    run_id = "run1"
    namespace_count = 2
    _, pods = _build_fake_topology(run_id, namespace_count, 4)
    kube = _FakeKubeClient({}, pods)
    mutation_client = _FakeMutationClient(kube, run_id)
    profile = WorkloadProfile(
        schema_version=1,
        id="churn-test",
        seed=7,
        object_count=4,
        namespace_count=namespace_count,
        steady_events_per_second=4,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    events = scheduled_events(profile)
    monotonic_fn, async_sleep = _virtual_clock()
    options = ReplayOptions(time_scale=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep)

    recorder = BenchmarkRecorder()
    live_state = dict(pods)
    await drive_live_churn(
        events,
        run_id=run_id,
        namespace_count=namespace_count,
        live_state=live_state,
        mutation_client=mutation_client,
        recorder=recorder,
        options=options,
    )
    assert len(mutation_client.calls) == len(events)
    for call, event in zip(mutation_client.calls, events, strict=True):
        namespace, name, phase = call
        expected_namespace, expected_name = live_object_identity(
            run_id, namespace_count, int(event.summary.name.removeprefix("pod-"))
        )
        assert (namespace, name) == (expected_namespace, expected_name)
        assert phase == event.summary.phase


async def test_drive_live_churn_aborts_on_guard_failure_and_never_continues() -> None:
    run_id = "run1"
    namespace_count = 2
    _, pods = _build_fake_topology(run_id, namespace_count, 4)
    kube = _FakeKubeClient({}, pods)
    mutation_client = _FakeMutationClient(kube, run_id)
    profile = WorkloadProfile(
        schema_version=1,
        id="churn-abort-test",
        seed=7,
        object_count=4,
        namespace_count=namespace_count,
        steady_events_per_second=4,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    events = scheduled_events(profile)
    assert len(events) >= 2

    monotonic_fn, async_sleep = _virtual_clock()
    options = ReplayOptions(time_scale=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep)

    recorder = BenchmarkRecorder()
    # Snapshot the driver's cached state *before* the target pod is replaced
    # (a fresh uid) - exactly like `run_live_replay` caching uids once during
    # the ownership gate, then churning without re-reading them each time.
    live_state = dict(pods)

    # Simulate the target pod being replaced right before the second
    # scheduled mutation - a real API server would fail the JSON-Patch
    # `test` op the same way.
    second_index = int(events[1].summary.name.removeprefix("pod-"))
    second_key = live_object_identity(run_id, namespace_count, second_index)
    kube.pods[second_key] = dataclasses.replace(kube.pods[second_key], uid="replaced-uid")

    with pytest.raises(ApiStatusError, match="test operation failed"):
        await drive_live_churn(
            events,
            run_id=run_id,
            namespace_count=namespace_count,
            live_state=live_state,
            mutation_client=mutation_client,
            recorder=recorder,
            options=options,
        )
    # Aborted at the 2nd call; a 3rd event must never have been attempted.
    assert len(mutation_client.calls) == 2


# ---------------------------------------------------------------------------
# Namespace filtering
# ---------------------------------------------------------------------------


async def test_make_live_watch_source_filters_to_expected_namespaces() -> None:
    run_id = "run1"
    _, pods = _build_fake_topology(run_id, 2, 4)
    distractor = PodSummary(
        name="stray",
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node="node-x",
    )
    kube = _FakeKubeClient({}, pods, distractor_pods=(distractor,))
    expected_namespaces = frozenset(namespace for namespace, _name in pods)
    source = make_live_watch_source(kube, expected_namespaces)

    seen: list[tuple[str, Summary]] = []
    agen = source("pods", "*")
    try:
        for _ in range(len(pods)):
            seen.append(await agen.__anext__())
        # The distractor pod, and only it, must never surface.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(agen.__anext__(), timeout=0.05)
    finally:
        assert isinstance(agen, AsyncGenerator)
        await agen.aclose()

    assert {pod.namespace for _event, pod in seen} == expected_namespaces
    assert all(pod.name != "stray" for _event, pod in seen)


async def test_make_live_watch_source_rejects_non_pod_kind() -> None:
    kube = _FakeKubeClient({}, {})
    source = make_live_watch_source(kube, frozenset())
    with pytest.raises(ValueError, match="only watches pods"):
        await source("deployments", "*").__anext__()


# ---------------------------------------------------------------------------
# Gates: time scale / run_id / topology / identity - all reject before
# any client is constructed.
# ---------------------------------------------------------------------------


async def test_run_live_replay_rejects_time_scale_other_than_one() -> None:
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=_never_called("active_context"),
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match=re.escape("time_scale == 1.0")):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_invalid_run_id() -> None:
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=_never_called("active_context"),
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="run_id must be 1-48"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id="Bad Run",
            deps=deps,
        )


@pytest.mark.parametrize(
    ("object_count", "namespace_count"),
    [(999, 20), (1000, 19), (1001, 20), (100, 7)],
)
async def test_run_live_replay_rejects_topology_mismatch_before_identity_gate(
    object_count: int, namespace_count: int
) -> None:
    profile = WorkloadProfile(
        schema_version=1,
        id="bad-topology",
        seed=1,
        object_count=object_count,
        namespace_count=namespace_count,
        steady_events_per_second=1,
        duration_seconds=1,
        bursts=(),
        failures=(),
    )
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=_never_called("active_context"),
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(
        ValueError, match=re.escape("object_count") + "|" + re.escape("namespace_count")
    ):
        await run_live_replay(
            profile,
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_wrong_active_context_before_mutation() -> None:
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=lambda: "some-other-context",
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="active kubeconfig context"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_wrong_aks_resource_id_before_mutation() -> None:
    async def context_host(_context: str) -> str:
        return FQDN

    deps = LiveDependencies(
        command_runner=_ok_command_runner(cluster_id="/subscriptions/x/wrong-cluster"),
        active_context=lambda: CONTEXT,
        context_host=context_host,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="expected"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_wrong_api_hostname_before_mutation() -> None:
    async def context_host(_context: str) -> str:
        return "not-the-real-host.example.com"

    deps = LiveDependencies(
        command_runner=_ok_command_runner(),
        active_context=lambda: CONTEXT,
        context_host=context_host,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="does not match"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_malformed_identity_command_output() -> None:
    async def command_runner(_args: Any) -> CommandResult:
        return CommandResult(0, "not-json{{{", "")

    async def context_host(_context: str) -> str:
        return FQDN

    deps = LiveDependencies(
        command_runner=command_runner,
        active_context=lambda: CONTEXT,
        context_host=context_host,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="malformed JSON"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_nonzero_identity_command_exit() -> None:
    async def command_runner(_args: Any) -> CommandResult:
        return CommandResult(1, "", "ERROR: az login required")

    async def context_host(_context: str) -> str:
        return FQDN

    deps = LiveDependencies(
        command_runner=command_runner,
        active_context=lambda: CONTEXT,
        context_host=context_host,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="az aks show failed"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_default_command_runner_surfaces_missing_executable() -> None:
    with pytest.raises(ValueError, match="executable not found"):
        await live._default_command_runner(["korvid-test-definitely-missing-binary-xyz"])


# ---------------------------------------------------------------------------
# Ownership gate
# ---------------------------------------------------------------------------


async def test_run_live_replay_rejects_missing_namespace_before_churn() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    del namespaces[manifests.namespace_name(RUN_ID, 5)]
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="missing expected namespaces"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_cross_run_namespace_label_before_churn() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    bad_name = manifests.namespace_name(RUN_ID, 3)
    namespaces[bad_name] = dataclasses.replace(namespaces[bad_name], labels=_labels("other-run"))
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="namespaces missing/mismatched ownership labels"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_missing_pod_before_churn() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    key = live_object_identity(RUN_ID, 20, 42)
    del pods[key]
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="missing"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_cross_run_pod_label_before_churn() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    key = live_object_identity(RUN_ID, 20, 42)
    pods[key] = dataclasses.replace(pods[key], labels=_labels("some-other-run-id"))
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    with pytest.raises(ValueError, match="mismatched ownership labels"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_never_constructs_app_client_when_ownership_fails() -> None:
    """The MEDIUM finding's harness/app-path split must not weaken the
    ownership-before-mutation ordering: when the ownership gate rejects, the
    application-path (telemetry-wired) `KubeClient` must never even be
    constructed - only the harness client is used for the (failing)
    ownership check."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    del namespaces[manifests.namespace_name(RUN_ID, 5)]
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        mutation_client_factory=_never_called("mutation_client_factory"),
    )
    deps = dataclasses.replace(deps, kube_client_factory=_never_called("kube_client_factory"))
    with pytest.raises(ValueError, match="missing expected namespaces"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


# ---------------------------------------------------------------------------
# `_verify_ownership` returns a reusable validated snapshot (no redundant
# re-listing for uids)
# ---------------------------------------------------------------------------


async def test_verify_ownership_returns_validated_pod_snapshot() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    kube = _FakeKubeClient(namespaces, pods)

    validated = await live._verify_ownership(
        kube, run_id=RUN_ID, namespace_count=20, object_count=1000
    )

    assert validated == pods
    # Exactly one `list_pods` call per namespace: the gate itself, no
    # redundant second pass.
    assert sorted(kube.list_pods_calls) == sorted(namespaces)


# ---------------------------------------------------------------------------
# Full happy path: real app-path wiring, telemetry, digest parity
# ---------------------------------------------------------------------------


async def test_run_live_replay_full_happy_path_matches_cluster_digest() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    harness_clients: list[_FakeKubeClient] = []
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(
        namespaces, pods, RUN_ID, harness_clients=harness_clients, app_clients=app_clients
    )
    monotonic_fn, async_sleep = _virtual_clock()
    options = ReplayOptions(
        time_scale=1.0, sample_interval=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep
    )

    report = await run_live_replay(
        _tiny_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.object_count == 1000
    assert report.dropped_updates == 0
    assert report.expected_digest == report.final_digest
    assert report.expected_digest == summary_digest(pods.values())

    # Exactly one harness client and one app-path client are constructed;
    # both connect to the required context and both get closed.
    assert len(harness_clients) == 1
    assert len(app_clients) == 1
    harness_client, app_client = harness_clients[0], app_clients[0]
    assert harness_client.connect_context == CONTEXT
    assert harness_client.closed
    assert app_client.connect_context == CONTEXT
    assert app_client.closed

    # MEDIUM finding: the app-path client's telemetry is the *only* source
    # of `report.api` - its single "list" comes from `watch_pods`'s own
    # internal LIST-then-WATCH, not from any harness read.
    assert app_client.watch_pods_calls == 1
    assert report.api.operations["watch_open"] == 1
    assert report.api.operations.get("list", 0) == 1

    # The harness client never watches - it is only ever used for the
    # ownership gate and the final independent re-read.
    assert harness_client.watch_pods_calls == 0

    # LOW finding: the ownership gate's validated snapshot is reused as the
    # pre-churn uid snapshot - each namespace is only `list_pods`-ed twice on
    # the harness client (the ownership gate itself, then the post-churn
    # independent re-read), never a redundant third time for uids.
    per_namespace_list_calls = Counter(harness_client.list_pods_calls)
    assert set(per_namespace_list_calls.values()) == {2}
    assert app_client.list_pods_calls == []

    assert report.churn_started_before_input


async def test_run_live_replay_aborts_and_still_closes_clients_on_guard_failure() -> None:
    """A guard failure mid-churn (a real API server's `test` op rejection)
    must propagate as `ApiStatusError` *and* still close the harness/app-path
    kube clients and the mutation client via `run_live_replay`'s `finally`
    teardown - the same guarantee `run_replay` gives on any mid-run failure."""

    class _AlwaysFailingMutationClient:
        def __init__(self) -> None:
            self.closed = False

        async def patch_pod_status_guarded(
            self, namespace: str, name: str, *, uid: str, phase: str
        ) -> None:
            raise ApiStatusError(422, "test operation failed for guarded patch")

        async def close(self) -> None:
            self.closed = True

    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    harness_clients: list[_FakeKubeClient] = []
    app_clients: list[_FakeKubeClient] = []
    failing_client = _AlwaysFailingMutationClient()
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        mutation_client_factory=lambda run_id_arg: failing_client,
        harness_clients=harness_clients,
        app_clients=app_clients,
    )
    monotonic_fn, async_sleep = _virtual_clock()
    options = ReplayOptions(time_scale=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep)

    with pytest.raises(ApiStatusError, match="test operation failed"):
        await run_live_replay(
            _tiny_live_profile(),
            options,
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )

    assert harness_clients[0].closed
    assert app_clients[0].closed
    assert failing_client.closed


async def test_run_live_replay_propagates_cancelled_error_and_still_closes_clients() -> None:
    """`asyncio.CancelledError` raised mid-churn (as real task cancellation
    delivers into whatever the run is awaiting) must propagate out of
    `run_live_replay` unchanged, and the sampler/watch-manager/mutation/kube
    clients must still be closed via the nested `finally` blocks: cancellation
    must never skip teardown or leave the real cluster's watches/clients open.

    This raises `CancelledError` directly from the injected `async_sleep` seam
    rather than calling `Task.cancel()` on a task that owns an in-flight
    `MeasuredKorvidApp.run_test()` - hard-cancelling *that* task triggers an
    unrelated Textual/pytest GC quirk (a dangling internal reactive-watcher
    coroutine surfacing as `PytestUnraisableExceptionWarning` at session
    teardown) that has nothing to do with `live.py`'s own cleanup correctness.
    """
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    harness_clients: list[_FakeKubeClient] = []
    app_clients: list[_FakeKubeClient] = []
    created_clients: list[_FakeMutationClient] = []
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        mutation_clients=created_clients,
        harness_clients=harness_clients,
        app_clients=app_clients,
    )

    async def cancelling_sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    # duration_seconds=1, steady_events_per_second=2 schedules events at
    # offsets 0.0 and 0.5: the first mutates without sleeping (delay <= 0),
    # the second's positive delay drives the cancelling sleep.
    options = ReplayOptions(time_scale=1.0, monotonic_fn=lambda: 0.0, async_sleep=cancelling_sleep)

    with pytest.raises(asyncio.CancelledError):
        await run_live_replay(
            _tiny_live_profile(),
            options,
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )

    assert harness_clients[0].closed
    assert app_clients[0].closed
    assert created_clients
    assert created_clients[0].closed
    # Cancellation must abort before the second churn mutation - no broad
    # cleanup, no continuing past the point of cancellation.
    assert len(created_clients[0].calls) == 1
