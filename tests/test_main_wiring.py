"""Tests for composition-root helpers in korvid.__main__."""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path
from typing import Any, cast

import pytest

from korvid.__main__ import _close_provider_in_background
from korvid.agent.setup import AgentSettings
from korvid.tools.executor import UIBridge
from tests.fixtures.provider_plugin.site_helpers import (
    FIXTURES_DIR,
    build_dist_info,
    discover_provider_entry_points,
)


class _BoomProvider:
    async def aclose(self) -> None:
        raise RuntimeError("boom")


class _OkProvider:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_close_task_reference_is_retained_until_done() -> None:
    from korvid.agent.provider import LLMProvider

    provider = _OkProvider()
    tasks: set[asyncio.Task[None]] = set()
    _close_provider_in_background(cast("LLMProvider", provider), tasks)
    assert len(tasks) == 1  # strong reference held while pending
    for _ in range(3):
        await asyncio.sleep(0)
    assert provider.closed
    assert not tasks  # reaped once complete


async def test_close_errors_are_consumed() -> None:
    from korvid.agent.provider import LLMProvider

    tasks: set[asyncio.Task[None]] = set()
    _close_provider_in_background(cast("LLMProvider", _BoomProvider()), tasks)
    for _ in range(3):
        await asyncio.sleep(0)
    # Exception must be retrieved by the done callback (no unhandled-task
    # warning); the set must not leak the failed task.
    assert not tasks


# --- Slice 3: late-bound UI bridge proxy ---


class _FakeApp(UIBridge):
    """Nominal test double for the app-side bridge."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        self.calls.append(f"navigate:{view}:{namespace}")
        return "ok-nav"

    async def agent_set_filter(self, pattern: str) -> str:
        self.calls.append(f"filter:{pattern}")
        return "ok-filter"

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        self.calls.append(f"logs:{namespace}/{pod}")
        return "ok-logs"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        self.calls.append(f"describe:{kind}/{name}")
        return "ok-describe"

    async def agent_drill_down(self, name: str) -> str:
        self.calls.append(f"drill:{name}")
        return "ok-drill"

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        self.calls.append(f"write:{action}:{kind}/{name}:{resources}")
        return "ok-write"

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        self.calls.append(f"propose:{action}:{kind}/{name}")
        return "ok-propose"

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        self.calls.append(f"proposal-status:{proposal_id}")
        return "ok-status"

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        self.calls.append(f"proposal-cancel:{proposal_id}")
        return "ok-cancel"


async def test_proxy_without_target_returns_error() -> None:
    from korvid.__main__ import _UIBridgeProxy

    proxy = _UIBridgeProxy()
    assert (await proxy.agent_navigate("pods")).startswith("ERROR:")
    assert (await proxy.agent_set_filter("x")).startswith("ERROR:")
    assert (await proxy.agent_open_logs("p", "ns")).startswith("ERROR:")
    assert (await proxy.agent_open_describe("pods", "p")).startswith("ERROR:")
    assert (await proxy.agent_drill_down("web")).startswith("ERROR:")
    assert (await proxy.agent_request_write("delete", "pods", "web-1")).startswith("ERROR:")


async def test_proxy_forwards_to_target() -> None:
    from korvid.__main__ import _UIBridgeProxy

    proxy = _UIBridgeProxy()
    app = _FakeApp()
    proxy.target = app
    assert await proxy.agent_navigate("pods", "prod") == "ok-nav"
    assert await proxy.agent_set_filter("web") == "ok-filter"
    assert await proxy.agent_open_logs("p", "ns") == "ok-logs"
    assert await proxy.agent_open_describe("pods", "p", "ns") == "ok-describe"
    assert await proxy.agent_drill_down("web") == "ok-drill"
    assert await proxy.agent_request_write("delete", "pods", "web-1", "ns") == "ok-write"
    resources = {"app": {"requests": {"cpu": "200m"}}}
    assert (
        await proxy.agent_request_write("resize", "pods", "web-1", "ns", None, resources)
        == "ok-write"
    )
    assert app.calls == [
        "navigate:pods:prod",
        "filter:web",
        "logs:ns/p",
        "describe:pods/p",
        "drill:web",
        "write:delete:pods/web-1:None",
        f"write:resize:pods/web-1:{resources}",
    ]


def test_agent_wiring_includes_ui_tools(monkeypatch: object) -> None:
    """The composition root arms the runtime with READ_TOOLS + UI_TOOLS."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
    )
    kube_stub = cast("Any", object())  # wiring never touches kube before a tool call
    runtime, _, _, _, _, _, proxy = _build_agent_wiring(config, kube_stub, {})
    assert runtime is not None
    names = [t["function"]["name"] for t in runtime._tools]
    assert "navigate" in names
    assert "list_resources" in names
    assert "delete_resource" in names  # writes armed by default (approval-gated)
    executor = cast("Any", runtime._executor)
    assert executor._ui is proxy

    # readonly strips every write tool: the model is never told they exist.
    ro_runtime, _, _, _, _, _, _ = _build_agent_wiring(
        dataclasses.replace(config, readonly=True), kube_stub, {}
    )
    assert ro_runtime is not None
    ro_names = [t["function"]["name"] for t in ro_runtime._tools]
    assert "delete_resource" not in ro_names
    assert "scale_resource" not in ro_names
    assert "rollout_restart" not in ro_names


