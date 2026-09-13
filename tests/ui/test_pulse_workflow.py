import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from textual.widgets import DataTable, Input

from korvid.core.config import KorvidConfig
from korvid.core.pulse import PulseTarget
from korvid.core.session_timeline import SessionTimeline
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.errors import KubeClientError
from korvid.k8s.models import GenericSummary
from korvid.k8s.pulse import PulsePage, PulseReader, PulseSource
from korvid.ui import object_navigation
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.pulse import PulseGoto, PulseScreen, PulseSummary
from tests.app_factory import build_test_app
from tests.ui.waits import until

DEPLOYMENT = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
FAILED = {
    "kind": "Deployment",
    "apiVersion": "apps/v1",
    "metadata": {"name": "blocked", "namespace": "default", "uid": "deployment-1", "generation": 2},
    "status": {
        "observedGeneration": 2,
        "conditions": [
            {
                "type": "ReplicaFailure",
                "status": "True",
                "reason": "FailedCreate",
                "message": "quota exceeded",
            }
        ],
    },
}


class Reader(PulseReader):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def read_pulse_page(
        self,
        source: PulseSource,
        namespace: str | None,
        continuation: str,
        limit: int,
        max_bytes: int,
    ) -> PulsePage:
        self.calls.append((source.key, namespace))
        return PulsePage((FAILED,) if source.key == "deployments" else ())


def pulse_app(*, warnings: Any = None) -> tuple[KorvidApp, Reader]:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "deployments":
            yield (
                "ADDED",
                GenericSummary("blocked", "default", "Deployment", "", uid="deployment-1"),
            )
        await asyncio.Event().wait()

    async def manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return FAILED

    reader = Reader()
    app = build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=build_alias_map([PODS_META, DEPLOYMENT]),
        pulse_reader=reader,
        get_manifest=manifest,
        agent_available=False,
        session_timeline=SessionTimeline(500, 262144),
        watch_warning_events=warnings,
    )
    return app, reader


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_ambient_to_problem_journey_without_optional_extras(size: tuple[int, int]) -> None:
    app, reader = pulse_app()
    async with app.run_test(size=size) as pilot:
        await until(
            pilot, lambda: bool(app._pulse.snapshot().current), label="ambient Deployment failure"
        )
        await until(
            pilot,
            lambda: "1 current" in str(app.query_one(PulseSummary).render()),
            label="ambient summary",
        )
        assert app.current_kind == "pods"
        assert app.query_one(PulseSummary).region.height == 1
        assert reader.calls == [
            ("pods", "default"),
            ("deployments", "default"),
            ("events", "default"),
        ]
        pulse_keys = ("colon", *"pulse", "enter", "enter")
        await pilot.press(*pulse_keys[:-1])
        await until(pilot, lambda: isinstance(app.screen, PulseScreen), label="Pulse detail")
        assert app.screen.query_one(DataTable).cursor_row == 1
        await pilot.press(pulse_keys[-1])
        await until(pilot, lambda: app.current_kind == "deployments", label="Deployment view")
        await until(
            pilot,
            lambda: app._view.selected_ns_name() == ("default", "blocked"),
            label="observed Deployment selected",
        )
        assert app._view.selected_uid("default", "blocked") == "deployment-1"
        baseline_keys = ("colon", *"deploy", "enter")
        assert len(pulse_keys) == len(baseline_keys) == 8


async def test_shared_warning_feed_does_not_add_watch_or_steal_approval_focus() -> None:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    subscriptions: list[str | None] = []

    async def warnings(namespace: str | None) -> AsyncIterator[dict[str, Any]]:
        subscriptions.append(namespace)
        while True:
            yield await queue.get()

    app, reader = pulse_app(warnings=warnings)
    approvals: list[bool | None] = []
    async with app.run_test(size=(80, 24)) as pilot:
        await until(pilot, lambda: bool(app._pulse.snapshot().current), label="initial Pulse")
        dialog = ConfirmScreen(
            "Delete protected resource?", "delete blocked", require_name="blocked"
        )
        await app.push_screen(dialog, approvals.append)
        input_widget = dialog.query_one(Input)
        await until(pilot, lambda: app.focused is input_widget, label="approval input focus")
        focus = app.focused
        for index in range(120):
            queue.put_nowait(
                {
                    "type": "Warning",
                    "reason": "NovelControllerFailure",
                    "message": "password=do-not-display [bold]plain[/bold]",
                    "lastTimestamp": datetime.now(UTC).isoformat(),
                    "metadata": {"uid": f"event-{index}", "namespace": "default"},
                    "involvedObject": {
                        "kind": "NewResource",
                        "namespace": "default",
                        "name": "unknown",
                        "uid": "custom-1",
                    },
                }
            )
        await until(
            pilot, lambda: bool(app._pulse.snapshot().recent), label="shared warning ingestion"
        )
        summary = app.screen_stack[0].query_one(PulseSummary)
        await until(
            pilot,
            lambda: "100 recent" in str(summary.render()),
            label="coalesced background summary",
        )
        app._pulse.open_detail()
        assert app.screen is dialog
        assert app.focused is focus
        assert input_widget.value == ""
        assert approvals == []
        assert subscriptions == [None]
        assert len(reader.calls) == 3
        assert "do-not-display" not in str(app._pulse.snapshot())
        await pilot.press("escape")
        assert approvals == [None]


