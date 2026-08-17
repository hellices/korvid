from __future__ import annotations

from korvid.core.resize_impact import ResizeImpactContext

_MAX_LINE = 240
_TITLE = "Pod-local resize impact (advisory):"
_RELATION_BOUNDARY = (
    "  Pod identity and relationship membership stay unchanged; graph relations are not traversed"
)
_RUNTIME_LIMIT = (
    "  node feasibility, Deferred/Infeasible status, actuation, and completion are not predicted"
)


def render_resize_impact_lines(context: ResizeImpactContext) -> tuple[str, ...]:
    lines = [_TITLE, _RELATION_BOUNDARY]
    if context.restart_required:
        lines.append(
            "  one or more changed resources require a container restart under resizePolicy"
        )
    if context.restart_policy_unknown:
        lines.append("  restart requirements could not be determined for every changed resource")
    elif context.all_changed_resources_not_required:
        lines.append("  changed resources do not require a container restart under resizePolicy")
    if context.memory_limit_decrease_not_required:
        lines.append(
            "  a memory-limit decrease using NotRequired has only best-effort OOM avoidance"
        )
    if context.memory_limit_assessment_unknown:
        lines.append("  memory-limit direction or policy could not be determined")
    lines.append(_RUNTIME_LIMIT)
    result = tuple(lines)
    if not all(len(line) <= _MAX_LINE for line in result):
        raise AssertionError("rendered resize impact line exceeded 240 characters")
    return result


def compose_resize_impact_lines(
    graph_lines: tuple[str, ...] | None, context: ResizeImpactContext
) -> tuple[str, ...]:
    local_lines = render_resize_impact_lines(context)
    return (*graph_lines, *local_lines) if graph_lines is not None else local_lines
