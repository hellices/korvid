"""Hierarchy tree navigation (issue #120): Enter on a helm release or an OLM
Subscription/CSV opens the component tree; `h` keeps the revision history."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest import mock

from textual.app import ScreenStackError

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.components import MAX_COMPONENT_DOCS, ComponentRef
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import (
    HELM_RELEASES_META,
    HELM_REVISIONS_META,
    HelmReleaseSummary,
    HelmRevisionSummary,
    release_uid,
)
from korvid.k8s.models import GenericSummary, OLMSubscriptionSummary
from korvid.k8s.olm import OPERATORS_GROUP
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.hierarchy_screen import HierarchyScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_SVC_META = ResourceMeta("Service", "services", "", "v1", True, ("svc",))
SUB_META = ResourceMeta("Subscription", "subscriptions", OPERATORS_GROUP, "v1alpha1", True)
CSV_META = ResourceMeta(
    "ClusterServiceVersion", "clusterserviceversions", OPERATORS_GROUP, "v1alpha1", True, ("csv",)
)
IP_META = ResourceMeta("InstallPlan", "installplans", OPERATORS_GROUP, "v1alpha1", True)
OPERATOR_META = ResourceMeta("Operator", "operators", OPERATORS_GROUP, "v1", False)

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "deployments": _DEPLOY_META,
    "services": _SVC_META,
    "helm": HELM_RELEASES_META,
    "helmreleases": HELM_RELEASES_META,
    "helmrevisions": HELM_REVISIONS_META,
    "subscriptions": SUB_META,
    "clusterserviceversions": CSV_META,
    "installplans": IP_META,
    f"operators.{OPERATORS_GROUP}": OPERATOR_META,
}


def _release(name: str, revision: int = 3) -> HelmReleaseSummary:
    return HelmReleaseSummary(
        name=name,
        namespace="default",
        kind="HelmRelease",
        created="2026-07-26T10:00:00Z",
        uid=release_uid("default", name),
        revision=revision,
        status="deployed",
        chart="nginx-1.2.3",
        app_version="1.25",
    )


def _revision(release: str, revision: int) -> HelmRevisionSummary:
    return HelmRevisionSummary(
        name=f"{release}.v{revision}",
        namespace="default",
        kind="HelmRevision",
        created="2026-07-26T10:00:00Z",
        uid=f"secret-uid-{release}-{revision}",
        owner_uids=(release_uid("default", release),),
        release=release,
        revision=revision,
        status="superseded",
        chart="nginx-1.2.3",
        app_version="1.25",
        description="Upgrade complete",
    )


def _deployment(name: str, namespace: str = "default") -> GenericSummary:
    return GenericSummary(
        name=name,
        namespace=namespace,
        kind="Deployment",
        created="2026-07-26T10:00:00Z",
        uid=f"dep-{name}",
    )


def _subscription(name: str) -> OLMSubscriptionSummary:
    return OLMSubscriptionSummary(
        name=name,
        namespace="operators",
        kind="Subscription",
        created="2026-07-26T10:00:00Z",
        uid=f"sub-{name}",
        channel="stable",
        source="operatorhubio-catalog",
        installed_csv=f"{name}.v1.14.4",
        state="AtLatestKnown",
    )


def make_app(
    data: dict[str, list[Summary]],
    *,
    components: dict[str, list[ComponentRef]] | None = None,
    manifests: dict[str, dict[str, Any]] | None = None,
    namespace: str = "default",
    aliases: dict[str, ResourceMeta] | None = None,
    helm_components: bool = True,
) -> tuple[KorvidApp, list[tuple[str, str | None, str]]]:
    store = ResourceStore()
    describe_calls: list[tuple[str, str | None, str]] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            if scope == ALL_NAMESPACES or not obj.namespace or obj.namespace == scope:
                yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        describe_calls.append((kind, ns, name))
        return (manifests or {}).get(name, {"kind": "Object", "metadata": {"name": name}})

    async def get_helm_components(ns: str, name: str) -> list[ComponentRef]:
        return (components or {}).get(name, [])

    async def list_namespaces() -> list[str]:
        return [namespace]

    app = KorvidApp(
        config=KorvidConfig(namespace=namespace),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(aliases if aliases is not None else _ALIASES),
        get_manifest=get_manifest,
        get_helm_components=get_helm_components if helm_components else None,
    )
    return app, describe_calls


async def _navigate(pilot, command: str, expect_kind: str) -> None:  # type: ignore[no-untyped-def]  # Pilot is generic; concrete app type not exposed
    await pilot.press("colon")
    for ch in command:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")
    await until(
        pilot, lambda: pilot.app.current_kind == expect_kind, label=f"view is {expect_kind}"
    )


def _tree_labels(app: KorvidApp) -> list[str]:
    screen = app.screen
    assert isinstance(screen, HierarchyScreen)
    from textual.widgets import Tree

    tree = screen.query_one(Tree)
    return [str(line.node.label) for line in tree._tree_lines]


def _base_screen_ready(app: KorvidApp) -> bool:
    return len(app.screen_stack) == 1 and not isinstance(app.screen, HierarchyScreen)


_HELM_DATA: dict[str, list[Summary]] = {
    "helmreleases": [_release("web")],
    "helmrevisions": [_revision("web", 1), _revision("web", 2), _revision("web", 3)],
    "deployments": [_deployment("web-nginx"), _deployment("other")],
}

_WEB_COMPONENTS = {
    "web": [
        ComponentRef(kind="Deployment", name="web-nginx"),
        ComponentRef(kind="MyCustomThing", name="cr-1"),
    ]
}


async def test_enter_on_release_opens_hierarchy_tree() -> None:
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        labels = _tree_labels(app)
        assert any("Deployment/web-nginx" in label for label in labels)
        assert any("MyCustomThing/cr-1" in label for label in labels)
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, HierarchyScreen), label="closed")
        assert app.current_kind == "helmreleases"


async def test_h_on_release_opens_revision_history() -> None:
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("h")
        await until(pilot, lambda: app.current_kind == "helmrevisions", label="history open")
        await until(pilot, lambda: table.row_count == 3, label="revisions listed")
        await pilot.press("escape")
        await until(pilot, lambda: app.current_kind == "helmreleases", label="popped")


async def test_goto_from_tree_jumps_to_view_with_cursor() -> None:
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")  # Deployment/web-nginx
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await until(
            pilot,
            lambda: app._cursor_row_key() == "default/web-nginx",
            label="target resource selected",
        )


async def test_escape_after_goto_reopens_the_tree_on_the_release_view() -> None:
    """Regression for issue #135: jumping to a component must leave a way
    back - Escape on the jump target reopens the hierarchy tree over the
    origin view, cursor still on the picked node."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")  # Deployment/web-nginx
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="tree reopened")
        # the tree reopens over the origin view, not the jump target
        assert app.current_kind == "helmreleases"
        # cursor back on the node that was picked
        from textual.widgets import Tree

        tree = app.screen.query_one(Tree)
        cursor = tree.cursor_node
        assert cursor is not None
        assert cursor.data is not None
        assert (cursor.data.kind, cursor.data.name) == ("deployments", "web-nginx")
        # closing the reopened tree lands on the release view as usual
        await pilot.press("escape")
        await until(pilot, lambda: not isinstance(app.screen, HierarchyScreen), label="closed")
        assert app.current_kind == "helmreleases"
        # the return is one-shot: another Escape must not resurrect the tree
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HierarchyScreen)


