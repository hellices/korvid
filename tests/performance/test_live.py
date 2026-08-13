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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from multidict import CIMultiDict, CIMultiDictProxy

from korvid.core.store import ALL_NAMESPACES, Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.telemetry import ReadTelemetry, ReadTelemetryEvent
from tests.performance import live, manifests
from tests.performance import replay as replay_mod
from tests.performance.live import (
    ChurnProgress,
    CommandResult,
    LiveDependencies,
    LiveLimits,
    build_guarded_label_patch,
    drive_live_churn,
    live_object_identity,
    make_live_watch_source,
    read_and_validate_owned_pods,
    run_live_replay,
)
from tests.performance.manifests import build_seed_manifests
from tests.performance.metrics import BenchmarkRecorder
from tests.performance.pacing import sample_paced_schedule
from tests.performance.profile import Burst, WorkloadProfile
from tests.performance.replay import ReplayOptions
from tests.performance.workload import scheduled_events, summary_digest
from tests.ui.waits import WaitTimeout

RUN_ID = "aks186"
CONTEXT = "aks-korvid-contract-test"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000000"
RESOURCE_GROUP = "rg-korvid-contract-test"
CLUSTER_NAME = "aks-korvid-contract-test"
CLUSTER_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.ContainerService/managedClusters/{CLUSTER_NAME}"
)
FQDN = "aks-korvid-contract-test-dns-abc123.hcp.eastus.azmk8s.io"
REQUIRED_TAGS = {"purpose": "korvid-contract-testing", "production-use": "prohibited"}

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
        #: Namespace argument of each watch, in order.
        self.watch_namespaces: list[str | None] = []
        #: True once the watch generator has been closed (the harness stopped
        #: the WatchManager); lets tests assert *when* a read happened
        #: relative to the application watch still being live.
        self.watch_finished = False
        #: Optional per-call spy invoked with the namespace being listed.
        self.on_list_pods: Callable[[str], None] | None = None
        #: Optional watch-open failure (e.g. a 403 the real client would
        #: record as read telemetry before raising).
        self.watch_error: ApiStatusError | None = None
        #: Read-only provider call logs for the UI-at-scale scenarios.
        self.get_object_calls: list[tuple[str | None, str]] = []
        self.stream_logs_calls = 0

    async def connect(self, context: str | None = None) -> None:
        self.connect_context = context

    async def close(self) -> None:
        self.closed = True

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        """Read-only manifest fetch backing the `describe` UI-at-scale scenario."""
        self.get_object_calls.append((namespace, name))
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "status": {"phase": "Running"},
        }

    async def stream_logs(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """Read-only log stream backing the `multi_log` UI-at-scale scenario.

        Yields one line and then stays open, exactly like a real follow stream
        the scenario dismisses with `escape`.
        """
        self.stream_logs_calls += 1
        yield LogLine(pod="bench", container="app", text="benchmark log line")
        await asyncio.Event().wait()

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        assert meta.kind == "Namespace"
        assert namespace is None
        self.list_objects_calls += 1
        if self.read_telemetry is not None:
            self.read_telemetry(ReadTelemetryEvent("list", "/api/v1/namespaces"))
        return list(self.namespaces.values())

    async def list_pods(self, namespace: str) -> list[PodSummary]:
        self.list_pods_calls.append(namespace)
        if self.on_list_pods is not None:
            self.on_list_pods(namespace)
        if self.read_telemetry is not None:
            self.read_telemetry(ReadTelemetryEvent("list", f"/api/v1/namespaces/{namespace}/pods"))
        return [pod for (ns, _name), pod in self.pods.items() if ns == namespace]

    def _initial_watch_pods(self, namespace: str | None) -> list[PodSummary]:
        """Pods the initial LIST of a watch returns: everything for the
        all-namespaces watch (plus the unowned distractors a real cluster-wide
        LIST would also return), only the namespace's own Pods when scoped."""
        if namespace is not None:
            return [pod for pod in self.pods.values() if pod.namespace == namespace]
        return [*self.pods.values(), *self.distractor_pods]

    async def watch_pods(self, namespace: str | None) -> AsyncIterator[tuple[str, PodSummary]]:
        # `namespace is None` is the all-namespaces watch the measured window
        # runs on; a concrete namespace is the scoped watch the
        # `namespace_switch` UI-at-scale scenario really triggers.
        self.watch_pods_calls += 1
        self.watch_namespaces.append(namespace)
        # Tracks the *currently open* watch: a scope change closes one
        # generator and opens another, so "some generator finished" would not
        # mean the application watch has stopped.
        self.watch_finished = False
        try:
            if self.watch_error is not None:
                if self.read_telemetry is not None:
                    self.read_telemetry(
                        ReadTelemetryEvent("error", "/api/v1/pods", status=self.watch_error.status)
                    )
                raise self.watch_error
            if self.read_telemetry is not None:
                self.read_telemetry(ReadTelemetryEvent("list", "/api/v1/pods"))
            for pod in self._initial_watch_pods(namespace):
                yield ("ADDED", pod)
            if self.read_telemetry is not None:
                self.read_telemetry(ReadTelemetryEvent("watch_open", "/api/v1/pods"))
            while True:
                event = await self.events.get()
                if self.read_telemetry is not None:
                    self.read_telemetry(ReadTelemetryEvent("watch_event", "/api/v1/pods"))
                yield event
        finally:
            self.watch_finished = True


class _FakeMutationClient:
    """Fake `MutationClient`: applies the guard checks a real JSON-Patch
    `test` op would enforce, then writes the dedicated tick label on the
    shared fake cluster and wakes the fake watch - so a guard failure here is
    exactly as fatal as a real 422 from the API server."""

    def __init__(
        self,
        kube: _FakeKubeClient | Callable[[], _FakeKubeClient],
        run_id: str,
    ) -> None:
        # `run_live_replay` constructs (and eagerly connects) the mutation
        # client *before* the application-path watch client, so tests that
        # need the watch client resolve it lazily via a callable.
        self._resolve_kube: Callable[[], _FakeKubeClient] = (
            kube if callable(kube) else (lambda: kube)
        )
        self._run_id = run_id
        self.calls: list[tuple[str, str, str]] = []
        self.closed = False
        self.connect_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def patch_pod_labels_guarded(
        self, namespace: str, name: str, *, uid: str, tick: str
    ) -> None:
        self.calls.append((namespace, name, tick))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self._apply(namespace, name, uid=uid, tick=tick)
        finally:
            self.in_flight -= 1

    async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
        kube = self._resolve_kube()
        current = kube.pods.get((namespace, name))
        labels = dict(current.labels) if current is not None else {}
        guard_ok = (
            current is not None
            and current.uid == uid
            and labels.get(manifests.MANAGED_BY_LABEL) == manifests.MANAGED_BY_VALUE
            and labels.get(manifests.RUN_LABEL) == self._run_id
        )
        if not guard_ok or current is None:
            raise ApiStatusError(422, "test operation failed for guarded patch")
        labels[manifests.TICK_LABEL] = tick
        updated = dataclasses.replace(current, labels=tuple(sorted(labels.items())))
        kube.pods[(namespace, name)] = updated
        kube.events.put_nowait(("MODIFIED", updated))

    async def close(self) -> None:
        self.closed = True


async def _context_host_ok(_context: str) -> str:
    return FQDN


def _report_as_benchmark(report: Any) -> Any:
    from tests.performance.cli import _to_benchmark_report

    return _to_benchmark_report(report)


def _never_called(label: str) -> Callable[..., Any]:
    def _fail(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError(f"{label} must not be called")

    return _fail


def _ok_command_runner(
    *,
    cluster_id: str = CLUSTER_ID,
    fqdn: str = FQDN,
    private_fqdn: str = "",
    resource_group: str = RESOURCE_GROUP,
    name: str = CLUSTER_NAME,
    tags: dict[str, str] | None = None,
    kubernetes_version: str = "1.30.4",
    agent_pool_profiles: list[dict[str, Any]] | None = None,
) -> Callable[[Any], Awaitable[CommandResult]]:
    async def _run(_args: Any) -> CommandResult:
        payload = {
            "id": cluster_id,
            "fqdn": fqdn,
            "privateFqdn": private_fqdn,
            "resourceGroup": resource_group,
            "name": name,
            "tags": REQUIRED_TAGS if tags is None else tags,
            "currentKubernetesVersion": kubernetes_version,
            "agentPoolProfiles": (
                [
                    {"name": "perftest", "count": 5, "currentOrchestratorVersion": "1.30.4"},
                    {"name": "system", "count": 1, "currentOrchestratorVersion": "1.30.4"},
                ]
                if agent_pool_profiles is None
                else agent_pool_profiles
            ),
        }
        return CommandResult(0, json.dumps(payload), "")

    return _run


_FIXED_SHA = "1234567890abcdef1234567890abcdef12345678"


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
        client = _FakeMutationClient(lambda: app_holder[-1], run_id_arg)
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
        resolve_sha=lambda: _FIXED_SHA,
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


def _paced_live_profile(*, seed: int = 1) -> WorkloadProfile:
    """`_tiny_live_profile` with a schedule that outlives the cursor probe.

    The harness fails a run whose churn finishes before input sampling does -
    an input percentile measured against an idle cluster is not the figure the
    report claims - so a `run_live_replay` test must schedule more events than
    the probe takes samples. Paired with `sample_paced_schedule`, the extra
    events cost event-loop turns, not wall time.
    """
    return dataclasses.replace(_tiny_live_profile(seed=seed), steady_events_per_second=10)


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


def test_build_guarded_label_patch_tests_uid_and_both_ownership_labels() -> None:
    ops = build_guarded_label_patch(uid="uid-1", run_id="run1", tick="7")
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
        {"op": "add", "path": "/metadata/labels/korvid.dev~1performance-tick", "value": "7"},
    ]


def test_build_guarded_label_patch_never_writes_status_spec_or_ownership() -> None:
    """Churn must stay metadata-only on a *non-ownership* label: `status` is
    kubelet-owned (a patched `status.phase` is reverted on the next node sync,
    breaking digest parity) and the design doc requires metadata-only updates.
    The two ownership labels are only ever `test` operands, never written."""
    ops = build_guarded_label_patch(uid="uid-1", run_id="run1", tick="7")
    writes = [op for op in ops if op["op"] != "test"]

    assert len(writes) == 1
    assert writes[0]["path"] == "/metadata/labels/korvid.dev~1performance-tick"
    assert not any(op["path"].startswith("/status") for op in ops)
    assert not any(op["path"].startswith("/spec") for op in ops)
    ownership_paths = {
        "/metadata/labels/app.kubernetes.io~1managed-by",
        "/metadata/labels/korvid.dev~1performance-run",
    }
    assert all(op["op"] == "test" for op in ops if op["path"] in ownership_paths)
    assert manifests.TICK_LABEL not in {manifests.MANAGED_BY_LABEL, manifests.RUN_LABEL}


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
    progress = ChurnProgress(requested_events=len(events))
    live_state = dict(pods)
    await drive_live_churn(
        events,
        run_id=run_id,
        namespace_count=namespace_count,
        live_state=live_state,
        mutation_client=mutation_client,
        options=options,
        progress=progress,
        limits=LiveLimits(churn_concurrency=1),
        profile=_tiny_live_profile(),
        recorder=BenchmarkRecorder(),
    )
    assert len(mutation_client.calls) == len(events)
    for call, event in zip(mutation_client.calls, events, strict=True):
        namespace, name, tick = call
        expected_namespace, expected_name = live_object_identity(
            run_id, namespace_count, event.object_index
        )
        assert (namespace, name) == (expected_namespace, expected_name)
        assert tick == str(event.sequence)
    assert progress.completed == len(events)
    # Event timing is recorded at watch receipt, never by the write path.
    assert recorder.pending_count() == 0


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

    # Snapshot the driver's cached state *before* the target pod is replaced
    # (a fresh uid) - exactly like `run_live_replay` caching uids once during
    # the ownership gate, then churning without re-reading them each time.
    live_state = dict(pods)

    # Simulate the target pod being replaced right before the second
    # scheduled mutation - a real API server would fail the JSON-Patch
    # `test` op the same way.
    second_index = events[1].object_index
    second_key = live_object_identity(run_id, namespace_count, second_index)
    kube.pods[second_key] = dataclasses.replace(kube.pods[second_key], uid="replaced-uid")

    with pytest.raises(ApiStatusError, match="test operation failed"):
        await drive_live_churn(
            events,
            run_id=run_id,
            namespace_count=namespace_count,
            live_state=live_state,
            mutation_client=mutation_client,
            options=options,
            progress=ChurnProgress(requested_events=len(events)),
            limits=LiveLimits(churn_concurrency=1),
            profile=_tiny_live_profile(),
            recorder=BenchmarkRecorder(),
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
    source = make_live_watch_source(
        kube, expected_namespaces, run_id=run_id, recorder=BenchmarkRecorder()
    )

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
    source = make_live_watch_source(kube, frozenset(), run_id="run1", recorder=BenchmarkRecorder())
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


async def test_run_live_replay_full_happy_path_matches_cluster_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    harness_clients: list[_FakeKubeClient] = []
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(
        namespaces, pods, RUN_ID, harness_clients=harness_clients, app_clients=app_clients
    )
    options = sample_paced_schedule(monkeypatch).options(sample_interval=1.0)

    report = await run_live_replay(
        _paced_live_profile(),
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

    # MEDIUM finding: the app-path client's telemetry is the *only* source of
    # `report.api` - every "list" comes from `watch_pods`'s own internal
    # LIST-then-WATCH, not from any harness read. Three watches, not one: the
    # `namespace_switch` UI-at-scale scenario really scopes down to a seeded
    # namespace and back, and a scope change restarts the application watch.
    assert app_client.watch_pods_calls == 3
    # Every watch stays cluster-wide: `make_live_watch_source` pins the read to
    # the owned namespace set regardless of the UI scope, so scoping the table
    # to one namespace never re-targets (or widens) the underlying watch.
    assert app_client.watch_namespaces == [None, None, None]
    # The watch really is stopped by teardown, so `watch_finished` is a
    # meaningful signal for the ground-truth-read ordering assertion.
    assert app_client.watch_finished
    assert report.api.operations["watch_open"] == 3
    assert report.api.operations.get("list", 0) == 3

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


async def test_churn_failed_reports_only_a_finished_task_that_did_not_succeed() -> None:
    """One predicate, used before each cursor sample: a churn task that is
    still running (or finished cleanly) must not suppress the measurement,
    while a crashed or cancelled one must."""

    async def never() -> None:
        await asyncio.Event().wait()

    async def fine() -> None:
        return None

    async def boom() -> None:
        raise RuntimeError("churn died")

    running = asyncio.create_task(never())
    assert live._churn_failed(running) is False
    running.cancel()
    # `CancelledError` from a plain `task.cancel()` carries no message, so the
    # required match pattern is the empty-message anchor (a literal `""` would
    # match anything and pytest warns about it).
    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await running
    assert live._churn_failed(running) is True

    finished = asyncio.create_task(fine())
    await finished
    assert live._churn_failed(finished) is False

    crashed = asyncio.create_task(boom())
    with pytest.raises(RuntimeError, match="churn died"):
        await crashed
    assert live._churn_failed(crashed) is True


async def test_run_live_replay_aborts_and_still_closes_clients_on_guard_failure() -> None:
    """A guard failure mid-churn (a real API server's `test` op rejection)
    must propagate as `ApiStatusError` *and* still close the harness/app-path
    kube clients and the mutation client via `run_live_replay`'s `finally`
    teardown - the same guarantee `run_replay` gives on any mid-run failure."""

    class _AlwaysFailingMutationClient:
        def __init__(self) -> None:
            self.closed = False
            self.connect_calls = 0

        async def connect(self) -> None:
            self.connect_calls += 1

        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
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
        # Yield first so the already-dispatched first mutation task runs to
        # completion, then cancel exactly like a real `Task.cancel()` landing
        # in the scheduler's sleep.
        await asyncio.sleep(0)
        raise asyncio.CancelledError

    # duration_seconds=1, steady_events_per_second=2 schedules events at
    # offsets 0.0 and 0.5: the first is dispatched without sleeping
    # (delay <= 0), the second's positive delay drives the cancelling sleep.
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


# ---------------------------------------------------------------------------
# C1: event timing is recorded at watch receipt, never at patch ack
# ---------------------------------------------------------------------------


async def test_make_live_watch_source_records_owned_modified_events_at_receipt() -> None:
    """Latency measurement must start where the *application* first sees the
    event, exactly like the deterministic `_ReplaySource`. Initial `ADDED`
    rows from the LIST phase and events from foreign namespaces are not churn
    events and must not be recorded."""
    run_id = "run1"
    _, pods = _build_fake_topology(run_id, 2, 4)
    kube = _FakeKubeClient({}, pods)
    recorder = BenchmarkRecorder()
    expected_namespaces = frozenset(namespace for namespace, _name in pods)
    source = make_live_watch_source(kube, expected_namespaces, run_id=run_id, recorder=recorder)

    agen = source("pods", "*")
    try:
        for _ in range(len(pods)):
            await agen.__anext__()
        # Four ADDED rows from the LIST phase are not churn events.
        assert recorder.pending_count() == 0

        owned = next(iter(pods.values()))
        kube.events.put_nowait(("MODIFIED", owned))
        await agen.__anext__()
        assert recorder.pending_count() == 1

        foreign = PodSummary(
            name="stray",
            namespace="default",
            phase="Running",
            ready="1/1",
            restarts=0,
            node="node-x",
        )
        unowned = dataclasses.replace(owned, labels=())
        kube.events.put_nowait(("MODIFIED", foreign))
        kube.events.put_nowait(("MODIFIED", unowned))
        await agen.__anext__()
        assert recorder.pending_count() == 1
    finally:
        assert isinstance(agen, AsyncGenerator)
        await agen.aclose()


async def test_run_live_replay_records_at_receipt_when_the_patch_ack_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the ack-race the review found: a real API server delivers the
    watch event over a different connection than the patch response, so the
    event can be rendered *before* the patch call returns. Recording at ack
    then appended a pending entry after its own render, which the final wait
    could never drain (a 60 s timeout) and which the CLI reported as a dropped
    update."""

    class _AckLagsMutationClient(_FakeMutationClient):
        async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
            await super()._apply(namespace, name, uid=uid, tick=tick)
            # Let the watch deliver and render before the caller resumes.
            for _ in range(20):
                await asyncio.sleep(0.005)

    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(namespaces, pods, RUN_ID, app_clients=app_clients)
    deps = dataclasses.replace(
        deps,
        mutation_client_factory=lambda run_id: _AckLagsMutationClient(
            lambda: app_clients[-1], run_id
        ),
    )
    options = sample_paced_schedule(monkeypatch).options()

    report = await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.dropped_updates == 0
    assert report.churn is not None
    # Every churned event is still rendered exactly once, and none of them is
    # left pending by the record-at-receipt ordering.
    assert report.event_to_render.count == report.churn.observed_events
    assert report.expected_digest == report.final_digest


# ---------------------------------------------------------------------------
# C2: metadata-only, ownership-preserving churn and digest convergence
# ---------------------------------------------------------------------------


async def test_run_live_replay_churns_the_tick_label_and_preserves_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    mutation_clients: list[_FakeMutationClient] = []
    deps = _happy_deps(namespaces, pods, RUN_ID, mutation_clients=mutation_clients)
    options = sample_paced_schedule(monkeypatch).options()

    report = await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    mutated = {(namespace, name) for namespace, name, _tick in mutation_clients[0].calls}
    assert mutated
    for key, pod in pods.items():
        labels = dict(pod.labels)
        assert labels[manifests.MANAGED_BY_LABEL] == manifests.MANAGED_BY_VALUE
        assert labels[manifests.RUN_LABEL] == RUN_ID
        # Kubelet-owned status is never touched by churn.
        assert pod.phase == "Running"
        assert (manifests.TICK_LABEL in labels) is (key in mutated)
    assert report.expected_digest == report.final_digest


async def test_run_live_replay_reads_ground_truth_while_the_watch_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ground-truth read and the convergence wait must both happen inside
    the measured window: reading after `watch_manager.stop_all()` compares a
    frozen store against a cluster that was still changing."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    harness_clients: list[_FakeKubeClient] = []
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(
        namespaces, pods, RUN_ID, harness_clients=harness_clients, app_clients=app_clients
    )
    options = sample_paced_schedule(monkeypatch).options()

    watch_live_during_reads: list[bool] = []

    def spy(_namespace: str) -> None:
        if len(harness_clients[0].list_pods_calls) > 20:  # the post-churn re-read
            watch_live_during_reads.append(not app_clients[0].watch_finished)

    async def run() -> None:
        await run_live_replay(
            _paced_live_profile(),
            options,
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )

    original_factory = deps.harness_kube_client_factory

    def harness_factory() -> _FakeKubeClient:
        client = original_factory()
        assert isinstance(client, _FakeKubeClient)
        client.on_list_pods = spy
        return client

    deps = dataclasses.replace(deps, harness_kube_client_factory=harness_factory)
    await run()

    assert len(watch_live_during_reads) == 20
    assert all(watch_live_during_reads)


async def test_run_live_replay_waits_for_the_store_digest_to_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watch propagation lags the patch acknowledgement. Without an explicit
    convergence wait the store digest is read while the last events are still
    in flight, producing a false digest mismatch (CLI exit 1)."""

    class _LaggingWatchMutationClient(_FakeMutationClient):
        async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
            kube = self._resolve_kube()
            before = kube.events.qsize()
            await super()._apply(namespace, name, uid=uid, tick=tick)
            # Pull the just-queued event back out and re-deliver it later, so
            # the cluster is already mutated while the watch has not caught up.
            assert kube.events.qsize() == before + 1
            delayed = kube.events.get_nowait()
            asyncio.get_running_loop().call_later(1.0, kube.events.put_nowait, delayed)

    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(namespaces, pods, RUN_ID, app_clients=app_clients)
    deps = dataclasses.replace(
        deps,
        mutation_client_factory=lambda run_id: _LaggingWatchMutationClient(
            lambda: app_clients[-1], run_id
        ),
    )
    options = sample_paced_schedule(monkeypatch).options()

    report = await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.expected_digest == report.final_digest
    assert report.expected_digest == summary_digest(pods.values())
    assert report.dropped_updates == 0


def test_check_row_count_rejects_a_regressed_render() -> None:
    """A late watch reconnect clears and re-seeds the store, so a digest that
    matched a moment ago can be recomputed over a partial store; the rendered
    row count is re-asserted at the same instant."""
    live._check_row_count(1000, 1000)

    with pytest.raises(ValueError, match="rendered row count regressed"):
        live._check_row_count(999, 1000)


# ---------------------------------------------------------------------------
# C3: bounded concurrency, bounded operation time, guarded 429-only retry
# ---------------------------------------------------------------------------


def _churn_profile(*, events_per_second: int, duration_seconds: int = 1) -> WorkloadProfile:
    return WorkloadProfile(
        schema_version=1,
        id="churn-test",
        seed=7,
        object_count=4,
        namespace_count=2,
        steady_events_per_second=events_per_second,
        duration_seconds=duration_seconds,
        bursts=(),
        failures=(),
    )


async def test_drive_live_churn_bounds_in_flight_mutations() -> None:
    """Serial patching cannot approach the scheduled rate (one round trip per
    event), so churn dispatches concurrently - but never without a ceiling."""

    class _YieldingMutationClient(_FakeMutationClient):
        async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
            for _ in range(3):
                await asyncio.sleep(0)
            await super()._apply(namespace, name, uid=uid, tick=tick)

    run_id = "run1"
    _, pods = _build_fake_topology(run_id, 2, 4)
    kube = _FakeKubeClient({}, pods)
    mutation_client = _YieldingMutationClient(kube, run_id)
    events = scheduled_events(_churn_profile(events_per_second=40))
    monotonic_fn, async_sleep = _virtual_clock()
    progress = ChurnProgress(requested_events=len(events))

    await drive_live_churn(
        events,
        run_id=run_id,
        namespace_count=2,
        live_state=dict(pods),
        mutation_client=mutation_client,
        options=ReplayOptions(time_scale=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep),
        progress=progress,
        limits=LiveLimits(churn_concurrency=4),
        profile=_tiny_live_profile(),
        recorder=BenchmarkRecorder(),
    )

    assert progress.completed == len(events) == 40
    assert mutation_client.max_in_flight > 1, "churn must not be effectively serial"
    assert mutation_client.max_in_flight <= 4


async def test_drive_live_churn_retries_only_429_with_the_identical_guarded_patch() -> None:
    """A single API Priority and Fairness throttle must not kill a 30-minute
    run, but the retry re-issues the *identical* guarded patch (same uid, same
    ownership tests) - it is never a relaxed or unguarded retry."""

    class _ThrottlingMutationClient(_FakeMutationClient):
        def __init__(self, kube: _FakeKubeClient, run_id: str, throttles: int) -> None:
            super().__init__(kube, run_id)
            self.remaining_throttles = throttles
            self.patch_arguments: list[tuple[str, str, str, str]] = []

        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
        ) -> None:
            self.patch_arguments.append((namespace, name, uid, tick))
            if self.remaining_throttles > 0:
                self.remaining_throttles -= 1
                raise ApiStatusError(429, "Too Many Requests")
            await super().patch_pod_labels_guarded(namespace, name, uid=uid, tick=tick)

    run_id = "run1"
    _, pods = _build_fake_topology(run_id, 2, 4)
    kube = _FakeKubeClient({}, pods)
    mutation_client = _ThrottlingMutationClient(kube, run_id, throttles=2)
    events = scheduled_events(_churn_profile(events_per_second=1))
    monotonic_fn, async_sleep = _virtual_clock()
    progress = ChurnProgress(requested_events=len(events))

    await drive_live_churn(
        events,
        run_id=run_id,
        namespace_count=2,
        live_state=dict(pods),
        mutation_client=mutation_client,
        options=ReplayOptions(time_scale=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep),
        progress=progress,
        limits=LiveLimits(churn_concurrency=1, mutation_throttle_retries=5),
        profile=_tiny_live_profile(),
        recorder=BenchmarkRecorder(),
    )

    assert len(mutation_client.patch_arguments) == 3
    assert len(set(mutation_client.patch_arguments)) == 1
    assert progress.mutation_throttles == 2
    assert progress.completed == 1


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"Retry-After": "7"}, '{"details": {"retryAfterSeconds": 5}}', 7.0),
        ({}, '{"details": {"retryAfterSeconds": 5}}', 5.0),
    ],
)
async def test_mutation_client_preserves_server_retry_after_hint(
    headers: dict[str, str], body: str, expected: float
) -> None:
    class _ThrottledCoreV1:
        async def patch_namespaced_pod(self, *_args: object, **_kwargs: object) -> None:
            exc = k8s_client.exceptions.ApiException(status=429, reason="Too Many Requests")
            object.__setattr__(exc, "headers", CIMultiDictProxy(CIMultiDict(headers)))
            object.__setattr__(exc, "body", body.encode())
            raise exc

    client = live._KubeMutationClient(CONTEXT, RUN_ID)
    client._core_v1 = _ThrottledCoreV1()  # type: ignore[assignment]  # focused adapter fake

    with pytest.raises(ApiStatusError, match="API 429") as caught:
        await client.patch_pod_labels_guarded(
            "korvid-perf-aks186-0",
            "bench-0",
            uid="pod-uid-0",
            tick="1",
        )

    assert caught.value.body == body
    assert caught.value.retry_after_seconds == expected


