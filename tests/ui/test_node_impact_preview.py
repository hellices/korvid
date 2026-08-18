import pytest

from korvid.core.impact import ImpactAction
from korvid.ui.node_impact_preview import (
    compose_node_maintenance_lines,
    render_node_maintenance_lines,
)


@pytest.mark.parametrize(
    ("action", "required"),
    [
        (
            ImpactAction.CORDON_NODE,
            (
                "current Pods are not evicted or moved",
                "new scheduling to the Node is blocked",
                "future placement and workload availability are not predicted",
            ),
        ),
        (
            ImpactAction.UNCORDON_NODE,
            (
                "current Pods are not moved",
                "future scheduling to the Node is permitted",
                "scheduler choice and capacity are not predicted",
            ),
        ),
        (
            ImpactAction.DRAIN_NODE,
            (
                "the drain impact plan defines exact eviction targets and skip reasons",
                "the Node remains cordoned if drain execution fails or is cancelled",
                "replacement placement, readiness, and application availability are not predicted",
            ),
        ),
    ],
)
def test_node_maintenance_lines_are_action_specific(
    action: ImpactAction, required: tuple[str, ...]
) -> None:
    lines = render_node_maintenance_lines(action)
    assert lines[0] == "Node maintenance impact (advisory):"
    assert all(f"  {text}" in lines for text in required)
    assert all(len(line) <= 240 for line in lines)


def test_graph_lines_precede_local_node_maintenance_lines() -> None:
    lines = compose_node_maintenance_lines(
        ("graph-derived impact (advisory):",),
        ImpactAction.DRAIN_NODE,
    )
    assert lines[0] == "graph-derived impact (advisory):"
    assert lines[1] == "Node maintenance impact (advisory):"


def test_non_node_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="node maintenance"):
        render_node_maintenance_lines(ImpactAction.DELETE)
