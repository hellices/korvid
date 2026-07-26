"""Tests for the help screen (issue #41) — keybinding discovery via ``?``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from textual.binding import Binding

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.command import command_help
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.help_screen import HelpScreen, collect_help, key_label

from .waits import until

# ---------------------------------------------------------------------------
# Pure unit tests: key_label
# ---------------------------------------------------------------------------


def test_key_label_symbol_keys() -> None:
    assert key_label("question_mark") == "?"
    assert key_label("colon") == ":"
    assert key_label("slash") == "/"


def test_key_label_modifiers_and_case() -> None:
    assert key_label("ctrl+s") == "Ctrl-S"
    assert key_label("shift+n") == "Shift-N"
    assert key_label("escape") == "Esc"
    assert key_label("w") == "w"


# ---------------------------------------------------------------------------
# Pure unit tests: collect_help
# ---------------------------------------------------------------------------


def _bindings() -> list[Binding]:
    return [
        Binding("q", "quit", "Quit"),
        Binding("l", "logs", "Logs"),
        Binding("w", "log_wrap", "Wrap", show=False),
        Binding("shift+l", "logs_multi", "Multi-log"),
        Binding("L", "logs_multi", "Multi-log", show=False),
        Binding("ctrl+a", "toggle_agent", "AI"),
        Binding("d", "describe", "Describe"),
        Binding("slash", "open_filter", "Filter/Search"),
    ]


def test_collect_help_groups_by_context() -> None:
    """Bindings land in the context where the key is actually pressed."""
    groups = dict(collect_help(_bindings(), []))
    assert ("q", "Quit") in groups["Global"]
    # l/d are pressed in the table even though they open other panes.
    assert ("l", "Logs") in groups["Table"]
    assert ("d", "Describe") in groups["Table"]
    assert ("w", "Wrap") in groups["Logs"]
    assert ("Ctrl-A", "AI") in groups["Agent"]


def test_collect_help_multi_context_binding_appears_in_each_group() -> None:
    """`/` filters the table and searches the log pane — listed in both."""
    groups = dict(collect_help(_bindings(), []))
    assert ("/", "Filter/Search") in groups["Table"]
    assert ("/", "Filter/Search") in groups["Logs"]


def test_collect_help_merges_duplicate_action_keys() -> None:
    """L and shift+l run the same action — one row, first key label wins."""
    groups = dict(collect_help(_bindings(), []))
    table = groups["Table"]
    multi = [entry for entry in table if entry[1] == "Multi-log"]
    assert multi == [("Shift-L", "Multi-log")]


def test_collect_help_includes_hidden_bindings() -> None:
    """show=False bindings are the discoverability gap — they must be listed."""
    groups = dict(collect_help(_bindings(), []))
    assert any(desc == "Wrap" for _, desc in groups["Logs"])


def test_every_app_binding_action_has_an_explicit_group() -> None:
    """New app bindings must be classified — the overlay may not silently drift."""
    from korvid.ui.widgets.help_screen import _ACTION_GROUPS

    for binding in KorvidApp.BINDINGS:
        action = binding.action if isinstance(binding, Binding) else binding[1]
        assert action in _ACTION_GROUPS, f"unclassified binding action: {action}"


def test_collect_help_describe_screen_bindings_join_describe_group() -> None:
    describe = [Binding("slash", "open_search", "Search")]
    groups = dict(collect_help(_bindings(), describe))
    assert ("/", "Search") in groups["Describe"]


def test_collect_help_appends_handler_keys_to_their_groups() -> None:
    """Keys handled in event handlers (not BINDINGS) still get rows."""
    handler_keys = [
        ("Table", "enter", "Drill down"),
        ("Table", "escape", "Pop drill level"),
        ("Logs", "escape", "Close pane"),
    ]
    groups = dict(collect_help(_bindings(), [], handler_keys=handler_keys))
    assert ("Enter", "Drill down") in groups["Table"]
    assert ("Esc", "Pop drill level") in groups["Table"]
    assert ("Esc", "Close pane") in groups["Logs"]


def test_app_handler_key_help_uses_known_groups() -> None:
    """HANDLER_KEY_HELP entries must reference groups the overlay renders."""
    from korvid.ui.widgets.help_screen import _GROUP_ORDER

    assert KorvidApp.HANDLER_KEY_HELP, "expected handler-key help metadata"
    for group, key, description in KorvidApp.HANDLER_KEY_HELP:
        assert group in _GROUP_ORDER, f"unknown group {group!r} for key {key!r}"
        assert description


# ---------------------------------------------------------------------------
# Pure unit tests: command_help
# ---------------------------------------------------------------------------


def test_command_help_covers_grammar() -> None:
    entries = dict(command_help())
    assert ":q" in entries
    assert any("ns" in k for k in entries)
    assert any("<kind>" in k for k in entries)
    assert any("ai" in k for k in entries)


def test_command_help_covers_every_reserved_builtin() -> None:
    """Each reserved builtin (:ai, :model, :mcp, ...) has a help row."""
    from korvid.ui.command import _RESERVED_BUILTINS

    joined = " ".join(f"{cmd} {desc}" for cmd, desc in command_help())
    for name in _RESERVED_BUILTINS:
        assert f":{name}" in joined, f"missing help entry for builtin :{name}"


# ---------------------------------------------------------------------------
# Pilot tests
# ---------------------------------------------------------------------------


def _pod(name: str) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
    )


def make_app(pods: list[PodSummary]) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in pods if kind == "pods" else []:
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
    )


def _help_text(app: KorvidApp) -> str:
    screen = app.screen
    assert isinstance(screen, HelpScreen)
    return screen.body_text()


async def test_question_mark_opens_help() -> None:
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        text = _help_text(app)
        assert "Logs" in text
        assert "Global" in text


async def test_help_lists_every_app_binding_description() -> None:
    """The overlay is generated from the real bindings — nothing may drift."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        text = _help_text(app)
        for binding in KorvidApp.BINDINGS:
            if isinstance(binding, Binding):
                description = binding.description
            else:
                description = binding[2] if len(binding) == 3 else ""
            assert description in text


async def test_help_lists_handler_keys() -> None:
    """Enter (drill down) and Esc (pop/close) are handled outside BINDINGS
    but are real user-facing keys — the overlay must render them too."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        text = _help_text(app)
        for _, _, description in KorvidApp.HANDLER_KEY_HELP:
            assert description in text
        assert "Drill down" in text
        assert "Enter" in text


async def test_help_lists_commands() -> None:
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        text = _help_text(app)
        assert ":ns" in text
        assert "Commands" in text


async def test_escape_and_q_close_help() -> None:
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, HelpScreen), label="help closed")

        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help reopen")
        await pilot.press("q")
        await until(pilot, lambda: not isinstance(app.screen, HelpScreen), label="help closed q")


async def test_question_mark_in_filter_input_stays_text() -> None:
    """Typing ? inside the filter Input must not hijack into the help screen."""
    app = make_app([_pod("myapp")])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("slash")
        await until(pilot, lambda: app.query_one(FilterBar).display, label="filter open")
        await pilot.press("question_mark")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)
        assert app.query_one(FilterBar).value == "?"


def test_help_screen_is_modal() -> None:
    from textual.screen import ModalScreen

    assert issubclass(HelpScreen, ModalScreen)