async def test_explicit_navigation_clears_the_hierarchy_return() -> None:
    """A :view navigation abandons the pending tree return - Escape on the
    new view must not teleport back to a tree the user walked away from."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await _navigate(pilot, "pods", "pods")
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HierarchyScreen)


async def test_escape_over_a_modal_keeps_the_hierarchy_return() -> None:
    """Escape that closes another modal (help, describe, ...) on the jump
    target must not consume the pending tree return - the *next* Escape on
    the base view still reopens the tree."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await pilot.press("question_mark")  # help modal over the jump target
        await until(pilot, lambda: len(app.screen_stack) > 1, label="help open")
        await pilot.press("escape")  # closes help - must not eat the return
        await until(pilot, lambda: len(app.screen_stack) == 1, label="help closed")
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="tree reopened")
        assert app.current_kind == "helmreleases"  # reopened over the origin view


async def test_hierarchy_return_is_scoped_to_the_initiating_pane() -> None:
    """The pending return belongs to the pane that jumped: Escape in the
    other pane must not consume it or hijack that pane's view; back in the
    initiating pane, Escape still reopens the tree."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await pilot.press("ctrl+w", "v")  # split: clone pane, focus pane 1
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split")
        await pilot.press("escape")  # pane 1 shows the same kind - must not hijack
        await pilot.pause()
        assert not isinstance(app.screen, HierarchyScreen)
        assert app._panes[1].kind == "deployments"  # pane 1 was not navigated away
        await pilot.press("ctrl+w", "w")  # focus back to the initiating pane 0
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="tree reopened")
        assert app._panes[0].kind == "helmreleases"


async def test_a_jump_in_one_pane_does_not_erase_the_other_panes_return() -> None:
    """Pending returns are per pane: opening a tree and jumping in pane B
    must not overwrite pane A's way back."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")
        await pilot.press("enter")  # pane 0 jumps; its return is pending
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await pilot.press("ctrl+w", "v")  # split: focus pane 1
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split")
        await _navigate(pilot, "helm", "helmreleases")  # pane 1 to the helm view
        await pilot.press("enter")  # pane 1 opens its own tree
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="tree 2 open")
        await pilot.press("down")
        await pilot.press("enter")  # pane 1 jumps too
        await until(pilot, lambda: app._panes[1].kind == "deployments", label="pane 1 jumped")
        await pilot.press("ctrl+w", "w")  # back to pane 0
        await pilot.press("escape")
        await until(
            pilot, lambda: isinstance(app.screen, HierarchyScreen), label="pane 0 tree reopened"
        )
        assert app._panes[0].kind == "helmreleases"


