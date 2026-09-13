import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.errors import KubeClientError
from korvid.k8s.pulse import PulsePage, PulseReader, PulseSource
from tests.ui.test_session_timeline_controller import FakeUiSurface
from tests.ui.test_write_coordinator import FakeContext, FakeView

NOW = datetime(2026, 9, 13, tzinfo=UTC)


class RunningUi(FakeUiSurface):
    def __init__(self) -> None:
        super().__init__()
        self.tasks: dict[str, list[asyncio.Task[Any]]] = {}
        self.depth = 1

    def run_worker(
        self,
        work: Any,
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Any:
        task = asyncio.create_task(work)
        self.tasks.setdefault(group, []).append(task)
        return task

    async def cancel_workers(self, group: str) -> None:
        self.cancelled_groups.append(group)
        tasks = self.tasks.get(group, [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def screen_depth(self) -> int:
        return self.depth

    async def settled(self) -> None:
        await asyncio.gather(
            *(task for task in self.tasks.get("pulse-refresh", []) if not task.cancelled())
        )


class Reader(PulseReader):
    def __init__(self) -> None:
        self.scopes: list[str | None] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def read_pulse_page(
        self,
        source: PulseSource,
        namespace: str | None,
        continuation: str,
        limit: int,
        max_bytes: int,
    ) -> PulsePage:
        self.scopes.append(namespace)
        self.entered.set()
        if self.block:
            await self.release.wait()
        return PulsePage(())


class Harness:
    def __init__(self) -> None:
        model_module = importlib.import_module("korvid.core.pulse")
        collector_module = importlib.import_module("korvid.core.pulse_collector")
        module = importlib.import_module("korvid.ui.pulse_controller")
        self.ui = RunningUi()
        self.view = FakeView(aliases={"pods": PODS_META})
        self.context = FakeContext()
        self.reader = Reader()
        self.model = model_module.PulseModel()
        self.presented: list[Any] = []
        self.navigate = AsyncMock()
        self.manifest = AsyncMock(return_value={"metadata": {"uid": "uid-1"}})
        self.now = NOW
        self.monotonic = 0.0
        self.origin = ("pane-0", 0)
        self.controller = module.PulseController(
            ui=self.ui,
            view=self.view,
            context=self.context,
            model=self.model,
            collector=collector_module.PulseCollector(
                self.reader,
                (PulseSource("pods", "", "v1", "pods"),),
                clock=lambda: self.now,
            ),
            present=self.presented.append,
            capture_navigation_origin=lambda: self.origin,
            navigate=self.navigate,
            get_manifest=self.manifest,
            clock=lambda: self.now,
            monotonic=lambda: self.monotonic,
        )

    def goto(self, *, uid: str | None = "uid-1", kind: str = "Pod") -> Any:
        records = importlib.import_module("korvid.core.pulse")
        widget = importlib.import_module("korvid.ui.widgets.pulse")
        return widget.PulseGoto(
            0, "default", records.PulseTarget("", kind, "default", "web-1", uid)
        )


async def test_start_does_not_block_and_overlapping_refreshes_coalesce() -> None:
    harness = Harness()
    harness.reader.block = True
    harness.controller.start()
    try:
        await harness.reader.entered.wait()
        for _index in range(20):
            harness.controller.request_refresh()
        harness.reader.release.set()
        await harness.ui.settled()
        assert harness.reader.scopes == ["default", "default"]
        assert len(harness.ui.tasks["pulse-refresh"]) == 1
    finally:
        await harness.controller.stop()


async def test_old_scope_results_never_publish_into_new_scope() -> None:
    harness = Harness()
    harness.reader.block = True
    harness.controller.start()
    try:
        await harness.reader.entered.wait()
        harness.view.scope = "other"
        harness.controller.sync_scope()
        assert harness.controller.snapshot().scope == "other"
        assert all(
            coverage.state == "loading" for coverage in harness.controller.snapshot().coverage
        )
        harness.reader.release.set()
        await harness.ui.settled()
        assert harness.reader.scopes == ["default", "other"]
        assert harness.controller.snapshot().scope == "other"
    finally:
        await harness.controller.stop()


async def test_suspension_cancels_reads_before_context_swap() -> None:
    harness = Harness()
    harness.reader.block = True
    harness.controller.start()
    await harness.reader.entered.wait()
    await harness.controller.suspend()
    harness.context.value = 1
    harness.context.is_switching = True
    harness.controller.request_refresh()
    assert len(harness.reader.scopes) == 1
    harness.context.is_switching = False
    harness.reader.block = False
    harness.controller.resume()
    harness.controller.tick()
    await harness.ui.settled()
    assert harness.controller.snapshot().epoch == 1
    assert len(harness.reader.scopes) == 2
    await harness.controller.stop()


async def test_event_burst_is_coalesced_without_reads_or_popups() -> None:
    harness = Harness()
    harness.controller.start()
    try:
        await harness.ui.settled()
        baseline = len(harness.presented)
        for index in range(200):
            harness.controller.record_warning(
                {
                    "type": "Warning",
                    "reason": "Uncataloged",
                    "metadata": {"uid": f"event-{index}", "namespace": "default"},
                    "lastTimestamp": NOW.isoformat(),
                    "involvedObject": {
                        "kind": "Pod",
                        "namespace": "default",
                        "name": "web-1",
                        "uid": "uid-1",
                    },
                },
                0,
            )
            harness.controller.tick()
        assert len(harness.reader.scopes) == 1
        assert len(harness.presented) == baseline
        assert harness.ui.notifications == []
        harness.monotonic = 0.25
        harness.controller.tick()
        assert len(harness.presented) == baseline + 1
        assert len(harness.presented[-1].recent) <= 100
    finally:
        await harness.controller.stop()


async def test_periodic_refresh_uses_fifteen_second_cadence() -> None:
    harness = Harness()
    harness.controller.start()
    try:
        await harness.ui.settled()
        harness.monotonic = 14.99
        harness.controller.tick()
        assert len(harness.reader.scopes) == 1
        harness.monotonic = 15.0
        harness.now += timedelta(seconds=15)
        harness.controller.tick()
        await harness.ui.settled()
        assert len(harness.reader.scopes) == 2
    finally:
        await harness.controller.stop()


async def test_pulse_does_not_open_over_an_approval_screen() -> None:
    harness = Harness()
    harness.ui.depth = 2
    harness.controller.open_detail()
    assert harness.ui.screens == []
    assert harness.navigate.await_count == 0


@pytest.mark.parametrize(("uid", "kind"), [(None, "Pod"), ("uid-1", "Unregistered")])
async def test_unresolvable_identity_is_not_navigable(uid: str | None, kind: str) -> None:
    harness = Harness()
    await harness.controller.navigate_to(harness.goto(uid=uid, kind=kind))
    assert harness.navigate.await_count == 0
    assert harness.ui.notifications


async def test_recreated_uid_cannot_navigate_by_same_name() -> None:
    harness = Harness()
    harness.manifest.return_value = {"metadata": {"uid": "replacement"}}
    await harness.controller.navigate_to(harness.goto())
    assert harness.navigate.await_count == 0
    assert "changed" in harness.ui.notifications[-1].message


async def test_context_crossed_during_identity_read_aborts_navigation() -> None:
    harness = Harness()

    async def changed(*_args: Any) -> dict[str, Any]:
        harness.context.value = 1
        return {"metadata": {"uid": "uid-1"}}

    harness.manifest.side_effect = changed
    await harness.controller.navigate_to(harness.goto())
    assert harness.navigate.await_count == 0


async def test_navigation_retains_epoch_and_uid() -> None:
    harness = Harness()
    await harness.controller.navigate_to(harness.goto())
    harness.navigate.assert_awaited_once_with(
        "pods", "default", "web-1", 0, "uid-1", harness.origin
    )
    assert harness.manifest.await_count == 1


@pytest.mark.parametrize("change", ["scope", "kind", "unchanged"])
@pytest.mark.parametrize("read_fails", [False, True])
async def test_pending_identity_read_revalidates_navigation_generation(
    change: str, read_fails: bool
) -> None:
    harness = Harness()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def manifest(*_args: Any) -> dict[str, Any]:
        entered.set()
        await release.wait()
        if read_fails:
            raise KubeClientError("read failed")
        return {"metadata": {"uid": "uid-1"}}

    harness.manifest.side_effect = manifest
    harness.controller.start()
    navigation = asyncio.create_task(harness.controller.navigate_to(harness.goto()))
    try:
        await entered.wait()
        if change == "scope":
            for scope in ("other", "default"):
                harness.view.scope = scope
                harness.controller.sync_scope()
        elif change == "kind":
            for kind in ("deployments", "pods"):
                harness.view.kind = kind
                harness.controller.sync_scope()
        else:
            harness.controller.sync_scope()
        release.set()
        await navigation
        assert harness.view.current_scope() == "default"
        assert harness.view.current_kind() == "pods"
        harness.manifest.assert_awaited_once_with("pods", "default", "web-1")
        assert harness.navigate.await_count == int(change == "unchanged" and not read_fails)
        if change == "unchanged" and read_fails:
            assert "cannot be verified" in harness.ui.notifications[-1].message
        else:
            assert harness.ui.notifications == []
    finally:
        release.set()
        await navigation
        await harness.controller.stop()


async def test_namespace_change_preserves_same_context_watch_failure() -> None:
    harness = Harness()
    harness.controller.start()
    try:
        await harness.ui.settled()
        harness.controller.warning_status(0, "forbidden", "Live Warning feed denied")
        harness.view.scope = "other"
        harness.controller.sync_scope()
        coverage = {entry.source: entry for entry in harness.controller.snapshot().coverage}
        assert coverage["warning-watch"].state == "forbidden"
        await harness.ui.settled()
    finally:
        await harness.controller.stop()


async def test_old_epoch_warning_is_rejected_before_the_next_tick() -> None:
    harness = Harness()
    harness.controller.start()
    try:
        await harness.ui.settled()
        harness.context.value = 1
        harness.controller.record_warning(
            {
                "type": "Warning",
                "reason": "OldContext",
                "metadata": {"uid": "event-old", "namespace": "default"},
                "lastTimestamp": NOW.isoformat(),
                "involvedObject": {
                    "kind": "Pod",
                    "name": "web-1",
                    "namespace": "default",
                    "uid": "uid-1",
                },
            },
            0,
        )
        assert harness.controller.snapshot().recent == ()
    finally:
        await harness.controller.stop()


async def test_unexpected_old_scope_failure_does_not_publish_or_lose_pending_refresh() -> None:
    harness = Harness()
    entered = asyncio.Event()
    release = asyncio.Event()
    observed: list[str | None] = []
    original = harness.controller._collector.collect

    async def collect(scope: str | None) -> Any:
        observed.append(scope)
        if scope == "default":
            entered.set()
            await release.wait()
            raise RuntimeError("old-context-secret")
        return await original(scope)

    harness.controller._collector.collect = collect
    harness.controller.start()
    try:
        await entered.wait()
        harness.view.scope = "other"
        harness.controller.sync_scope()
        release.set()
        await harness.ui.settled()
        assert observed == ["default", "other"]
        assert not any(entry.state == "failed" for entry in harness.controller.snapshot().coverage)
        assert "old-context-secret" not in str(harness.controller.snapshot())
    finally:
        await harness.controller.stop()


async def test_successful_retry_clears_unexpected_collection_failure() -> None:
    harness = Harness()
    original = harness.controller._collector.collect
    harness.controller._collector.collect = AsyncMock(side_effect=RuntimeError("secret"))
    harness.controller.start()
    try:
        await harness.ui.settled()
        assert any(
            entry.source == "collector" and entry.state == "failed"
            for entry in harness.controller.snapshot().coverage
        )
        harness.controller._collector.collect = original
        harness.controller.request_refresh()
        await harness.ui.settled()
        assert not any(entry.state == "failed" for entry in harness.controller.snapshot().coverage)
    finally:
        await harness.controller.stop()


@pytest.mark.parametrize(("kind", "namespace"), [("Pod", ""), ("Node", "default")])
async def test_discovery_scope_mismatch_cannot_be_used_for_navigation(
    kind: str, namespace: str
) -> None:
    harness = Harness()
    harness.view._aliases["nodes"] = ResourceMeta("Node", "nodes", "", "v1", False)
    records = importlib.import_module("korvid.core.pulse")
    widgets = importlib.import_module("korvid.ui.widgets.pulse")
    selection = widgets.PulseGoto(
        0, "default", records.PulseTarget("", kind, namespace, "web-1", "uid-1")
    )
    await harness.controller.navigate_to(selection)
    assert harness.navigate.await_count == 0
    assert harness.manifest.await_count == 0


async def test_cluster_scoped_target_keeps_empty_namespace() -> None:
    harness = Harness()
    harness.view._aliases["nodes"] = ResourceMeta("Node", "nodes", "", "v1", False)
    records = importlib.import_module("korvid.core.pulse")
    widgets = importlib.import_module("korvid.ui.widgets.pulse")
    selection = widgets.PulseGoto(
        0, "default", records.PulseTarget("", "Node", "", "node-1", "uid-1")
    )
    await harness.controller.navigate_to(selection)
    harness.manifest.assert_awaited_once_with("nodes", None, "node-1")
    harness.navigate.assert_awaited_once_with("nodes", "", "node-1", 0, "uid-1", harness.origin)
    assert harness.navigate.await_count == 1


async def test_suspension_drains_navigation_identity_reads() -> None:
    harness = Harness()
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def blocked(*_args: Any) -> dict[str, Any]:
        entered.set()
        try:
            await asyncio.Event().wait()
            return {}
        finally:
            finished.set()

    harness.manifest.side_effect = blocked
    harness.controller._on_result(harness.goto())
    await entered.wait()
    try:
        await harness.controller.suspend()
        assert finished.is_set()
        assert harness.ui.tasks["pulse-navigation"][0].cancelled()
        assert harness.navigate.await_count == 0
        assert harness.ui.notifications == []
    finally:
        await harness.controller.stop()


@pytest.mark.parametrize("changed", ["context", "scope", "view"])
async def test_late_identity_failure_does_not_notify_in_a_different_frame(changed: str) -> None:
    harness = Harness()

    async def failed(*_args: Any) -> dict[str, Any]:
        if changed == "context":
            harness.context.value = 1
        elif changed == "scope":
            harness.view.scope = "other"
        else:
            harness.view.kind = "deployments"
        raise KubeClientError("read failed")

    harness.manifest.side_effect = failed
    await harness.controller.navigate_to(harness.goto())
    assert harness.ui.notifications == []
    assert harness.navigate.await_count == 0


async def test_same_frame_identity_failure_is_explained() -> None:
    harness = Harness()
    harness.manifest.side_effect = KubeClientError("read failed")
    await harness.controller.navigate_to(harness.goto())
    assert "cannot be verified" in harness.ui.notifications[-1].message
    assert harness.navigate.await_count == 0
