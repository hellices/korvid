"""Live benchmark tests that retain only product-facing replay behavior."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.telemetry import ReadTelemetry, ReadTelemetryEvent
from tests.performance import live, manifests
from tests.performance import replay as replay_mod
from tests.performance.live import (
    CommandResult,
    LiveDependencies,
    LiveLimits,
    live_object_identity,
    run_live_replay,
)
from tests.performance.metrics import (
    BenchmarkRecorder,
    UpdateLatencyKind,
    render_markdown,
    report_payload,
)
from tests.performance.pacing import sample_paced_schedule
from tests.performance.profile import Burst, WorkloadProfile
from tests.performance.replay import ReplayOptions
from tests.performance.workload import summary_digest
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
_FIXED_SHA = "1234567890abcdef1234567890abcdef12345678"


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
        self.list_pods_calls: list[str] = []
        self.watch_pods_calls = 0
        self.watch_namespaces: list[str | None] = []
        self.watch_finished = False
        self.on_list_pods: Callable[[str], None] | None = None
        self.watch_error: ApiStatusError | None = None

    async def connect(self, context: str | None = None) -> None:
        self.connect_context = context

    async def close(self) -> None:
        self.closed = True

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        assert meta.kind == "Pod"
        return {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": name, "namespace": namespace},
            "status": {"phase": "Running"},
        }

    async def stream_logs(self, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        del args, kwargs
        yield LogLine(pod="bench", container="app", text="benchmark log line")
        await asyncio.Event().wait()

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        assert meta.kind == "Namespace"
        assert namespace is None
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
        if namespace is not None:
            return [pod for pod in self.pods.values() if pod.namespace == namespace]
        return [*self.pods.values(), *self.distractor_pods]

    async def watch_pods(self, namespace: str | None) -> AsyncIterator[tuple[str, PodSummary]]:
        self.watch_pods_calls += 1
        self.watch_namespaces.append(namespace)
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
    def __init__(
        self,
        kube: _FakeKubeClient | Callable[[], _FakeKubeClient],
        run_id: str,
    ) -> None:
        self._resolve_kube: Callable[[], _FakeKubeClient] = (
            kube if callable(kube) else (lambda: kube)
        )
        self._run_id = run_id
        self.calls: list[tuple[str, str, str]] = []
        self.closed = False

    async def connect(self) -> None:
        return None

    async def patch_pod_labels_guarded(
        self, namespace: str, name: str, *, uid: str, tick: str
    ) -> None:
        self.calls.append((namespace, name, tick))
        await self._apply(namespace, name, uid=uid, tick=tick)

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
    async def context_host(context: str) -> str:
        assert context == CONTEXT
        return FQDN

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
    return dataclasses.replace(_tiny_live_profile(seed=seed), steady_events_per_second=10)


class _WithheldWatchMutationClient(_FakeMutationClient):
    def __init__(
        self,
        kube: Callable[[], _FakeKubeClient],
        run_id: str,
        *,
        release: asyncio.Event,
    ) -> None:
        super().__init__(kube, run_id)
        self._release = release

    async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
        await self._release.wait()
        await super()._apply(namespace, name, uid=uid, tick=tick)


async def _poll_until(
    predicate: Callable[[], bool],
    run: asyncio.Task[Any],
    *,
    label: str,
    attempts: int = 1000,
) -> None:
    for _ in range(attempts):
        if predicate():
            return
        if run.done():
            await run
            raise AssertionError(f"the live run finished before {label}")
        await asyncio.sleep(0.01)
    raise AssertionError(f"{label} never happened")


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


async def test_run_live_replay_records_at_receipt_when_the_patch_ack_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AckLagsMutationClient(_FakeMutationClient):
        async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
            await super()._apply(namespace, name, uid=uid, tick=tick)
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
    assert report.update_latency.count == report.churn.observed_events
    assert report.expected_digest == report.final_digest


async def test_run_live_replay_publishes_a_metadata_only_update_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    deps = _happy_deps(namespaces, pods, RUN_ID)

    report = await run_live_replay(
        _paced_live_profile(),
        sample_paced_schedule(monkeypatch).options(),
        context=CONTEXT,
        expected_cluster_id=CLUSTER_ID,
        run_id=RUN_ID,
        deps=deps,
    )

    assert report.update_latency_kind is UpdateLatencyKind.WATCH_TO_DIFF_COMPLETION
    assert report.update_latency.count > 0

    latency = report_payload(_report_as_benchmark(report))["latency"]
    assert isinstance(latency, dict)
    assert latency["update_latency_kind"] == "watch_to_diff_completion"
    assert latency["event_to_render"] is None
    watch_to_diff = latency["watch_to_diff_completion"]
    assert isinstance(watch_to_diff, dict)
    assert watch_to_diff["count"] == report.update_latency.count

    markdown = render_markdown(_report_as_benchmark(report))
    assert "- Watch receipt to diff completion p95:" in markdown
    assert "Event to render" not in markdown


async def test_run_live_replay_does_not_sample_input_before_the_first_watch_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    app_clients: list[_FakeKubeClient] = []
    mutation_clients: list[_WithheldWatchMutationClient] = []
    release = asyncio.Event()

    def mutation_factory(run_id_arg: str) -> _WithheldWatchMutationClient:
        client = _WithheldWatchMutationClient(lambda: app_clients[-1], run_id_arg, release=release)
        mutation_clients.append(client)
        return client

    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        app_clients=app_clients,
        mutation_client_factory=mutation_factory,
    )

    pairs = 3
    schedule = sample_paced_schedule(monkeypatch, pairs=pairs)

    backlog_at_sampling: list[int] = []
    real_sample = replay_mod.sample_cursor_input

    async def spy(pilot: Any, table: Any, recorder: BenchmarkRecorder, **kwargs: Any) -> None:
        backlog_at_sampling.append(recorder.phases().max_backlog_depth)
        await real_sample(pilot, table, recorder, **kwargs)

    monkeypatch.setattr(live, "sample_cursor_input", spy)

    run = asyncio.create_task(
        run_live_replay(
            _paced_live_profile(),
            schedule.options(),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
        )
    )
    try:
        await _poll_until(
            lambda: bool(mutation_clients) and bool(mutation_clients[0].calls),
            run,
            label="the first guarded patch was dispatched",
        )
        for _ in range(6):
            await asyncio.sleep(0.05)
        assert backlog_at_sampling == []
    finally:
        release.set()

    report = await run

    assert backlog_at_sampling != []
    assert backlog_at_sampling[0] >= 1
    assert report.input_latency.count == 2 * pairs
    assert report.churn_started_before_input


async def test_run_live_replay_fails_when_churn_completes_without_a_watch_receipt() -> None:
    class _SilentMutationClient(_FakeMutationClient):
        async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
            return None

    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(
        namespaces,
        pods,
        RUN_ID,
        app_clients=app_clients,
        mutation_client_factory=lambda run_id_arg: _SilentMutationClient(
            lambda: app_clients[-1], run_id_arg
        ),
    )
    monotonic_fn, async_sleep = _virtual_clock()

    with pytest.raises(WaitTimeout, match="input sampling incomplete"):
        await run_live_replay(
            _tiny_live_profile(),
            ReplayOptions(
                time_scale=1.0,
                monotonic_fn=monotonic_fn,
                async_sleep=async_sleep,
                input_sample_pairs=2,
            ),
            context=CONTEXT,
            expected_cluster_id=CLUSTER_ID,
            run_id=RUN_ID,
            deps=deps,
            limits=LiveLimits(initial_render_timeout_seconds=30.0),
        )


async def test_run_live_replay_reads_ground_truth_while_the_watch_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespaces, pods = _build_fake_topology(RUN_ID, 20, 1000)
    harness_clients: list[_FakeKubeClient] = []
    app_clients: list[_FakeKubeClient] = []
    deps = _happy_deps(
        namespaces, pods, RUN_ID, harness_clients=harness_clients, app_clients=app_clients
    )
    options = sample_paced_schedule(monkeypatch).options()

    watch_live_during_reads: list[bool] = []

    def spy(_namespace: str) -> None:
        if len(harness_clients[0].list_pods_calls) > 20:
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
    class _LaggingWatchMutationClient(_FakeMutationClient):
        async def _apply(self, namespace: str, name: str, *, uid: str, tick: str) -> None:
            kube = self._resolve_kube()
            before = kube.events.qsize()
            await super()._apply(namespace, name, uid=uid, tick=tick)
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


async def test_run_live_replay_names_watch_api_errors_in_a_wait_timeout() -> None:
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


async def test_run_live_replay_keeps_churn_running_throughout_input_sampling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert snapshots[-1] - snapshots[0] >= 2
    assert snapshots == sorted(snapshots)
    assert report.churn_started_before_input


async def test_run_live_replay_exercises_ui_at_scale_scenarios_during_churn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    assert report.expected_digest == report.final_digest
    assert report.dropped_updates == 0

    recorded = {scenario.name for scenario in report.ui_scenarios}
    assert recorded == {"filter", "sort", "namespace_switch", "split_pane", "describe", "multi_log"}
    for scenario in report.ui_scenarios:
        assert scenario.ok, f"scenario {scenario.name} did not complete"
        assert scenario.latency_seconds >= 0.0


async def test_run_live_replay_times_the_post_burst_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