async def test_mutation_client_connect_uses_refreshable_kube_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_refreshable = AsyncMock()
    api = MagicMock()
    api_factory = MagicMock(return_value=api)
    core_v1_factory = MagicMock()
    monkeypatch.setattr(live, "load_refreshable_kube_config", load_refreshable, raising=False)
    monkeypatch.setattr(k8s_config, "load_kube_config", AsyncMock())
    monkeypatch.setattr(k8s_client, "ApiClient", api_factory)
    monkeypatch.setattr(k8s_client, "CoreV1Api", core_v1_factory)

    client = live._KubeMutationClient(CONTEXT, RUN_ID)
    await client.connect()

    load_refreshable.assert_awaited_once()
    call = load_refreshable.await_args
    assert call is not None
    assert call.kwargs["context"] == CONTEXT
    assert call.kwargs["persist_config"] is False
    configuration = call.kwargs["client_configuration"]
    api_factory.assert_called_once_with(configuration)
    core_v1_factory.assert_called_once_with(api)


async def test_mutation_retry_respects_server_hint_with_an_explicit_delay_bound() -> None:
    class _ThrottleOnce:
        def __init__(self) -> None:
            self.calls = 0

        async def connect(self) -> None:
            pass

        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
        ) -> None:
            self.calls += 1
            if self.calls == 1:
                raise ApiStatusError(
                    429,
                    "Too Many Requests",
                    retry_after_seconds=10.0,
                )

        async def close(self) -> None:
            pass

    client = _ThrottleOnce()
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    await live._mutate_once(
        client,
        namespace="korvid-perf-run1-0",
        name="bench-0",
        uid="pod-uid-0",
        tick="1",
        progress=ChurnProgress(),
        limits=LiveLimits(
            mutation_throttle_retries=1,
            mutation_retry_base_delay_seconds=0.5,
            mutation_retry_max_delay_seconds=3.0,
        ),
        sleep=_sleep,
        now=lambda: 0.0,
    )

    assert client.calls == 2
    assert sleeps == [3.0]


