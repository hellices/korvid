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
