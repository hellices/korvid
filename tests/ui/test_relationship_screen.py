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


def _dependent_with_missing_edge_resolution_graph() -> RelationshipGraph:
    """A dependent whose *edge* was recorded `MISSING` at build time, yet
    whose *subject* (the dependent itself) is a perfectly real, discovered
    node (issue #281 review round 2, finding 2). `EdgeResolution` always
    describes `edge.target`; a `dependents_of(root)` row's navigable
    resource is `edge.subject`, so this edge's resolution state must not
    block navigating to it."""
    root = _resource("Service", "api")
    pod = _resource("Pod", "api-0")
    edge = _edge(pod, root, relation=RelationKind.SELECTS, resolution=EdgeResolution.MISSING)
    return RelationshipGraph(nodes=(root, pod), edges=(edge,), coverage=())


def _deep_dependents_graph() -> RelationshipGraph:
    """A genuine two-hop dependent chain (root <- rs1 <- pod1), well within
    the default node/depth caps — nothing here is capped or cyclic, so the
    depth-2 row must be rendered from real BFS depth, not omitted."""
    root = _resource("Deployment", "api")
    rs1 = _resource("ReplicaSet", "api-rs1")
    pod1 = _resource("Pod", "api-rs1-0")
    edges = (
        _edge(rs1, root, relation=RelationKind.OWNED_BY),
        _edge(pod1, rs1, relation=RelationKind.OWNED_BY),
    )
    return RelationshipGraph(nodes=(root, rs1, pod1), edges=edges, coverage=())


def _many_direct_dependents_graph(count: int = 30, max_nodes: int = 3) -> RelationshipGraph:
    """`count` direct dependents against a deliberately tiny `max_nodes` —
    the *base* (unexpanded) view must already be bounded; nothing about
    this scenario touches expansion or cycles at all."""
    root = _resource("Deployment", "api")
    dependents = tuple(_resource("Pod", f"api-{i}") for i in range(count))
    edges = tuple(_edge(dep, root, relation=RelationKind.OWNED_BY) for dep in dependents)
    return RelationshipGraph(
        nodes=(root, *dependents), edges=edges, coverage=(), limits=GraphLimits(max_nodes=max_nodes)
    )


def _many_cycles_graph(count: int = 50, max_nodes: int = 2) -> RelationshipGraph:
    """`count` back-edges from rs2 to rs1, all discovered as cycles one
    level into expansion. `RelationshipGraph.walk_dependents` itself does
    not cap `cycles` (only `edges`/`added_nodes`), so rendering must cap
    independently or a resource with many cyclic references renders one
    row per cycle."""
    root = _resource("Deployment", "api")
    rs1 = _resource("ReplicaSet", "api-rs1")
    rs2 = _resource("ReplicaSet", "api-rs2")
    edges = [
        _edge(rs1, root, relation=RelationKind.OWNED_BY),
        _edge(rs2, root, relation=RelationKind.OWNED_BY),
    ]
    edges.extend(
        _edge(rs2, rs1, relation=RelationKind.MANAGED_BY, field=f"spec.selector[{i}]")
        for i in range(count)
    )
    return RelationshipGraph(
        nodes=(root, rs1, rs2),
        edges=tuple(edges),
        coverage=(),
        limits=GraphLimits(max_nodes=max_nodes),
    )


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


# ---------------------------------------------------------------------------
# Review round 2 (issue #281 Task 6 findings)
# ---------------------------------------------------------------------------


async def test_escape_dismisses_with_none() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda value: setattr(app, "result", value))
        await pilot.press("escape")
        assert app.result is None


async def test_dependent_subject_navigable_despite_missing_edge_resolution() -> None:
    """Finding 2: `edge.resolution` describes `edge.target`, not the
    dependent (`edge.subject`) a Dependents row navigates to. A dependent
    whose recorded edge is `MISSING` (target-side) must still dismiss with
    its own, real, indexed subject."""
    app = HostApp()
    screen = RelationshipScreen(
        _dependent_with_missing_edge_resolution_graph(), _resource("Service", "api")
    )
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda value: setattr(app, "result", value))
        # row 0: "Dependencies" header (empty); row 1: "Dependents" header;
        # row 2: the sole dependent edge (subject=Pod/prod/api-0).
        await pilot.press("down", "down", "enter")
        assert app.result == ("goto", "", "Pod", "prod", "api-0")


