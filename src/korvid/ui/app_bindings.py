"""Declarative bindings and CSS for the Textual application shell."""

from __future__ import annotations

from textual.binding import Binding, BindingType

# Every remappable binding carries an ``id`` so the `keybindings:` config
# section can remap it via Textual's keymap (issue #35); the intentionally
# fixed favorite-namespace shortcuts are the sole exception. Uppercase
# duplicates of shift+<letter> keys share the action under an ``--alt`` id.
APP_BINDINGS: list[Binding | tuple[str, str] | tuple[str, str, str]] = [
    Binding("q", "quit", "Quit", id="quit"),
    Binding("question_mark", "help", "Help", id="help"),
    # Top bar collapse/expand (issue #142): the grouped legend's toggle.
    Binding("tilde", "toggle_topbar", "Legend", show=False, id="toggle_topbar"),
    Binding("colon", "open_command", "Command", id="open_command"),
    Binding("slash", "open_filter", "Filter/Search", id="open_filter"),
    Binding("0", "toggle_all_namespaces", "All NS", id="toggle_all_namespaces"),
    # `favorite_namespaces` shortcuts (issue #108): UI-only jumps, bound
    # in config order. They deliberately have no keymap id because 1-9 are
    # reserved for this feature. Hidden from the footer; the help overlay
    # merges the nine bindings into a single row.
    *[
        Binding(
            str(i),
            f"favorite_namespace({i})",
            "Jump to favorite namespace (1-9)",
            show=False,
        )
        for i in range(1, 10)
    ],
    Binding("d", "describe", "Describe", id="describe"),
    Binding("g", "relationships", "Relationships", id="relationships"),
    Binding("T", "timeline", "Timeline", id="timeline"),
    Binding("s", "shell", "Shell", id="shell"),
    Binding("l", "logs", "Logs", id="logs"),
    Binding("shift+l", "logs_multi", "Multi-log", id="logs_multi"),
    # Real terminals deliver Shift+<letter> as the uppercase character,
    # not "shift+x"; bind both so the shortcut works outside Pilot tests.
    Binding("L", "logs_multi", "Multi-log", show=False, id="logs_multi--alt"),
    Binding("f", "log_format", "JSON/raw", id="log_format"),
    Binding("w", "log_wrap", "Wrap", show=False, id="log_wrap"),
    Binding("t", "log_timestamps", "Timestamps", show=False, id="log_timestamps"),
    Binding("ctrl+s", "log_save", "Save logs", show=False, id="log_save"),
    Binding("p", "log_previous", "Prev logs", id="log_previous"),
    Binding("n", "log_search_next", "Next hit", id="log_search_next"),
    Binding("shift+n", "log_search_prev", "Prev hit / Sort name", id="log_search_prev"),
    Binding("N", "log_search_prev", "Prev hit / Sort name", show=False, id="log_search_prev--alt"),
    # Column sorting (issue #37); shift+n doubles as sort-by-name when
    # no search pane is open (see action_log_search_prev).
    Binding("shift+a", "sort_by_age", "Sort age", show=False, id="sort_by_age"),
    Binding("A", "sort_by_age", "Sort age", show=False, id="sort_by_age--alt"),
    Binding("shift+c", "sort_by_cpu", "Sort CPU", show=False, id="sort_by_cpu"),
    Binding("C", "sort_by_cpu", "Sort CPU", show=False, id="sort_by_cpu--alt"),
    Binding("shift+m", "sort_by_mem", "Sort MEM", show=False, id="sort_by_mem"),
    Binding("M", "sort_by_mem", "Sort MEM", show=False, id="sort_by_mem--alt"),
    # Interactive column picker (issue #138): every sortable column of
    # the current view, no exact names to remember.
    Binding("o", "sort_picker", "Sort by column", show=False, id="sort_picker"),
    Binding("ctrl+a", "toggle_agent", "AI", priority=True, id="toggle_agent"),
    Binding(
        "ctrl+x",
        "interrupt_agent",
        "Stop agent",
        priority=True,
        show=False,
        id="interrupt_agent",
    ),
    Binding("ctrl+d", "delete_resource", "Delete", id="delete_resource"),
    Binding("r", "rollout_restart", "Restart", id="rollout_restart"),
    Binding(
        "R",
        "resize_pod",
        "Resize pod CPU/memory in place (K8s 1.35+)",
        show=False,
        id="resize_pod",
    ),
    Binding("S", "scale_resource", "Scale", id="scale_resource"),
    Binding("e", "edit_resource", "Edit", show=False, id="edit_resource"),
    Binding("i", "hint_details", "Hint details", show=False, id="hint_details"),
    Binding(
        "I",
        "operator_install",
        "Install operator / approve InstallPlan",
        id="operator_install",
    ),
    # Real terminals deliver Shift+F as "F" (see shift+l above).
    Binding("shift+f", "port_forward", "Port-forward", id="port_forward"),
    Binding("F", "port_forward", "Port-forward", show=False, id="port_forward--alt"),
    # Node ops (issue #40): cordon / uncordon / drain, nodes view only.
    Binding("c", "cordon_node", "Cordon", id="cordon_node"),
    Binding("u", "uncordon_node", "Uncordon", id="uncordon_node"),
    Binding("shift+d", "drain_node", "Drain", id="drain_node"),
    Binding("D", "drain_node", "Drain", show=False, id="drain_node--alt"),
    # Helm ops (issues #31/#114): dedicated per-view bindings so the
    # overloaded i/u/r keys carry the right footer label and remain
    # independently remappable; `check_action` routes each key to the
    # binding whose view is on screen.
    Binding("i", "helm_install", "Install chart", id="helm_install"),
    Binding("u", "helm_upgrade", "Upgrade", id="helm_upgrade"),
    Binding("r", "helm_rollback", "Rollback", id="helm_rollback"),
    # Revision history moved off Enter (issue #120): Enter opens the
    # hierarchy tree, `h` keeps the flat history drill-down.
    Binding("h", "helm_history", "History", id="helm_history"),
    Binding("ctrl+t", "transfer", "Transfer", show=False, id="transfer"),
]

