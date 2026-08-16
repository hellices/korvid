"""MCP follow mode's mirror map (issue #153): cluster reads arriving over
MCP translate to the same UIBridge calls the ui_only tools use, so the TUI
shows what the external host is reading."""

from __future__ import annotations

import pytest

from korvid.tools.follow import FOLLOWABLE_TOOLS, mirror_read, read_summary

from .executor_fakes import FakeBridge


async def test_list_resources_mirrors_as_navigation() -> None:
    ui = FakeBridge()
    result = await mirror_read(ui, "list_resources", {"kind": "pods", "namespace": "prod"})
    assert result is not None
    assert ui.calls == [("navigate", {"view": "pods", "namespace": "prod"})]


async def test_list_resources_without_namespace_navigates_to_all() -> None:
    """An omitted namespace lists across the cluster: the mirrored view must
    show the same scope, not whatever namespace the pane happened to be on."""
    ui = FakeBridge()
    await mirror_read(ui, "list_resources", {"kind": "deployments"})
    assert ui.calls == [("navigate", {"view": "deployments", "namespace": "all"})]


async def test_get_resource_mirrors_as_describe() -> None:
    ui = FakeBridge()
    await mirror_read(ui, "get_resource", {"kind": "pods", "name": "api-1", "namespace": "prod"})
    assert ui.calls == [("open_describe", {"kind": "pods", "name": "api-1", "namespace": "prod"})]


async def test_get_events_mirrors_as_describe() -> None:
    ui = FakeBridge()
    await mirror_read(ui, "get_events", {"kind": "deployment", "namespace": "prod", "name": "api"})
    assert ui.calls == [
        ("open_describe", {"kind": "deployment", "name": "api", "namespace": "prod"})
    ]


async def test_get_logs_mirrors_as_log_pane() -> None:
    ui = FakeBridge()
    await mirror_read(
        ui, "get_logs", {"pod": "api-1", "namespace": "prod", "container": "main", "tail_lines": 50}
    )
    assert ui.calls == [("open_logs", {"pod": "api-1", "namespace": "prod", "container": "main"})]


async def test_diagnose_pod_mirrors_as_pod_describe() -> None:
    ui = FakeBridge()
    # the registry schema names the target 'pod' (not 'name')
    await mirror_read(ui, "diagnose_pod", {"pod": "api-1", "namespace": "prod"})
    assert ui.calls == [("open_describe", {"kind": "pods", "name": "api-1", "namespace": "prod"})]


async def test_list_operators_mirrors_as_subscriptions_view() -> None:
    ui = FakeBridge()
    await mirror_read(ui, "list_operators", {})
    assert ui.calls == [("navigate", {"view": "subscriptions", "namespace": "all"})]


async def test_unmapped_tool_mirrors_nothing() -> None:
    ui = FakeBridge()
    result = await mirror_read(ui, "navigate", {"view": "pods"})
    assert result is None
    assert ui.calls == []


async def test_bad_arguments_never_raise() -> None:
    """Fire-and-forget: a malformed call must not produce an unhandled task
    exception - and non-string junk must not reach the bridge."""
    ui = FakeBridge()
    result = await mirror_read(ui, "get_logs", {"pod": 42})
    assert result is None
    assert ui.calls == []


async def test_bridge_errors_never_raise() -> None:
    class ExplodingBridge(FakeBridge):
        async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
            raise RuntimeError("boom")

    result = await mirror_read(ExplodingBridge(), "list_resources", {"kind": "pods"})
    assert result is None


def test_followable_tools_cover_every_cluster_read_on_the_mcp_surface() -> None:
    """Every cluster_read tool exposed over MCP has a mirror decision: a new
    read tool must either get a mapping or be deliberately listed as
    unmirrored - silence is how reads become invisible again."""
    from korvid.tools.follow import UNMIRRORED_TOOLS
    from korvid.tools.registry import TOOL_DEFS

    mcp_reads = {t.name for t in TOOL_DEFS if t.effect == "cluster_read" and "mcp" in t.surfaces}
    assert mcp_reads <= FOLLOWABLE_TOOLS | UNMIRRORED_TOOLS
    assert not (FOLLOWABLE_TOOLS & UNMIRRORED_TOOLS)


