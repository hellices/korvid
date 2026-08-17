from korvid.core.resize_impact import ResizeImpactContext
from korvid.ui.resize_impact_preview import (
    compose_resize_impact_lines,
    render_resize_impact_lines,
)


def _context(**changes: bool) -> ResizeImpactContext:
    values = {
        "cpu_changed": True,
        "memory_request_changed": False,
        "memory_limit_changed": False,
        "restart_required": False,
        "cpu_restart_required": False,
        "memory_restart_required": False,
        "restart_policy_unknown": False,
        "all_changed_resources_not_required": True,
        "memory_limit_decreased": False,
        "memory_limit_decrease_not_required": False,
        "memory_limit_assessment_unknown": False,
    }
    values.update(changes)
    return ResizeImpactContext(**values)


def test_cpu_only_not_required_resize_is_specific() -> None:
    lines = render_resize_impact_lines(_context())
    assert lines == (
        "Pod-local resize impact (advisory):",
        "  Pod identity and relationship membership stay unchanged; graph relations are not traversed",
        "  changed resources do not require a container restart under resizePolicy",
        "  node feasibility, Deferred/Infeasible status, actuation, and completion are not predicted",
    )


def test_restart_and_memory_decrease_warnings_are_conditional() -> None:
    lines = render_resize_impact_lines(
        _context(
            memory_limit_changed=True,
            restart_required=True,
            memory_restart_required=True,
            all_changed_resources_not_required=False,
            memory_limit_decrease_not_required=True,
        )
    )
    assert "  changed memory resources require a container restart under resizePolicy" in lines
    assert "  a memory-limit decrease using NotRequired has only best-effort OOM avoidance" in lines


def test_restart_warning_names_every_triggering_resource() -> None:
    lines = render_resize_impact_lines(
        _context(
            restart_required=True,
            cpu_restart_required=True,
            memory_restart_required=True,
            all_changed_resources_not_required=False,
        )
    )
    assert (
        "  changed CPU and memory resources require a container restart under resizePolicy" in lines
    )


def test_unknown_input_never_becomes_a_no_restart_claim() -> None:
    lines = render_resize_impact_lines(
        _context(
            restart_policy_unknown=True,
            all_changed_resources_not_required=False,
            memory_limit_assessment_unknown=True,
        )
    )
    assert "  restart requirements could not be determined for every changed resource" in lines
    assert "  memory-limit direction or policy could not be determined" in lines
    assert not any("do not require" in line for line in lines)


def test_graph_and_local_sections_keep_their_order() -> None:
    lines = compose_resize_impact_lines(("graph-derived impact (advisory):",), _context())
    assert lines[0] == "graph-derived impact (advisory):"
    assert lines[1] == "Pod-local resize impact (advisory):"


def test_every_line_is_machine_bounded() -> None:
    assert all(len(line) <= 240 for line in render_resize_impact_lines(_context()))
