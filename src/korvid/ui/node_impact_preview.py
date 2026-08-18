from __future__ import annotations

from collections.abc import Mapping

from korvid.core.impact import ImpactAction

_TITLE = "Node maintenance impact (advisory):"

_ACTION_LINES: Mapping[ImpactAction, tuple[str, ...]] = {
    ImpactAction.CORDON_NODE: (
        "  current Pods are not evicted or moved",
        "  the Node is marked unschedulable for ordinary workload placement",
        "  future placement and workload availability are not predicted",
    ),
    ImpactAction.UNCORDON_NODE: (
        "  current Pods are not moved",
        "  future scheduling to the Node is permitted",
        "  scheduler choice and capacity are not predicted",
    ),
    ImpactAction.DRAIN_NODE: (
        "  the drain impact plan defines exact eviction targets and skip reasons",
        "  after the Node is successfully cordoned, it remains cordoned"
        " if drain execution later fails or is cancelled",
        "  replacement placement, readiness, and application availability are not predicted",
    ),
}


def render_node_maintenance_lines(action: ImpactAction) -> tuple[str, ...]:
    try:
        lines = (_TITLE, *_ACTION_LINES[action])
    except KeyError as exc:
        raise ValueError(f"{action.value} is not a node maintenance action") from exc
    return lines


def compose_node_maintenance_lines(
    graph_lines: tuple[str, ...] | None,
    action: ImpactAction,
) -> tuple[str, ...]:
    local_lines = render_node_maintenance_lines(action)
    if graph_lines is None:
        return local_lines
    if action is not ImpactAction.DRAIN_NODE:
        raise ValueError(f"{action.value} must not carry graph-derived lines")
    return (*graph_lines, *local_lines)