async def test_mutation_retry_jitter_avoids_lockstep_workers_when_server_hint_dominates() -> None:
    class _ThrottleOnce:
        def __init__(self) -> None:
            self.calls = 0

        async def connect(self) -> None:
            pass

        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
        ) -> None:
            self.calls += 1
            if self.calls == 1:
                raise ApiStatusError(
                    429,
                    "Too Many Requests",
                    retry_after_seconds=1.0,
                )

        async def close(self) -> None:
            pass

    sleeps: list[list[float]] = [[], []]
    for index, name in enumerate(("bench-0", "bench-1")):

        async def _sleep(delay: float, target: list[float] = sleeps[index]) -> None:
            target.append(delay)

        await live._mutate_once(
            _ThrottleOnce(),
            namespace="korvid-perf-run1-0",
            name=name,
            uid=f"pod-uid-{index}",
            tick="1",
            progress=ChurnProgress(),
            limits=LiveLimits(
                mutation_throttle_retries=1,
                mutation_retry_base_delay_seconds=0.5,
                mutation_retry_max_delay_seconds=3.0,
            ),
            sleep=_sleep,
            now=lambda: 0.0,
        )

    assert 1.0 < sleeps[0][0] <= 1.5
    assert 1.0 < sleeps[1][0] <= 1.5
    assert sleeps[0] != sleeps[1]