async def test_expansion_renders_genuine_depth_two_row() -> None:
    """Finding 3: depth labels must come from an order-independent BFS over
    the returned edge set, not from `TraversalResult.edges`' incidental
    iteration order. root <- rs1 <- pod1 is a genuine, uncapped, uncyclic
    two-hop chain."""
    app = HostApp()
    screen = RelationshipScreen(_deep_dependents_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("d")
        table = app.screen.query_one(DataTable)
        await until(pilot, lambda: table.row_count > 3, label="expanded rows rendered")
        text = "\n".join(_all_cells(table))
        assert "depth 2" in text.lower()


async def test_many_direct_dependents_row_count_stays_bounded() -> None:
    """Finding 1: direct dependency/dependent rows are not currently
    bounded at all — a resource with many direct dependents must still
    render a `row_count` within the shared `max_nodes`-based budget, with
    the omitted rows visibly summarized rather than silently dropped."""
    app = HostApp()
    graph = _many_direct_dependents_graph(count=30, max_nodes=3)
    screen = RelationshipScreen(graph, _resource("Deployment", "api"))
    async with app.run_test():
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        text = "\n".join(_all_cells(table))
        assert table.row_count <= graph.limits.max_nodes + 4
        assert "capped" in text.lower()


async def test_many_cycle_edges_row_count_stays_bounded() -> None:
    """Finding 1: `RelationshipGraph.walk_dependents` does not cap
    `cycles`, so many cyclic back-edges must not translate into one
    rendered row each — the shared render budget must bound cycle rows
    too, with the omission visibly summarized."""
    app = HostApp()
    graph = _many_cycles_graph(count=50, max_nodes=2)
    screen = RelationshipScreen(graph, _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("d")
        table = app.screen.query_one(DataTable)
        await until(pilot, lambda: table.row_count > 3, label="expanded rows rendered")
        text = "\n".join(_all_cells(table))
        assert table.row_count <= graph.limits.max_nodes + 4
        assert "cycle" in text.lower()
        assert "capped" in text.lower()


def _graph_without_the_root() -> RelationshipGraph:
    """Complete coverage, but the root is not among the snapshot's nodes —
    it was deleted/recreated (stale UID) or dropped at a source cap. Its
    empty Dependencies/Dependents sections say nothing about the real
    cluster, so the screen must state that explicitly."""
    other = _resource("Pod", "unrelated-0")
    return RelationshipGraph(nodes=(other,), edges=(), coverage=())


async def test_root_absent_from_the_snapshot_is_stated_even_when_coverage_is_complete() -> None:
    app = HostApp()
    root = GraphResource(group="", kind="Pod", namespace=_NAMESPACE, name="api-0", uid="stale-uid")
    screen = RelationshipScreen(_graph_without_the_root(), root)
    async with app.run_test():
        await app.push_screen(screen)
        banner = str(app.screen.query_one("#relationship-coverage", Static).render())
        assert "complete" in banner.lower()
        assert "not present in this snapshot" in banner.lower()


async def test_root_present_in_the_snapshot_gets_no_absence_note() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph(), _resource("Deployment", "api"))
    async with app.run_test():
        await app.push_screen(screen)
        banner = str(app.screen.query_one("#relationship-coverage", Static).render())
        assert "not present in this snapshot" not in banner.lower()


async def test_absent_root_note_survives_the_coverage_detail_toggle() -> None:
    app = HostApp()
    root = GraphResource(group="", kind="Pod", namespace=_NAMESPACE, name="api-0", uid="stale-uid")
    screen = RelationshipScreen(_graph_without_the_root(), root)
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("c")
        banner = str(app.screen.query_one("#relationship-coverage", Static).render())
        assert "not present in this snapshot" in banner.lower()
