from __future__ import annotations

from collections.abc import Mapping

from korvid.core.impact import ImpactAction

_TITLE = "Node maintenance impact (advisory):"
_MAX_LINE = 240

_ACTION_LINES: Mapping[ImpactAction, tuple[str, ...]] = {
    ImpactAction.CORDON_NODE: (
        "  current Pods are not evicted or moved",
        "  new scheduling to the Node is blocked",
        "  future placement and workload availability are not predicted",
    ),
    ImpactAction.UNCORDON_NODE: (
        "  current Pods are not moved",
        "  future scheduling to the Node is permitted",
        "  scheduler choice and capacity are not predicted",
    ),
    ImpactAction.DRAIN_NODE: (
        "  the drain impact plan defines exact eviction targets and skip reasons",
        "  the Node remains cordoned if drain execution fails or is cancelled",
        "  replacement placement, readiness, and application availability are not predicted",
    ),
}


def render_node_maintenance_lines(action: ImpactAction) -> tuple[str, ...]:
    try:
        lines = (_TITLE, *_ACTION_LINES[action])
    except KeyError as exc:
        raise ValueError(f"{action.value} is not a node maintenance action") from exc
    if not all(len(line) <= _MAX_LINE for line in lines):
        raise AssertionError("rendered node maintenance line exceeded 240 characters")
    return lines


def compose_node_maintenance_lines(
    graph_lines: tuple[str, ...] | None,
    action: ImpactAction,
) -> tuple[str, ...]:
    local_lines = render_node_maintenance_lines(action)
    return (*graph_lines, *local_lines) if graph_lines is not None else local_lines