async def test_drive_live_churn_gives_up_after_the_bounded_throttle_retries() -> None:
    class _AlwaysThrottling(_FakeMutationClient):
        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
        ) -> None:
            self.calls.append((namespace, name, tick))
            raise ApiStatusError(429, "Too Many Requests")

    run_id = "run1"
    _, pods = _build_fake_topology(run_id, 2, 4)
    mutation_client = _AlwaysThrottling(_FakeKubeClient({}, pods), run_id)
    events = scheduled_events(_churn_profile(events_per_second=1))
    monotonic_fn, async_sleep = _virtual_clock()
    progress = ChurnProgress(requested_events=len(events))

    with pytest.raises(ApiStatusError, match="API 429"):
        await drive_live_churn(
            events,
            run_id=run_id,
            namespace_count=2,
            live_state=dict(pods),
            mutation_client=mutation_client,
            options=ReplayOptions(
                time_scale=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep
            ),
            progress=progress,
            limits=LiveLimits(churn_concurrency=1, mutation_throttle_retries=2),
            profile=_tiny_live_profile(),
            recorder=BenchmarkRecorder(),
        )

    assert len(mutation_client.calls) == 3
    assert progress.mutation_throttles == 2


async def test_drive_live_churn_never_retries_a_failed_ownership_guard() -> None:
    """422 is a failed JSON-Patch `test` op: the target is not what the run
    validated. Retrying it - guarded or not - is exactly the behaviour the
    safety contract forbids."""

    class _GuardFailingMutationClient(_FakeMutationClient):
        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
        ) -> None:
            self.calls.append((namespace, name, tick))
            raise ApiStatusError(422, "test operation failed for guarded patch")

    run_id = "run1"
    _, pods = _build_fake_topology(run_id, 2, 4)
    mutation_client = _GuardFailingMutationClient(_FakeKubeClient({}, pods), run_id)
    events = scheduled_events(_churn_profile(events_per_second=1))
    monotonic_fn, async_sleep = _virtual_clock()
    progress = ChurnProgress(requested_events=len(events))

    with pytest.raises(ApiStatusError, match="test operation failed"):
        await drive_live_churn(
            events,
            run_id=run_id,
            namespace_count=2,
            live_state=dict(pods),
            mutation_client=mutation_client,
            options=ReplayOptions(
                time_scale=1.0, monotonic_fn=monotonic_fn, async_sleep=async_sleep
            ),
            progress=progress,
            limits=LiveLimits(churn_concurrency=1, mutation_throttle_retries=5),
            profile=_tiny_live_profile(),
            recorder=BenchmarkRecorder(),
        )

    assert len(mutation_client.calls) == 1
    assert progress.mutation_throttles == 0


