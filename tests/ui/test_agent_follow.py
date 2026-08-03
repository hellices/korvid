"""Agent follow mode: mirror the built-in agent's cluster reads on screen.

Small local models rarely volunteer the UI tools (`open_describe`,
`open_logs`) — they call the data-returning cluster reads and answer in
text while the screen sits idle. With agent follow on (the default), each
successful read in a chat turn is mirrored through the same UIBridge
methods, exactly like MCP follow mode (issue #153) does for external reads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from korvid.agent.events import AgentEvent, ToolCallFinished, ToolCallStarted
from korvid.ui.messages import UnknownCommand
from korvid.ui.widgets.describe_screen import DescribeScreen
from tests.ui.test_agent_ui_drive import make_app


class _ScriptedRuntime:
    """Duck-typed AgentRuntime replaying a fixed event script."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    async def run_turn(self, text: str, screen_context: str) -> AsyncIterator[AgentEvent]:
        for event in self._events:
            yield event


def _read_events(*, ok: bool = True) -> list[AgentEvent]:
    return [
        ToolCallStarted(
            call_id="c1",
            name="get_resource",
            arguments='{"kind": "pods", "name": "web-1", "namespace": "default"}',
        ),
        ToolCallFinished(call_id="c1", name="get_resource", ok=ok, summary=""),
    ]


async def test_successful_agent_read_is_mirrored_as_describe() -> None:
    app = make_app()
    app._agent_runtime = _ScriptedRuntime(_read_events())  # type: ignore[assignment]  # fake
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_agent_turn("what is wrong with web-1?")
        await pilot.pause()
        assert isinstance(app.screen, DescribeScreen)


async def test_failed_agent_read_is_not_mirrored() -> None:
    """A 404'd read must not steer the screen to a view it never loaded."""
    app = make_app()
    app._agent_runtime = _ScriptedRuntime(_read_events(ok=False))  # type: ignore[assignment]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_agent_turn("what is wrong with web-1?")
        await pilot.pause()
        assert not isinstance(app.screen, DescribeScreen)


async def test_agent_follow_off_disables_mirroring() -> None:
    app = make_app()
    app._agent_runtime = _ScriptedRuntime(_read_events())  # type: ignore[assignment]  # fake
    async with app.run_test() as pilot:
        await pilot.pause()
        app._agent_follow = False
        await app._run_agent_turn("what is wrong with web-1?")
        await pilot.pause()
        assert not isinstance(app.screen, DescribeScreen)


async def test_list_read_mirrors_as_navigation() -> None:
    app = make_app()
    events: list[AgentEvent] = [
        ToolCallStarted(call_id="c1", name="list_resources", arguments='{"kind": "deployments"}'),
        ToolCallFinished(call_id="c1", name="list_resources", ok=True, summary=""),
    ]
    app._agent_runtime = _ScriptedRuntime(events)  # type: ignore[assignment]  # fake
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_agent_turn("list deployments")
        await pilot.pause()
        assert app.current_kind == "deployments"


async def test_malformed_tool_arguments_do_not_break_the_turn() -> None:
    """Small models emit broken JSON: the mirror is skipped, never raised."""
    app = make_app()
    events: list[AgentEvent] = [
        ToolCallStarted(call_id="c1", name="get_resource", arguments='{"kind": broken'),
        ToolCallFinished(call_id="c1", name="get_resource", ok=True, summary=""),
    ]
    app._agent_runtime = _ScriptedRuntime(events)  # type: ignore[assignment]  # fake
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._run_agent_turn("show web-1")
        await pilot.pause()
        assert not isinstance(app.screen, DescribeScreen)


async def test_ai_follow_command_toggles_state() -> None:
    app = make_app()
    app._agent_available = True  # command routing gates on availability
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._agent_follow is True  # default on
        app.on_unknown_command(UnknownCommand("ai follow off"))
        assert app._agent_follow is False
        app.on_unknown_command(UnknownCommand("ai follow"))  # bare toggle
        assert app._agent_follow is True


def test_agent_follow_config_defaults_on() -> None:
    from korvid.core.config import KorvidConfig

    assert KorvidConfig().agent_follow is True
