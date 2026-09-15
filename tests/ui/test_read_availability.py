"""The read keys' silent availability decisions (issue #388 final review).

The owners feed these functions the facts their handler reads (proved by
the app-wiring tests in `test_action_palette_workflow.py`); what is pinned
here is the decision itself - which refusal wins, in the order the handler
checks it, and with the wording the keypress would notify.
"""

from __future__ import annotations

from korvid.k8s.discovery import ResourceMeta
from korvid.ui.action_availability import CONTEXT_SWITCH_IN_PROGRESS, AvailabilityCode
from korvid.ui.read_availability import (
    SEARCH_ACTIONS,
    PaneSearch,
    describe_reason,
    hint_details_reason,
    relationships_reason,
    search_reason,
)

_PODS = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_HELM = ResourceMeta("Release", "releases", "", "v1", True, (), synthetic=True)

_CLOSED = PaneSearch(displayed=False, hits=False)
_OPEN_EMPTY = PaneSearch(displayed=True, hits=False)
_OPEN_WITH_HITS = PaneSearch(displayed=True, hits=True)


def test_describe_refuses_a_composition_without_a_manifest_fetcher_first() -> None:
    reason = describe_reason(manifest=False, switching=True, selected=False)
    assert reason is not None
    assert reason.message == "Describe unavailable"
    assert reason.code is AvailabilityCode.MISSING_CAPABILITY


def test_describe_refuses_a_context_switch_before_the_selection() -> None:
    assert describe_reason(manifest=True, switching=True, selected=False) == (
        CONTEXT_SWITCH_IN_PROGRESS
    )


def test_describe_refuses_an_empty_table_last_and_allows_a_selected_row() -> None:
    reason = describe_reason(manifest=True, switching=False, selected=False)
    assert reason is not None
    assert reason.message == "No resource selected"
    assert describe_reason(manifest=True, switching=False, selected=True) is None


def test_hint_details_separates_no_row_from_a_row_without_a_hint() -> None:
    no_row = hint_details_reason(row=False, hinted=False)
    assert no_row is not None
    assert no_row.code is AvailabilityCode.NO_SELECTION
    healthy = hint_details_reason(row=True, hinted=False)
    assert healthy is not None
    assert healthy.code is AvailabilityCode.UNSUPPORTED_RESOURCE
    assert hint_details_reason(row=True, hinted=True) is None


def test_relationships_refuses_a_session_without_a_loader_first() -> None:
    reason = relationships_reason(
        loader=False, switching=True, meta=None, kind="pods", selected=False
    )
    assert reason is not None
    assert reason.message == "Relationships unavailable in this session"


def test_relationships_refuses_a_context_switch_before_the_view() -> None:
    assert relationships_reason(
        loader=True, switching=True, meta=None, kind="pods", selected=False
    ) == (CONTEXT_SWITCH_IN_PROGRESS)


def test_relationships_explains_an_undiscovered_kind_and_a_synthetic_view() -> None:
    undiscovered = relationships_reason(
        loader=True, switching=False, meta=None, kind="widgets", selected=True
    )
    assert undiscovered is not None
    assert undiscovered.message == "widgets is not a discovered view"
    synthetic = relationships_reason(
        loader=True, switching=False, meta=_HELM, kind="releases", selected=True
    )
    assert synthetic is not None
    assert synthetic.message == "Release is a read-only view"


def test_relationships_allows_a_selected_row_on_a_real_view() -> None:
    assert (
        relationships_reason(loader=True, switching=False, meta=_PODS, kind="pods", selected=True)
        is None
    )
    refused = relationships_reason(
        loader=True, switching=False, meta=_PODS, kind="pods", selected=False
    )
    assert refused is not None
    assert refused.message == "No resource selected"


def test_search_follows_the_describe_pane_before_the_log_pane() -> None:
    """The app asks the describe pane first, so an open describe pane
    without hits refuses even while the log pane has some."""
    for action in SEARCH_ACTIONS:
        refused = search_reason(action, describe=_OPEN_EMPTY, logs=_OPEN_WITH_HITS)
        assert refused is not None
        assert refused.code is AvailabilityCode.NO_ACTIVE_SEARCH
        assert search_reason(action, describe=_OPEN_WITH_HITS, logs=_CLOSED) is None


def test_search_falls_through_to_the_log_pane_when_describe_is_closed() -> None:
    for action in SEARCH_ACTIONS:
        assert search_reason(action, describe=_CLOSED, logs=_OPEN_WITH_HITS) is None
        refused = search_reason(action, describe=_CLOSED, logs=_OPEN_EMPTY)
        assert refused is not None
        assert refused.code is AvailabilityCode.NO_ACTIVE_SEARCH


def test_with_no_pane_open_only_next_is_refused() -> None:
    """`N` still sorts the table by name with no pane open - a real effect -
    while `n` would do nothing at all."""
    refused = search_reason("log_search_next", describe=_CLOSED, logs=_CLOSED)
    assert refused is not None
    assert refused.code is AvailabilityCode.PANE_CLOSED
    assert search_reason("log_search_prev", describe=_CLOSED, logs=_CLOSED) is None
