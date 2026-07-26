"""Help overlay (issue #41) — keybindings and ``:`` commands, discoverable via ``?``.

The content is generated from the **actual** `Binding` lists and the command
grammar helpers, never from a hand-maintained table, so it cannot drift from
the real key handling.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

# Friendly labels for Textual key names that would read poorly verbatim.
_KEY_NAMES = {
    "question_mark": "?",
    "colon": ":",
    "slash": "/",
    "escape": "Esc",
    "enter": "Enter",
    "space": "Space",
    "tab": "Tab",
}

# Display order of the binding groups in the overlay.
_GROUP_ORDER = ("Global", "Table", "Logs", "Describe", "Agent")

# Context(s) where each app action's key is actually pressed.  Key names and
# descriptions are still generated from the live Binding objects; only the
# grouping needs human context.  A test asserts every KorvidApp binding
# action appears here, so new bindings cannot land unclassified.
_ACTION_GROUPS: dict[str, tuple[str, ...]] = {
    "quit": ("Global",),
    "help": ("Global",),
    "open_command": ("Global",),
    "toggle_all_namespaces": ("Global",),
    # `/` filters the table but searches inside the log / describe panes.
    "open_filter": ("Table", "Logs"),
    "describe": ("Table",),
    "shell": ("Table",),
    "logs": ("Table",),
    "logs_multi": ("Table",),
    "delete_resource": ("Table",),
    "rollout_restart": ("Table",),
    "resize_pod": ("Table",),
    "operator_install": ("Table",),
    "scale_resource": ("Table",),
    "edit_resource": ("Table",),
    "hint_details": ("Table",),
    "log_format": ("Logs",),
    "log_wrap": ("Logs",),
    "log_timestamps": ("Logs",),
    "log_save": ("Logs",),
    "log_previous": ("Logs",),
    "log_search_next": ("Logs",),
    "log_search_prev": ("Logs",),
    "toggle_agent": ("Agent",),
}


def key_label(key: str) -> str:
    """Human-friendly label for a Textual key name (``ctrl+s`` → ``Ctrl-S``)."""
    parts = key.split("+")
    plain = parts[-1]
    plain = _KEY_NAMES.get(plain, plain)
    modifiers = [m.capitalize() for m in parts[:-1]]
    if modifiers:
        return "-".join([*modifiers, plain.upper() if len(plain) == 1 else plain])
    return plain


def _groups_for_action(action: str) -> tuple[str, ...]:
    return _ACTION_GROUPS.get(action, ("Global",))


def _as_binding(binding: BindingType) -> Binding:
    if isinstance(binding, Binding):
        return binding
    key, action, *rest = binding
    return Binding(key, action, rest[0] if rest else "")


def collect_help(
    app_bindings: Sequence[BindingType],
    describe_bindings: Sequence[BindingType],
    handler_keys: Sequence[tuple[str, str, str]] = (),
) -> list[tuple[str, list[tuple[str, str]]]]:
    """Group ``(key, description)`` rows from real bindings for the overlay.

    App bindings are grouped by the context where the key is pressed (see
    `_ACTION_GROUPS`; multi-context keys like ``/`` appear in each group);
    the describe screen's own bindings all land in the Describe group.
    Alternate keys bound to the same action (e.g. ``shift+l`` and ``L``)
    merge into one row under the first key encountered, and hidden
    (``show=False``) bindings are included on purpose — they are exactly the
    discoverability gap.

    `handler_keys` covers user-facing keys handled in event handlers rather
    than ``BINDINGS`` (e.g. Enter drill-down, Esc close/pop); each entry is
    ``(group, key, description)``.
    """
    groups: dict[str, list[tuple[str, str]]] = {name: [] for name in _GROUP_ORDER}
    seen_actions: set[tuple[str, str]] = set()

    def _add(group: str, binding: Binding) -> None:
        marker = (group, binding.action)
        if marker in seen_actions:
            return
        seen_actions.add(marker)
        groups[group].append((key_label(binding.key), binding.description))

    for raw in app_bindings:
        binding = _as_binding(raw)
        for group in _groups_for_action(binding.action):
            _add(group, binding)
    for raw in describe_bindings:
        _add("Describe", _as_binding(raw))
    for group, key, description in handler_keys:
        groups[group].append((key_label(key), description))

    return [(name, groups[name]) for name in _GROUP_ORDER if groups[name]]


class HelpScreen(ModalScreen[None]):
    """Modal overlay listing keybindings by context plus ``:`` commands."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close", show=False),
        Binding("question_mark", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen VerticalScroll {
        width: 70;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    """

    def __init__(
        self,
        groups: list[tuple[str, list[tuple[str, str]]]],
        commands: list[tuple[str, str]],
    ) -> None:
        super().__init__()
        self._groups = groups
        self._commands = commands

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(Text(self.body_text()), id="help-body")

    def body_text(self) -> str:
        """Plain-text rendering of the overlay content (also used by tests)."""
        sections: list[str] = ["korvid — help  (Esc/q/? to close)", ""]
        key_width = 10
        for name, entries in self._groups:
            sections.append(name)
            sections.extend(f"  {key:<{key_width}} {desc}" for key, desc in entries)
            sections.append("")
        sections.append("Commands")
        sections.extend(f"  {cmd:<{key_width}} {desc}" for cmd, desc in self._commands)
        return "\n".join(sections)
