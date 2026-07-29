"""Hierarchy tree for helm releases and operators (issue #120).

`build_hierarchy` turns component refs plus live-store lookups into a
`HierarchyNode` tree; `HierarchyScreen` renders it and resolves to a
("goto"|"describe", kind, namespace, name) tuple or None.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.widgets import Static, Tree

from korvid.k8s.components import ComponentRef
from korvid.ui.widgets.hierarchy_screen import (
    HierarchyNode,
    HierarchyScreen,
    build_hierarchy,
)

# ---------------------------------------------------------------------------
# Pure unit tests: build_hierarchy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Live:
    name: str
    namespace: str
    uid: str = ""
    owner_uids: tuple[str, ...] = ()


_VIEWS = {
    "Deployment": "deployments",
    "ReplicaSet": "replicasets",
    "Pod": "pods",
    "Service": "services",
    "ConfigMap": "configmaps",
}


def _resolve(kind: str) -> str | None:
    return _VIEWS.get(kind)


def _lookup_from(data: dict[str, list[_Live]]):  # type: ignore[no-untyped-def]  # test helper
    def lookup(view: str) -> list[_Live]:
        return data.get(view, [])

    return lookup


def test_components_become_child_nodes() -> None:
    refs = [
        ComponentRef(kind="Service", name="web"),
        ComponentRef(kind="ConfigMap", name="web-config"),
    ]
    data = {
        "services": [_Live("web", "default")],
        "configmaps": [_Live("web-config", "default")],
    }
    root = build_hierarchy(
        "helm/web", refs, namespace="default", resolve=_resolve, lookup=_lookup_from(data)
    )
    assert root.label == "helm/web"
    assert [c.label for c in root.children] == ["Service/web", "ConfigMap/web-config"]
    svc = root.children[0]
    assert (svc.kind, svc.namespace, svc.name) == ("services", "default", "web")


def test_deployment_expands_runtime_descendants_by_owner_uids() -> None:
    refs = [ComponentRef(kind="Deployment", name="web")]
    data = {
        "deployments": [_Live("web", "default", uid="dep-1")],
        "replicasets": [
            _Live("web-abc", "default", uid="rs-1", owner_uids=("dep-1",)),
            _Live("other-rs", "default", uid="rs-9", owner_uids=("dep-9",)),
        ],
        "pods": [
            _Live("web-abc-1", "default", uid="p-1", owner_uids=("rs-1",)),
            _Live("web-abc-2", "default", uid="p-2", owner_uids=("rs-1",)),
            _Live("stranger", "default", uid="p-9", owner_uids=("rs-9",)),
        ],
    }
    root = build_hierarchy(
        "helm/web", refs, namespace="default", resolve=_resolve, lookup=_lookup_from(data)
    )
    dep = root.children[0]
    assert [c.label for c in dep.children] == ["ReplicaSet/web-abc"]
    rs = dep.children[0]
    assert [c.label for c in rs.children] == ["Pod/web-abc-1", "Pod/web-abc-2"]
    assert rs.children[0].kind == "pods"


def test_missing_live_object_is_marked() -> None:
    refs = [ComponentRef(kind="Service", name="gone")]
    root = build_hierarchy(
        "helm/web", refs, namespace="default", resolve=_resolve, lookup=_lookup_from({})
    )
    node = root.children[0]
    assert node.label == "Service/gone (missing)"
    # Still navigable: describe on a missing object gives the 404 explanation.
    assert (node.kind, node.name) == ("services", "gone")


def test_unknown_kind_is_shown_but_not_navigable() -> None:
    refs = [ComponentRef(kind="MyCustomThing", name="x")]
    root = build_hierarchy(
        "helm/web", refs, namespace="default", resolve=_resolve, lookup=_lookup_from({})
    )
    node = root.children[0]
    assert node.label == "MyCustomThing/x"
    assert node.kind == ""


def test_component_namespace_overrides_release_namespace() -> None:
    refs = [ComponentRef(kind="Service", name="web", namespace="other")]
    data = {"services": [_Live("web", "other")]}
    root = build_hierarchy(
        "helm/web", refs, namespace="default", resolve=_resolve, lookup=_lookup_from(data)
    )
    assert root.children[0].namespace == "other"


# ---------------------------------------------------------------------------
# Pilot tests: HierarchyScreen
# ---------------------------------------------------------------------------


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.result: object = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


def _sample_root() -> HierarchyNode:
    pod = HierarchyNode(label="Pod/web-1", kind="pods", namespace="default", name="web-1")
    dep = HierarchyNode(
        label="Deployment/web",
        kind="deployments",
        namespace="default",
        name="web",
        children=[pod],
    )
    crd = HierarchyNode(label="MyCustomThing/x")
    return HierarchyNode(label="helm/web", children=[dep, crd])


async def test_screen_renders_expanded_tree() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(HierarchyScreen("helm/web", _sample_root()))
        await pilot.pause()
        tree = app.screen.query_one(Tree)
        labels = [str(line.node.label) for line in tree._tree_lines]
        assert "helm/web" in labels[0]
        assert any("Deployment/web" in label for label in labels)
        assert any("Pod/web-1" in label for label in labels)


async def test_enter_on_navigable_node_dismisses_with_goto() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: object) -> None:
            app.result = v

        await app.push_screen(HierarchyScreen("helm/web", _sample_root()), _done)
        await pilot.pause()
        await pilot.press("down")  # Deployment/web
        await pilot.press("enter")
        await pilot.pause()
        assert app.result == ("goto", "deployments", "default", "web")


async def test_enter_on_non_navigable_node_keeps_screen_open() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(HierarchyScreen("helm/web", _sample_root()))
        await pilot.pause()
        await pilot.press("down", "down", "down")  # MyCustomThing/x
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, HierarchyScreen)


async def test_d_dismisses_with_describe() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: object) -> None:
            app.result = v

        await app.push_screen(HierarchyScreen("helm/web", _sample_root()), _done)
        await pilot.pause()
        await pilot.press("down")  # Deployment/web
        await pilot.press("d")
        await pilot.pause()
        assert app.result == ("describe", "deployments", "default", "web")


async def test_escape_dismisses_with_none() -> None:
    app = HostApp()
    async with app.run_test() as pilot:

        def _done(v: object) -> None:
            app.result = v

        await app.push_screen(HierarchyScreen("helm/web", _sample_root()), _done)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.result is None