def test_agent_wiring_gates_resize_tool_on_discovery(monkeypatch: object) -> None:
    """resize_pod is offered only when the cluster has pods/resize (issue #27),
    and never in readonly mode."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
    )
    kube_stub = cast("Any", object())

    runtime, _, _, _, _, _, _ = _build_agent_wiring(
        config, kube_stub, {}, pod_resize_supported=True
    )
    assert runtime is not None
    assert "resize_pod" in [t["function"]["name"] for t in runtime._tools]

    gated, _, _, _, _, _, _ = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=False)
    assert gated is not None
    assert "resize_pod" not in [t["function"]["name"] for t in gated._tools]

    ro, _, _, _, _, _, _ = _build_agent_wiring(
        dataclasses.replace(config, readonly=True), kube_stub, {}, pod_resize_supported=True
    )
    assert ro is not None
    assert "resize_pod" not in [t["function"]["name"] for t in ro._tools]


def test_mcp_controller_builds_fresh_servers() -> None:
    """uvicorn servers are single-use: each :mcp on must get a new one."""
    from korvid.__main__ import _build_mcp_controller
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient
    from korvid.mcp.server import MCPController

    config = KorvidConfig(mcp_enabled=True, mcp_port=1234)
    controller = _build_mcp_controller(config, cast("KubeClient", object()), {}, None)
    assert isinstance(controller, MCPController)
    factory = controller._factory
    assert factory() is not factory()


async def test_mcp_controller_exposes_read_and_ui_tools() -> None:
    """The MCP surface is read + UI-drive: write tools stay with the
    built-in agent until an approval UX for external callers exists."""
    from korvid.__main__ import _build_mcp_controller
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient
    from korvid.mcp.server import MCPController
    from korvid.tools.executor import READ_TOOLS, UI_TOOLS

    config = KorvidConfig(mcp_enabled=True, mcp_port=1234)
    controller = _build_mcp_controller(config, cast("KubeClient", object()), {}, None)
    assert isinstance(controller, MCPController)
    server = controller._factory()
    names = [t.name for t in await server.list_tools()]
    assert names == [t["function"]["name"] for t in READ_TOOLS + UI_TOOLS]


async def test_mcp_controller_exposes_proposal_tools_when_configured() -> None:
    """`mcp.write_proposals: true` adds the proposal tools and a per-run
    capability token; each fresh server gets a fresh token."""
    from korvid.__main__ import _build_mcp_controller
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient
    from korvid.mcp.server import MCPController

    config = KorvidConfig(mcp_enabled=True, mcp_port=1234, mcp_write_proposals=True)
    controller = _build_mcp_controller(config, cast("KubeClient", object()), {}, None)
    assert isinstance(controller, MCPController)
    server = controller._factory()
    names = {t.name for t in await server.list_tools()}
    assert {"propose_write", "get_write_proposal", "cancel_write_proposal"} <= names
    token = server._capability_token
    assert isinstance(token, str)
    assert len(token) >= 32
    other = controller._factory()
    assert other._capability_token != token


def test_proposal_store_wired_only_when_enabled() -> None:
    """One ProposalStore is built when mcp.write_proposals is on; base
    installs get None (the app then reports the feature as disabled)."""
    from korvid.__main__ import _build_proposal_store
    from korvid.core.config import KorvidConfig
    from korvid.tools.proposals import ProposalStore

    assert _build_proposal_store(KorvidConfig()) is None
    store = _build_proposal_store(KorvidConfig(mcp_write_proposals=True))
    assert isinstance(store, ProposalStore)


async def test_mcp_controller_omits_proposal_tools_by_default() -> None:
    from korvid.__main__ import _build_mcp_controller
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    config = KorvidConfig(mcp_enabled=True, mcp_port=1234)
    controller = _build_mcp_controller(config, cast("KubeClient", object()), {}, None)
    assert controller is not None
    server = controller._factory()  # type: ignore[attr-defined]  # concrete MCPController in this branch
    names = {t.name for t in await server.list_tools()}
    assert not names & {"propose_write", "get_write_proposal", "cancel_write_proposal"}
    assert server._capability_token is None


class _OverlapProbeBridge(UIBridge):
    """Records whether any two bridge calls ever overlap in time."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def _enter(self) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0)  # yield so a concurrent caller could interleave
        self.active -= 1
        return "ok"

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return await self._enter()

    async def agent_set_filter(self, pattern: str) -> str:
        return await self._enter()

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return await self._enter()

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return await self._enter()

    async def agent_drill_down(self, name: str) -> str:
        return await self._enter()

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        return await self._enter()

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return await self._enter()

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return await self._enter()

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        return await self._enter()


async def test_ui_bridge_proxy_serializes_concurrent_callers() -> None:
    """The proxy is shared by the built-in agent and the MCP server's
    concurrent stateless requests; UI operations (log pane swaps, describe
    views) are not safe to interleave, so calls must never overlap."""
    from korvid.__main__ import _UIBridgeProxy

    probe = _OverlapProbeBridge()
    proxy = _UIBridgeProxy()
    proxy.target = probe
    await asyncio.gather(
        proxy.agent_open_logs("a", "ns"),
        proxy.agent_open_logs("b", "ns"),
        proxy.agent_open_describe("pods", "a"),
        proxy.agent_navigate("pods"),
        proxy.agent_set_filter("x"),
        proxy.agent_drill_down("a"),
        proxy.agent_request_write("delete", "pods", "a"),
    )
    assert probe.max_active == 1