async def test_return_origin_is_captured_at_tree_open_not_at_dismissal() -> None:
    """agent_navigate may change the pane while the tree is open: the
    return's origin must be the view the tree was opened over, not
    whatever the pane happened to show at dismissal."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        # the agent switches the underlying pane while the tree is open
        result = await app.agent_navigate("pods", None)
        assert result.startswith("switched")
        await pilot.press("down")
        await pilot.press("enter")  # goto Deployment/web-nginx
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="tree reopened")
        # ...over the view the tree was actually opened from
        assert app.current_kind == "helmreleases"


async def test_return_is_refused_when_a_ctx_switch_starts_during_the_navigate() -> None:
    """The reopen path awaits a navigation: a context switch that starts
    while it holds the nav lock must abort the reopen - pushing the old
    cluster's tree over a tearing-down view would describe the wrong
    cluster."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")

        # A switch begins the moment the reopen's navigate runs.
        original = app._navigate

        async def navigate_then_switch(*args: object, **kwargs: object) -> None:
            await original(*args, **kwargs)  # type: ignore[arg-type]  # passthrough wrapper
            app._ctx_switching = True

        app._navigate = navigate_then_switch  # type: ignore[method-assign]  # test seam
        try:
            await pilot.press("escape")
            await until(
                pilot,
                lambda: app._ctx_switching and not isinstance(app.screen, HierarchyScreen),
                label="hierarchy return aborted by context switch",
            )
            assert not isinstance(app.screen, HierarchyScreen)
        finally:
            app._navigate = original  # type: ignore[method-assign]  # restore
            app._ctx_switching = False


