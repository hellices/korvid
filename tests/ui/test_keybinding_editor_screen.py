"""Keyboard-driven contracts for the standalone staged keybinding editor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.pilot import Pilot
from textual.widget import Widget
from textual.widgets import Button, Input, OptionList, Static

from korvid.core.keymap_edit import KeymapRules

from .waits import until

if TYPE_CHECKING:
    from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen


_DESCRIPTIONS = {
    "logs": "Tail workload output [plain]",
    "describe": "Inspect resource details",
    "relationships": "Find linked objects",
    "agent": "Open assistant",
    "snapshot": "Capture a snapshot",
}


def _rules() -> KeymapRules:
    return KeymapRules(
        actions={
            "logs": ("l",),
            "describe": ("d",),
            "relationships": ("g",),
            "agent": ("ctrl+a",),
            "snapshot": ("shift+s", "S"),
        },
        priority_actions=frozenset({"agent"}),
        reserved_keys={"1": "namespace slot"},
        priority_reserved_keys={"ctrl+p": "palette close"},
        action_contexts={
            "logs": frozenset({("", "pods")}),
            "describe": frozenset({("", "pods"), ("apps", "deployments")}),
            "relationships": frozenset({("", "pods")}),
            "snapshot": frozenset({("apps", "deployments")}),
        },
    )


class _Host(App[None]):
    def __init__(
        self,
        overrides: Mapping[str, str] | None = None,
        *,
        rules: KeymapRules | None = None,
    ) -> None:
        from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen

        super().__init__()
        self.original = dict(overrides or {})
        self.saved: list[dict[str, str]] = []
        self.save_error: str | None = None
        self.result: bool | None = None
        self.editor = KeybindingEditorScreen(
            rules or _rules(), self.original, _DESCRIPTIONS, apply=self._apply
        )

    def compose(self) -> ComposeResult:
        yield Button("Open editor", id="opener")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "opener":
            event.stop()
            self.push_screen(self.editor, callback=self._done)

    def _apply(self, overrides: Mapping[str, str]) -> str | None:
        self.saved.append(dict(overrides))
        return self.save_error

    def _done(self, result: bool | None) -> None:
        self.result = result


async def _open(app: _Host, pilot: Pilot[None]) -> KeybindingEditorScreen:
    await pilot.press("enter")
    await until(
        pilot,
        lambda: (
            app.screen is app.editor and app.editor.query_one("#keybinding-search", Input).has_focus
        ),
        label="editor search focused",
    )
    return app.editor


async def _type_key(pilot: Pilot[None], value: str, *, stage: bool = True) -> None:
    await pilot.press("f2", "ctrl+shift+a", *value)
    if stage:
        await pilot.press("enter")


def _text(screen: KeybindingEditorScreen, selector: str) -> str:
    return str(screen.query_one(selector, Static).render())


def _selected(screen: KeybindingEditorScreen) -> str | None:
    actions = screen.query_one("#keybinding-actions", OptionList)
    if actions.highlighted is None:
        return None
    return actions.get_option_at_index(actions.highlighted).id


def test_control_keys_cover_fixed_and_inherited_keyboard_controls() -> None:
    from korvid.ui.widgets.keybinding_editor import CONTROL_KEYS, KeybindingEditorScreen

    required = {
        "f1",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "escape",
        "enter",
        "tab",
        "shift+tab",
        "up",
        "down",
        "left",
        "right",
    }
    for widget in (KeybindingEditorScreen, Input, OptionList, Button, VerticalScroll):
        for binding in widget.BINDINGS:
            keys = binding.key if isinstance(binding, Binding) else binding[0]
            required.update(keys.split(","))
    assert isinstance(CONTROL_KEYS, frozenset)
    assert required <= CONTROL_KEYS
    assert KeybindingEditorScreen.CONTROL_KEYS == CONTROL_KEYS


async def test_f1_search_and_f2_key_input_support_integration_keyboard_flow() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        for action, key in (("describe", "ctrl+g"), ("relationships", "ctrl+l")):
            await pilot.press("f1", "ctrl+u", *action, "tab", "enter")
            assert _selected(screen) == action
            await pilot.press("f2", "ctrl+u", *key, "enter")
            assert screen.edit.overrides[action] == key
        await pilot.press("f9", "f10")
        await until(pilot, lambda: app.result is True)
        assert app.saved == [{"describe": "ctrl+g", "relationships": "ctrl+l"}]


async def test_exact_action_search_selects_that_owner_over_substring_matches() -> None:
    app = _Host(rules=KeymapRules(actions={"logs_previous": ("p",), "logs": ("l",)}))
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        assert _selected(screen) == "logs_previous"
        await pilot.press("f1", "ctrl+u", *"logs", "tab", "enter")
        assert _selected(screen) == "logs"
        await _type_key(pilot, "ctrl+g")
        assert screen.edit.overrides == {"logs": "ctrl+g"}


async def test_search_and_selection_show_supplied_metadata_and_original_keys() -> None:
    app = _Host({"logs": "ctrl+l"})
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        details = _text(screen, ".keybinding-details")
        assert "logs" in details
        assert _DESCRIPTIONS["logs"] in details
        assert "core/pods" in details
        assert "Defaults: l" in details
        assert "Original: ctrl+l" in details
        assert "Proposed: ctrl+l" in details

        await pilot.press(*"linked", "enter")
        actions = screen.query_one("#keybinding-actions", OptionList)
        await until(pilot, lambda: actions.has_focus and actions.option_count == 1)
        assert _selected(screen) == "relationships"
        await pilot.press("enter")
        assert screen.query_one("#keybinding-key", Input).has_focus
        await pilot.press("shift+tab")
        assert actions.has_focus
        assert app.saved == []


async def test_arrow_selection_and_empty_search_do_not_stage_or_save() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press("tab", "down")
        await until(pilot, lambda: _selected(screen) == "describe")
        assert "apps/deployments" in _text(screen, ".keybinding-details")
        await pilot.press("up", "shift+tab", *"no such action")
        await until(
            pilot, lambda: screen.query_one("#keybinding-actions", OptionList).option_count == 0
        )
        await pilot.press("f2", "f3", "f4", "f5", "f7", "f9", "f10")
        assert screen.edit.overrides == {}
        assert app.saved == []
        assert app.screen is screen


async def test_conflict_chain_keeps_pending_map_and_guides_hidden_owners() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press(*"workload")
        await _type_key(pilot, "d")
        assert screen.edit.overrides == {"logs": "d"}
        status = _text(screen, "#keybinding-status")
        assert all(part in status for part in ("logs", "describe", "core/pods"))

        await pilot.press("f4")
        await until(pilot, lambda: _selected(screen) == "describe")
        assert screen.query_one("#keybinding-search", Input).value == ""
        assert screen.edit.overrides == {"logs": "d"}
        await _type_key(pilot, "g")
        await pilot.press("f4")
        await until(pilot, lambda: _selected(screen) == "relationships")
        await _type_key(pilot, "l")

        assert screen.edit.overrides == {"logs": "d", "describe": "g", "relationships": "l"}
        assert not screen.edit.conflicts()
        assert len(app.screen_stack) == 2
        assert app.original == {}
        assert app.saved == []


async def test_valid_swap_and_undo_use_the_model_without_saving() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        assert screen.query_one("#keybinding-swap", Button).disabled
        await _type_key(pilot, "d")
        assert not screen.query_one("#keybinding-swap", Button).disabled
        await pilot.press("f5")
        assert screen.edit.overrides == {"logs": "d", "describe": "l"}
        assert not screen.edit.conflicts()
        await pilot.press("f6")
        assert screen.edit.overrides == {"logs": "d"}
        assert screen.edit.can_swap
        await pilot.press("f6", "f5")
        assert not screen.edit.dirty
        assert app.saved == []


async def test_suggestion_stages_the_first_deterministic_model_result() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "d")
        await pilot.press("f4")
        suggestion = screen.edit.suggestions("describe", limit=1)[0]
        await pilot.press("f3")
        assert screen.edit.overrides == {"logs": "d", "describe": suggestion}
        await pilot.press("f6", "f3")
        assert screen.edit.overrides == {"logs": "d", "describe": suggestion}
        assert app.saved == []


async def test_independent_conflicts_can_leave_no_suggestion_or_valid_swap() -> None:
    rules = KeymapRules(
        actions={"first": ("a",), "second": ("b",), "third": ("c",), "fourth": ("d",)}
    )
    app = _Host(rules=rules)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "b")
        await pilot.press("f1", "ctrl+u", *"third", "tab", "enter")
        await _type_key(pilot, "d")
        proposal = screen.edit.overrides
        assert len(screen.edit.conflicts()) == 2
        assert screen.edit.suggestions("third") == ()
        await pilot.press("f3")
        assert "No free-key suggestion" in _text(screen, "#keybinding-status")
        assert screen.edit.overrides == proposal
        await pilot.press("f5")
        assert screen.query_one("#keybinding-swap", Button).disabled
        assert screen.edit.overrides == proposal
        assert app.saved == []


async def test_buttons_support_arrow_navigation_and_enter_without_saving() -> None:
    app = _Host()
    async with app.run_test(size=(46, 16)) as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("tab")
        assert screen.query_one("#keybinding-stage", Button).has_focus
        await pilot.press("right")
        assert screen.query_one("#keybinding-suggest", Button).has_focus
        await pilot.press("enter")
        assert screen.edit.overrides == {}
        await pilot.press("down")
        assert screen.query_one("#keybinding-undo", Button).has_focus
        await pilot.press("enter")
        assert screen.edit.overrides == {"logs": "ctrl+g"}
        await pilot.press("left", "up")
        assert screen.query_one("#keybinding-stage", Button).has_focus
        await pilot.press("enter")
        assert app.saved == []


async def test_disjoint_scopes_allow_key_reuse_and_display_alias_defaults() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press("f1", "ctrl+u", *"snapshot", "tab", "enter")
        details = _text(screen, ".keybinding-details")
        assert "Defaults: shift+s, S" in details
        assert "Original: S" in details
        await pilot.press("f1", "ctrl+u", *"logs", "tab", "enter")
        await _type_key(pilot, "shift+s")
        assert not screen.edit.conflicts()
        assert not screen.edit.can_swap
        assert "Proposed: S" in _text(screen, ".keybinding-details")
        await pilot.press("f9", "f10")
        await until(pilot, lambda: app.result is True)
        assert app.saved == [{"logs": "shift+s"}]


async def test_resetting_an_explicit_default_is_included_in_the_review() -> None:
    app = _Host({"logs": "l"})
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press("f7", "f9")
        assert "logs: l -> l (defaults)" in _text(screen, "#keybinding-preview")
        assert screen.edit.dirty
        await pilot.press("f10")
        await until(pilot, lambda: app.result is True)
        assert app.saved == [{}]


async def test_reset_selected_and_reset_all_are_staged_and_reversible() -> None:
    original = {"logs": "z", "describe": "l"}
    app = _Host(original)
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await pilot.press("f7")
        assert screen.edit.overrides == {"describe": "l"}
        assert screen.edit.conflicts()[0].actions == ("logs", "describe")
        await pilot.press("f5")
        assert screen.edit.overrides == {"describe": "z"}
        await pilot.press("f6", "f6")
        assert screen.edit.overrides == original
        await pilot.press("f8")
        assert screen.edit.overrides == {}
        assert screen.edit.dirty
        await pilot.press("f6")
        assert screen.edit.overrides == original
        assert app.original == original
        assert app.saved == []


@pytest.mark.parametrize(
    ("typed", "canonical"),
    [("ctrl+g", "ctrl+g"), ("?", "question_mark"), ("shift+x", "X"), ("[", "left_square_bracket")],
)
async def test_key_input_accepts_terminal_names_punctuation_and_shifted_keys(
    typed: str, canonical: str
) -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, typed)
        assert screen.edit.overrides == {"logs": typed}
        assert screen.edit.rules.keys("logs", screen.edit.overrides) == (canonical,)
        assert canonical in _text(screen, "#keybinding-preview")
        assert app.saved == []


@pytest.mark.parametrize("typed", ["[bold]bad[/]", "ctrl+w v", "1"])
async def test_planner_errors_are_literal_and_never_change_the_proposal(typed: str) -> None:
    app = _Host({"logs": "z"})
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, typed)
        status = screen.query_one("#keybinding-status", Static)
        assert typed in str(status.render())
        rendered = status.render()
        assert isinstance(rendered, Content)
        assert not rendered.spans
        assert screen.edit.overrides == {"logs": "z"}
        assert not screen.edit.can_undo
        await pilot.press("f9", "f10")
        assert app.saved == []


async def test_review_shows_every_change_and_only_f10_applies_once() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("shift+tab", "down", "enter")
        await _type_key(pilot, "ctrl+d")
        await pilot.press("f9")
        preview = _text(screen, "#keybinding-preview")
        assert all(part in preview for part in ("logs", "l", "ctrl+g", "describe", "d", "ctrl+d"))
        assert not screen.query_one("#keybinding-apply", Button).disabled
        assert app.saved == []
        await pilot.press("enter")
        assert app.saved == []
        await pilot.press("f9", "f10", "f10")
        await until(pilot, lambda: app.result is True)
        assert app.saved == [{"logs": "ctrl+g", "describe": "ctrl+d"}]
        assert app.original == {}
        assert app.query_one("#opener", Button).has_focus


async def test_apply_rejects_unreviewed_and_unresolved_proposals() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("f10")
        assert app.saved == []
        await _type_key(pilot, "d")
        await pilot.press("f9", "f10")
        assert screen.query_one("#keybinding-apply", Button).disabled
        assert "conflict" in _text(screen, "#keybinding-status").lower()
        assert app.saved == []
        assert app.screen is screen


async def test_unsaved_input_invalidates_review_even_when_the_text_is_restored() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("f9")
        assert not screen.query_one("#keybinding-apply", Button).disabled
        await _type_key(pilot, "ctrl+k", stage=False)
        await pilot.press("f10")
        assert screen.query_one("#keybinding-apply", Button).disabled
        assert app.saved == []
        await _type_key(pilot, "ctrl+g", stage=False)
        await pilot.press("f9", "f10")
        assert app.saved == []
        await pilot.press("enter", "f10")
        assert app.saved == []
        await pilot.press("f9", "f10")
        await until(pilot, lambda: app.result is True)
        assert app.saved == [{"logs": "ctrl+g"}]


async def test_restoring_input_before_change_messages_arrive_still_invalidates_review() -> None:
    app = _Host()
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("f9")
        key_input = screen.query_one("#keybinding-key", Input)
        key_input.value = "ctrl+k"
        key_input.value = "ctrl+g"
        screen.action_apply()
        assert app.saved == []
        assert app.screen is screen
        await pilot.press("f9", "f10")
        assert app.saved == []
        await pilot.press("enter", "f9", "f10")
        await until(pilot, lambda: app.result is True)
        assert app.saved == [{"logs": "ctrl+g"}]


@pytest.mark.parametrize("control", ["f3", "f6", "f7", "f8"])
async def test_proposal_operations_invalidate_review(control: str) -> None:
    app = _Host({"logs": "z"})
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("f9", control, "f10")
        assert screen.query_one("#keybinding-apply", Button).disabled
        assert app.saved == []


@pytest.mark.parametrize("change_after_review", [False, True])
async def test_validator_warnings_block_apply_without_pairwise_conflicts(
    change_after_review: bool,
) -> None:
    reserved = {"1": "namespace slot"}
    app = _Host(rules=replace(_rules(), reserved_keys=reserved))
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        if change_after_review:
            await pilot.press("f9")
        reserved["ctrl+g"] = "new fixed control"
        if not change_after_review:
            await pilot.press("f9")
        await pilot.press("f10")
        assert not screen.edit.conflicts()
        assert "new fixed control" in _text(screen, "#keybinding-status")
        assert screen.query_one("#keybinding-apply", Button).disabled
        assert app.saved == []


async def test_callback_failure_preserves_baseline_and_requires_a_new_confirmation() -> None:
    app = _Host()
    app.save_error = "[red]Could not persist changes[/]"
    async with app.run_test() as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("f9", "f10", "f10")
        assert app.saved == [{"logs": "ctrl+g"}]
        assert app.save_error in _text(screen, "#keybinding-status")
        assert app.screen is screen
        assert screen.edit.overrides == {"logs": "ctrl+g"}
        assert screen.edit.changes()[0].before == ("l",)
        assert screen.edit.can_undo
        assert app.original == {}
        app.save_error = None
        await pilot.press("f9", "f10")
        await until(pilot, lambda: app.result is True)
        assert len(app.saved) == 2


@pytest.mark.parametrize("review", [False, True])
async def test_escape_discards_pending_changes_and_restores_original_focus(review: bool) -> None:
    app = _Host({"logs": "z"})
    async with app.run_test() as pilot:
        await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        if review:
            await pilot.press("f9")
        await pilot.press("escape")
        await until(pilot, lambda: app.result is False)
        assert app.saved == []
        assert app.original == {"logs": "z"}
        assert app.query_one("#opener", Button).has_focus


@pytest.mark.parametrize("size", [(46, 16), (32, 12)])
async def test_small_terminal_keeps_enabled_controls_keyboard_reachable(
    size: tuple[int, int],
) -> None:
    app = _Host({"logs": "z"})
    async with app.run_test(size=size) as pilot:
        screen = await _open(app, pilot)
        await _type_key(pilot, "ctrl+g")
        await pilot.press("f9", "f2", "shift+tab", "shift+tab")
        assert screen.query_one("#keybinding-search", Input).has_focus
        body = screen.query_one(VerticalScroll)
        controls = [widget for widget in screen.focus_chain if widget.id]
        for control in controls[1:]:
            await pilot.press("tab")

            def is_reachable(control: Widget = control) -> bool:
                return control.has_focus and body.content_region.overlaps(control.region)

            await until(
                pilot,
                is_reachable,
                label=f"{control.id} reachable in the scroll body",
            )
        assert screen.query_one("#keybinding-cancel", Button).has_focus
        assert body.size.height <= int(size[1] * 0.8)
        assert body.scroll_y > 0
        await pilot.press("shift+tab")
        assert screen.query_one("#keybinding-apply", Button).has_focus
        await pilot.press("enter")
        assert app.saved == []
        await pilot.press("f10")
        await until(pilot, lambda: app.result is True)
        assert app.saved == [{"logs": "ctrl+g"}]