async def test_pod_resize_probe_is_bounded(monkeypatch: object) -> None:
    """The foreground discovery probe must not delay TUI startup on a hung
    apiserver: it times out quickly and answers False (feature stays off)."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)

    import korvid.__main__ as main_mod

    mp.setattr(main_mod, "_RESIZE_PROBE_TIMEOUT", 0.05)

    class HungKube:
        async def supports_pod_resize(self) -> bool:
            await asyncio.sleep(60)
            return True

    assert await main_mod._probe_pod_resize(cast("Any", HungKube())) is False


async def test_pod_resize_probe_passes_through_result() -> None:
    import korvid.__main__ as main_mod

    class FastKube:
        async def supports_pod_resize(self) -> bool:
            return True

    assert await main_mod._probe_pod_resize(cast("Any", FastKube())) is True


async def test_pod_resize_probe_skipped_in_readonly() -> None:
    """A readonly session can never expose either resize entry point, so the
    probe must not spend a network round trip (or its timeout) on it."""
    import korvid.__main__ as main_mod

    class ExplodingKube:
        async def supports_pod_resize(self) -> bool:
            raise AssertionError("probe must not run in readonly mode")

    assert await main_mod._probe_pod_resize(cast("Any", ExplodingKube()), readonly=True) is False


async def test_discovery_drops_aliases_shadowing_synthetic_helm_views() -> None:
    """A CRD sharing the reserved plural (e.g. Flux HelmRelease) must not
    leave aliases behind: `:hr` resolving to plural "helmreleases" would
    navigate to the synthetic Secret browser, not the CRD the alias named."""
    from korvid.__main__ import _discover_in_background
    from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
    from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META

    flux_meta = ResourceMeta(
        "HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io", "v2", True, ("hr",)
    )

    class FakeKube:
        async def discover_resources(self) -> list[ResourceMeta]:
            return [PODS_META, flux_meta]

    class FakeApp:
        def on_aliases_updated(self) -> None:
            pass

    aliases = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META])
    await _discover_in_background(FakeKube(), aliases, FakeApp())  # type: ignore[arg-type]
    assert aliases["helmreleases"] is HELM_RELEASES_META
    assert aliases["helm"] is HELM_RELEASES_META
    assert aliases["helmrevisions"] is HELM_REVISIONS_META
    assert "hr" not in aliases  # Flux's shortname would silently misroute
    assert aliases["pods"] is PODS_META


async def test_get_manifest_routes_helm_revision_names_to_specific_revision() -> None:
    """`d` on a revision row named "web.v3" must fetch exactly that revision;
    the parsing lives in the _make_get_manifest factory, not the client."""
    from korvid.__main__ import _make_get_manifest
    from korvid.k8s.discovery import PODS_META, build_alias_map
    from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META

    calls: list[tuple[str, str, int | None]] = []

    class FakeKube:
        async def get_helm_release(
            self, namespace: str, name: str, revision: int | None = None
        ) -> dict[str, object]:
            calls.append((namespace, name, revision))
            return {"name": name, "revision": revision}

    aliases = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META])
    get_manifest = _make_get_manifest(FakeKube(), aliases)  # type: ignore[arg-type]

    await get_manifest("helmrevisions", "default", "web.v3")
    assert calls[-1] == ("default", "web", 3)

    await get_manifest("helmreleases", "default", "web")
    release_call: tuple[str, str, int | None] = ("default", "web", None)
    assert calls[-1] == release_call

    with pytest.raises(ValueError, match="revision"):
        await get_manifest("helmrevisions", "default", "not-a-revision-row")
    with pytest.raises(ValueError, match="namespace"):
        await get_manifest("helmreleases", None, "web")


async def test_agent_wiring_injects_cluster_context(monkeypatch: object) -> None:
    """A detected-provider note reaches the runtime's system prompt (issue #30)."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
    )
    kube_stub = cast("Any", object())
    note = "This cluster runs on Azure (AKS managed)."
    runtime, _, rebuild, retarget, _, _, _ = _build_agent_wiring(
        config, kube_stub, {}, cluster_context=note
    )
    assert runtime is not None
    assert rebuild is not None
    assert note in runtime._messages[0]["content"]

    from korvid.agent.setup import AgentSettings

    rebuilt = rebuild(
        AgentSettings(
            provider="openai",
            auth_method="api_key",
            base_url="http://localhost:9999/v1",
            model="m",
            api_key_env="KORVID_TEST_KEY",
        )
    )
    assert rebuilt is not None
    assert note in rebuilt._messages[0]["content"]

    # `:ctx` switch (issue #36): the live runtime is re-armed in place and
    # any later wizard rebuild uses the new cluster's note and tool set.
    new_note = "This cluster runs on AWS (EKS managed)."
    retarget(rebuilt, True, new_note)
    assert new_note in rebuilt._messages[0]["content"]
    assert note not in rebuilt._messages[0]["content"]
    assert "resize_pod" in [t["function"]["name"] for t in rebuilt._tools]
    rebuilt_after = rebuild(
        AgentSettings(
            provider="openai",
            auth_method="api_key",
            base_url="http://localhost:9999/v1",
            model="m",
            api_key_env="KORVID_TEST_KEY",
        )
    )
    assert rebuilt_after is not None
    assert new_note in rebuilt_after._messages[0]["content"]
    assert "resize_pod" in [t["function"]["name"] for t in rebuilt_after._tools]


