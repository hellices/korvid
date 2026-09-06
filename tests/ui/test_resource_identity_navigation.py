from typing import Any

from korvid.core.relationships import GraphResource
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.models import GenericSummary
from korvid.ui.relationship_controller import graph_source_metas
from tests.ui.test_app import make_app
from tests.ui.waits import until


async def test_agent_describe_retains_qualified_resource_identity() -> None:
    native = ResourceMeta("Subscription", "subscriptions", "operators.coreos.com", "v1", True)
    foreign = ResourceMeta("Subscription", "subscriptions", "example.io", "v1", True)
    aliases = build_alias_map([PODS_META, foreign, native])
    calls: list[str] = []

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        calls.append(kind)
        meta = aliases[kind]
        return {
            "apiVersion": f"{meta.group}/{meta.version}",
            "kind": meta.kind,
            "metadata": {"name": name, "namespace": namespace},
        }

    app = make_app([], aliases=aliases, get_manifest=get_manifest)
    async with app.run_test():
        result = await app._agent_ui.agent_open_describe(
            "subscriptions.operators.coreos.com", "web", "default"
        )
        assert not result.startswith("ERROR:")
        assert calls == ["subscriptions.operators.coreos.com"]


def test_relationship_sources_resolve_native_group_after_alias_collision() -> None:
    native = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
    foreign = ResourceMeta("Deployment", "deployments", "example.io", "v1", True)
    aliases = build_alias_map([PODS_META, foreign, native])
    root = GraphResource(group="", kind="Pod", namespace="default", name="web")
    sources, missing = graph_source_metas(root, "default", aliases)
    assert native in sources
    assert foreign not in sources
    assert not any(spec.group == "apps" and spec.plural == "deployments" for spec in missing)


async def test_native_drill_uses_qualified_child_after_plural_collision() -> None:
    deployment = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
    native = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True)
    foreign = ResourceMeta("ReplicaSet", "replicasets", "example.io", "v1", True)
    aliases = build_alias_map([PODS_META, deployment, foreign, native])
    parent = GenericSummary(
        name="web", namespace="default", kind="Deployment", created="", uid="deployment-uid"
    )
    child = GenericSummary(
        name="web-rs",
        namespace="default",
        kind="ReplicaSet",
        created="",
        owner_uids=("deployment-uid",),
    )
    app = make_app(
        [],
        aliases=aliases,
        extra_data={"deployments": [parent], "replicasets.apps": [child]},
    )
    async with app.run_test() as pilot:
        await app._workspace_ctl.navigate("deployments", "default")
        await until(
            pilot,
            lambda: bool(app.store.get("deployments", "default")),
            label="deployment snapshot",
        )
        assert await app._workspace_ctl.drill_into("default", "web") is None
        assert app.current_kind == "replicasets.apps"
        assert app.store.get("replicasets.apps", "default") == [child]