async def test_drive_live_churn_bounds_every_mutation_attempt() -> None:
    """A stalled patch must not hang the run: every attempt is bounded."""

    class _HangingMutationClient(_FakeMutationClient):
        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
        ) -> None:
            self.calls.append((namespace, name, tick))
            await asyncio.sleep(30)

    run_id = "run1"
    _, pods = _build_fake_topology(run_id, 2, 4)
    mutation_client = _HangingMutationClient(_FakeKubeClient({}, pods), run_id)
    events = scheduled_events(_churn_profile(events_per_second=1))
    progress = ChurnProgress(requested_events=len(events))

    with pytest.raises(TimeoutError):
        await drive_live_churn(
            events,
            run_id=run_id,
            namespace_count=2,
            live_state=dict(pods),
            mutation_client=mutation_client,
            options=ReplayOptions(time_scale=1.0),
            progress=progress,
            limits=LiveLimits(churn_concurrency=1, mutation_timeout_seconds=0.05),
            profile=_tiny_live_profile(),
            recorder=BenchmarkRecorder(),
        )

    assert len(mutation_client.calls) == 1
    assert progress.completed == 0


async def test_run_live_replay_reports_requested_and_achieved_churn_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The design doc forbids presenting a requested rate as an achieved one:
    both must be reported, and they must be allowed to differ."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)
    options = sample_paced_schedule(monkeypatch).options()

    profile = dataclasses.replace(_tiny_live_profile(), duration_seconds=2)
    report = await run_live_replay(
        profile,
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.churn is not None
    # 4 events requested over 2 seconds (2 events/s requested). The test's
    # pacing seam lets one scheduled event through per completed cursor sample,
    # so the run consumes 3 of the 4 inter-event delays inside the measured
    # window and the achieved rate lands somewhere else entirely - which is the
    # point: the two figures are reported separately and may differ.
    assert report.churn.requested_events == 4
    assert report.churn.requested_events_per_second == 2.0
    assert report.churn.observed_events == 4
    assert report.churn.wall_seconds == 1.5
    assert report.churn.achieved_events_per_second == pytest.approx(4 / 1.5)
    assert report.churn.requested_events_per_second != report.churn.achieved_events_per_second
    assert report.churn.mutation_throttles == 0


async def test_run_live_replay_counts_mutation_throttles_outside_read_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harness write traffic must never be reported as application read-path
    API telemetry."""

    class _ThrottleOnceMutationClient(_FakeMutationClient):
        def __init__(
            self, kube: Callable[[], _FakeKubeClient] | _FakeKubeClient, run_id: str
        ) -> None:
            super().__init__(kube, run_id)
            self.throttled = False

        async def patch_pod_labels_guarded(
            self, namespace: str, name: str, *, uid: str, tick: str
        ) -> None:
            if not self.throttled:
                self.throttled = True
                raise ApiStatusError(429, "Too Many Requests")
            await super().patch_pod_labels_guarded(namespace, name, uid=uid, tick=tick)

    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(namespaces, pods, RUN_ID, app_clients=app_clients)
    deps = dataclasses.replace(
        deps,
        mutation_client_factory=lambda run_id: _ThrottleOnceMutationClient(
            lambda: app_clients[-1], run_id
        ),
    )
    options = sample_paced_schedule(monkeypatch).options()

    report = await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.churn is not None
    assert report.churn.mutation_throttles == 1
    assert report.api.throttles == 0
    assert report.api.operations.get("error", 0) == 0
    assert report.expected_digest == report.final_digest


# ---------------------------------------------------------------------------
# I2: cancellation cancels and drains every mutation task before teardown
# ---------------------------------------------------------------------------


async def test_run_live_replay_stops_mutating_when_the_outer_task_is_cancelled() -> None:
    """Real `Task.cancel()` on the whole run: the churn task (and every patch
    inside its task group) must be cancelled and awaited *before* the clients
    are closed, so no mutation outlives teardown."""

    class _PacedMutationClient(_FakeMutationClient):
        async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
            await asyncio.sleep(0.01)
            await super()._apply(namespace, name, uid=uid, tick=tick)

    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    app_clients: list[_FakeKubeClient] = []
    harness_clients: list[_FakeKubeClient] = []
    created: list[_PacedMutationClient] = []

    def mutation_factory(run_id: str) -> _PacedMutationClient:
        client = _PacedMutationClient(lambda: app_clients[-1], run_id)
        created.append(client)
        return client

    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        app_clients=app_clients,
        harness_clients=harness_clients,
        mutation_client_factory=mutation_factory,
    )
    # 300 events over 30 s of real wall time: the run is still churning when
    # the cancellation lands.
    profile = dataclasses.replace(
        _tiny_live_profile(), steady_events_per_second=10, duration_seconds=30
    )

    task = asyncio.create_task(
        run_live_replay(
            profile,
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )
    )
    for _ in range(2000):
        if created and len(created[0].calls) >= 2:
            break
        await asyncio.sleep(0.005)
    assert created, "the mutation client was never constructed"
    assert len(created[0].calls) >= 2, "churn never started"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    calls_at_cancellation = len(created[0].calls)
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert len(created[0].calls) == calls_at_cancellation
    assert created[0].closed
    assert harness_clients[0].closed
    assert app_clients[0].closed


# ---------------------------------------------------------------------------
# I3/I4: exact identity and ownership, before *and* after churn
# ---------------------------------------------------------------------------


async def test_verify_ownership_rejects_unexpected_pod_in_an_owned_namespace() -> None:
    """The application watch filters by namespace, so a foreign Pod in an owned
    namespace enters the benchmark store; the gate must name it instead of
    letting the initial-render wait time out with a generic message."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    stray_namespace = manifests.namespace_name(RUN_ID, 3)
    pods[(stray_namespace, "intruder")] = PodSummary(
        name="intruder",
        namespace=stray_namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node="node-9",
        labels=_labels(RUN_ID),
    )
    kube = _FakeKubeClient(namespaces, pods)

    with pytest.raises(
        ValueError, match=f"unexpected pods in owned namespaces: {stray_namespace}/intruder"
    ):
        await live._verify_ownership(kube, run_id=RUN_ID, namespace_count=20, object_count=1000)


async def test_read_and_validate_owned_pods_returns_the_validated_snapshot() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    kube = _FakeKubeClient(namespaces, pods)

    validated = await read_and_validate_owned_pods(kube, run_id=RUN_ID, expected=pods)

    assert sorted((pod.namespace, pod.name) for pod in validated) == sorted(pods)


@pytest.mark.parametrize(
    ("corrupt", "message"),
    [
        ("labels", "lost ownership labels"),
        ("uid", "uid changed"),
        ("missing", "missing"),
        ("extra", "unexpected pod"),
    ],
)
async def test_read_and_validate_owned_pods_rejects_identity_or_ownership_loss(
    corrupt: str, message: str
) -> None:
    """The ground-truth digest may only be computed from Pods that still are
    what the ownership gate validated."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    expected = dict(pods)
    key = live_object_identity(RUN_ID, 20, 42)
    if corrupt == "labels":
        pods[key] = dataclasses.replace(pods[key], labels=_labels("some-other-run"))
    elif corrupt == "uid":
        pods[key] = dataclasses.replace(pods[key], uid="recreated-uid")
    elif corrupt == "missing":
        del pods[key]
    else:
        namespace = key[0]
        pods[(namespace, "intruder")] = dataclasses.replace(
            pods[key], name="intruder", uid="intruder-uid"
        )
    kube = _FakeKubeClient(namespaces, pods)

    with pytest.raises(ValueError, match=f"post-churn ownership revalidation failed.*{message}"):
        await read_and_validate_owned_pods(kube, run_id=RUN_ID, expected=expected)


async def test_run_live_replay_rejects_a_pod_that_lost_ownership_during_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    harness_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(namespaces, pods, RUN_ID, harness_clients=harness_clients)
    victim = live_object_identity(RUN_ID, 20, 7)

    def strip_ownership_before_the_final_read(_namespace: str) -> None:
        if len(harness_clients[0].list_pods_calls) > 20:
            pods[victim] = dataclasses.replace(pods[victim], labels=())

    original_factory = deps.harness_kube_client_factory

    def harness_factory() -> _FakeKubeClient:
        client = original_factory()
        assert isinstance(client, _FakeKubeClient)
        client.on_list_pods = strip_ownership_before_the_final_read
        return client

    deps = dataclasses.replace(deps, harness_kube_client_factory=harness_factory)
    options = sample_paced_schedule(monkeypatch).options()

    with pytest.raises(ValueError, match="post-churn ownership revalidation failed"):
        await run_live_replay(
            _paced_live_profile(),
            options,
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


# ---------------------------------------------------------------------------
# I7 / M12 / M7 / profile revalidation
# ---------------------------------------------------------------------------


async def test_run_live_replay_names_watch_api_errors_in_a_wait_timeout() -> None:
    """`KorvidApp.on_mount` overwrites `WatchManager.on_error` with its own TUI
    notification, so a 403 that killed the watch is otherwise invisible: the
    operator would see only "not met within Ns" after paying for cluster setup."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(namespaces, pods, RUN_ID, app_clients=app_clients)
    original_factory = deps.kube_client_factory

    def failing_kube_factory(read_telemetry: ReadTelemetry) -> _FakeKubeClient:
        client = original_factory(read_telemetry)
        assert isinstance(client, _FakeKubeClient)
        client.watch_error = ApiStatusError(403, "Forbidden")
        return client

    deps = dataclasses.replace(deps, kube_client_factory=failing_kube_factory)

    with pytest.raises(WaitTimeout, match="status=403"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
            limits=LiveLimits(initial_render_timeout_seconds=0.3),
        )


async def test_run_live_replay_connects_the_mutation_client_before_the_app_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`load_kube_config` can invoke an exec credential plugin; that latency is
    paid once, up front, under an explicit bound - not inside the first
    measured mutation."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    mutation_clients: list[_FakeMutationClient] = []
    app_clients: list[_FakeKubeClient] = []
    connect_order: list[str] = []

    def mutation_factory(run_id: str) -> _FakeMutationClient:
        client = _FakeMutationClient(lambda: app_clients[-1], run_id)
        mutation_clients.append(client)
        return client

    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        app_clients=app_clients,
        mutation_client_factory=mutation_factory,
    )
    original_kube_factory = deps.kube_client_factory

    def recording_kube_factory(read_telemetry: ReadTelemetry) -> _FakeKubeClient:
        connect_order.append("app-client")
        return original_kube_factory(read_telemetry)  # type: ignore[return-value]

    deps = dataclasses.replace(deps, kube_client_factory=recording_kube_factory)
    options = sample_paced_schedule(monkeypatch).options()

    await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert mutation_clients[0].connect_calls == 1
    assert connect_order == ["app-client"]