@pytest.mark.parametrize("tool", sorted(FOLLOWABLE_TOOLS))
async def test_every_followable_tool_reaches_the_bridge(tool: str) -> None:
    args = {
        "list_resources": {"kind": "pods"},
        "get_resource": {"kind": "pods", "name": "x", "namespace": "d"},
        "get_events": {"kind": "pods", "name": "x", "namespace": "d"},
        "get_logs": {"pod": "x", "namespace": "d"},
        "diagnose_pod": {"pod": "x", "namespace": "d"},
        "diagnose_workload": {
            "kind": "deployments",
            "name": "x",
            "namespace": "d",
        },
        "list_operators": {},
        "helm_list_releases": {},
        "diagnose_service": {"service": "x", "namespace": "d"},
        "diagnose_pvc": {"pvc": "x", "namespace": "d"},
    }[tool]
    ui = FakeBridge()
    result = await mirror_read(ui, tool, args)
    assert result is not None
    assert len(ui.calls) == 1


def test_read_summary_names_the_target_and_scope() -> None:
    assert read_summary("get_logs", {"pod": "api-1", "namespace": "prod"}) == (
        "get_logs api-1 (ns prod)"
    )
    assert read_summary("list_resources", {"kind": "pods"}) == "list_resources pods (ns all)"
    assert (
        read_summary("get_resource", {"kind": "deploy", "name": "api", "namespace": "prod"})
        == "get_resource deploy/api (ns prod)"
    )
    assert read_summary("list_operators", {}) == "list_operators (ns all)"


def test_read_summary_sanitizes_and_bounds_hostile_arguments() -> None:
    """The summary crosses into a status toast: caller-controlled values
    must not inject newlines/control characters or bloat the line."""
    line = read_summary(
        "get_logs",
        {"pod": "evil\napproved: scale ok\x1b[2Jdone" + "x" * 500, "namespace": "d\u202ecba"},
    )
    assert "\n" not in line
    assert "\x1b" not in line
    assert "\u202e" not in line  # bidi override cannot reorder the toast
    assert len(line) <= 200


async def test_helm_list_releases_mirrors_as_the_helm_view() -> None:
    ui = FakeBridge()
    result = await mirror_read(ui, "helm_list_releases", {"namespace": "prod"})
    assert result is not None
    assert ui.calls == [("navigate", {"view": "helm", "namespace": "prod"})]


async def test_diagnose_service_follow_opens_service_describe() -> None:
    ui = FakeBridge()
    result = await mirror_read(
        ui,
        "diagnose_service",
        {"service": "api", "namespace": "shop"},
    )
    assert result is not None
    assert ui.calls == [("open_describe", {"kind": "services", "name": "api", "namespace": "shop"})]


async def test_diagnose_pvc_follow_opens_claim_describe() -> None:
    ui = FakeBridge()
    result = await mirror_read(
        ui,
        "diagnose_pvc",
        {"pvc": "data", "namespace": "shop"},
    )
    assert result is not None
    assert ui.calls == [
        ("open_describe", {"kind": "persistentvolumeclaims", "name": "data", "namespace": "shop"})
    ]


def test_external_reads_are_explicitly_unmirrored() -> None:
    """No screen shows a Prometheus query, so the decision must be recorded.

    An external read has no resource view to navigate to; leaving it out
    of both sets would make the pairing guard silently stop covering the
    newest kind of read.
    """
    from korvid.tools.follow import UNMIRRORED_TOOLS
    from korvid.tools.registry import TOOL_DEFS

    external = {t.name for t in TOOL_DEFS if t.effect == "external_read" and "mcp" in t.surfaces}
    assert external
    assert external <= UNMIRRORED_TOOLS
    assert not (external & FOLLOWABLE_TOOLS)


def test_the_summary_names_the_signal_and_workload_of_an_external_read() -> None:
    """`query_metrics (ns prod)` alone would not say what was asked."""
    summary = read_summary(
        "query_metrics", {"signal": "cpu", "workload": "api", "namespace": "prod"}
    )
    assert "cpu" in summary
    assert "api" in summary
    assert "ns prod" in summary


def test_the_summary_sanitises_an_external_read_workload() -> None:
    summary = read_summary("search_logs", {"workload": "api\nfake toast line", "namespace": "p"})
    assert "\n" not in summary