async def test_refresh_hierarchy_survives_an_empty_screen_stack() -> None:
    """A ResourcesUpdated dispatched during app teardown reaches
    _refresh_hierarchy after the screen stack is emptied: reading
    App.screen then raises ScreenStackError and fails the whole run
    (flaky-CI issue #147) - the refresh must treat 'no screen' as
    'no tree open'."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        # Simulate the teardown interleaving deterministically: the message
        # handler runs while the screen stack is already empty.
        with mock.patch.object(
            type(app), "screen", property(mock.Mock(side_effect=ScreenStackError))
        ):
            app._refresh_hierarchy()  # must not raise
        assert True  # reaching here is the assertion: no ScreenStackError


async def test_describe_from_tree_node() -> None:
    app, describe_calls = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")  # Deployment/web-nginx
        await pilot.press("d")
        await until(
            pilot,
            lambda: ("deployments", "default", "web-nginx") in describe_calls,
            label="tree node describe fetched",
        )


_SUB_MANIFEST: dict[str, Any] = {
    "kind": "Subscription",
    "metadata": {"name": "argocd-operator", "namespace": "operators"},
    "spec": {"name": "argocd-operator"},
    "status": {
        "installedCSV": "argocd-operator.v1.14.4",
        "installPlanRef": {"name": "install-abc", "namespace": "operators"},
    },
}

_OPERATOR_MANIFEST: dict[str, Any] = {
    "kind": "Operator",
    "metadata": {"name": "argocd-operator.operators"},
    "status": {
        "components": {
            "refs": [
                {
                    "kind": "ClusterServiceVersion",
                    "name": "argocd-operator.v1.14.4",
                    "namespace": "operators",
                    "apiVersion": "operators.coreos.com/v1alpha1",
                },
                {
                    "kind": "Deployment",
                    "name": "argocd-operator-controller",
                    "namespace": "operators",
                    "apiVersion": "apps/v1",
                },
            ]
        }
    },
}


async def test_enter_on_subscription_opens_hierarchy_from_operator_refs() -> None:
    app, _ = make_app(
        {"subscriptions": [_subscription("argocd-operator")]},
        manifests={
            "argocd-operator": _SUB_MANIFEST,
            "argocd-operator.operators": _OPERATOR_MANIFEST,
        },
        namespace="operators",
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "subscriptions", "subscriptions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="subscription listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        labels = _tree_labels(app)
        assert any("Deployment/argocd-operator-controller" in label for label in labels)
        assert any("ClusterServiceVersion/argocd-operator.v1.14.4" in label for label in labels)


async def test_subscription_falls_back_to_installplan_components() -> None:
    """Without the Operator API (older OLM), the InstallPlan's status.plan
    still names everything the install created."""
    aliases_without_operator = {
        k: v for k, v in _ALIASES.items() if k != f"operators.{OPERATORS_GROUP}"
    }
    ip_manifest: dict[str, Any] = {
        "kind": "InstallPlan",
        "status": {
            "plan": [
                {
                    "resource": {
                        "group": "apps",
                        "version": "v1",
                        "kind": "Deployment",
                        "name": "argocd-operator-controller",
                    }
                }
            ]
        },
    }
    app, _ = make_app(
        {"subscriptions": [_subscription("argocd-operator")]},
        manifests={"argocd-operator": _SUB_MANIFEST, "install-abc": ip_manifest},
        namespace="operators",
        aliases=aliases_without_operator,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "subscriptions", "subscriptions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="subscription listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        labels = _tree_labels(app)
        assert any("Deployment/argocd-operator-controller" in label for label in labels)


def test_component_resolution_matches_declared_group() -> None:
    """Two CRDs sharing a Kind across groups: the declared apiVersion picks
    the right view, and namespacedness rides along for scoping."""
    aliases = dict(_ALIASES)
    aliases["widgets.a.example.com"] = ResourceMeta(
        "Widget", "widgets", "a.example.com", "v1", True
    )
    aliases["widgets.b.example.com"] = ResourceMeta(
        "Widget", "widgets", "b.example.com", "v1", False
    )
    app, _ = make_app({}, aliases=aliases)
    ref_b = ComponentRef(kind="Widget", name="x", api_version="b.example.com/v1")
    assert app._view_for_component(ref_b) == ("widgets.b.example.com", False)
    ref_a = ComponentRef(kind="Widget", name="x", api_version="a.example.com/v1")
    assert app._view_for_component(ref_a) == ("widgets.a.example.com", True)
    # Core group ("v1") and undeclared apiVersion still resolve normally.
    assert app._view_for_component(ComponentRef(kind="Pod", name="p", api_version="v1")) == (
        "pods",
        True,
    )
    assert app._view_for_component(ComponentRef(kind="Deployment", name="d")) == (
        "deployments",
        True,
    )
    # A declared group with no discovered match must not fall back to the
    # wrong group's view.
    ghost = ComponentRef(kind="Widget", name="x", api_version="c.example.com/v1")
    assert app._view_for_component(ghost) is None


async def test_open_tree_refreshes_on_store_update() -> None:
    """A store event for a watched kind rebuilds the open tree in place
    (issue #120 requires live updates via store notifications)."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        # A second pane elsewhere could be watching deployments; simulate it.
        await app.watch_manager.start("deployments", "default")
        await until(
            pilot,
            lambda: len(app.store.get("deployments", "default")) == 2,
            label="deployments store populated",
        )
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        assert any(label == "Deployment/web-nginx" for label in _tree_labels(app))
        app.store.apply_event("deployments", "default", "DELETED", _deployment("web-nginx"))
        await until(
            pilot,
            lambda: any("Deployment/web-nginx (missing)" in label for label in _tree_labels(app)),
            label="tree refreshed with missing marker",
        )


async def test_tree_does_not_open_when_view_changed_during_fetch() -> None:
    """The user moved on while components were being fetched: the tree must
    not pop over an unrelated view."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    gate = asyncio.Event()

    async def slow_components(ns: str, name: str) -> list[ComponentRef]:
        await gate.wait()
        return _WEB_COMPONENTS["web"]

    app._get_helm_components = slow_components
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")  # fetch parked on the gate
        await _navigate(pilot, "pods", "pods")
        gate.set()
        await until(
            pilot,
            lambda: all(worker.is_finished for worker in app.workers),
            label="hierarchy worker finished",
        )
        assert not isinstance(app.screen, HierarchyScreen)
        assert app.current_kind == "pods"


async def test_enter_on_csv_opens_hierarchy_from_operator_labels() -> None:
    """CSV path: the operators.coreos.com/<name> label OLM stamps on the CSV
    leads to the Operator object's component refs."""
    csv = GenericSummary(
        name="argocd-operator.v1.14.4",
        namespace="operators",
        kind="ClusterServiceVersion",
        created="2026-07-26T10:00:00Z",
        uid="csv-1",
    )
    csv_manifest: dict[str, Any] = {
        "kind": "ClusterServiceVersion",
        "metadata": {
            "name": "argocd-operator.v1.14.4",
            "labels": {"operators.coreos.com/argocd-operator.operators": ""},
        },
    }
    app, _ = make_app(
        {"clusterserviceversions": [csv]},
        manifests={
            "argocd-operator.v1.14.4": csv_manifest,
            "argocd-operator.operators": _OPERATOR_MANIFEST,
        },
        namespace="operators",
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "clusterserviceversions", "clusterserviceversions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="csv listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        labels = _tree_labels(app)
        assert any("Deployment/argocd-operator-controller" in label for label in labels)
        # The Operator's refs include the CSV itself; the root must not be
        # re-listed as its own child (Enter there would loop back here).
        assert not any(
            label.startswith("ClusterServiceVersion/argocd-operator.v1.14.4")
            for label in labels[1:]
        )


async def test_same_named_component_in_another_namespace_is_kept() -> None:
    """The root self-filter matches on the full identity: OLM copies CSVs
    into other namespaces under the same name, and those copies are real
    components, not the root."""
    csv = GenericSummary(
        name="argocd-operator.v1.14.4",
        namespace="operators",
        kind="ClusterServiceVersion",
        created="2026-07-26T10:00:00Z",
        uid="csv-1",
    )
    csv_manifest: dict[str, Any] = {
        "kind": "ClusterServiceVersion",
        "metadata": {
            "name": "argocd-operator.v1.14.4",
            "labels": {"operators.coreos.com/argocd-operator.operators": ""},
        },
    }
    operator_manifest: dict[str, Any] = {
        "kind": "Operator",
        "metadata": {"name": "argocd-operator.operators"},
        "status": {
            "components": {
                "refs": [
                    {
                        "kind": "ClusterServiceVersion",
                        "name": "argocd-operator.v1.14.4",
                        "namespace": "operators",
                        "apiVersion": "operators.coreos.com/v1alpha1",
                    },
                    {
                        "kind": "ClusterServiceVersion",
                        "name": "argocd-operator.v1.14.4",
                        "namespace": "other-ns",
                        "apiVersion": "operators.coreos.com/v1alpha1",
                    },
                ]
            }
        },
    }
    app, _ = make_app(
        {"clusterserviceversions": [csv]},
        manifests={
            "argocd-operator.v1.14.4": csv_manifest,
            "argocd-operator.operators": operator_manifest,
        },
        namespace="operators",
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "clusterserviceversions", "clusterserviceversions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="csv listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        screen = app.screen
        assert isinstance(screen, HierarchyScreen)
        children = screen._root.children
        # The root's own entry is dropped; the other-namespace copy stays.
        assert [(c.name, c.namespace) for c in children] == [
            ("argocd-operator.v1.14.4", "other-ns")
        ]


async def test_enter_without_components_accessor_falls_back_to_revision_drill() -> None:
    """No get_helm_components wired (degraded session): Enter keeps the
    pre-#120 behaviour and drills into the release's revisions."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS, helm_components=False)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "helmrevisions", label="drilled")
        assert not isinstance(app.screen, HierarchyScreen)


async def test_jump_notifies_when_object_never_appears() -> None:
    """Goto for an object that never lands in the table must not fail
    silently - the user gets told instead of staring at a wrong cursor."""
    components = {"web": [ComponentRef(kind="Deployment", name="ghost")]}
    app, _ = make_app(_HELM_DATA, components=components)
    app._jump_poll_attempts = 3
    notices: list[str] = []
    original = app.notify

    def _capture(message: str, **kwargs: Any) -> Any:
        notices.append(message)
        return original(message, **kwargs)

    async with app.run_test() as pilot:
        app.notify = _capture  # type: ignore[method-assign]  # test spy
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        await pilot.press("down")  # Deployment/ghost
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="jumped")
        await until(
            pilot,
            lambda: any("ghost" in n for n in notices),
            label="missing jump target notification",
        )


async def test_lookup_uses_a_watch_covering_the_component_namespace() -> None:
    """A component declared in another namespace must not be judged by the
    root-scope bucket: a watch on the root scope that cannot contain the
    object must not produce a false '(missing)'."""
    svc = GenericSummary(
        name="ext",
        namespace="other",
        kind="Service",
        created="2026-07-26T10:00:00Z",
        uid="svc-ext",
    )
    data: dict[str, list[Summary]] = {**_HELM_DATA, "services": [svc]}
    components = {"web": [ComponentRef(kind="Service", name="ext", namespace="other")]}
    app, _ = make_app(data, components=components)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        # The root-scope services watch is live but cannot contain "ext".
        await app.watch_manager.start("services", "default")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        assert any(label == "Service/ext" for label in _tree_labels(app))
        assert not any("(missing)" in label for label in _tree_labels(app))


async def test_jump_aborts_on_stale_context_epoch() -> None:
    """A context switch that crosses a tree-goto must stop it before it
    navigates to (or focuses) a same-named object in the new cluster."""
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await app._jump_to_object("deployments", "default", "web-nginx", epoch=app._ctx_epoch - 1)
        assert app.current_kind == "helmreleases"


async def test_csv_without_operator_api_lists_owned_workloads() -> None:
    """Third OLM source (issue #120): without the Operator API, Deployments
    whose ownerReferences point at the CSV still populate the tree."""
    csv = GenericSummary(
        name="argocd-operator.v1.14.4",
        namespace="operators",
        kind="ClusterServiceVersion",
        created="2026-07-26T10:00:00Z",
        uid="csv-uid-1",
    )
    owned = GenericSummary(
        name="argocd-operator-controller",
        namespace="operators",
        kind="Deployment",
        created="2026-07-26T10:00:00Z",
        uid="dep-owned",
        owner_uids=("csv-uid-1",),
    )
    csv_manifest: dict[str, Any] = {
        "kind": "ClusterServiceVersion",
        "metadata": {"name": "argocd-operator.v1.14.4", "uid": "csv-uid-1"},
    }
    aliases_without_operator = {
        k: v for k, v in _ALIASES.items() if k != f"operators.{OPERATORS_GROUP}"
    }
    app, _ = make_app(
        {"clusterserviceversions": [csv], "deployments": [owned]},
        manifests={"argocd-operator.v1.14.4": csv_manifest},
        namespace="operators",
        aliases=aliases_without_operator,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "clusterserviceversions", "clusterserviceversions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="csv listed")
        await app.watch_manager.start("deployments", "operators")
        await until(
            pilot,
            lambda: len(app.store.get("deployments", "operators")) == 1,
            label="owned deployment watched",
        )
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        assert any("Deployment/argocd-operator-controller" in label for label in _tree_labels(app))


async def test_owned_workloads_fallback_is_capped() -> None:
    """The CSV ownerReferences fallback obeys the same component cap as the
    manifest parsers - a pathological bucket cannot flood the tree."""
    csv = GenericSummary(
        name="argocd-operator.v1.14.4",
        namespace="operators",
        kind="ClusterServiceVersion",
        created="2026-07-26T10:00:00Z",
        uid="csv-uid-1",
    )
    owned: list[Summary] = [
        GenericSummary(
            name=f"owned-{i}",
            namespace="operators",
            kind="Deployment",
            created="2026-07-26T10:00:00Z",
            uid=f"dep-{i}",
            owner_uids=("csv-uid-1",),
        )
        for i in range(MAX_COMPONENT_DOCS + 1)
    ]
    csv_manifest: dict[str, Any] = {
        "kind": "ClusterServiceVersion",
        "metadata": {"name": "argocd-operator.v1.14.4", "uid": "csv-uid-1"},
    }
    app, _ = make_app(
        {"clusterserviceversions": [csv], "deployments": owned},
        manifests={"argocd-operator.v1.14.4": csv_manifest},
        namespace="operators",
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await app.watch_manager.start("deployments", "operators")
        await until(
            pilot,
            lambda: len(app.store.get("deployments", "operators")) == len(owned),
            label="owned deployments watched",
        )
        refs = app._refs_from_owned_workloads(csv_manifest, "operators")
        assert len(refs) == MAX_COMPONENT_DOCS


async def test_alias_discovery_refreshes_open_tree() -> None:
    """A kind discovered while the tree is open (background alias merge)
    turns its display-only nodes navigable on the next aliases update."""

    def _kind_of(app: KorvidApp, label: str) -> str:
        screen = app.screen
        assert isinstance(screen, HierarchyScreen)
        from textual.widgets import Tree

        tree = screen.query_one(Tree)
        for line in tree._tree_lines:
            data = line.node.data
            if data is not None and data.label == label:
                return str(data.kind)
        return "<absent>"

    late_aliases = {k: v for k, v in _ALIASES.items() if k != "deployments"}
    app, _ = make_app(_HELM_DATA, components=_WEB_COMPONENTS, aliases=late_aliases)
    async with app.run_test() as pilot:
        await until(pilot, lambda: _base_screen_ready(app), label="base screen ready")
        await _navigate(pilot, "helm", "helmreleases")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="release listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        assert _kind_of(app, "Deployment/web-nginx") == ""
        app.aliases["deployments"] = _ALIASES["deployments"]
        app.on_aliases_updated()
        await until(
            pilot,
            lambda: _kind_of(app, "Deployment/web-nginx") == "deployments",
            label="tree refreshed after alias discovery",
        )