async def test_run_live_replay_bounds_the_mutation_client_connect() -> None:
    class _HangingConnectMutationClient(_FakeMutationClient):
        async def connect(self) -> None:
            await asyncio.sleep(30)

    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    created: list[_HangingConnectMutationClient] = []

    def mutation_factory(run_id: str) -> _HangingConnectMutationClient:
        client = _HangingConnectMutationClient(_FakeKubeClient(namespaces, pods), run_id)
        created.append(client)
        return client

    deps = _happy_deps(namespaces, pods, RUN_ID, mutation_client_factory=mutation_factory)

    with pytest.raises(TimeoutError):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
            limits=LiveLimits(mutation_connect_timeout_seconds=0.05),
        )

    assert created[0].closed


async def test_run_live_replay_measures_input_latency_with_the_injected_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Churn uses the injected clock; input latency must use the same one, or a
    virtual-clock latency assertion measures nothing."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)
    options = sample_paced_schedule(monkeypatch, pairs=3).options()

    report = await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.input_latency.count == 6
    assert report.input_latency.maximum_seconds == 0.0


async def test_run_live_replay_takes_the_configured_number_of_cursor_sample_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live input figure must rest on the same configurable sample size as
    replay, and each `down`/`up` pair must return the cursor to its original
    row so the post-churn row and digest checks see an unmoved selection."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)
    options = sample_paced_schedule(monkeypatch, pairs=5).options()

    report = await run_live_replay(
        # Ten samples need a schedule with more than ten events left to run,
        # or churn legitimately finishes mid-probe and the run fails by name.
        dataclasses.replace(_paced_live_profile(), steady_events_per_second=20),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.input_latency.count == 10


async def test_run_live_replay_keeps_churn_running_throughout_input_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live probe must measure against an actively mutating cluster for
    its whole duration, never against a driver held after the first mutation.

    Pacing comes from injected seams, not wall time: the churn driver's
    inter-event sleep waits for a permit that each completed cursor sample
    releases, so guarded mutations keep landing while the probe runs.
    """
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    mutation_clients: list[_FakeMutationClient] = []
    deps = _happy_deps(namespaces, pods, RUN_ID, mutation_clients=mutation_clients)

    pairs = 3
    total_samples = 2 * pairs
    schedule = sample_paced_schedule(monkeypatch, pairs=pairs)

    snapshots: list[int] = []
    paced_measure = replay_mod.measure_cursor_input

    async def spy(*args: Any, **kwargs: Any) -> float:
        elapsed = await paced_measure(*args, **kwargs)
        snapshots.append(len(mutation_clients[0].calls))
        return float(elapsed)

    monkeypatch.setattr(replay_mod, "measure_cursor_input", spy)

    report = await run_live_replay(
        _paced_live_profile(),
        schedule.options(),
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert len(snapshots) == total_samples
    assert report.input_latency.count == total_samples
    # The decisive claim: churn is not gated to a single mutation for the
    # whole probe - more than one guarded patch lands while sampling runs.
    assert snapshots[-1] - snapshots[0] >= 2
    assert snapshots == sorted(snapshots)
    assert report.churn_started_before_input


async def test_run_live_replay_rejects_input_sampling_when_churn_finishes_early() -> None:
    """A cleanly completed churn task must fail the run if input sampling did
    not finish first; otherwise the reported percentile includes idle samples."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)
    monotonic_fn, async_sleep = _virtual_clock()
    options = ReplayOptions(
        time_scale=1.0,
        monotonic_fn=monotonic_fn,
        async_sleep=async_sleep,
        input_sample_pairs=3,
    )

    with pytest.raises(
        WaitTimeout,
        match="input sampling incomplete: churn finished before all 3 cursor sample pairs completed",
    ):
        await run_live_replay(
            dataclasses.replace(_tiny_live_profile(), steady_events_per_second=1),
            options,
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_a_non_positive_input_sample_pair_count() -> None:
    """Zero pairs would publish an input percentile computed from no samples.
    Rejected before any cluster identity, ownership, or mutation work."""
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=_never_called("active_context"),
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )

    with pytest.raises(ValueError, match="input_sample_pairs must be positive"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0, input_sample_pairs=0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


@pytest.mark.parametrize(
    "timeout",
    [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
    ids=["zero", "negative", "nan", "positive-infinity", "negative-infinity"],
)
async def test_run_live_replay_rejects_a_non_finite_or_non_positive_input_ack_timeout(
    timeout: float,
) -> None:
    """A live run reaches the cursor probe only after cluster identity,
    ownership and churn have already started, so an unbounded
    `asyncio.timeout(inf)`/`nan` would hang a seeded cluster indefinitely.
    Every external seam below fails the test if it is called, proving the
    rejection happens before any cluster or subprocess work."""
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=_never_called("active_context"),
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
        resolve_sha=_never_called("resolve_sha"),
    )

    with pytest.raises(ValueError, match="input_ack_timeout must be finite and positive"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0, input_ack_timeout=timeout),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_zero_event_profiles_before_external_work() -> None:
    """Zero-event live profiles must fail before identity, ownership, or SHA work."""
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=_never_called("active_context"),
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
        resolve_sha=_never_called("resolve_sha"),
    )

    with pytest.raises(
        ValueError,
        match="performance input sampling requires at least one scheduled churn event",
    ):
        await run_live_replay(
            dataclasses.replace(_tiny_live_profile(), steady_events_per_second=0),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_a_profile_whose_bursts_escape_its_duration() -> None:
    """A profile rewritten with `dataclasses.replace` (the CLI's `--duration`)
    never passes through `load_profile`; the invariants are re-checked here,
    before any cluster identity or ownership work."""
    profile = dataclasses.replace(
        _tiny_live_profile(),
        duration_seconds=2,
        bursts=(Burst(start_second=1, duration_seconds=30, events_per_second=100),),
    )
    deps = LiveDependencies(
        command_runner=_never_called("command_runner"),
        active_context=_never_called("active_context"),
        context_host=_never_called("context_host"),
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )

    with pytest.raises(ValueError, match="falls outside duration_seconds"):
        await run_live_replay(
            profile,
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_bounds_the_context_host_lookup() -> None:
    """A kubeconfig exec plugin invoked while resolving the API hostname can
    hang; the identity gate must bound that lookup, not block forever."""

    async def hanging_context_host(_context: str) -> str:
        await asyncio.sleep(30)
        return FQDN

    deps = LiveDependencies(
        command_runner=_ok_command_runner(),
        active_context=lambda: CONTEXT,
        context_host=hanging_context_host,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )

    with pytest.raises(TimeoutError):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
            limits=LiveLimits(read_connect_timeout_seconds=0.05),
        )


async def test_run_live_replay_bounds_the_harness_client_connect() -> None:
    """A stuck harness-client connect (exec credential plugin) must be bounded
    before the ownership gate can run."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)

    class _HangingConnectKube(_FakeKubeClient):
        async def connect(self, context: str | None = None) -> None:
            await asyncio.sleep(30)

    created: list[_HangingConnectKube] = []

    def harness_factory() -> _HangingConnectKube:
        client = _HangingConnectKube(namespaces, pods)
        created.append(client)
        return client

    deps = dataclasses.replace(
        _happy_deps(namespaces, pods, RUN_ID),
        harness_kube_client_factory=harness_factory,
    )

    with pytest.raises(TimeoutError):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
            limits=LiveLimits(read_connect_timeout_seconds=0.05),
        )

    assert created[0].closed


async def test_run_live_replay_bounds_the_app_client_connect() -> None:
    """A stuck application-path client connect must be bounded too."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)

    class _HangingConnectKube(_FakeKubeClient):
        async def connect(self, context: str | None = None) -> None:
            await asyncio.sleep(30)

    created: list[_HangingConnectKube] = []

    def app_factory(read_telemetry: ReadTelemetry) -> _HangingConnectKube:
        client = _HangingConnectKube(namespaces, pods)
        client.read_telemetry = read_telemetry
        created.append(client)
        return client

    deps = dataclasses.replace(
        _happy_deps(namespaces, pods, RUN_ID),
        kube_client_factory=app_factory,
    )

    with pytest.raises(TimeoutError):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
            limits=LiveLimits(read_connect_timeout_seconds=0.05),
        )

    assert created[0].closed


def _identity_deps(command_runner: Callable[[Any], Awaitable[CommandResult]]) -> LiveDependencies:
    async def context_host(_context: str) -> str:
        return FQDN

    return LiveDependencies(
        command_runner=command_runner,
        active_context=lambda: CONTEXT,
        context_host=context_host,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )


async def test_verify_cluster_identity_uses_fixed_resource_group_and_cluster_name_lookup() -> None:
    captured_args: list[list[str]] = []

    async def recording_command_runner(args: list[str]) -> CommandResult:
        captured_args.append(args)
        return await _ok_command_runner()(args)

    await live._verify_cluster_identity(
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        deps=_identity_deps(recording_command_runner),
        limits=LiveLimits(),
    )

    assert captured_args == [
        [
            "az",
            "aks",
            "show",
            "--resource-group",
            RESOURCE_GROUP,
            "--name",
            CLUSTER_NAME,
            "-o",
            "json",
        ]
    ]


async def test_run_live_replay_bounds_the_az_aks_show_lookup() -> None:
    """The first external `az aks show` call in the identity gate can hang on a
    stuck credential/exec plugin; it must be bounded by the read/connect timeout,
    not block the fail-closed gate forever."""

    async def hanging_command_runner(_args: Any) -> CommandResult:
        await asyncio.sleep(30)
        return CommandResult(0, "{}", "")

    deps = _identity_deps(hanging_command_runner)

    with pytest.raises(TimeoutError):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
            limits=LiveLimits(read_connect_timeout_seconds=0.05),
        )