async def test_cloud_provider_probe_is_bounded(monkeypatch: object) -> None:
    """Provider detection is a hint: a hung node list answers unknown quickly."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)

    import korvid.__main__ as main_mod
    from korvid.k8s.csp import ProviderInfo

    mp.setattr(main_mod, "_RESIZE_PROBE_TIMEOUT", 0.05)

    class HungKube:
        async def detect_cloud_provider(self) -> ProviderInfo:
            await asyncio.sleep(60)
            return ProviderInfo("azure", "aks")

    info = await main_mod._probe_cloud_provider(cast("Any", HungKube()))
    assert info.provider == "unknown"


async def test_cloud_provider_probe_passes_through_result() -> None:
    import korvid.__main__ as main_mod
    from korvid.k8s.csp import ProviderInfo

    class FastKube:
        async def detect_cloud_provider(self) -> ProviderInfo:
            return ProviderInfo("aws", "eks")

    info = await main_mod._probe_cloud_provider(cast("Any", FastKube()))
    assert info.display == "eks"


async def test_discovery_maps_operators_alias_to_packagemanifests() -> None:
    """Where OLM serves PackageManifests, `:operators` opens the catalog;
    the alias must not shadow a real kind that already claimed the name."""
    from korvid.__main__ import _discover_in_background
    from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
    from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META

    pkg_meta = ResourceMeta(
        "PackageManifest", "packagemanifests", "packages.operators.coreos.com", "v1", True
    )

    class FakeKube:
        async def discover_resources(self) -> list[ResourceMeta]:
            return [PODS_META, pkg_meta]

    class FakeApp:
        def on_aliases_updated(self) -> None:
            pass

    aliases = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META])
    await _discover_in_background(FakeKube(), aliases, FakeApp())  # type: ignore[arg-type]
    assert aliases["operators"] is pkg_meta
    assert aliases["packagemanifests"] is pkg_meta


async def test_discovery_without_olm_has_no_operators_alias() -> None:
    from korvid.__main__ import _discover_in_background
    from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
    from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META

    class FakeKube:
        async def discover_resources(self) -> list[ResourceMeta]:
            return [PODS_META]

    class FakeApp:
        def on_aliases_updated(self) -> None:
            pass

    aliases = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META])
    await _discover_in_background(FakeKube(), aliases, FakeApp())  # type: ignore[arg-type]
    assert "operators" not in aliases


async def test_discovery_does_not_shadow_a_real_operators_kind() -> None:
    """OLM v1 (or any CRD) can define a kind whose plural is "operators";
    that real kind keeps the alias and the convenience mapping backs off."""
    from korvid.__main__ import _discover_in_background
    from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
    from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META

    operators_meta = ResourceMeta("Operator", "operators", "operators.coreos.com", "v1", False)
    pkg_meta = ResourceMeta(
        "PackageManifest", "packagemanifests", "packages.operators.coreos.com", "v1", True
    )

    class FakeKube:
        async def discover_resources(self) -> list[ResourceMeta]:
            return [PODS_META, operators_meta, pkg_meta]

    class FakeApp:
        def on_aliases_updated(self) -> None:
            pass

    aliases = build_alias_map([PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META])
    await _discover_in_background(FakeKube(), aliases, FakeApp())  # type: ignore[arg-type]
    assert aliases["operators"] is operators_meta
    assert aliases["packagemanifests"] is pkg_meta


async def test_discovery_preserves_olm_metas_under_group_qualified_aliases() -> None:
    """First-meta-wins alias collapsing must not hide OLM: when another API
    group claims 'subscriptions' first, the OLM Subscription stays reachable
    under its kubectl-style plural.group alias."""
    from korvid.__main__ import _discover_in_background
    from korvid.k8s.discovery import ResourceMeta

    foreign_sub = ResourceMeta("Subscription", "subscriptions", "messaging.example.com", "v1", True)
    olm_sub = ResourceMeta(
        "Subscription", "subscriptions", "operators.coreos.com", "v1alpha1", True
    )
    pkg = ResourceMeta(
        "PackageManifest", "packagemanifests", "packages.operators.coreos.com", "v1", True
    )

    class FakeKube:
        async def discover_resources(self) -> list[ResourceMeta]:
            return [foreign_sub, olm_sub, pkg]

    class FakeApp:
        def on_aliases_updated(self) -> None:
            pass

    aliases: dict[str, ResourceMeta] = {}
    await _discover_in_background(FakeKube(), aliases, FakeApp())  # type: ignore[arg-type]
    assert aliases["subscriptions"] is foreign_sub
    assert aliases["subscriptions.operators.coreos.com"] is olm_sub
    assert aliases["packagemanifests.packages.operators.coreos.com"] is pkg


async def test_ctx_switch_quiesces_discovery_before_swapping_connection() -> None:
    """switch_context closes the old ApiClient — the background discovery
    task issuing requests on it must be cancelled (and the alias map reseeded)
    before the connection swap, not after (issue #36 review)."""
    import asyncio
    import contextlib
    from types import SimpleNamespace

    from korvid.__main__ import _make_switch_context
    from korvid.core.config import KorvidConfig
    from korvid.k8s.csp import detect_provider

    events: list[str] = []

    class FakeKube:
        async def switch_context(self, name: str | None) -> None:
            events.append("connection-swapped")

        async def detect_cloud_provider(self) -> Any:
            return detect_provider([])

        async def discover_resources(self) -> list[Any]:
            return []

    async def _old_discovery() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("discovery-cancelled")
            raise

    old_task = asyncio.create_task(_old_discovery())
    await asyncio.sleep(0)  # let it start so cancellation unwinds it

    aliases: dict[str, Any] = {"stale-crd": object()}
    discovery_box: list[asyncio.Task[None]] = [old_task]
    startup_config = KorvidConfig(namespace="default", readonly=True)
    switch = _make_switch_context(
        startup_config,
        cast("Any", FakeKube()),
        aliases,
        cast("Any", [SimpleNamespace(agent_runtime=None, config=startup_config)]),  # app_box
        discovery_box,
        lambda runtime, resize, note: None,
    )
    try:
        await switch("ctx-b")
    finally:
        discovery_box[0].cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await discovery_box[0]

    # The stale discovery task drains before the connection is retargeted.
    assert events == [
        "discovery-cancelled",
        "connection-swapped",
    ]
    assert "stale-crd" not in aliases  # reseeded before the swap


def test_build_helm_wires_binary_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """A detected helm binary becomes a HelmCLI bound to the configured context."""
    import korvid.__main__ as main_mod
    from korvid.__main__ import _build_helm
    from korvid.core.config import KorvidConfig

    monkeypatch.setattr(main_mod, "find_helm", lambda: "/usr/local/bin/helm")
    helm = _build_helm(KorvidConfig(kube_context="staging"))
    assert helm is not None
    assert helm._binary == "/usr/local/bin/helm"
    assert helm._kube_context == "staging"


def test_build_helm_returns_none_without_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """No helm on PATH means the app gets helm=None (actions gated off)."""
    import korvid.__main__ as main_mod
    from korvid.__main__ import _build_helm
    from korvid.core.config import KorvidConfig

    monkeypatch.setattr(main_mod, "find_helm", lambda: None)
    assert _build_helm(KorvidConfig()) is None


async def test_agent_wiring_applies_the_small_profile(monkeypatch: object) -> None:
    """`agent.profile: small` shrinks the tool surface, budgets, and prompt
    at the composition root (issue #71); rebuilds after the :ai wizard honor
    the wizard's profile choice."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.profiles import SMALL_MAX_HISTORY_CHARS, SMALL_MAX_ITERATIONS
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
        agent_profile="small",
    )
    kube_stub = cast("Any", object())
    runtime, _, rebuild, _, _, _, _ = _build_agent_wiring(
        config, kube_stub, {}, pod_resize_supported=True
    )
    assert runtime is not None
    names = [t["function"]["name"] for t in runtime._tools]
    assert "diagnose_pod" in names
    assert "open_logs" in names
    assert "delete_resource" in names  # writes stay available (approval-gated)
    assert "resize_pod" in names
    assert "navigate" not in names
    assert "set_filter" not in names
    assert "drill_down" not in names
    assert runtime._max_iterations == SMALL_MAX_ITERATIONS
    assert runtime._max_history_chars == SMALL_MAX_HISTORY_CHARS
    assert runtime._max_result_chars is not None
    assert "one tool at a time" in runtime._messages[0]["content"]

    # The wizard's rebuild carries its own profile choice.
    assert rebuild is not None
    full_runtime = rebuild(
        AgentSettings(
            provider="openai-compat",
            auth_method="api_key",
            base_url="http://localhost:9999/v1",
            model="m",
            api_key_env="KORVID_TEST_KEY",
            profile="full",
        )
    )
    assert full_runtime is not None
    full_names = [t["function"]["name"] for t in full_runtime._tools]
    assert "navigate" in full_names
    assert full_runtime._max_iterations != SMALL_MAX_ITERATIONS


async def test_ctx_retarget_keeps_the_small_profile_surface(monkeypatch: object) -> None:
    """A `:ctx` switch recomposes the tool set from the *active* profile
    (issues #36 + #71): retargeting a small-profile runtime picks up the new
    cluster's capabilities (resize) without resurrecting the full surface or
    resetting the small system prompt."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
        agent_profile="small",
    )
    kube_stub = cast("Any", object())
    runtime, _, _, retarget, _, _, _ = _build_agent_wiring(
        config, kube_stub, {}, pod_resize_supported=False
    )
    assert runtime is not None
    assert "resize_pod" not in [t["function"]["name"] for t in runtime._tools]

    retarget(runtime, True, "The cluster runs on AWS EKS.")
    names = [t["function"]["name"] for t in runtime._tools]
    assert "resize_pod" in names  # new cluster's capability picked up
    assert "navigate" not in names  # still the small surface
    prompt = runtime._messages[0]["content"]
    assert "one tool at a time" in prompt  # still the small system prompt
    assert "AWS EKS" in prompt  # new cluster's environment note


async def test_ctx_switch_result_carries_the_context_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch result reports the target context's kubeconfig namespace so
    the app can adopt it as the session default (issue #36); no fallback
    namespace set is derived from config (issue #108)."""
    import asyncio
    import contextlib
    from types import SimpleNamespace

    import korvid.__main__ as main_mod
    from korvid.__main__ import _make_switch_context
    from korvid.core.config import KorvidConfig
    from korvid.k8s.csp import detect_provider

    class FakeKube:
        async def switch_context(self, name: str | None) -> None:
            pass

        async def detect_cloud_provider(self) -> Any:
            return detect_provider([])

        async def discover_resources(self) -> list[Any]:
            return []

    ctx_namespaces = {"ctx-b": "ns-b", "ctx-c": None}
    monkeypatch.setattr(main_mod, "resolve_context_namespace", ctx_namespaces.get)

    startup = KorvidConfig(namespace="startup-ns", readonly=True)
    app_stub = SimpleNamespace(agent_runtime=None, config=startup)
    discovery_box: list[asyncio.Task[None]] = []
    switch = _make_switch_context(
        startup,
        cast("Any", FakeKube()),
        {},
        cast("Any", [app_stub]),
        discovery_box,
        lambda runtime, resize, note: None,
    )
    try:
        result_b = await switch("ctx-b")
        result_c = await switch("ctx-c")
    finally:
        for task in discovery_box:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    assert result_b.context_namespace == "ns-b"
    assert result_c.context_namespace is None
    assert not hasattr(result_b, "fallback_namespaces")


def test_startup_namespace_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup namespace resolves CLI > config `namespace:` > kubeconfig
    context namespace > `default` (issue #108)."""
    import korvid.__main__ as main_mod
    from korvid.__main__ import _load_startup_config
    from korvid.core.config import KorvidConfig

    monkeypatch.setattr(main_mod, "resolve_context_name", lambda name: name)
    monkeypatch.setattr(main_mod, "resolve_context_namespace", lambda name: "ctx-ns")

    monkeypatch.setattr(main_mod, "load_config", lambda: KorvidConfig(namespace="cfg-ns"))
    assert _load_startup_config(False, namespace="cli-ns").namespace == "cli-ns"
    assert _load_startup_config(False).namespace == "cfg-ns"

    monkeypatch.setattr(main_mod, "load_config", lambda: KorvidConfig())
    assert _load_startup_config(False).namespace == "ctx-ns"

    monkeypatch.setattr(main_mod, "resolve_context_namespace", lambda name: None)
    assert _load_startup_config(False).namespace == "default"


def test_cli_namespace_flag_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`korvid -n team-a` and `--namespace team-a` select the startup
    namespace (issue #108)."""
    import korvid.__main__ as main_mod

    calls: list[str | None] = []

    def fake_run(coro: Any) -> None:
        coro.close()

    def fake_run_app(
        readonly: bool = False, mcp: bool = False, namespace: str | None = None
    ) -> Any:
        calls.append(namespace)

        async def noop() -> None:
            pass

        return noop()

    monkeypatch.setattr("asyncio.run", fake_run)
    monkeypatch.setattr(main_mod, "_run", fake_run_app)
    monkeypatch.setattr("sys.argv", ["korvid", "-n", "team-a"])
    main_mod.main()
    monkeypatch.setattr("sys.argv", ["korvid", "--namespace", "team-b"])
    main_mod.main()
    monkeypatch.setattr("sys.argv", ["korvid"])
    main_mod.main()
    assert calls == ["team-a", "team-b", None]


def test_protected_context_name_glob_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_protected_context_name` resolves the effective context (kubeconfig
    active name for None) and returns it only when a glob matches (issue #83)."""
    import korvid.__main__ as main_mod
    from korvid.core.config import KorvidConfig

    monkeypatch.setattr(main_mod, "resolve_context_name", lambda ctx: ctx or "prod-active")
    config = KorvidConfig(protected_contexts=("prod-*",))
    assert main_mod._protected_context_name(config, "prod-eu") == "prod-eu"
    assert main_mod._protected_context_name(config, None) == "prod-active"
    assert main_mod._protected_context_name(config, "dev") is None
    assert main_mod._protected_context_name(KorvidConfig(), "prod-eu") is None


def _uninstall_packages(monkeypatch: pytest.MonkeyPatch, *packages: str) -> None:
    """Simulate an install without the given third-party packages (issue #73).

    The composition root probes capability with `importlib.util.find_spec`
    (catching ImportError is unreliable: parts of an extra may arrive
    transitively or be imported lazily), so make the probe report the
    packages as absent.
    """
    import importlib.util

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.partition(".")[0] in packages:
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


_MCP_ROOTS = ("mcp", "anyio", "starlette", "uvicorn")
_AGENT_ROOTS = ("httpx", "keyring")


def test_missing_mcp_extra_degrades_when_not_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the [mcp] extra and without --mcp, the TUI gets None wiring
    (the `:mcp` command reports the feature as unavailable)."""
    from korvid.__main__ import _build_mcp_controller
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, *_MCP_ROOTS)
    controller = _build_mcp_controller(KorvidConfig(), cast("KubeClient", object()), {}, None)
    assert controller is None


def test_missing_mcp_extra_fails_actionably_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--mcp` / mcp.enabled with the extra missing must exit with an
    install hint, never a bare ImportError traceback."""
    from korvid.__main__ import _build_mcp_controller
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, *_MCP_ROOTS)
    with pytest.raises(SystemExit, match=r"korvid\[mcp\]"):
        _build_mcp_controller(
            KorvidConfig(mcp_enabled=True), cast("KubeClient", object()), {}, None
        )


def test_missing_agent_extra_degrades_when_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the [agent] extra and without agent.provider configured,
    the wiring is runtime-less and the retarget hook is a safe no-op."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, *_AGENT_ROOTS)
    runtime, configurator, rebuild, retarget, _, provider_box, _ = _build_agent_wiring(
        KorvidConfig(), cast("KubeClient", object()), {}
    )
    assert runtime is None
    assert configurator is None
    assert rebuild is None
    assert provider_box == [None]
    retarget(None, True, "ctx")  # must not raise


def test_missing_agent_extra_fails_actionably_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent.provider in config.yaml with the extra missing must exit with
    an install hint, never a bare ImportError traceback."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, *_AGENT_ROOTS)
    with pytest.raises(SystemExit, match=r"korvid\[agent\]"):
        _build_agent_wiring(
            KorvidConfig(agent_enabled=True, agent_provider="ollama"),
            cast("KubeClient", object()),
            {},
        )


def test_missing_first_party_module_is_not_treated_as_missing_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a missing extra package may degrade the wiring: a broken
    first-party module is a defect and must propagate, never be silently
    disabled or misreported as an uninstalled extra."""
    import builtins
    import sys

    from korvid.__main__ import _build_mcp_controller
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    for cached in list(sys.modules):
        if cached == "korvid.mcp" or cached.startswith("korvid.mcp."):
            monkeypatch.delitem(sys.modules, cached)

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "korvid.mcp" or name.startswith("korvid.mcp."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModuleNotFoundError, match=r"korvid\.mcp"):
        _build_mcp_controller(KorvidConfig(), cast("KubeClient", object()), {}, None)


def test_mcp_only_install_does_not_compose_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An [mcp]-only install has httpx (mcp depends on it transitively) but
    not keyring — the agent wiring must still degrade and must not load the
    embedded-agent loop, and TokenStore's lazy keyring import must not fool
    the capability probe."""
    import sys

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, "keyring")  # httpx stays importable
    for cached in list(sys.modules):
        if cached in ("korvid.agent.runtime", "korvid.agent.profiles"):
            monkeypatch.delitem(sys.modules, cached)

    runtime, configurator, rebuild, _, _, provider_box, _ = _build_agent_wiring(
        KorvidConfig(), cast("KubeClient", object()), {}
    )
    assert runtime is None
    assert configurator is None
    assert rebuild is None
    assert provider_box == [None]
    assert "korvid.agent.runtime" not in sys.modules
    assert "korvid.agent.profiles" not in sys.modules


async def test_mcp_controller_wires_follow_hooks() -> None:
    """MCP follow mode (issue #153): the factory hands the late-bound app
    hooks and the shared UI proxy to every server it builds; before the app
    exists the hooks degrade to 'follow off' and dropped notes."""
    from korvid.__main__ import _build_mcp_controller, _MCPAppHooks
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient
    from korvid.mcp.server import MCPController

    hooks = _MCPAppHooks()
    config = KorvidConfig(mcp_enabled=True, mcp_port=1234)
    bridge = _FakeApp()
    controller = _build_mcp_controller(
        config, cast("KubeClient", object()), {}, bridge, mcp_hooks=hooks
    )
    assert isinstance(controller, MCPController)
    server = controller._factory()
    assert server._ui is bridge
    assert server._follow_enabled == hooks.follow_enabled  # bound to the hooks
    assert server._note_activity == hooks.note_activity
    assert hooks.follow_enabled() is False  # app not attached yet
    hooks.note_activity("dropped")  # must not raise

    class _App:
        mcp_follow_enabled = True

        def __init__(self) -> None:
            self.notes: list[str] = []

        def note_mcp_activity(self, line: str) -> None:
            self.notes.append(line)

    fake_app = _App()
    hooks.app = cast("Any", fake_app)  # duck-typed stand-in for KorvidApp
    assert hooks.follow_enabled() is True
    hooks.note_activity("seen")
    assert fake_app.notes == ["seen"]


async def test_mcp_executor_receives_custom_column_names() -> None:
    """list_resources renders user-configured columns (issue #158): the
    composition root hands the configured names to every executor."""
    from korvid.__main__ import _build_mcp_controller, _custom_column_names
    from korvid.core.config import KorvidConfig, ViewConfig
    from korvid.k8s.client import KubeClient
    from korvid.k8s.columns import CustomColumn

    view = ViewConfig(columns=(CustomColumn(name="TEAM", source="label", expr="team"),))
    config = KorvidConfig(mcp_enabled=True, mcp_port=1234, views={"deployments": view})
    assert _custom_column_names(config) == {"deployments": ("TEAM",)}
    controller = _build_mcp_controller(config, cast("KubeClient", object()), {}, None)
    assert controller is not None
    server = controller._factory()  # type: ignore[attr-defined]  # test introspection
    assert server._executor._custom_columns == {"deployments": ("TEAM",)}


def test_telepresence_wiring_respects_detection_and_kill_switch() -> None:
    """Optional integration (issue #159): absent binary or the config
    kill-switch yields None; a detected binary yields a CLI wrapper."""
    from unittest import mock

    from korvid.__main__ import _build_telepresence
    from korvid.core.config import KorvidConfig
    from korvid.k8s.telepresence import TelepresenceCLI

    with mock.patch("korvid.__main__.find_telepresence", return_value=None):
        assert _build_telepresence(KorvidConfig()) is None
    with mock.patch("korvid.__main__.find_telepresence", return_value="/x/telepresence"):
        assert isinstance(_build_telepresence(KorvidConfig()), TelepresenceCLI)
        assert _build_telepresence(KorvidConfig(telepresence_enabled=False)) is None


async def test_disconnect_agent_releases_the_provider(monkeypatch: object) -> None:
    """`:ai off` (issue #167): the disconnect closure empties the provider
    box (so teardown/rebuild never touch the dead provider) and closes the
    old provider in the background."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
    )
    kube_stub = cast("Any", object())
    runtime, _, _, _, disconnect, provider_box, _ = _build_agent_wiring(config, kube_stub, {})
    assert runtime is not None
    provider = provider_box[0]
    assert provider is not None
    closed: list[bool] = []

    async def fake_aclose() -> None:
        closed.append(True)

    mp.setattr(provider, "aclose", fake_aclose)
    disconnect()
    assert provider_box[0] is None  # the box never points at a dead provider
    for _ in range(10):
        if closed:
            break
        await asyncio.sleep(0.01)
    assert closed == [True]  # released in the background, not leaked
    disconnect()  # idempotent when already off
    assert provider_box[0] is None


def _install_company_plugin_site(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    build_dist_info(
        tmp_path,
        dist_name="company_provider",
        version="1.0",
        entry_point_name="company-llm",
        entry_point_value="company_provider:CompanyProviderPlugin",
    )
    build_dist_info(
        tmp_path,
        dist_name="unselected_provider",
        version="1.0",
        entry_point_name="unselected-thing",
        entry_point_value="unselected_provider:UnselectedPlugin",
    )
    monkeypatch.syspath_prepend(str(FIXTURES_DIR))
    monkeypatch.setattr(
        "korvid.providers.plugin_registry._discover_entry_points",
        lambda: discover_provider_entry_points(tmp_path),
    )


def _company_plugin_config() -> Any:
    from korvid.core.config import KorvidConfig

    return KorvidConfig(
        agent_enabled=True,
        agent_provider="company-llm",
        agent_auth_method="api_key",
        agent_base_url="https://fixtures.example.test/v1",
        agent_model="fixture-model",
        agent_api_key_env="KORVID_TEST_KEY",
    )


def _company_plugin_settings(*, options: dict[str, object] | None = None) -> AgentSettings:
    return AgentSettings(
        provider="company-llm",
        auth_method="api_key",
        base_url="https://fixtures.example.test/v1",
        model="fixture-model",
        api_key_env="KORVID_TEST_KEY",
        options=options or {},
    )


class _FakeKubeCloseOnly:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def _wait_for_close_count(provider: object, expected: int) -> None:
    inner = cast("Any", provider)._provider
    for _ in range(20):
        if inner.close_calls == expected:
            return
        await asyncio.sleep(0.01)
    assert inner.close_calls == expected


async def test_plugin_rebuild_failure_keeps_the_previous_provider_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from korvid.__main__ import _build_agent_wiring
    from korvid.providers.plugin_registry import ProviderPluginError

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    runtime, _, rebuild, _, _, provider_box, _ = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    assert runtime is not None
    assert rebuild is not None
    old_provider = provider_box[0]
    assert old_provider is not None

    with pytest.raises(ProviderPluginError, match="factory failed"):
        rebuild(_company_plugin_settings(options={"raise_in_create": True}))

    assert provider_box[0] is old_provider
    await asyncio.sleep(0.05)
    assert cast("Any", old_provider)._provider.close_calls == 0


async def test_plugin_rebuild_closes_the_replaced_provider_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    runtime, _, rebuild, _, _, provider_box, _ = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    assert runtime is not None
    assert rebuild is not None
    old_provider = provider_box[0]
    assert old_provider is not None

    new_runtime = rebuild(_company_plugin_settings())

    assert new_runtime is not None
    assert provider_box[0] is not None
    assert provider_box[0] is not old_provider
    await _wait_for_close_count(old_provider, 1)
    assert cast("Any", provider_box[0])._provider.close_calls == 0


async def test_plugin_disconnect_then_shutdown_does_not_double_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from korvid.__main__ import _build_agent_wiring, _shutdown

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    runtime, _, _, _, disconnect, provider_box, _ = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    assert runtime is not None
    provider = provider_box[0]
    assert provider is not None

    disconnect()
    await _wait_for_close_count(provider, 1)
    kube = _FakeKubeCloseOnly()
    await _shutdown(None, provider_box[0], cast("Any", kube))

    assert cast("Any", provider)._provider.close_calls == 1
    assert kube.closed is True


async def test_plugin_shutdown_closes_the_current_provider_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from korvid.__main__ import _build_agent_wiring, _shutdown

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    runtime, _, _, _, _, provider_box, _ = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    assert runtime is not None
    provider = provider_box[0]
    assert provider is not None

    kube = _FakeKubeCloseOnly()
    await _shutdown(None, provider, cast("Any", kube))

    assert cast("Any", provider)._provider.close_calls == 1
    assert kube.closed is True


def test_agent_wiring_creates_shared_plugin_registry(monkeypatch: object) -> None:
    """_build_agent_wiring creates one ProviderPluginRegistry and passes it
    to both the initial create_provider and the configurator, ensuring shared
    cache lifetime."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.providers.plugin_registry import ProviderPluginRegistry

    registries: list[ProviderPluginRegistry] = []
    original_init = ProviderPluginRegistry.__init__

    def capture_init(self: ProviderPluginRegistry) -> None:
        original_init(self)
        registries.append(self)

    mp.setattr(ProviderPluginRegistry, "__init__", capture_init)

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
    )
    kube_stub = cast("Any", object())
    _build_agent_wiring(config, kube_stub, {})
    assert len(registries) == 1  # one registry per build


def test_agent_wiring_initial_plugin_error_becomes_warning(monkeypatch: object) -> None:
    """A ProviderPluginError at initial creation must become a startup warning
    — the app remains operational with provider=None.

    Uses a production-real path: a fake ProviderPluginRegistry whose
    load_selected raises ProviderPluginError is injected via the
    ProviderPluginRegistry constructor in __main__, flowing through
    _create_initial_provider → create_provider → _create_via_plugin.
    """
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.providers.plugin_registry import ProviderPluginError

    class _BoomRegistry:
        """Fake registry whose load_selected always raises."""

        def load_selected(self, name: str) -> None:
            raise ProviderPluginError("bad plugin entrypoint")

    # Replace ProviderPluginRegistry() in __main__ with our fake
    mp.setattr(
        "korvid.providers.plugin_registry.ProviderPluginRegistry",
        lambda: _BoomRegistry(),
    )

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="corp-llm",
        agent_auth_method="api_key",
        agent_base_url="http://x/v1",
        agent_model="m",
    )
    warnings: list[str] = []
    kube_stub = cast("Any", object())
    runtime, configurator, _rebuild, _, _, provider_box, _ = _build_agent_wiring(
        config,
        kube_stub,
        {},
        startup_warnings=warnings,
    )
    assert runtime is None  # provider disabled, not a crash
    assert provider_box[0] is None
    assert configurator is not None  # wizard must remain usable
    assert len(warnings) == 1
    assert "Provider plugin failed" in warnings[0]
    assert "bad plugin entrypoint" in warnings[0]


async def test_agent_wiring_rebuild_passes_plugin_registry(monkeypatch: object) -> None:
    """The rebuild closure must pass the same plugin_registry to create_provider
    so plugin cache is shared across initial/rebuild."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import KorvidConfig

    captured_kwargs: list[dict[str, Any]] = []
    original_create = __import__(
        "korvid.providers.registry", fromlist=["create_provider"]
    ).create_provider

    def capture_create(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        return original_create(
            **{k: v for k, v in kwargs.items() if k not in ("plugin_registry", "options")}
        )

    mp.setattr("korvid.providers.registry.create_provider", capture_create)

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
    )
    kube_stub = cast("Any", object())
    _, _, rebuild, _, _, _, _ = _build_agent_wiring(config, kube_stub, {})
    assert rebuild is not None

    # Initial create must have plugin_registry
    assert captured_kwargs[0].get("plugin_registry") is not None
    initial_registry = captured_kwargs[0]["plugin_registry"]

    # Rebuild must share the same registry
    settings = AgentSettings(
        provider="openai",
        auth_method="api_key",
        base_url="http://localhost:9999/v1",
        model="new-model",
        api_key_env="KORVID_TEST_KEY",
    )
    rebuild(settings)
    assert captured_kwargs[-1].get("plugin_registry") is initial_registry


def test_agent_wiring_seeds_options_from_config(monkeypatch: object) -> None:
    """Options from config reach both create_provider and the configurator."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    captured_kwargs: list[dict[str, Any]] = []
    original_create = __import__(
        "korvid.providers.registry", fromlist=["create_provider"]
    ).create_provider

    def capture_create(**kwargs: Any) -> Any:
        captured_kwargs.append(kwargs)
        return original_create(
            **{k: v for k, v in kwargs.items() if k not in ("plugin_registry", "options")}
        )

    mp.setattr("korvid.providers.registry.create_provider", capture_create)

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
        agent_options={"tenant": "corp", "region": "us"},
    )
    kube_stub = cast("Any", object())
    _build_agent_wiring(config, kube_stub, {})
    assert captured_kwargs[0].get("options") == {"tenant": "corp", "region": "us"}


def test_validate_ca_bundle_accepts_none_and_rejects_missing(tmp_path: Any) -> None:
    """network.ca_bundle (issue #168): unset is fine; a missing bundle fails
    startup actionably, naming the configured path — never a silent
    fallback to default trust."""
    import pytest

    from korvid.__main__ import _validate_ca_bundle

    _validate_ca_bundle(None)  # unset: default trust, no error
    with pytest.raises(SystemExit, match=r"nope\.pem"):
        _validate_ca_bundle(str(tmp_path / "nope.pem"))


def test_validate_ca_bundle_rejects_malformed(tmp_path: Any) -> None:
    import pytest

    from korvid.__main__ import _validate_ca_bundle

    bad = tmp_path / "garbage.pem"
    bad.write_text("this is not a certificate")
    with pytest.raises(SystemExit, match=r"garbage\.pem"):
        _validate_ca_bundle(str(bad))


# ---------------------------------------------------------------------------
# Finding #8: Rebuild transactional — profile/runtime failures
# ---------------------------------------------------------------------------


async def test_rebuild_profile_failure_closes_new_provider_keeps_old(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If build_profile raises during rebuild, the new provider must be closed
    and the old provider_box/runtime state must remain untouched."""
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    runtime, _, rebuild, _, _, provider_box, _ = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    assert runtime is not None
    assert rebuild is not None
    old_provider = provider_box[0]
    assert old_provider is not None

    # Use a settings with a profile that causes ToolExecutor to fail
    # Patch ToolExecutor so that it raises on the next construction
    from korvid.tools import executor as executor_mod

    def _boom_te_init(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("tool executor construction failed")

    monkeypatch.setattr(executor_mod.ToolExecutor, "__init__", _boom_te_init)

    with pytest.raises(RuntimeError, match="tool executor construction failed"):
        rebuild(_company_plugin_settings())

    # Old provider must be alive and still in the box
    assert provider_box[0] is old_provider
    assert cast("Any", old_provider)._provider.close_calls == 0


async def test_rebuild_runtime_failure_closes_new_provider_keeps_old(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If AgentRuntime construction raises, the new provider must be closed
    and old state must remain live."""
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    runtime, _, rebuild, _, _, provider_box, _ = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    assert runtime is not None
    assert rebuild is not None
    old_provider = provider_box[0]
    assert old_provider is not None

    # Patch AgentRuntime to raise on next instantiation
    from korvid.agent import runtime as runtime_mod

    def _boom_init(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("runtime construction failed")

    monkeypatch.setattr(runtime_mod.AgentRuntime, "__init__", _boom_init)

    with pytest.raises(RuntimeError, match="runtime construction failed"):
        rebuild(_company_plugin_settings())

    assert provider_box[0] is old_provider
    assert cast("Any", old_provider)._provider.close_calls == 0


# ---------------------------------------------------------------------------
# Finding #1: options_error gates third-party plugin creation
# ---------------------------------------------------------------------------


def test_options_error_gates_plugin_creation_at_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """At initial startup, options_error on a plugin provider must surface
    as a warning and disable the agent (not silently start with options={})."""
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    config = dataclasses.replace(
        _company_plugin_config(),
        agent_options={},
        agent_options_error="agent.options must be a mapping with string keys",
    )
    warnings: list[str] = []
    runtime, _, _, _, _, provider_box, _ = _build_agent_wiring(
        config,
        cast("Any", object()),
        {},
        startup_warnings=warnings,
    )
    # The agent must be disabled (None runtime) and the warning must be surfaced.
    assert runtime is None
    assert provider_box[0] is None
    assert any("agent.options" in w for w in warnings)


def test_options_error_does_not_gate_builtin_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Built-in providers must remain usable even when options_error exists."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
        agent_options={},
        agent_options_error="agent.options exceeded max depth",
    )
    runtime, _, _, _, _, _, _ = _build_agent_wiring(
        config,
        cast("Any", object()),
        {},
    )
    # Built-in provider starts fine despite options_error
    assert runtime is not None


def test_options_error_fails_rebuild_for_plugin_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """At rebuild time, options_error on a plugin must raise ProviderPluginError
    (the wizard sees it as an actionable failure)."""
    from korvid.providers.plugin_registry import ProviderPluginError, ProviderPluginRegistry
    from korvid.providers.registry import create_provider

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    registry = ProviderPluginRegistry()

    with pytest.raises(ProviderPluginError, match=r"agent\.options"):
        create_provider(
            enabled=True,
            provider="company-llm",
            auth_method="api_key",
            base_url="https://fixtures.example.test/v1",
            model="fixture-model",
            api_key_env="KORVID_TEST_KEY",
            plugin_registry=registry,
            options={},
            options_error="agent.options must be ASCII keys only",
        )
