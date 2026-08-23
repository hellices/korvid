"""Protected contexts (issue #83): red marker, layered write confirmation,
re-evaluation on `:ctx` switch, and the optional agent block.

The marker/state lives on the app (`_protected_context`); every write
approval dialog is built through `KorvidApp._confirm_screen`, which layers
the type-the-context-name requirement while a protected context is active.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from textual.widgets import Input

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import PodSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.agent_panel import AgentPanel
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

from .agent_session_fakes import FakeSession
from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_ALIASES = {"pods": _PODS_META}


class _Recorder(WriteOps):
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("delete", meta.plural, namespace, name))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("scale", meta.plural, namespace, name, replicas))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("restart", meta.plural, namespace, name))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("replace", meta.plural, namespace, name))


def make_app(
    audit_path: Path,
    *,
    protected_context: str | None = None,
    agent_disable_in_protected: bool = False,
    recorder: _Recorder | None = None,
) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "pods":
            yield (
                "ADDED",
                PodSummary(
                    name="web-1",
                    namespace="default",
                    phase="Running",
                    ready="1/1",
                    restarts=0,
                    node=None,
                    uid="pod-uid-1",
                ),
            )
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(
            namespace="default",
            kube_context=protected_context or "dev",
            protected_contexts=("prod-*",) if protected_context else (),
            agent_disable_in_protected=agent_disable_in_protected,
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        write_ops=recorder or _Recorder(),
        audit=AuditLog(audit_path),
        protected_context=protected_context,
    )


async def _pod_row_ready(app: KorvidApp, pilot: Any) -> None:
    await until(
        pilot,
        lambda: app.query_one(ResourceTable).row_count > 0,
        label="pod row visible",
    )


async def test_status_bar_shows_protected_marker(tmp_path: Path) -> None:
    app = make_app(tmp_path / "audit.log", protected_context="prod-eu")
    async with app.run_test() as pilot:
        await _pod_row_ready(app, pilot)
        status = app.query_one(StatusBar)
        assert "PROTECTED" in str(status.render())
        assert status.has_class("protected")


async def test_status_bar_plain_when_unprotected(tmp_path: Path) -> None:
    app = make_app(tmp_path / "audit.log")
    async with app.run_test() as pilot:
        await _pod_row_ready(app, pilot)
        status = app.query_one(StatusBar)
        assert "PROTECTED" not in str(status.render())
        assert not status.has_class("protected")


async def test_write_confirm_requires_context_name(tmp_path: Path) -> None:
    """In a protected context `y` must not confirm a delete; typing the
    context name must."""
    recorder = _Recorder()
    app = make_app(tmp_path / "audit.log", protected_context="prod-eu", recorder=recorder)
    async with app.run_test() as pilot:
        await _pod_row_ready(app, pilot)
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="confirm dialog open",
        )
        texts = str(app.screen.query_one(".confirm-protected").render())
        assert "prod-eu" in texts
        await pilot.press("y")
        await pilot.pause()
        assert recorder.calls == []  # y alone never confirms in protected mode
        gate = app.screen.query_one("#confirm-name", Input)
        gate.value = "prod-eu"
        await pilot.press("enter")
        await until(pilot, lambda: bool(recorder.calls), label="delete executed")
        assert recorder.calls[0][:2] == ("delete", "pods")


async def test_write_confirm_plain_y_when_unprotected(tmp_path: Path) -> None:
    recorder = _Recorder()
    app = make_app(tmp_path / "audit.log", recorder=recorder)
    async with app.run_test() as pilot:
        await _pod_row_ready(app, pilot)
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="confirm dialog open",
        )
        await pilot.press("y")
        await until(pilot, lambda: bool(recorder.calls), label="delete executed")


class _RecordingSession(FakeSession):
    """An `AgentSession` that records the prompts it receives."""

    @property
    def turns(self) -> list[str]:
        return self.prompts


async def test_agent_prompt_refused_when_disabled_in_protected(tmp_path: Path) -> None:
    """The blocked prompt must never reach the session."""
    from korvid.ui.messages import AgentPromptSubmitted

    app = make_app(
        tmp_path / "audit.log",
        protected_context="prod-eu",
        agent_disable_in_protected=True,
    )
    session = _RecordingSession()
    app._agent_ui._session = session
    async with app.run_test() as pilot:
        await _pod_row_ready(app, pilot)
        app.post_message(AgentPromptSubmitted("delete everything"))
        await until(
            pilot,
            lambda: any("disabled in protected context" in n.message for n in app._notifications),
            label="agent refusal notification",
        )
        assert session.turns == []


async def test_agent_prompt_allowed_in_protected_without_flag(tmp_path: Path) -> None:
    """Without agent.disable_in_protected the agent still runs (its writes
    remain approval-gated through the protected ConfirmScreen)."""
    from korvid.ui.messages import AgentPromptSubmitted

    app = make_app(tmp_path / "audit.log", protected_context="prod-eu")
    session = _RecordingSession()
    app._agent_ui._session = session
    async with app.run_test() as pilot:
        await _pod_row_ready(app, pilot)
        app.post_message(AgentPromptSubmitted("what is wrong?"))
        await until(pilot, lambda: session.turns == ["what is wrong?"], label="turn ran")
        assert not any("disabled in protected context" in n.message for n in app._notifications)


async def test_agent_write_approval_uses_protected_gate(tmp_path: Path) -> None:
    """The agent's approval dialog goes through the same protected layer:
    the ConfirmScreen it awaits demands the typed context name."""
    recorder = _Recorder()
    app = make_app(tmp_path / "audit.log", protected_context="prod-eu", recorder=recorder)
    async with app.run_test() as pilot:
        await _pod_row_ready(app, pilot)
        # Approval dialogs only surface while the agent panel is expanded.
        app.query_one(AgentPanel).display = True
        result_box: list[str] = []

        async def _request() -> None:
            result_box.append(
                await app._agent_ui._await_user_approval("Agent delete", "delete pods/web-1")
            )

        task = asyncio.create_task(_request())
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="approval dialog open",
        )
        await pilot.press("y")
        await pilot.pause()
        assert not result_box  # y alone resolves nothing in protected mode
        gate = app.screen.query_one("#confirm-name", Input)
        gate.value = "prod-eu"
        await pilot.press("enter")
        await task
        assert result_box == ["approved"]