async def test_run_live_replay_rejects_wrong_resource_group_before_mutation() -> None:
    deps = _identity_deps(_ok_command_runner(resource_group="rg-production"))
    with pytest.raises(ValueError, match="resource group"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_wrong_cluster_name_before_mutation() -> None:
    deps = _identity_deps(_ok_command_runner(name="aks-production"))
    with pytest.raises(ValueError, match="cluster name"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_missing_required_tag_before_mutation() -> None:
    deps = _identity_deps(_ok_command_runner(tags={"purpose": "korvid-contract-testing"}))
    with pytest.raises(ValueError, match="required tag"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_wrong_required_tag_value_before_mutation() -> None:
    deps = _identity_deps(
        _ok_command_runner(tags={"purpose": "korvid-contract-testing", "production-use": "allowed"})
    )
    with pytest.raises(ValueError, match="required tag"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_non_running_pod_before_churn() -> None:
    """The ownership/preflight gate requires exactly 1,000 Running, Ready owned
    Pods: a labelled but non-Running Pod must be reported and reject the run
    before any mutation, since the later check only counts table rows."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    victim = (manifests.namespace_name(RUN_ID, 0), manifests.pod_name(20, 0))
    pods[victim] = dataclasses.replace(pods[victim], phase="Pending")

    deps = LiveDependencies(
        command_runner=_ok_command_runner(),
        active_context=lambda: CONTEXT,
        context_host=_context_host_ok,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=lambda: _FakeKubeClient(namespaces, pods),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )

    with pytest.raises(ValueError, match="not Running or not Ready"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_rejects_not_ready_pod_before_churn() -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    victim = (manifests.namespace_name(RUN_ID, 1), manifests.pod_name(20, 1))
    pods[victim] = dataclasses.replace(pods[victim], ready="0/1")

    deps = LiveDependencies(
        command_runner=_ok_command_runner(),
        active_context=lambda: CONTEXT,
        context_host=_context_host_ok,
        kube_client_factory=_never_called("kube_client_factory"),
        harness_kube_client_factory=lambda: _FakeKubeClient(namespaces, pods),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )

    with pytest.raises(ValueError, match="not Running or not Ready"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_fails_closed_when_sha_cannot_be_resolved() -> None:
    """A live evidence run must not publish an untraceable artifact: if no
    immutable korvid SHA can be resolved, the run fails closed before any
    client is constructed."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = dataclasses.replace(
        _happy_deps(namespaces, pods, RUN_ID),
        resolve_sha=lambda: None,
        harness_kube_client_factory=_never_called("harness_kube_client_factory"),
        kube_client_factory=_never_called("kube_client_factory"),
        mutation_client_factory=_never_called("mutation_client_factory"),
    )

    with pytest.raises(ValueError, match="immutable korvid SHA"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(time_scale=1.0),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )


async def test_run_live_replay_builds_a_live_specific_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retained live evidence must record which cluster matrix was qualified:
    the verified context/ARM id plus Kubernetes server version and node-pool
    metadata, resolved through bounded seams and persisted in the manifest."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)
    options = sample_paced_schedule(monkeypatch).options(sample_interval=1.0)

    report = await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    manifest = report.manifest
    assert manifest.korvid_sha == _FIXED_SHA
    assert manifest.context == CONTEXT
    assert manifest.cluster_id == CLUSTER_ID
    assert manifest.kubernetes_version == "1.30.4"
    pool_names = {pool.name for pool in manifest.node_pools}
    assert "perftest" in pool_names
    perftest = next(pool for pool in manifest.node_pools if pool.name == "perftest")
    assert perftest.node_count == 5
    assert perftest.kubernetes_version == "1.30.4"

    # Persisted in both machine-readable and human-readable output.
    from tests.performance.metrics import render_markdown, report_payload

    payload = report_payload(_report_as_benchmark(report))
    manifest_payload = payload["manifest"]
    assert isinstance(manifest_payload, dict)
    assert manifest_payload["context"] == CONTEXT
    assert manifest_payload["cluster_id"] == CLUSTER_ID
    assert manifest_payload["kubernetes_version"] == "1.30.4"
    assert manifest_payload["node_pools"] == [
        {"name": "perftest", "kubernetes_version": "1.30.4", "node_count": 5},
        {"name": "system", "kubernetes_version": "1.30.4", "node_count": 1},
    ]

    markdown = render_markdown(_report_as_benchmark(report))
    assert f"- Context: `{CONTEXT}`" in markdown
    assert "- Kubernetes version: `1.30.4`" in markdown
    assert "perftest" in markdown


async def test_run_live_replay_exercises_ui_at_scale_scenarios_during_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passing live run must provide UI-at-scale evidence: filter, sort,
    namespace switch, split pane, describe, and multi-log are driven through the
    real Textual pilot during active churn and their outcomes/latencies are
    recorded, without weakening the digest/ownership safety guarantees."""
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)
    options = sample_paced_schedule(monkeypatch).options(sample_interval=1.0)

    report = await run_live_replay(
        _paced_live_profile(),
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    # Safety invariants still hold.
    assert report.expected_digest == report.final_digest
    assert report.dropped_updates == 0

    recorded = {scenario.name for scenario in report.ui_scenarios}
    assert recorded == {"filter", "sort", "namespace_switch", "split_pane", "describe", "multi_log"}
    for scenario in report.ui_scenarios:
        assert scenario.ok, f"scenario {scenario.name} did not complete"
        assert scenario.latency_seconds >= 0.0


class _InertPilot:
    """A pilot whose key presses reach an app that never changes state.

    `pilot.press` returning without raising says nothing about whether the
    binding did anything: in the live wiring `describe` and `multi_log` bail
    out with an "unavailable" warning when no manifest/log provider is wired,
    and the namespace toggle is a no-op while `config.namespace` already is
    `ALL_NAMESPACES`. None of those raise.
    """

    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)

    async def pause(self, delay: float | None = None) -> None:
        return None


class _InertApp:
    """Live app state frozen at its post-LIST values: nothing a scenario is
    supposed to change ever changes."""

    def __init__(self) -> None:
        self.filter_pattern = ""
        self._sorts: dict[str, object] = {}
        self.current_scope = ALL_NAMESPACES
        self.screen = object()
        self._panes = [object()]

        class _Pane:
            display = False

        self._log_pane = _Pane()


async def test_ui_scenarios_are_not_marked_ok_when_the_app_state_never_changes() -> None:
    """A scenario must assert the observable state it claims to exercise.

    Without this the live report claims UI-at-scale evidence for scenarios
    that silently did nothing, and (since the CLI folds scenario outcomes into
    the exit status) a qualification would "pass" on that empty evidence.
    """
    recorder = BenchmarkRecorder()
    ticks = iter(float(i) for i in range(10_000))

    await live.drive_ui_scenarios(
        _InertPilot(),
        recorder,
        now=lambda: next(ticks),
        app=_InertApp(),
        scoped_namespace=f"korvid-perf-{RUN_ID}-0",
    )

    results = recorder._ui_scenarios
    assert results, "scenarios must still be recorded, not skipped"
    assert [scenario.name for scenario in results if scenario.ok] == []


async def test_run_live_replay_times_the_post_burst_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live churn driver must mark each burst boundary.

    Without it `post_burst_drain_seconds` stays empty and
    `max_post_burst_drain_seconds` is `None`, so the published <=3-second live
    burst-drain budget cannot be evaluated at all — the live reports simply
    print "n/a" where the evidence should be.
    """
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)
    options = sample_paced_schedule(monkeypatch).options(sample_interval=1.0)
    profile = dataclasses.replace(
        _tiny_live_profile(),
        steady_events_per_second=2,
        duration_seconds=4,
        bursts=(Burst(start_second=1, duration_seconds=1, events_per_second=4),),
    )

    report = await run_live_replay(
        profile,
        options,
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.phases.post_burst_drain_seconds
    assert report.phases.max_post_burst_drain_seconds is not None
    assert report.phases.max_post_burst_drain_seconds >= 0.0


def _pod_summary(namespace: str, name: str, **kwargs: Any) -> PodSummary:
    defaults: dict[str, Any] = {
        "namespace": namespace,
        "name": name,
        "ready": "1/1",
        "phase": "Running",
        "restarts": 0,
        "node": "node-a",
        "created": "",
        "uid": "uid-1",
        "labels": {},
    }
    defaults.update(kwargs)
    return PodSummary(**{k: v for k, v in defaults.items() if k in _POD_SUMMARY_FIELDS})


_POD_SUMMARY_FIELDS = {field.name for field in dataclasses.fields(PodSummary)}


class _StubRow:
    def __init__(self, value: str) -> None:
        self.key = SimpleNamespace(value=value)


class _StubTable:
    """Minimal stand-in exposing exactly what the rendered-row check reads."""

    def __init__(self, rows: dict[str, list[object]]) -> None:
        self._rows = rows
        self.row_count = len(rows)
        self.ordered_rows = [_StubRow(key) for key in rows]

    def get_row(self, row_key: object) -> list[object]:
        key = row_key.value if hasattr(row_key, "value") else row_key
        return self._rows[str(key)]


def test_rendered_rows_check_rejects_a_stale_cell() -> None:
    """The published digest criterion compares a store digest with a store
    digest, so 1,000 stale cells satisfy it. The rendered table must be checked
    against the store independently — especially now that the in-place diff
    updates cells from its own cached record of what it last wrote.
    """
    pods = [
        _pod_summary("ns-a", "bench-0", phase="Running"),
        _pod_summary("ns-a", "bench-1", phase="CrashLoopBackOff"),
    ]
    table = _StubTable(
        {
            "ns-a/bench-0": ["ns-a", "bench-0", "1/1", "Running", "0", "node-a"],
            # Stale: the store says CrashLoopBackOff, the table still shows Running.
            "ns-a/bench-1": ["ns-a", "bench-1", "1/1", "Running", "0", "node-a"],
        }
    )

    with pytest.raises(ValueError, match="ns-a/bench-1"):
        replay_mod.check_rendered_rows(table, pods)


def test_rendered_rows_check_accepts_a_table_that_matches_the_store() -> None:
    pods = [
        _pod_summary("ns-a", "bench-0", phase="Running"),
        _pod_summary("ns-a", "bench-1", phase="CrashLoopBackOff", ready="0/1", restarts=7),
    ]
    table = _StubTable(
        {
            "ns-a/bench-0": ["ns-a", "bench-0", "1/1", "Running", "0", "node-a"],
            "ns-a/bench-1": ["ns-a", "bench-1", "0/1", "CrashLoopBackOff", "7", "node-a"],
        }
    )

    replay_mod.check_rendered_rows(table, pods)

    assert table.row_count == 2