# Display order of the binding groups in the help overlay (issue #41) and,
# by extension, the Action Palette's categories (issue #388): both render
# from this single classification so they cannot drift from each other.
HELP_GROUP_ORDER = ("Global", "Table", "Helm", "Logs", "Describe", "Agent")

# Context(s) where each app action's key is actually pressed. Key names and
# descriptions are generated from the live `Binding` objects; only the
# grouping needs human context. `test_every_app_binding_action_has_an_explicit_group`
# asserts every `KorvidApp` binding action appears here, so new bindings
# cannot land unclassified. `open_action_palette` is pre-classified for the
# palette-open binding a later task wires up.
ACTION_HELP_GROUPS: dict[str, tuple[str, ...]] = {
    "quit": ("Global",),
    "help": ("Global",),
    "open_command": ("Global",),
    "open_action_palette": ("Global",),
    "toggle_all_namespaces": ("Global",),
    # Parametrised 1-9 favorites (issue #108) merge into one row via the
    # base-action dedupe in help_screen's `_add`.
    "favorite_namespace": ("Global",),
    # `/` filters the table but searches inside the log / describe panes.
    "open_filter": ("Table", "Logs"),
    "describe": ("Table",),
    "relationships": ("Table",),
    "timeline": ("Table",),
    "shell": ("Table",),
    "port_forward": ("Table",),
    "logs": ("Table",),
    "logs_multi": ("Table",),
    "delete_resource": ("Table",),
    "rollout_restart": ("Table",),
    "resize_pod": ("Table",),
    "operator_install": ("Table",),
    "cordon_node": ("Table",),
    "uncordon_node": ("Table",),
    "drain_node": ("Table",),
    "scale_resource": ("Table",),
    "edit_resource": ("Table",),
    "hint_details": ("Table",),
    "transfer": ("Table",),
    # Column sorting (issue #37); shift+n doubles as sort-by-name via
    # log_search_prev's no-pane fallback.
    "sort_by_age": ("Table",),
    "sort_by_cpu": ("Table",),
    "sort_by_mem": ("Table",),
    "sort_picker": ("Table",),
    "toggle_topbar": ("Global",),
    "log_format": ("Logs",),
    "log_wrap": ("Logs",),
    "log_timestamps": ("Logs",),
    "log_save": ("Logs",),
    "log_previous": ("Logs",),
    "log_search_next": ("Logs",),
    # N steps back through hits in a pane, sorts by name in the table.
    "log_search_prev": ("Logs", "Table"),
    "toggle_agent": ("Agent",),
    "interrupt_agent": ("Agent",),
    "helm_install": ("Helm",),
    "helm_upgrade": ("Helm",),
    "helm_rollback": ("Helm",),
    "helm_history": ("Helm",),
}


def base_action(action: str) -> str:
    """Action name without call parameters (`favorite_namespace(3)` → `favorite_namespace`)."""
    return action.partition("(")[0]


def help_groups_for_action(action: str) -> tuple[str, ...]:
    """Help-overlay/palette group(s) for `action`, defaulting to `("Global",)`."""
    return ACTION_HELP_GROUPS.get(base_action(action), ("Global",))


def as_binding(binding: BindingType) -> Binding:
    """Normalize a `Binding` or legacy `(key, action[, description])` tuple."""
    if isinstance(binding, Binding):
        return binding
    key, action, *rest = binding
    return Binding(key, action, rest[0] if rest else "")


# User-facing keys handled in event handlers rather than BINDINGS:
# Enter drills down via `on_data_table_row_selected`, Escape closes
# panes / pops a drill level via `on_key`.  Listed here so the help
# overlay (`?`) renders them alongside the real bindings.
#: user-facing keys handled in event handlers or via dispatch rather than
#: dedicated bindings: (help group, default key, description, action id).
#: A non-empty action id ties the row to a remappable binding so the help
#: overlay shows the effective key (issue #35), not the default.
APP_HANDLER_KEY_HELP: tuple[tuple[str, str, str, str], ...] = (
    (
        "Table",
        "enter",
        "Drill down (pods → containers, deploy → rs → pods, helm/operator → hierarchy tree)",
        "",
    ),
    ("Table", "escape", "Pop one drill-down level", ""),
    ("Table", "ctrl+w v", "Split workspace into two panes", ""),
    ("Table", "ctrl+w w", "Focus the other pane", ""),
    ("Table", "ctrl+w q", "Close the focused pane", ""),
    ("Logs", "escape", "Close pane (or dismiss search)", ""),
)

APP_CSS = """
#workspace {
    height: 1fr;
}
#workspace ResourceTable {
    width: 1fr;
    height: 1fr;
}
ResourceTable.split-pane {
    border: round $panel;
}
/* A class, not `:focus`: the accent border marks the command-routing
   target (the focused pane; see `WorkspaceState.focused_index`), which must
   stay visible while an Input (command/filter bar, agent panel) owns
   keyboard focus. */
ResourceTable.split-pane.focused-pane {
    border: round $accent;
}
"""
