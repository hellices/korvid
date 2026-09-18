"""Why a read key would do nothing right now (issue #388 final review).

The palette shows every bound action, so it has to know when one of them
would be a no-op: `d` on an empty table, `g` on a synthetic view, `h` on a
healthy pod, `n` with no search running. Each of those refusals already
exists inside the handler that owns the key; what did not exist was a way
to ask the same question *without* the notification the keypress emits.

These are the answers, kept in one reviewed place and phrased once: pure
functions over facts their owner reads anyway (`ResourceInspectController`
for `d`/`h`/`n`/`N`, `WorkspaceController` for `g`). Nothing here performs
I/O, starts a worker, or notifies — the owners pass in what they already
know, in the same order their handler checks it, so a probe and its
keypress can never disagree about *why*.
"""

from __future__ import annotations

from dataclasses import dataclass

from korvid.k8s.discovery import ResourceMeta
from korvid.ui.action_availability import (
    CONTEXT_SWITCH_IN_PROGRESS,
    AvailabilityCode,
    UnavailableReason,
)

#: The two search keys answered here. `n` and `N` step through hits in
#: whichever read pane is on screen, so their availability is a question
#: about panes, not about the table.
SEARCH_ACTIONS: tuple[str, ...] = ("log_search_next", "log_search_prev")

#: Palette action -> the sort column `KorvidApp.action_sort_by_age` /
#: `action_sort_by_cpu` / `action_sort_by_mem` pass to
#: `WorkspaceController.sort_by`. These three are the sorts that can
#: silently do nothing, because a view can render without their column.
#: `sort_by("name")` is absent on purpose: no key runs it directly (`N`
#: falls back to it), and NAME is the one column `replace: true` keeps.
SORT_ACTION_COLUMNS: dict[str, str] = {
    "sort_by_age": "age",
    "sort_by_cpu": "cpu",
    "sort_by_mem": "mem",
}

#: The two columns that exist only on the pods view, which is the only one
#: with a metrics feed behind them. AGE is not one of them: every
#: discovered view renders it.
_METRIC_COLUMNS: frozenset[str] = frozenset({"cpu", "mem"})

#: `:ns` in a session with no namespace listing wired. One wording for
#: the picker's own refusal and the palette row that reports it (#388).
NAMESPACE_LISTING_UNAVAILABLE = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY, "Namespace listing unavailable"
)

#: The wording `ViewState.selected_ns_name` notifies with when a real
#: keypress finds no row, said silently.
NO_SELECTION = UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected")

#: A read pane is on screen but its search matched nothing, so `n`/`N`
#: have nowhere to step.
_NO_ACTIVE_SEARCH = UnavailableReason(
    AvailabilityCode.NO_ACTIVE_SEARCH, "No search hits to step through"
)


@dataclass(frozen=True, slots=True)
class PaneSearch:
    """Whether a read pane is on screen, and whether its search has hits.

    Both halves matter: a pane that is not displayed never sees `n`/`N` at
    all, and a displayed pane whose search matched nothing steps nowhere.
    """

    displayed: bool
    hits: bool


def describe_reason(*, manifest: bool, switching: bool, selected: bool) -> UnavailableReason | None:
    """Why `d` can't describe anything right now, or None.

    `ResourceInspectController.describe_selected` refuses in exactly this
    order: no manifest fetcher in this composition (it notifies "Describe
    unavailable"), a `:ctx` switch in flight, then no selected row.
    """
    if not manifest:
        return UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, "Describe unavailable")
    if switching:
        return CONTEXT_SWITCH_IN_PROGRESS
    if not selected:
        return NO_SELECTION
    return None


