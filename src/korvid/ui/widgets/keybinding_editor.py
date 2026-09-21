"""A staged, keyboard-accessible keymap editor with explicit review and apply."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from korvid.core.keymap_edit import KeymapEdit, KeymapRules

_EDITOR_BINDINGS: list[BindingType] = [
    Binding("f1", "focus_search", "Search"),
    Binding("f2", "focus_key", "Edit key"),
    Binding("f3", "suggest_key", "Suggest"),
    Binding("f4", "next_conflict", "Next owner"),
    Binding("f5", "swap_keys", "Swap"),
    Binding("f6", "undo", "Undo"),
    Binding("f7", "reset_selected", "Reset action"),
    Binding("f8", "reset_all", "Reset all"),
    Binding("f9", "review", "Review"),
    Binding("f10", "apply", "Apply"),
    Binding("escape", "cancel", "Cancel"),
]

CONTROL_KEYS = frozenset({"tab", "shift+tab", "backtab"}) | frozenset(
    key
    for binding in (
        *_EDITOR_BINDINGS,
        *ModalScreen.BINDINGS,
        *Input.BINDINGS,
        *OptionList.BINDINGS,
        *Button.BINDINGS,
        *VerticalScroll.BINDINGS,
    )
    for key in (binding.key if isinstance(binding, Binding) else binding[0]).split(",")
)


def _scope(contexts: frozenset[tuple[str, str]]) -> str:
    return ", ".join(f"{group or 'core'}/{resource}" for group, resource in sorted(contexts)) or (
        "global / unspecified"
    )


class KeybindingEditorScreen(ModalScreen[bool]):
    """Edit an isolated proposal and apply it only after a separate review.

    Args:
        rules: The same binding metadata and validation policy used at startup.
        overrides: The original override section, copied by the edit session.
        descriptions: Display descriptions indexed by the rules' action names.
        apply: Synchronous persistence callback. Return None on success, or a
            safe error string on failure. Only F10 invokes this callback.
        cleanup_required: Whether startup rejected persisted keybinding entries.
    """

    CONTROL_KEYS: ClassVar[frozenset[str]] = CONTROL_KEYS
    BINDINGS: ClassVar[list[BindingType]] = _EDITOR_BINDINGS

    DEFAULT_CSS = """
    KeybindingEditorScreen {
        align: center middle;
    }
    KeybindingEditorScreen .keybinding-body {
        width: 88;
        max-width: 96%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    KeybindingEditorScreen .keybinding-title {
        text-style: bold;
    }
    KeybindingEditorScreen .keybinding-hint {
        color: $text-muted;
    }
    KeybindingEditorScreen .keybinding-actions {
        height: 7;
    }
    KeybindingEditorScreen .keybinding-details,
    KeybindingEditorScreen .keybinding-status,
    KeybindingEditorScreen .keybinding-preview {
        height: auto;
        margin-top: 1;
    }
    KeybindingEditorScreen .keybinding-controls {
        grid-size: 2;
        grid-rows: 3;
        grid-gutter: 0 1;
        height: 15;
        margin-top: 1;
    }
    KeybindingEditorScreen Button {
        width: 1fr;
        min-width: 0;
    }
    """

    def __init__(
        self,
        rules: KeymapRules,
        overrides: Mapping[str, str],
        descriptions: Mapping[str, str],
        *,
        apply: Callable[[Mapping[str, str]], str | None],
        cleanup_required: bool = False,
    ) -> None:
        super().__init__()
        self.edit = KeymapEdit(rules, overrides, cleanup_required=cleanup_required)
        self._original = self.edit.overrides
        self._descriptions = dict(descriptions)
        self._apply_callback = apply
        self._apply_error: str | None = None
        self._selected_action: str | None = None
        self._reviewed: dict[str, str] | None = None
        self._input_dirty = False
        self._message = "Enter stages only. Review with F9, then explicitly apply with F10."

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="keybinding-body"):
            yield Static("Keybinding editor", classes="keybinding-title", markup=False)
            yield Static(
                "F1 search · F2 key · Enter stages · F9 review · F10 apply · Esc cancel",
                classes="keybinding-hint",
                markup=False,
            )
            yield Input(placeholder="Search actions or descriptions (F1)", id="keybinding-search")
            yield OptionList(id="keybinding-actions", classes="keybinding-actions")
            yield Static(classes="keybinding-details", markup=False)
            yield Input(
                placeholder="Key name, e.g. ctrl+g, ?, shift+g (F2)",
                id="keybinding-key",
                select_on_focus=True,
            )
            yield Static(id="keybinding-status", classes="keybinding-status", markup=False)
            yield Static(id="keybinding-preview", classes="keybinding-preview", markup=False)
            with Grid(classes="keybinding-controls"):
                yield Button("Enter Stage", id="keybinding-stage")
                yield Button("F3 Suggest", id="keybinding-suggest")
                yield Button("F4 Next owner", id="keybinding-next")
                yield Button("F5 Swap", id="keybinding-swap")
                yield Button("F6 Undo", id="keybinding-undo")
                yield Button("F7 Reset", id="keybinding-reset")
                yield Button("F8 Reset all", id="keybinding-reset-all")
                yield Button("F9 Review", id="keybinding-review")
                yield Button("F10 Apply", id="keybinding-apply", variant="primary")
                yield Button("Esc Cancel", id="keybinding-cancel")

    def on_mount(self) -> None:
        self._filter_actions("")
        self.watch(
            self.query_one("#keybinding-key", Input), "value", self._key_value_changed, init=False
        )
        self.action_focus_search()

    @on(Input.Changed, "#keybinding-search")
    def _search_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._filter_actions(event.value)

    @on(Input.Changed, "#keybinding-key")
    def _key_changed(self, event: Input.Changed) -> None:
        event.stop()
        if self._input_dirty:
            self._refresh("Unstaged key input. Press Enter to stage it, then F9 to review again.")

    def _key_value_changed(self, value: str) -> None:
        action = self._selected_action
        if action is None or value == self.edit.rules.keys(action, self.edit.overrides)[0]:
            return
        self._input_dirty = True
        self._reviewed = None

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "keybinding-key":
            self.action_stage_key()
        else:
            self.query_one("#keybinding-actions", OptionList).focus()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        self._selected_action = event.option.id
        self._fill_key()
        self._refresh()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self._selected_action = event.option.id
        self._fill_key()
        self._refresh()
        self.action_focus_key()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        actions = {
            "keybinding-stage": self.action_stage_key,
            "keybinding-suggest": self.action_suggest_key,
            "keybinding-next": self.action_next_conflict,
            "keybinding-swap": self.action_swap_keys,
            "keybinding-undo": self.action_undo,
            "keybinding-reset": self.action_reset_selected,
            "keybinding-reset-all": self.action_reset_all,
            "keybinding-review": self.action_review,
            "keybinding-apply": self.action_confirm_hint,
            "keybinding-cancel": self.action_cancel,
        }
        action = actions.get(event.button.id or "")
        if action is not None:
            action()

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.focused, Button) and event.key in {"up", "down", "left", "right"}:
            event.stop()
            event.prevent_default()
            if event.key in {"down", "right"}:
                self.focus_next("Button")
            else:
                self.focus_previous("Button")

    def action_focus_search(self) -> None:
        """Focus the action search without modifying the proposal."""
        search = self.query_one("#keybinding-search", Input)
        search.focus()
        search.action_select_all()

    def action_focus_key(self) -> None:
        """Focus and select the key name for replacement."""
        key_input = self.query_one("#keybinding-key", Input)
        if not key_input.disabled:
            key_input.focus()
            key_input.action_select_all()

    def action_stage_key(self) -> None:
        """Stage the typed key through the pure model, without saving."""
        if self._selected_action is None:
            return
        self._reviewed = None
        try:
            self.edit.assign(self._selected_action, self.query_one("#keybinding-key", Input).value)
        except ValueError as error:
            self._refresh(str(error))
            return
        self._proposal_changed("Key staged. Resolve any conflicts, then review with F9.")

    def action_suggest_key(self) -> None:
        """Stage the model's first deterministic valid suggestion."""
        if self._selected_action is None:
            return
        suggestions = self.edit.suggestions(self._selected_action, limit=1)
        if not suggestions:
            self._refresh("No free-key suggestion is available for the complete pending map.")
            return
        self.edit.assign(self._selected_action, suggestions[0])
        self._proposal_changed(f"Suggested key staged: {suggestions[0]}")

    def action_next_conflict(self) -> None:
        """Select the next conflicting owner without discarding the pending map."""
        owners = tuple(
            dict.fromkeys(owner for conflict in self.edit.conflicts() for owner in conflict.actions)
        )
        if not owners:
            self._refresh("No conflicting owners in the pending map.")
            return
        index = owners.index(self._selected_action) + 1 if self._selected_action in owners else 0
        self._selected_action = owners[index % len(owners)]
        with self.prevent(Input.Changed):
            self.query_one("#keybinding-search", Input).value = ""
        self._filter_actions("")
        self.query_one("#keybinding-actions", OptionList).focus()
        self._refresh(f"Resolve {self._selected_action}; all pending assignments are retained.")

    def action_swap_keys(self) -> None:
        """Perform only a complete valid swap offered by the model."""
        if self.edit.can_swap and self.edit.swap():
            self._proposal_changed("Swap staged. Review with F9 before applying.")
        else:
            self._refresh("No valid two-way swap is available.")

    def action_undo(self) -> None:
        """Restore the preceding proposal and conflict decision."""
        if self.edit.undo():
            self._proposal_changed("Last proposal change undone.")
        else:
            self._refresh("No staged change to undo.")

    def action_reset_selected(self) -> None:
        """Stage the selected action's defaults, retaining any conflicts."""
        if self._selected_action is not None:
            self.edit.reset(self._selected_action)
            self._proposal_changed("Selected action reset to its defaults in the pending map.")

    def action_reset_all(self) -> None:
        """Stage removal of all overrides without touching live configuration."""
        self.edit.reset_all()
        self._proposal_changed("All overrides reset in the pending map. F6 undoes this reset.")

    def action_review(self) -> None:
        """Review the entire proposal, never invoking the apply callback."""
        self._reviewed = None
        problem = self._review_problem()
        if problem is not None:
            self._refresh(problem)
            return
        self._reviewed = self.edit.overrides
        self._apply_error = None
        self._refresh("Review complete. Only F10 applies these changes; Escape cancels.")
        self.query_one("#keybinding-preview", Static).scroll_visible()

    def action_confirm_hint(self) -> None:
        """Keep Enter non-destructive even when the Apply control has focus."""
        self._refresh("Press F10 to explicitly apply the reviewed changes. Enter never saves.")

    def action_apply(self) -> None:
        """Consume one fresh review and synchronously invoke the save callback."""
        problem = self._review_problem()
        proposal = self.edit.overrides
        if problem is not None or self._reviewed is None or self._reviewed != proposal:
            self._reviewed = None
            self._refresh(problem or "Review the current pending map with F9 before applying.")
            return
        self._reviewed = None
        self._refresh("Applying the reviewed changes.")
        error = self._apply_callback(proposal)
        if error is None:
            self.dismiss(True)
        else:
            self._apply_error = error
            self._refresh("Apply failed. Review with F9 before retrying.")

    def action_cancel(self) -> None:
        """Dismiss without applying any staged or typed changes."""
        self.dismiss(False)

    def _filter_actions(self, query: str) -> None:
        query = query.strip().casefold()
        matches = [
            action
            for action in self.edit.rules.actions
            if query in f"{action} {self._descriptions.get(action, '')}".casefold()
        ]
        preferred = next(
            (action for action in matches if action.casefold() == query), self._selected_action
        )
        self._selected_action = preferred if preferred in matches else next(iter(matches), None)
        actions = self.query_one("#keybinding-actions", OptionList)
        with self.prevent(OptionList.OptionHighlighted):
            actions.clear_options()
            actions.add_options(
                Option(Text(f"{action} — {self._descriptions.get(action, '')}"), id=action)
                for action in matches
            )
            actions.highlighted = (
                matches.index(self._selected_action) if self._selected_action is not None else None
            )
        self._fill_key()
        self._refresh()

    def _fill_key(self) -> None:
        key_input = self.query_one("#keybinding-key", Input)
        key_input.disabled = self._selected_action is None
        with self.prevent(Input.Changed):
            key_input.value = (
                self.edit.rules.keys(self._selected_action, self.edit.overrides)[0]
                if self._selected_action is not None
                else ""
            )
        self._input_dirty = False

    def _proposal_changed(self, message: str) -> None:
        self._reviewed = None
        self._apply_error = None
        self._fill_key()
        self._refresh(message)

    def _review_problem(self) -> str | None:
        if self._input_dirty:
            return "Stage the key input with Enter before reviewing or applying."
        if self.edit.conflicts():
            return "Resolve all pending conflicts before applying."
        if self.edit.rules.plan(self.edit.overrides).warnings:
            return "Resolve validation warnings before applying."
        if not self.edit.dirty:
            return "No pending changes to apply."
        return None

    def _refresh(self, message: str | None = None) -> None:
        if message is not None:
            self._message = message
        self._render_details()
        self._render_preview()
        status = [self._message]
        if self._apply_error is not None:
            status.append(f"Apply error: {self._apply_error}")
        status.extend(
            f"Conflict: {conflict.key} — {' / '.join(conflict.actions)}; scope: {_scope(conflict.contexts)}"
            for conflict in self.edit.conflicts()
        )
        status.extend(self.edit.rules.plan(self.edit.overrides).warnings)
        self.query_one("#keybinding-status", Static).update("\n".join(status))
        self._update_controls()

    def _render_details(self) -> None:
        action = self._selected_action
        details = "No matching actions. Change the search with F1."
        if action is not None:
            rules = self.edit.rules
            details = "\n".join(
                (
                    f"Action: {action} — {self._descriptions.get(action, '')}",
                    f"Scope: {_scope(rules.action_contexts.get(action, frozenset()))}",
                    f"Defaults: {', '.join(rules.actions[action])}",
                    f"Original: {', '.join(rules.keys(action, self._original))}",
                    f"Proposed: {', '.join(rules.keys(action, self.edit.overrides))}",
                )
            )
        self.query_one(".keybinding-details", Static).update(details)

    def _render_preview(self) -> None:
        heading = (
            "Reviewed changes (F10 applies):" if self._reviewed is not None else "Pending changes:"
        )
        proposal = self.edit.overrides
        lines = [heading]
        if self.edit.cleanup_pending:
            lines.append("Remove rejected persisted keybindings.")
        for change in self.edit.changes():
            defaults = " (defaults)" if change.action not in proposal else ""
            lines.append(
                f"{change.action}: {', '.join(change.before)} -> {', '.join(change.after)}{defaults}"
            )
        if len(lines) == 1:
            lines.append("No changes.")
        self.query_one("#keybinding-preview", Static).update("\n".join(lines))

    def _update_controls(self) -> None:
        disabled = {
            "stage": self._selected_action is None,
            "suggest": self._selected_action is None,
            "next": not self.edit.conflicts(),
            "swap": not self.edit.can_swap,
            "undo": not self.edit.can_undo,
            "reset": self._selected_action is None,
            "reset-all": not self.edit.can_reset_all,
            "review": not self.edit.dirty,
            "apply": self._reviewed is None or self._review_problem() is not None,
        }
        for control, is_disabled in disabled.items():
            self.query_one(f"#keybinding-{control}", Button).disabled = is_disabled
