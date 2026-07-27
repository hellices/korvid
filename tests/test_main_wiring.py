"""Tests for composition-root helpers in korvid.__main__."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, cast

import pytest

from korvid.__main__ import _close_provider_in_background
from korvid.agent.tools import UIBridge


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
    runtime, _, _, _, _, proxy = _build_agent_wiring(config, kube_stub, {})
    assert runtime is not None
    names = [t["function"]["name"] for t in runtime._tools]
    assert "navigate" in names
    assert "list_resources" in names
    assert "delete_resource" in names  # writes armed by default (approval-gated)
    executor = cast("Any", runtime._executor)
    assert executor._ui is proxy

    # readonly strips every write tool: the model is never told they exist.
    ro_runtime, _, _, _, _, _ = _build_agent_wiring(
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

    runtime, _, _, _, _, _ = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=True)
    assert runtime is not None
    assert "resize_pod" in [t["function"]["name"] for t in runtime._tools]

    gated, _, _, _, _, _ = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=False)
    assert gated is not None
    assert "resize_pod" not in [t["function"]["name"] for t in gated._tools]

    ro, _, _, _, _, _ = _build_agent_wiring(
        dataclasses.replace(config, readonly=True), kube_stub, {}, pod_resize_supported=True
    )
    assert ro is not None
    assert "resize_pod" not in [t["function"]["name"] for t in ro._tools]


def test_mcp_factory_builds_fresh_servers() -> None:
    """uvicorn servers are single-use: each :mcp on must get a new one."""
    from korvid.__main__ import _make_mcp_factory
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    config = KorvidConfig(mcp_enabled=True, mcp_port=1234)
    factory = _make_mcp_factory(config, cast("KubeClient", object()), {}, None)
    assert factory() is not factory()


async def test_mcp_factory_exposes_read_and_ui_tools() -> None:
    """The MCP surface is read + UI-drive: write tools stay with the
    built-in agent until an approval UX for external callers exists."""
    from korvid.__main__ import _make_mcp_factory
    from korvid.agent.tools import READ_TOOLS, UI_TOOLS
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    config = KorvidConfig(mcp_enabled=True, mcp_port=1234)
    server = _make_mcp_factory(config, cast("KubeClient", object()), {}, None)()
    names = [t.name for t in await server.list_tools()]
    assert names == [t["function"]["name"] for t in READ_TOOLS + UI_TOOLS]


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
    runtime, _, rebuild, retarget, _, _ = _build_agent_wiring(
        config, kube_stub, {}, cluster_context=note
    )
    assert runtime is not None
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

    class FakeMCP:
        def __init__(self, *, running: bool) -> None:
            self.running = running

        async def stop(self) -> str:
            self.running = False
            events.append("mcp-stopped")
            return "MCP off"

        async def start(self) -> str:
            self.running = True
            events.append("mcp-started")
            return "MCP on :4321"

    aliases: dict[str, Any] = {"stale-crd": object()}
    discovery_box: list[asyncio.Task[None]] = [old_task]
    switch = _make_switch_context(
        KorvidConfig(namespace="default", readonly=True),
        cast("Any", FakeKube()),
        aliases,
        cast("Any", [SimpleNamespace(agent_runtime=None)]),  # app_box
        discovery_box,
        lambda runtime, resize, note: None,
        cast("Any", FakeMCP(running=True)),
    )
    try:
        result = await switch("ctx-b")
    finally:
        discovery_box[0].cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await discovery_box[0]

    # The MCP server is quiesced first (in-flight tool calls drain against
    # the old cluster) and resumed only after the connection is retargeted.
    assert events == [
        "mcp-stopped",
        "discovery-cancelled",
        "connection-swapped",
        "mcp-started",
    ]
    assert "stale-crd" not in aliases  # reseeded before the swap
    assert result.mcp_status == "MCP on :4321"