def hint_details_reason(*, row: bool, hinted: bool) -> UnavailableReason | None:
    """Why `h` has no detail overlay to open, or None.

    `ResourceInspectController.hint_details` returns silently when there is
    no row under the cursor, and when the row it finds is a pod the hint
    strip never flagged (`pod_needs_hint` is False) - a healthy pod has
    nothing to expand. The pods-view half of the guard is the binding's
    own (`ActionPolicy` gates `hint_details` on the pods view), so it is
    not repeated here.
    """
    if not row:
        return NO_SELECTION
    if not hinted:
        return UnavailableReason(
            AvailabilityCode.UNSUPPORTED_RESOURCE, "The selected row has no hint to open"
        )
    return None


def relationships_reason(
    *,
    loader: bool,
    switching: bool,
    meta: ResourceMeta | None,
    kind: str,
    selected: bool,
) -> UnavailableReason | None:
    """Why `g` can't load a relationship graph right now, or None.

    `WorkspaceController.show_relationships` refuses in exactly this order,
    and with exactly this wording: no loader composed, a `:ctx` switch in
    flight, a kind discovery has not resolved, a synthetic (client-side)
    view, then no selected row.
    """
    if not loader:
        return UnavailableReason(
            AvailabilityCode.MISSING_CAPABILITY, "Relationships unavailable in this session"
        )
    if switching:
        return CONTEXT_SWITCH_IN_PROGRESS
    if meta is None:
        return UnavailableReason(
            AvailabilityCode.UNSUPPORTED_RESOURCE, f"{kind} is not a discovered view"
        )
    if meta.synthetic:
        return UnavailableReason(
            AvailabilityCode.UNSUPPORTED_RESOURCE, f"{meta.kind} is a read-only view"
        )
    if not selected:
        return NO_SELECTION
    return None


def sort_column_reason(column: str, *, kind: str, replaced: bool) -> UnavailableReason | None:
    """Why `A`/`C`/`M` would discard the keypress right now, or None.

    `WorkspaceController.sort_by` returns without reordering anything -
    and without a notification - in exactly two states: a metric column on
    any view but pods (nothing else has CPU/MEM columns or a metrics
    feed), and a `replace: true` view, which renders its own columns
    instead of AGE/CPU/MEM, so the reorder would have no visible effect.
    NAME survives both, because `replace: true` keeps that column and `N`
    falls back to sorting by it. The handler shares this function, so the
    greyed-out row and the silent keypress cannot drift.

    The wording deliberately carries no cluster data. This is row text on
    a 36-column terminal, where a refusal naming an arbitrarily long view
    or column name would be clipped - and a disabled palette row is never
    highlighted, so nothing scrolls the missing words back into view.

    Args:
        column: A builtin sort column (`name`, `age`, `cpu` or `mem`) -
            the only ones the key handlers and `:sort` pass to `sort_by`.
        kind: The view the focused pane is showing.
        replaced: Whether that view's `replace: true` hid the builtins.
    """
    if column == "name":
        return None
    label = column.upper()
    if column in _METRIC_COLUMNS and kind != "pods":
        return UnavailableReason(
            AvailabilityCode.UNSUPPORTED_RESOURCE, f"{label} is not a column on this view"
        )
    if replaced:
        return UnavailableReason(
            AvailabilityCode.UNSUPPORTED_RESOURCE, f"This view replaces the {label} column"
        )
    return None


def search_reason(
    action: str, *, describe: PaneSearch, logs: PaneSearch
) -> UnavailableReason | None:
    """Why `n`/`N` would step nowhere right now, or None.

    `KorvidApp.action_log_search_next` / `action_log_search_prev` ask the
    describe pane first and the log pane second, and each pane's
    `search_next`/`search_prev` returns immediately without hits. With
    neither pane displayed the two keys part company: `n` does nothing at
    all, while `N` falls back to sorting the table by name — a real
    effect, so it stays invocable.
    """
    for pane in (describe, logs):
        if pane.displayed:
            return None if pane.hits else _NO_ACTIVE_SEARCH
    if action == "log_search_prev":
        return None
    return UnavailableReason(AvailabilityCode.PANE_CLOSED, "Open the log or describe pane first")
