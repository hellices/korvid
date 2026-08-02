"""Grouped, collapsible top bar with the corvid mark (issue #142).

Replaces the stock Textual ``Footer``: the flat run of uniform keys never
registered visually, so the view-scoped legend (issue #114) delivered
little value. The bar renders labeled groups with styled key caps and a
compact logo, in two modes:

- **collapsed** (default): one line — logo + view name + the highest
  priority keys for this view + a "more" hint,
- **expanded**: the full grouped legend.

Which actions are visible stays decided exclusively by the app's
``check_action`` / ``_ACTION_VIEWS`` (the entries arrive pre-filtered from
``screen.active_bindings``); this module only classifies and presents.
Group structure is fixed, members swap per view.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Static

#: Compact brand mark: the raven from widgets/logo.py, bar-sized.
LOGO_MARK = "( o> korvid"

#: Below this terminal width the expanded legend cannot fit legibly: the
#: bar renders collapsed regardless of the user's toggle.
MIN_EXPANDED_WIDTH = 80

#: Fixed group structure (issue #142 decision); members change per view.
GROUP_ORDER: tuple[str, ...] = ("Nav", "Sort", "Actions", "Logs", "Panes", "Agent")

#: action id -> group; anything unlisted lands in Actions.
_ACTION_GROUPS: dict[str, str] = {
    "quit": "Nav",
    "help": "Nav",
    "open_command": "Nav",
    "open_filter": "Nav",
    "toggle_all_namespaces": "Nav",
    "toggle_topbar": "Nav",
    "sort_by_age": "Sort",
    "sort_by_cpu": "Sort",
    "sort_by_mem": "Sort",
    "sort_picker": "Sort",
    "log_format": "Logs",
    "log_wrap": "Logs",
    "log_timestamps": "Logs",
    "log_save": "Logs",
    "log_previous": "Logs",
    "log_search_next": "Logs",
    "log_search_prev": "Logs",
    "toggle_agent": "Agent",
}

#: Collapsed-mode priority (issue #142 decision: fixed, deterministic):
#: the first entries of this list that are active in the current view are
#: the "3-4 most relevant keys". View-specific verbs outrank generic ones.
PRIORITY_ACTIONS: tuple[str, ...] = (
    "describe",
    "logs",
    "shell",
    "helm_install",
    "helm_upgrade",
    "helm_rollback",
    "operator_install",
    "cordon_node",
    "drain_node",
    "rollout_restart",
    "scale_resource",
    "port_forward",
    "transfer",
    "hint_details",
    "open_filter",
    "open_command",
)

#: How many keys the collapsed line shows.
COLLAPSED_KEYS = 4

#: Handler keys (no Binding object) the bar still advertises: Enter/Escape
#: drive drilling, ctrl+w drives the pane chord. Presentation only — the
#: help overlay stays the exhaustive reference.
_STATIC_ENTRIES: tuple[tuple[str, str, str], ...] = (
    ("Nav", "enter", "drill"),
    ("Nav", "esc", "back"),
    ("Panes", "ctrl+w", "v/w/q split"),
)


@dataclass(frozen=True)
class KeyEntry:
    """One visible binding, pre-filtered by the app's check_action."""

    key: str  # display form, e.g. "d" or "ctrl+a"
    action: str  # base action id (no --alt suffix, no parameters)
    description: str


def group_of(action: str) -> str:
    """The fixed group an action renders under; unlisted -> Actions."""
    return _ACTION_GROUPS.get(action, "Actions")


def collapsed_entries(entries: list[KeyEntry], limit: int = COLLAPSED_KEYS) -> list[KeyEntry]:
    """The highest-priority *limit* entries for the collapsed line.

    Priority order is the fixed PRIORITY_ACTIONS list — deterministic per
    view; entries not listed there never make the collapsed cut.
    """
    by_action = {entry.action: entry for entry in entries}
    picked = [by_action[a] for a in PRIORITY_ACTIONS if a in by_action]
    return picked[:limit]


def _append_key(text: Text, key: str, description: str) -> None:
    text.append(f" {key} ", style="reverse")
    text.append(f" {description}", style="dim")


def build_collapsed(view: str, entries: list[KeyEntry], toggle_key: str) -> Text:
    """One-line bar: logo, view, top keys, more-hint."""
    text = Text()
    text.append(LOGO_MARK, style="bold cyan")
    text.append("  ")
    text.append(view, style="bold")
    for entry in collapsed_entries(entries):
        text.append("  ")
        _append_key(text, entry.key, entry.description)
    text.append("  ")
    text.append(f" {toggle_key} ", style="reverse dim")
    text.append(" more", style="dim")
    return text


def build_expanded(view: str, entries: list[KeyEntry]) -> Text:
    """The full grouped legend: fixed group order, per-view members."""
    groups: dict[str, list[KeyEntry]] = {}
    for entry in entries:
        groups.setdefault(group_of(entry.action), []).append(entry)
    text = Text()
    text.append(LOGO_MARK, style="bold cyan")
    text.append("  ")
    text.append(view, style="bold")
    for name in GROUP_ORDER:
        members = groups.get(name, [])
        statics = [(k, d) for g, k, d in _STATIC_ENTRIES if g == name]
        if not members and not statics:
            continue
        text.append("  │  ", style="dim")
        text.append(name, style="bold underline")
        for key, description in statics:
            text.append(" ")
            _append_key(text, key, description)
        for entry in members:
            text.append(" ")
            _append_key(text, entry.key, entry.description)
    return text


def build_legend(
    view: str, entries: list[KeyEntry], *, expanded: bool, width: int, toggle_key: str
) -> Text:
    """The bar content for one render: expanded only when toggled *and*
    the terminal is wide enough (narrow terminals collapse automatically)."""
    if expanded and width >= MIN_EXPANDED_WIDTH:
        return build_expanded(view, entries)
    return build_collapsed(view, entries, toggle_key)


class TopBar(Static):
    """The custom top bar; the app feeds it view name + active entries."""

    DEFAULT_CSS = """
    TopBar {
        dock: top;
        width: 100%;
        height: auto;
        background: $panel;
        padding: 0 1;
    }
    """

    expanded: bool = False
    _view: str = ""
    _toggle_key: str = "~"

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._entries: list[KeyEntry] = []

    def update_legend(
        self, view: str, entries: list[KeyEntry], *, expanded: bool, toggle_key: str
    ) -> None:
        self._view = view
        self._entries = list(entries)
        self.expanded = expanded
        self._toggle_key = toggle_key
        self._render_bar()

    def _render_bar(self) -> None:
        width = self.size.width or MIN_EXPANDED_WIDTH
        self.update(
            build_legend(
                self._view,
                self._entries,
                expanded=self.expanded,
                width=width,
                toggle_key=self._toggle_key,
            )
        )

    def on_resize(self) -> None:
        # Auto-collapse below the width threshold (and re-expand above it).
        self._render_bar()
