"""Telepresence integration UI, phase 1 (issue #159): the `:tp` status
panel, graceful degradation without the binary, the config kill-switch,
and the one-shot install hint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META
from korvid.k8s.telepresence import ActiveIntercept, TelepresenceStatus
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.telepresence_screen import (
    TelepresenceScreen,
    intercept_lines,
    status_lines,
)

from .waits import until

_ALIASES = {"pods": PODS_META}


class FakeTelepresence:
    """CLI double: canned status/intercepts, call recording."""

    def __init__(
        self,
        status: TelepresenceStatus | None = None,
        intercepts: list[ActiveIntercept] | None = None,
    ) -> None:
        self.status_result = status or TelepresenceStatus(
            connected=False, user_running=False, root_running=False
        )
        self.intercepts_result = intercepts or []
        self.calls: list[str] = []

    async def status(self) -> TelepresenceStatus:
        self.calls.append("status")
        return self.status_result

    async def list_intercepts(self, daemon: str | None = None) -> list[ActiveIntercept]:
        self.calls.append("list")
        return self.intercepts_result


def make_app(
    *,
    telepresence: FakeTelepresence | None = None,
    probe: object = None,
) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if False:  # pragma: no cover - makes this an async generator
            yield ("ADDED", None)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
        telepresence=telepresence,  # type: ignore[arg-type]  # test seam
        probe_traffic_manager=probe,  # type: ignore[arg-type]  # test seam
    )


# ---------------------------------------------------------------------------
# pure line builders
# ---------------------------------------------------------------------------


def test_status_lines_connected() -> None:
    lines = status_lines(
        TelepresenceStatus(
            connected=True,
            user_running=True,
            root_running=True,
            version="2.30.1",
            kubernetes_context="prod",
            traffic_manager_version="2.30.1",
        )
    )
    text = "\n".join(lines)
    assert "user daemon: running (v2.30.1)" in text
    assert "session: connected to prod" in text
    assert "traffic manager: v2.30.1" in text


def test_status_lines_error_short_circuits() -> None:
    lines = status_lines(
        TelepresenceStatus(
            connected=False, user_running=False, root_running=False, error="daemon exploded"
        )
    )
    assert lines == ["telepresence reported: daemon exploded"]


def test_intercept_lines() -> None:
    lines = intercept_lines(
        [
            ActiveIntercept(
                workload="web",
                namespace="prod",
                kind="Deployment",
                client="alice@laptop",
                port="8080",
            )
        ]
    )
    assert lines == ["Deployment prod/web port 8080 by alice@laptop"]


# ---------------------------------------------------------------------------
# :tp command
# ---------------------------------------------------------------------------


async def test_tp_opens_the_status_panel() -> None:
    tp = FakeTelepresence(
        status=TelepresenceStatus(connected=False, user_running=False, root_running=False)
    )
    app = make_app(telepresence=tp)
    async with app.run_test() as pilot:
        app._handle_telepresence_command()
        await until(
            pilot,
            lambda: isinstance(app.screen, TelepresenceScreen),
            label="panel opened",
        )
        assert tp.calls == ["status"]  # not connected: intercepts never queried
        await pilot.press("escape")
        await until(
            pilot,
            lambda: not isinstance(app.screen, TelepresenceScreen),
            label="panel closed",
        )


async def test_tp_queries_intercepts_when_connected() -> None:
    tp = FakeTelepresence(
        status=TelepresenceStatus(connected=True, user_running=True, root_running=True),
        intercepts=[ActiveIntercept(workload="web", namespace="prod")],
    )
    app = make_app(telepresence=tp)
    async with app.run_test() as pilot:
        app._handle_telepresence_command()
        await until(
            pilot,
            lambda: isinstance(app.screen, TelepresenceScreen),
            label="panel opened",
        )
        assert tp.calls == ["status", "list"]
        from textual.widgets import Static

        text = " ".join(str(s.render()) for s in app.screen.query(Static))
        assert "prod/web" in text


async def test_tp_without_the_binary_notifies() -> None:
    app = make_app(telepresence=None)
    async with app.run_test() as pilot:
        app._handle_telepresence_command()
        await until(
            pilot,
            lambda: any("telepresence" in n.message for n in app._notifications),
            label="absence explained",
        )
        assert not isinstance(app.screen, TelepresenceScreen)


async def test_tp_cli_failure_is_a_notification_not_a_crash() -> None:
    class ExplodingTP(FakeTelepresence):
        async def status(self) -> TelepresenceStatus:
            from korvid.k8s.telepresence import TelepresenceError

            raise TelepresenceError("connector refused")

    app = make_app(telepresence=ExplodingTP())
    async with app.run_test() as pilot:
        app._handle_telepresence_command()
        await until(
            pilot,
            lambda: any("connector refused" in n.message for n in app._notifications),
            label="failure surfaced",
        )
        assert not isinstance(app.screen, TelepresenceScreen)


# ---------------------------------------------------------------------------
# install hint (client absent, traffic-manager present)
# ---------------------------------------------------------------------------


async def test_install_hint_fires_once_when_manager_detected() -> None:
    calls = {"n": 0}

    async def probe() -> bool:
        calls["n"] += 1
        return True

    app = make_app(telepresence=None, probe=probe)
    async with app.run_test() as pilot:
        await app._maybe_hint_telepresence()
        await until(
            pilot,
            lambda: any("traffic-manager detected" in n.message for n in app._notifications),
            label="hint shown",
        )
        await app._maybe_hint_telepresence()  # once per session, never a storm
        await pilot.pause()
        assert calls["n"] == 1
        hints = sum(1 for n in app._notifications if "traffic-manager detected" in n.message)
        assert hints == 1


async def test_no_hint_when_manager_absent_or_client_present() -> None:
    async def absent() -> bool:
        return False

    app = make_app(telepresence=None, probe=absent)
    async with app.run_test() as pilot:
        await app._maybe_hint_telepresence()
        await pilot.pause()
        assert not any("traffic-manager" in n.message for n in app._notifications)

    async def present() -> bool:
        return True

    with_client = make_app(telepresence=FakeTelepresence(), probe=present)
    async with with_client.run_test() as pilot:
        await with_client._maybe_hint_telepresence()
        await pilot.pause()
        assert not any("traffic-manager" in n.message for n in with_client._notifications)


async def test_help_hides_tp_when_the_integration_is_unavailable() -> None:
    """Absent binary (or kill-switch) = 'no UI': the help overlay must not
    advertise a command that only answers with a warning."""
    from korvid.ui.command import command_help

    without = command_help(telepresence=False)
    assert not any(":tp" in cmd for cmd, _ in without)
    with_tp = command_help(telepresence=True)
    assert any(":tp" in cmd for cmd, _ in with_tp)


async def test_multi_daemon_panel_scopes_list_to_the_selected_daemon() -> None:
    class RecordingTP(FakeTelepresence):
        def __init__(self) -> None:
            super().__init__(
                status=TelepresenceStatus(
                    connected=True,
                    user_running=True,
                    root_running=True,
                    daemon_name="prod-conn",
                )
            )
            self.list_daemons: list[str | None] = []

        async def list_intercepts(self, daemon: str | None = None) -> list[ActiveIntercept]:
            self.list_daemons.append(daemon)
            return []

    tp = RecordingTP()
    app = make_app(telepresence=tp)
    async with app.run_test() as pilot:
        app._handle_telepresence_command()
        await until(
            pilot,
            lambda: isinstance(app.screen, TelepresenceScreen),
            label="panel opened",
        )
        assert tp.list_daemons == ["prod-conn"]


async def test_cli_error_notification_renders_without_markup() -> None:
    """stderr tails are hostile input for a markup-enabled toast."""

    class ExplodingTP(FakeTelepresence):
        async def status(self) -> TelepresenceStatus:
            from korvid.k8s.telepresence import TelepresenceError

            raise TelepresenceError("connector [bold red]refused[/]")

    app = make_app(telepresence=ExplodingTP())
    async with app.run_test() as pilot:
        app._handle_telepresence_command()
        await until(
            pilot,
            lambda: any("refused" in n.message for n in app._notifications),
            label="failure surfaced",
        )
        note = next(n for n in app._notifications if "refused" in n.message)
        assert note.markup is False


async def test_hint_retries_after_a_managerless_start() -> None:
    """The startup cluster may lack a traffic-manager while a later :ctx
    target runs one: a no-manager probe must not consume the session's
    single hint."""
    answers = {"present": False}

    async def probe() -> bool:
        return answers["present"]

    app = make_app(telepresence=None, probe=probe)
    async with app.run_test() as pilot:
        await app._maybe_hint_telepresence()
        await pilot.pause()
        assert not any("traffic-manager" in n.message for n in app._notifications)
        answers["present"] = True  # the cluster behind a :ctx switch has one
        await app._maybe_hint_telepresence()
        await until(
            pilot,
            lambda: any("traffic-manager detected" in n.message for n in app._notifications),
            label="hint shown after the switch",
        )


async def test_probe_result_from_the_old_context_is_discarded() -> None:
    """A probe still in flight across a :ctx switch answers for the old
    cluster: its result must be discarded (no stale hint) and the switch's
    re-probe must not be lost."""
    gate = asyncio.Event()
    answers = [True, False]  # old cluster has a manager; the new one does not

    async def probe() -> bool:
        await gate.wait()
        return answers.pop(0)

    app = make_app(telepresence=None, probe=probe)
    async with app.run_test() as pilot:
        first = asyncio.create_task(app._maybe_hint_telepresence())
        await pilot.pause()
        app._ctx._epoch += 1  # what a :ctx switch does
        second = asyncio.create_task(app._maybe_hint_telepresence())  # switch re-probe
        await pilot.pause()
        gate.set()
        await first
        await second
        await pilot.pause()
        # the old cluster's True answer is stale - no hint for the new one
        assert not any("traffic-manager detected" in n.message for n in app._notifications)
        assert not answers  # the queued re-probe really ran after the first
