"""Hierarchy tree navigation (issue #120): Enter on a helm release or an OLM
Subscription/CSV opens the component tree; `h` keeps the revision history."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.components import ComponentRef
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
SUB_META = ResourceMeta("Subscription", "subscriptions", OPERATORS_GROUP, "v1alpha1", True)
CSV_META = ResourceMeta(
    "ClusterServiceVersion", "clusterserviceversions", OPERATORS_GROUP, "v1alpha1", True, ("csv",)
)
IP_META = ResourceMeta("InstallPlan", "installplans", OPERATORS_GROUP, "v1alpha1", True)
OPERATOR_META = ResourceMeta("Operator", "operators", OPERATORS_GROUP, "v1", False)

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "deployments": _DEPLOY_META,
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
) -> tuple[KorvidApp, list[tuple[str, str | None, str]]]:
    store = ResourceStore()
    describe_calls: list[tuple[str, str | None, str]] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
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
        get_helm_components=get_helm_components,
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
        await pilot.pause(0.1)
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
        await pilot.pause(0.1)
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
        await pilot.pause(0.1)
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
            label="cursor on target",
        )


async def test_describe_from_tree_node() -> None:
    app, describe_calls = make_app(_HELM_DATA, components=_WEB_COMPONENTS)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
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
            label="describe fetched",
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
        await pilot.pause(0.1)
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
        await pilot.pause(0.1)
        await _navigate(pilot, "subscriptions", "subscriptions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="subscription listed")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HierarchyScreen), label="hierarchy open")
        labels = _tree_labels(app)
        assert any("Deployment/argocd-operator-controller" in label for label in labels)
