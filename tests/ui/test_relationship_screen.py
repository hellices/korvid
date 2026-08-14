"""Tests for the keyboard-navigable relationship view (issue #281, Task 6).

`RelationshipScreen` renders one root `GraphResource`'s direct
`dependencies_of`/`dependents_of` from an already-built, immutable
`RelationshipGraph` (Task 4) as a bounded, keyboard-navigable adjacency
table. It performs no I/O and consumes no live app/store state — the graph
and root are handed to it fully formed.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    EvidencePointer,
    GraphLimits,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
)
from korvid.k8s.relationship_facts import FactConfidence, RelationKind
from korvid.ui.widgets.relationship_screen import RelationshipScreen

from .waits import until

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NAMESPACE = "prod"


def _resource(
    kind: str, name: str, *, namespace: str = _NAMESPACE, group: str = ""
) -> GraphResource:
    return GraphResource(group=group, kind=kind, namespace=namespace, name=name)


def _edge(
    subject: GraphResource,
    target: GraphResource,
    *,
    relation: RelationKind = RelationKind.OWNED_BY,
    confidence: FactConfidence = FactConfidence.DECLARED,
    field: str = "metadata.ownerReferences[0]",
    resolution: EdgeResolution = EdgeResolution.RESOLVED,
) -> RelationshipEdge:
    return RelationshipEdge(
        subject=subject,
        target=target,
        relation=relation,
        confidence=confidence,
        evidence=EvidencePointer(resource=subject, field=field),
        resolution=resolution,
    )


def _graph() -> RelationshipGraph:
    """Root Deployment/prod/api declares a single, resolved dependency."""
    root = _resource("Deployment", "api")
    pod = _resource("Pod", "api-0")
    edge = _edge(root, pod)
    return RelationshipGraph(nodes=(root, pod), edges=(edge,), coverage=())


def _incomplete_graph() -> RelationshipGraph:
    """No edges; one non-complete coverage record (a forbidden LIST)."""
    root = _resource("Service", "api")
    coverage = (
        CoverageRecord(
            group="",
            resource="services",
            scope=_NAMESPACE,
            state=CoverageState.FORBIDDEN,
            detail="rbac denied",
        ),
    )
    return RelationshipGraph(nodes=(root,), edges=(), coverage=coverage)


def _graph_with_missing_target() -> RelationshipGraph:
    """Root Pod/prod/api-0 declares a dependency whose target was never found."""
    root = _resource("Pod", "api-0")
    missing = _resource("ConfigMap", "gone-cm")
    edge = _edge(
        root,
        missing,
        relation=RelationKind.USES_CONFIG,
        field="spec.containers[0].envFrom[0].configMapRef",
        resolution=EdgeResolution.MISSING,
    )
    return RelationshipGraph(nodes=(root,), edges=(edge,), coverage=())


def _cyclic_capped_graph() -> RelationshipGraph:
    """Root Deployment/prod/api has two direct dependents; expanding one
    level deeper both cycles back to an already-visited node and hits the
    (deliberately tiny) node cap on the other. Coverage also carries a
    forbidden record so the `c` toggle test can assert on it too."""
    root = _resource("Deployment", "api")
    rs1 = _resource("ReplicaSet", "api-rs1")
    rs2 = _resource("ReplicaSet", "api-rs2")
    pod_x = _resource("Pod", "api-rs1-x")
    edges = (
        _edge(rs1, root, relation=RelationKind.OWNED_BY),
        _edge(rs2, root, relation=RelationKind.OWNED_BY),
        # rs2 -> rs1 only surfaces one level deeper than root's direct
        # dependents; rs2 is already visited by then, so it is a genuine
        # cycle rather than a new dependent of root.
        _edge(rs2, rs1, relation=RelationKind.MANAGED_BY),
        _edge(pod_x, rs1, relation=RelationKind.OWNED_BY),  # beyond the node cap -> truncated
    )
    coverage = (
        CoverageRecord(
            group="",
            resource="secrets",
            scope=_NAMESPACE,
            state=CoverageState.FORBIDDEN,
            detail="rbac denied",
        ),
    )
    return RelationshipGraph(
        nodes=(root, rs1, rs2, pod_x),
        edges=edges,
        coverage=coverage,
        limits=GraphLimits(max_nodes=2),
    )


def _secret_graph(secret_name: str) -> RelationshipGraph:
    """Root Pod/prod/api-0 depends on a resource whose *name* carries Rich
    markup syntax — it must render literally, never as styling."""
    root = _resource("Pod", "api-0")
    # kind/namespace/group deliberately blank: the resource label collapses
    # to exactly `secret_name`, letting the test assert on it verbatim.
    target = GraphResource(group="", kind="", namespace="", name=secret_name)
    edge = _edge(
        root,
        target,
        relation=RelationKind.USES_CONFIG,
        field="spec.containers[0].envFrom[0].secretRef",
    )
    return RelationshipGraph(nodes=(root, target), edges=(edge,), coverage=())


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


def _all_cells(table: DataTable[str]) -> list[str]:
    return [str(cell) for index in range(table.row_count) for cell in table.get_row_at(index)]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def test_screen_separates_dependencies_and_dependents() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        rows = _all_cells(table)
        assert "Dependencies" in rows
        assert "Dependents" in rows
        assert "owned_by" in rows
        assert "declared" in rows
        assert "metadata.ownerReferences[0]" in rows
        await pilot.press("escape")


async def test_incomplete_banner_names_coverage_state() -> None:
    app = HostApp()
    screen = RelationshipScreen(_incomplete_graph(), _resource("Service", "api"))
    async with app.run_test():
        await app.push_screen(screen)
        banner = app.screen.query_one("#relationship-coverage", Static)
        text = str(banner.render())
        assert "incomplete" in text.lower()
        assert "forbidden" in text.lower()


# ---------------------------------------------------------------------------
# Interaction
# ---------------------------------------------------------------------------


async def test_enter_on_resolved_row_returns_exact_goto() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda value: setattr(app, "result", value))
        await pilot.press("down", "enter")
        assert app.result == ("goto", "", "Pod", "prod", "api-0")


async def test_enter_on_missing_row_keeps_screen_open() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph_with_missing_target(), _resource("Pod", "api-0"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("down", "enter")
        assert app.screen is screen
        status = str(app.screen.query_one("#relationship-status", Static).render())
        assert "missing" in status


async def test_expansion_and_coverage_remain_bounded() -> None:
    app = HostApp()
    screen = RelationshipScreen(_cyclic_capped_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("d")
        table = app.screen.query_one(DataTable)
        await until(pilot, lambda: table.row_count > 4, label="expanded rows rendered")
        text = "\n".join(_all_cells(table))
        assert "cycle" in text.lower()
        assert "capped" in text.lower()
        assert table.row_count <= screen.graph.limits.max_nodes + 4
        await pilot.press("c")
        coverage = str(app.screen.query_one("#relationship-coverage", Static).render())
        assert "forbidden" in coverage


async def test_markup_names_and_secret_metadata_render_literally() -> None:
    app = HostApp()
    screen = RelationshipScreen(_secret_graph("[red]tls[/]"), _resource("Pod", "api-0"))
    async with app.run_test():
        await app.push_screen(screen)
        rendered = app.screen.query_one(DataTable).get_row_at(1)
        assert "[red]tls[/]" in {str(cell) for cell in rendered}
        assert "secret-value" not in repr(screen)