async def test_workspace_uid_guard_refuses_same_name_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _reader = pulse_app()
    focused: list[str] = []

    def focus_row(key: str) -> bool:
        focused.append(key)
        return True

    async with app.run_test() as pilot:
        await until(pilot, lambda: bool(app._pulse.snapshot().current), label="initial Pulse")
        monkeypatch.setattr(app._workspace_ctl._surface, "focus_row", focus_row)
        await app._workspace_ctl.jump_to_object(
            "deployments", "default", "blocked", epoch=0, expected_uid="old-uid"
        )
        assert focused == []
        await until(
            pilot,
            lambda: any(
                "identity changed" in notification.message for notification in app._notifications
            ),
            label="UID refusal",
        )
        assert any(
            "identity changed" in notification.message for notification in app._notifications
        )


@pytest.mark.parametrize("change", ["scope", "kind", "uid", "unchanged"])
async def test_pending_workspace_await_revalidates_navigation_generation(
    monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    app, _reader = pulse_app()
    entered = asyncio.Event()
    release = asyncio.Event()
    focus_row = Mock(return_value=True)
    notify = Mock()

    async with app.run_test():
        original = app._workspace_ctl.navigate

        async def navigate(view: str | None, namespace: str | None, **kwargs: Any) -> None:
            await original(view, namespace, **kwargs)
            entered.set()
            await release.wait()

        monkeypatch.setattr(app._workspace_ctl, "navigate", navigate)
        monkeypatch.setattr(app._workspace_ctl._surface, "focus_row", focus_row)
        monkeypatch.setattr(app._workspace_ctl._ui, "notify", notify)
        selection = PulseGoto(
            0, "default", PulseTarget("apps", "Deployment", "default", "blocked", "deployment-1")
        )
        navigation = asyncio.create_task(app._pulse.navigate_to(selection))
        try:
            await entered.wait()
            if change == "scope":
                await original(None, "other")
                await original(None, "default")
            elif change == "kind":
                await original("pods", None)
                await original("deployments", None)
            uid = "replacement" if change == "uid" else "deployment-1"
            app.store.apply_event(
                "deployments",
                "default",
                "ADDED",
                GenericSummary("blocked", "default", "Deployment", "", uid=uid),
            )
            release.set()
            await navigation
            assert app.current_kind == "deployments"
            assert app.current_scope == "default"
            assert focus_row.call_count == int(change == "unchanged")
            if change == "unchanged":
                focus_row.assert_called_once_with("default/blocked")
            if change == "uid":
                assert "identity changed" in notify.call_args.args[0]
            else:
                assert notify.call_count == 0
        finally:
            release.set()
            await navigation


@pytest.mark.parametrize("round_trip", [False, True])
async def test_pending_workspace_lock_revalidates_navigation_generation(
    monkeypatch: pytest.MonkeyPatch, round_trip: bool
) -> None:
    app, _reader = pulse_app()
    entered = asyncio.Event()
    focus_row = Mock(return_value=True)

    async with app.run_test():
        original = app._workspace_ctl.navigate

        async def navigate(view: str | None, namespace: str | None, **kwargs: Any) -> None:
            entered.set()
            await original(view, namespace, **kwargs)
            app.store.apply_event(
                "deployments",
                "default",
                "ADDED",
                GenericSummary("blocked", "default", "Deployment", "", uid="deployment-1"),
            )

        monkeypatch.setattr(app._workspace_ctl, "navigate", navigate)
        monkeypatch.setattr(app._workspace_ctl._surface, "focus_row", focus_row)
        selection = PulseGoto(
            0, "default", PulseTarget("apps", "Deployment", "default", "blocked", "deployment-1")
        )
        async with app._workspace_ctl.nav_lock:
            navigation = asyncio.create_task(app._pulse.navigate_to(selection))
            await entered.wait()
            if round_trip:
                await app._workspace_ctl._navigate_locked(app._pane, None, "other")
                await app._workspace_ctl._navigate_locked(app._pane, None, "default")
        await navigation
        assert app.current_kind == ("pods" if round_trip else "deployments")
        assert app.current_scope == "default"
        assert focus_row.call_count == int(not round_trip)


@pytest.mark.parametrize("round_trip", [False, True])
async def test_pending_workspace_poll_revalidates_navigation_generation(
    monkeypatch: pytest.MonkeyPatch, round_trip: bool
) -> None:
    app, _reader = pulse_app()
    entered = asyncio.Event()
    release = asyncio.Event()
    notify = Mock()

    async def wait_for_poll(_delay: float) -> None:
        entered.set()
        await release.wait()

    async with app.run_test():
        app._workspace_ctl._jump_poll_attempts = 1
        monkeypatch.setattr(object_navigation, "asyncio", SimpleNamespace(sleep=wait_for_poll))
        monkeypatch.setattr(app._workspace_ctl._surface, "focus_row", Mock(return_value=False))
        monkeypatch.setattr(app._workspace_ctl._ui, "notify", notify)
        selection = PulseGoto(
            0, "default", PulseTarget("apps", "Deployment", "default", "blocked", "deployment-1")
        )
        navigation = asyncio.create_task(app._pulse.navigate_to(selection))
        try:
            await entered.wait()
            if round_trip:
                await app._workspace_ctl.navigate(None, "other")
                await app._workspace_ctl.navigate(None, "default")
            release.set()
            await navigation
            assert notify.call_count == int(not round_trip)
            if not round_trip:
                assert "not visible" in notify.call_args.args[0]
        finally:
            release.set()
            await navigation


class WatchStopHandoff:
    def __init__(self, read_fails: bool) -> None:
        self.read_fails = read_fails
        self.read_started = asyncio.Event()
        self.read_released = asyncio.Event()
        self.read_finished = asyncio.Event()
        self.watch_started = asyncio.Event()
        self.watch_stopping = asyncio.Event()
        self.watch_released = asyncio.Event()

    async def source(self, kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if (kind, scope) == ("pods", "default"):
            self.watch_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.watch_stopping.set()
                await self.watch_released.wait()
        if kind == "deployments":
            yield (
                "ADDED",
                GenericSummary("blocked", "default", "Deployment", "", uid="deployment-1"),
            )
        await asyncio.Event().wait()

    async def manifest(self, kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        self.read_started.set()
        await self.read_released.wait()
        self.read_finished.set()
        if self.read_fails:
            raise KubeClientError("identity read failed")
        return FAILED


@pytest.mark.parametrize("moment", ["during_stop", "after_stop", "unchanged"])
@pytest.mark.parametrize("read_fails", [False, True])
async def test_real_watch_stop_handoff_preserves_newer_navigation(
    monkeypatch: pytest.MonkeyPatch, moment: str, read_fails: bool
) -> None:
    handoff = WatchStopHandoff(read_fails)
    store = ResourceStore()
    app = build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, handoff.source),
        aliases=build_alias_map([PODS_META, DEPLOYMENT]),
        pulse_reader=Reader(),
        get_manifest=handoff.manifest,
        agent_available=False,
    )
    focus_row = Mock(return_value=True)
    notify = Mock()
    async with app.run_test():
        await handoff.watch_started.wait()
        monkeypatch.setattr(app._workspace_ctl._surface, "focus_row", focus_row)
        monkeypatch.setattr(app._pulse._ui, "notify", notify)
        selection = PulseGoto(
            0, "default", PulseTarget("apps", "Deployment", "default", "blocked", "deployment-1")
        )
        generation = app._pane.nav_gen
        tasks = [asyncio.create_task(app._pulse.navigate_to(selection))]
        try:
            await handoff.read_started.wait()
            if moment != "unchanged":
                transition = asyncio.create_task(app._workspace_ctl.navigate(None, "other"))
                tasks.append(transition)
                await handoff.watch_stopping.wait()
                assert app.current_scope == "default"
                assert app._pane.nav_gen > generation
                if moment == "after_stop":
                    handoff.watch_released.set()
                    await transition
            else:
                handoff.watch_released.set()
            handoff.read_released.set()
            await handoff.read_finished.wait()
            handoff.watch_released.set()
            await asyncio.gather(*tasks)
            if moment == "unchanged":
                assert app.current_scope == "default"
                assert app.current_kind == ("pods" if read_fails else "deployments")
                assert focus_row.call_count == int(not read_fails)
                assert notify.call_count == int(read_fails)
            else:
                assert app.current_scope == "other"
                assert app.current_kind == "pods"
                assert focus_row.call_count == 0
                assert notify.call_count == 0
        finally:
            handoff.read_released.set()
            handoff.watch_released.set()
            await asyncio.gather(*tasks)
