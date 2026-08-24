"""Tests for composition-root helpers in korvid.__main__."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest

import korvid
import korvid.__main__
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


async def test_close_background_does_not_log_secret_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Background provider close must log a fixed message, never the raw
    exception payload which may contain secrets from third-party plugins."""
    from korvid.agent.provider import LLMProvider

    class _SecretBoomProvider:
        async def aclose(self) -> None:
            raise RuntimeError("SUPER_SECRET_API_KEY_leak_attempt" * 5)

    tasks: set[asyncio.Task[None]] = set()
    _close_provider_in_background(cast("LLMProvider", _SecretBoomProvider()), tasks)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not tasks
    assert "SUPER_SECRET_API_KEY" not in caplog.text


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
    from korvid.__main__ import _AgentToolUIBridgeProxy

    proxy = _AgentToolUIBridgeProxy()
    assert (await proxy.agent_navigate("pods")).startswith("ERROR:")
    assert (await proxy.agent_set_filter("x")).startswith("ERROR:")
    assert (await proxy.agent_open_logs("p", "ns")).startswith("ERROR:")
    assert (await proxy.agent_open_describe("pods", "p")).startswith("ERROR:")
    assert (await proxy.agent_drill_down("web")).startswith("ERROR:")
    assert (await proxy.agent_request_write("delete", "pods", "web-1")).startswith("ERROR:")


async def test_proxy_forwards_to_target() -> None:
    from korvid.__main__ import _AgentToolUIBridgeProxy

    proxy = _AgentToolUIBridgeProxy()
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


# --- Task 12: the second port, the agent UI bridge ---


async def test_the_agent_ui_proxy_refuses_before_the_app_exists() -> None:
    """The session's workspace port is late-bound like the tool port, but
    it must never *fabricate* an answer: a snapshot invented before the UI
    exists would be a lie about the screen, and a fabricated action result
    would tell the model something happened that did not."""
    from korvid.__main__ import _AgentUiBridgeProxy
    from korvid.agent.interaction import Navigate

    proxy = _AgentUiBridgeProxy()
    with pytest.raises(RuntimeError, match="agent UI not ready"):
        proxy.snapshot()
    with pytest.raises(RuntimeError, match="agent UI not ready"):
        await proxy.apply(Navigate(view="pods"))


async def test_the_agent_ui_proxy_forwards_once_bound() -> None:
    from korvid.__main__ import _AgentUiBridgeProxy
    from korvid.agent.interaction import (
        AgentUiBridge,
        InteractionContext,
        Navigate,
        PaneContext,
        UiAction,
        UiActionResult,
    )

    snapshot = InteractionContext(
        kube_context="kind-dev",
        context_epoch=3,
        focused_pane=PaneContext(kind="pods", scope="default", filter_pattern=None, selected=None),
        secondary_pane=None,
        timeline_cursor=None,
    )

    class _Bridge(AgentUiBridge):
        def __init__(self) -> None:
            self.applied: list[UiAction] = []

        def snapshot(self) -> InteractionContext:
            return snapshot

        async def apply(self, action: UiAction) -> UiActionResult:
            self.applied.append(action)
            return UiActionResult(ok=True, message="done", context=self.snapshot())

    proxy = _AgentUiBridgeProxy()
    bridge = _Bridge()
    proxy.target = bridge
    assert proxy.snapshot() is snapshot
    result = await proxy.apply(Navigate(view="pods"))
    assert result.ok is True
    assert [action.view for action in bridge.applied if isinstance(action, Navigate)] == ["pods"]


def test_the_wiring_exposes_both_ports_separately() -> None:
    """Two ports, two lifetimes: the tools-layer `UIBridge` the executor and
    MCP share, and the agent-layer `AgentUiBridge` the session reads. They
    are deliberately not the same object."""
    from korvid.__main__ import (
        _AgentToolUIBridgeProxy,
        _AgentUiBridgeProxy,
        _build_agent_wiring,
    )
    from korvid.core.config import KorvidConfig

    wiring = _build_agent_wiring(KorvidConfig(), cast("Any", object()), {})
    assert isinstance(wiring.tool_bridge, _AgentToolUIBridgeProxy)
    assert isinstance(wiring.ui_bridge, _AgentUiBridgeProxy)


def test_the_composition_root_composes_the_agent_from_harness_parts_only() -> None:
    """One agent object, assembled here, from the published harness parts.

    `tests/test_agent_replacement_guard.py` proves the retired names are
    gone from the whole tree; this pins the positive half for the surface
    that actually wires the agent — the composition root plus every screen,
    widget and controller in `ui/`. A prompt string, a tier-to-surface
    table or a second engine appearing in `ui/` would mean the UI had
    started composing an agent of its own again.
    """
    banned = (
        "cluster_context_note",
        "prompt_pack",
        "PROMPT_PACKS",
        "NativeAgentEngine",
        "ToolHarness",
        "RequestGateway",
    )
    ui_package = Path(korvid.__file__).parent / "ui"
    ui_sources = sorted(ui_package.rglob("*.py"))
    assert len(ui_sources) > 1  # the ui package really was scanned
    for path in ui_sources:
        source = path.read_text(encoding="utf-8")
        for name in banned:
            assert name not in source, f"{path}: {name}"

    root = Path(korvid.__main__.__file__).read_text(encoding="utf-8")
    for part in (
        "NativeAgentEngine",
        "DefaultAgentSession",
        "PromptHarness",
        "RequestGateway",
        "ToolHarness",
        "ConversationState",
        "EvidenceLedger",
        "ModelRouter",
    ):
        assert part in root, f"__main__.py no longer wires {part}"


#: Model-facing prompt material the composition root must never touch. Wiring
#: a `PromptHarness` is the whole job; reaching past it into the layer text,
#: the pack registries, or the cluster-note formatter would make `__main__.py`
#: a second author of what the model reads — which is exactly the split
#: `prompt_harness.py` exists to hold (issue #316 task 6).
_FORBIDDEN_PROMPT_COMPOSITION = (
    "cluster_context_note",
    "SAFETY_CONTRACT",
    "COMMON_ROLE",
    "PROMPT_PACKS",
    "PROVIDER_PROMPT_OVERLAYS",
    "MODEL_PROMPT_OVERLAYS",
    "LOW_KORVID_OPERATOR_PACK",
    "HIGH_KORVID_OPERATOR_PACK",
    "LOW_TOOL_DESCRIPTIONS",
    "ComposedPrompt",
    "PromptInputs",
    "prompt_pack_id",
)


def _referenced_names(tree: ast.AST) -> set[str]:
    """Every bare name and attribute the module mentions anywhere."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
            names.update(alias.asname for alias in node.names if alias.asname)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _main_tree() -> ast.Module:
    return ast.parse(
        Path(korvid.__main__.__file__).read_text(encoding="utf-8"),
        filename="__main__.py",
    )


def test_the_composition_root_composes_no_model_facing_prompt_text() -> None:
    """Structural, not textual: the AST must not *reference* the prompt layers.

    A grep would trip over the legitimate `from korvid.agent.prompt_harness
    import PromptHarness`; this looks at what the module actually names, so
    constructing and injecting the harness stays allowed while reaching into
    its layers does not.
    """
    referenced = _referenced_names(_main_tree())

    found = sorted(name for name in _FORBIDDEN_PROMPT_COMPOSITION if name in referenced)
    assert found == [], f"__main__.py composes model-facing prompt text: {found}"


def test_the_composition_root_never_imports_the_prompt_pack_registry() -> None:
    """Layer text reaches the model through the harness or not at all."""
    banned_modules = ("korvid.agent.prompt_packs",)
    for node in ast.walk(_main_tree()):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in banned_modules, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in banned_modules, alias.name


def test_the_composition_root_still_builds_and_injects_the_prompt_harness() -> None:
    """The positive half: the guard must not pass by wiring nothing at all."""
    tree = _main_tree()
    constructed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PromptHarness"
    ]
    assert constructed, "__main__.py no longer constructs a PromptHarness"

    injected = [
        keyword
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "prompt_harness"
    ]
    assert injected, "__main__.py no longer injects the harness into the session"


def test_agent_wiring_includes_ui_tools(monkeypatch: object) -> None:
    """The composition root arms the session with READ_TOOLS + UI_TOOLS."""
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
        agent_model_tier="high",
    )
    kube_stub = cast("Any", object())  # wiring never touches kube before a tool call
    wiring = _build_agent_wiring(config, kube_stub, {})
    session = wiring.session
    proxy = wiring.tool_bridge
    assert session is not None
    names = [t["function"]["name"] for t in session.policy.tools]
    assert "navigate" in names
    assert "list_resources" in names
    assert "delete_resource" in names  # writes armed by default (approval-gated)
    assert wiring.tool_bridge is proxy

    # readonly strips every write tool: the model is never told they exist.
    wiring = _build_agent_wiring(dataclasses.replace(config, readonly=True), kube_stub, {})
    ro_session = wiring.session
    assert ro_session is not None
    ro_names = [t["function"]["name"] for t in ro_session.policy.tools]
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

    wiring = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=True)
    session = wiring.session
    assert session is not None
    assert "resize_pod" in [t["function"]["name"] for t in session.policy.tools]

    wiring = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=False)
    gated = wiring.session
    assert gated is not None
    assert "resize_pod" not in [t["function"]["name"] for t in gated.policy.tools]

    wiring = _build_agent_wiring(
        dataclasses.replace(config, readonly=True), kube_stub, {}, pod_resize_supported=True
    )
    ro = wiring.session
    assert ro is not None
    assert "resize_pod" not in [t["function"]["name"] for t in ro.policy.tools]


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
    from korvid.__main__ import _AgentToolUIBridgeProxy

    probe = _OverlapProbeBridge()
    proxy = _AgentToolUIBridgeProxy()
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


async def test_cluster_facts_reach_the_session_as_facts_not_prose(
    monkeypatch: object,
) -> None:
    """The detected cloud provider is a typed fact the session composes its
    own prompt from (issue #30 + #316 task 12) — the composition root never
    hands the agent a sentence to paste into a system message."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.interaction import ClusterFacts
    from korvid.agent.setup import AgentSettings
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
    azure = ClusterFacts(provider="azure", distribution="aks")
    wiring = _build_agent_wiring(config, kube_stub, {}, cluster=azure)
    assert wiring.session is not None
    assert wiring.rebuild is not None

    rebuilt = wiring.rebuild(
        AgentSettings(
            provider="openai",
            auth_method="api_key",
            base_url="http://localhost:9999/v1",
            model="m",
            api_key_env="KORVID_TEST_KEY",
        )
    )
    assert rebuilt is not None
    # A rebuild inherits the cluster the wiring last learned about; what
    # that produces on the wire is pinned by the end-to-end test below.
    assert rebuilt.policy.model == wiring.session.policy.model


def test_the_wiring_takes_cluster_facts_not_a_note() -> None:
    """`cluster_context` prose is gone: the parameter is typed."""
    import inspect

    from korvid.__main__ import _build_agent_wiring

    parameters = inspect.signature(_build_agent_wiring).parameters
    assert "cluster_context" not in parameters
    assert "cluster" in parameters


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
        cast("Any", [SimpleNamespace(agent_session=None, config=startup_config)]),  # app_box
        discovery_box,
        lambda session, resize, cluster: None,
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


async def test_the_low_tier_is_resolved_from_config(monkeypatch: object) -> None:
    """`agent.model_tier: low` is an explicit user choice: the router must
    resolve it and say so, and the surface must shrink with it (issue #71)."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.model_policy import CapabilitySource, ModelTier
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
        agent_model_tier="low",
    )
    kube_stub = cast("Any", object())
    wiring = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=True)
    session = wiring.session
    rebuild = wiring.rebuild
    assert session is not None
    assert session.policy.tier is ModelTier.LOW
    assert session.policy.route_source is CapabilitySource.USER
    names = [t["function"]["name"] for t in session.policy.tools]
    assert "diagnose_pod" in names
    assert "open_logs" in names
    assert "delete_resource" in names  # writes stay available (approval-gated)
    assert "resize_pod" in names
    assert "navigate" not in names
    assert "set_filter" not in names
    assert "drill_down" not in names
    assert session.policy.max_tool_calls_per_iteration == 1
    assert session.policy.allow_parallel_tool_calls is False

    # The wizard's rebuild carries its own tier choice.
    assert rebuild is not None
    high = rebuild(
        AgentSettings(
            provider="openai-compat",
            auth_method="api_key",
            base_url="http://localhost:9999/v1",
            model="m",
            api_key_env="KORVID_TEST_KEY",
            model_tier="high",
        )
    )
    assert high is not None
    assert high.policy.tier is ModelTier.HIGH
    assert high.policy.route_source is CapabilitySource.USER
    assert "navigate" in [t["function"]["name"] for t in high.policy.tools]


async def test_an_unset_tier_is_routed_not_forced(monkeypatch: object) -> None:
    """With no `agent.model_tier` the router decides, and reports where the
    decision came from — never `USER`, which would be a lie about intent."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.model_policy import CapabilitySource
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
    wiring = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=True)
    session = wiring.session
    assert session is not None
    assert session.policy.route_source is not CapabilitySource.USER
    assert session.policy.route_source in (
        CapabilitySource.CATALOG,
        CapabilitySource.PROVIDER,
        CapabilitySource.FALLBACK,
    )


async def test_a_ctx_retarget_rearms_the_surface_and_keeps_the_tier(
    monkeypatch: object,
) -> None:
    """A `:ctx` switch re-resolves the policy from the *current* provider
    facts and the new cluster's environment (issues #36 + #71): the new
    cluster's resize capability is picked up without changing the routed
    tier or the model, which `retarget` refuses outright."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.interaction import ClusterFacts
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
        agent_model_tier="low",
    )
    kube_stub = cast("Any", object())
    wiring = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=False)
    session = wiring.session
    retarget = wiring.retarget
    assert session is not None
    assert "resize_pod" not in [t["function"]["name"] for t in session.policy.tools]
    before = session.policy

    retarget(session, True, ClusterFacts(provider="aws", distribution="eks"))
    names = [t["function"]["name"] for t in session.policy.tools]
    assert "resize_pod" in names  # new cluster's capability picked up
    assert "navigate" not in names  # still the low-tier surface
    assert session.policy.tier is before.tier
    assert session.policy.model == before.model


async def test_a_ctx_retarget_re_arms_a_later_rebuild(monkeypatch: object) -> None:
    """The switch also updates what a *future* wizard rebuild starts from:
    a session built after the switch sees the new cluster's capabilities."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    mp.setenv("KORVID_TEST_KEY", "k")

    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.interaction import ClusterFacts
    from korvid.agent.setup import AgentSettings
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="openai",
        agent_auth_method="api_key",
        agent_base_url="http://localhost:9999/v1",
        agent_model="m",
        agent_api_key_env="KORVID_TEST_KEY",
        agent_model_tier="low",
    )
    kube_stub = cast("Any", object())
    wiring = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=False)
    session = wiring.session
    assert session is not None
    wiring.retarget(session, True, ClusterFacts(provider="aws", distribution="eks"))

    rebuild = wiring.rebuild
    assert rebuild is not None
    rebuilt = rebuild(
        AgentSettings(
            provider="openai",
            auth_method="api_key",
            base_url="http://localhost:9999/v1",
            model="m",
            api_key_env="KORVID_TEST_KEY",
            model_tier="low",
        )
    )
    assert rebuilt is not None
    assert "resize_pod" in [t["function"]["name"] for t in rebuilt.policy.tools]


#: Two rules at the parser's own per-rule ceiling. Composed into the
#: static prompt they push a low-tier policy (24,000-character history,
#: a 25% static share) past its budget — the exact shape of an operator
#: who migrated a large retired prompt block into `agent.rules`.
_OVERSIZED_RULES = ("R" * 1000, "S" * 1000)


async def test_an_uncomposable_prompt_disables_only_the_agent_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rules block too large for the routed model must not fail the start.

    `DefaultAgentSession` validates the static prompt at construction, so
    an operator whose `agent.rules` no longer fit the automatically routed
    low-tier budget would otherwise get a traceback out of the composition
    root instead of a TUI — and, under the app's restart-on-failure
    handling, a start that fails the same way every time. It is the same
    class of configuration mistake as a model that cannot call tools, and
    degrades the same way: korvid comes up, the agent is off, one warning
    says why and what to change, and the `:ai` wizard is still there to
    point the agent somewhere it fits.
    """
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _stub_providers(monkeypatch)
    warnings: list[str] = []
    wiring = _build_agent_wiring(
        _agent_config(agent_rules=_OVERSIZED_RULES),
        cast("Any", object()),
        {},
        startup_warnings=warnings,
    )

    assert wiring.session is None
    assert wiring.session_box[0] is None
    # The provider stays owned by the box the teardown guard reads: a
    # degraded agent must not leak the credential client it built.
    assert wiring.provider_box[0] is not None
    # Recovery is still wired: the wizard can re-point the agent, and the
    # rebuild it drives is the same transaction it always was.
    assert wiring.configurator is not None
    assert wiring.rebuild is not None
    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.startswith("agent disabled:")
    assert "agent.rules" in warning
    # Actionable, and never an echo of what the operator wrote: the rule
    # text can carry anything, including secrets.
    assert "R" * 1000 not in warning
    assert "S" * 1000 not in warning


async def test_the_degraded_start_neither_raises_nor_composes_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is caught where the session is built, not swallowed later.

    Pins the two halves a restart loop would need: nothing propagates out
    of the wiring, and the failure really is the prompt harness refusing
    this policy (so a future change that stops validating eagerly fails
    here rather than silently shipping an agent with an over-budget
    prompt).
    """
    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.prompt_harness import PromptHarness, StaticPromptTooLargeError

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _stub_providers(monkeypatch)
    refusals: list[tuple[str, ...]] = []
    original = PromptHarness.validate

    def _record(self: Any, policy: Any, user_rules: tuple[str, ...] = ()) -> None:
        try:
            original(self, policy, user_rules)
        except StaticPromptTooLargeError:
            refusals.append(user_rules)
            raise

    monkeypatch.setattr(PromptHarness, "validate", _record)

    warnings: list[str] = []
    wiring = _build_agent_wiring(
        _agent_config(agent_rules=_OVERSIZED_RULES),
        cast("Any", object()),
        {},
        startup_warnings=warnings,
    )

    assert refusals == [_OVERSIZED_RULES]
    assert wiring.session is None
    assert warnings != []


def test_an_over_budget_prompt_names_the_knob_the_operator_controls() -> None:
    """The rules are the operator's, so the hint points at the rules.

    `StaticPromptTooLargeError` is the one composition failure a
    configuration change fixes: shorten `agent.rules`, or route somewhere
    with a larger budget. The rule text itself is never echoed — it is
    operator-authored and can carry anything.
    """
    from korvid.__main__ import _warn_agent_disabled
    from korvid.agent.prompt_harness import StaticPromptTooLargeError

    warnings: list[str] = []
    _warn_agent_disabled(
        StaticPromptTooLargeError("static system prompt is 9001 characters, over 25%"),
        warnings,
    )

    assert len(warnings) == 1
    warning = warnings[0]
    assert "agent.rules" in warning
    assert ":ai" in warning


@pytest.mark.parametrize(
    "error_name",
    ["UnknownPromptPackError", "UnknownPromptOverlayError", "PromptCompositionError"],
)
def test_a_missing_prompt_pack_is_not_blamed_on_the_operators_rules(error_name: str) -> None:
    """A pack or overlay korvid ships is korvid's, not the operator's.

    `UnknownPromptPackError`/`UnknownPromptOverlayError` mean the routed
    policy named a prompt layer the *installed* korvid does not carry — a
    broken or partial install, or a bug. Telling that operator to shorten
    `agent.rules` sends them to edit configuration that is already
    correct, and they can shorten it to nothing without the start
    recovering. The hint has to name the install and where to report it.
    """
    from korvid.__main__ import _warn_agent_disabled
    from korvid.agent import prompt_harness

    error_type = getattr(prompt_harness, error_name)
    warnings: list[str] = []
    _warn_agent_disabled(error_type("prompt pack 'k' is not a shipped pack"), warnings)

    assert len(warnings) == 1
    warning = warnings[0]
    assert warning.startswith("agent disabled:")
    assert "agent.rules" not in warning
    assert "reinstall" in warning
    assert "report" in warning


def test_the_two_prompt_hints_are_fixed_text_that_quotes_no_payload() -> None:
    """Neither hint is assembled from anything a failure was carrying.

    A startup warning is rendered in the TUI and written to the log, so
    the hint half of it must be a constant. Only the exception's own
    (korvid-authored, bounded) message varies, and the warning is exactly
    that message followed by the constant.
    """
    from korvid.__main__ import _PROMPT_DEGRADE_HINT, _PROMPT_PACKAGING_HINT, _warn_agent_disabled
    from korvid.agent.prompt_harness import StaticPromptTooLargeError, UnknownPromptPackError

    over_budget: list[str] = []
    _warn_agent_disabled(StaticPromptTooLargeError("static system prompt too large"), over_budget)
    missing_pack: list[str] = []
    _warn_agent_disabled(UnknownPromptPackError("prompt pack 'k' is not shipped"), missing_pack)

    assert over_budget == [
        f"agent disabled: static system prompt too large — {_PROMPT_DEGRADE_HINT}"
    ]
    assert missing_pack == [
        f"agent disabled: prompt pack 'k' is not shipped — {_PROMPT_PACKAGING_HINT}"
    ]
    assert _PROMPT_DEGRADE_HINT != _PROMPT_PACKAGING_HINT


def test_a_model_that_cannot_call_tools_still_gets_no_prompt_advice() -> None:
    """The other degrade arm is unchanged: neither prompt hint applies."""
    from korvid.__main__ import _PROMPT_DEGRADE_HINT, _PROMPT_PACKAGING_HINT, _warn_agent_disabled
    from korvid.agent.model_policy import ModelRoutingError

    warnings: list[str] = []
    _warn_agent_disabled(ModelRoutingError("model reports no tool support"), warnings)

    assert len(warnings) == 1
    assert _PROMPT_DEGRADE_HINT not in warnings[0]
    assert _PROMPT_PACKAGING_HINT not in warnings[0]


async def test_a_rebuild_that_cannot_compose_stays_transactional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup degrade must not soften the `:ai` wizard's swap.

    A start that routes high composes the same rules comfortably; asking
    the wizard for the low tier makes them over-budget. That failure is
    the wizard's to show — the live session and provider stay exactly as
    they were, only the half-built replacement is released, and the error
    reaches the caller instead of leaving the user with a silently
    unchanged agent.
    """
    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.prompt_harness import StaticPromptTooLargeError

    providers = _stub_providers(monkeypatch)
    warnings: list[str] = []
    wiring = _build_agent_wiring(
        _agent_config(agent_model_tier="high", agent_rules=_OVERSIZED_RULES),
        cast("Any", object()),
        {},
        startup_warnings=warnings,
    )
    session = wiring.session
    assert session is not None
    assert warnings == []
    live_provider = wiring.provider_box[0]
    rebuild = wiring.rebuild
    assert rebuild is not None

    with pytest.raises(StaticPromptTooLargeError, match="static system prompt"):
        rebuild(
            AgentSettings(
                provider="openai",
                auth_method="api_key",
                base_url="http://localhost:9999/v1",
                model="m",
                api_key_env="KORVID_TEST_KEY",
                model_tier="low",
            )
        )

    assert wiring.provider_box[0] is live_provider
    assert wiring.session_box[0] is session
    # Only the replacement it built is released.
    assert providers[-1] is not live_provider
    await _wait_for_provider_close(providers[-1])
    await session.aclose()


async def _wait_for_provider_close(provider: Any) -> None:
    """Wait for the background close of a discarded provider."""
    for _ in range(50):
        if provider.closed:
            return
        await asyncio.sleep(0.01)
    assert provider.closed


async def test_a_refused_retarget_fails_the_switch_instead_of_keeping_the_old_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retarget the session refuses must fail the `:ctx` switch.

    Swallowing it leaves an agent armed with the *previous* cluster's tool
    surface and prompt facts while the TUI, the audit log and the write
    perimeter have all moved to the new one — the agent would answer about
    a cluster nobody is looking at, and cite evidence read from it. The
    context-switch transaction owns rollback and the user-visible failure,
    so the composition root's job is to let the error reach it.
    """
    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.interaction import ClusterFacts
    from korvid.agent.model_policy import ModelDescriptor
    from korvid.agent.session import SessionRetargetError

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    providers = _stub_providers(monkeypatch)
    wiring = _build_agent_wiring(
        _agent_config(agent_model_tier="low"), cast("Any", object()), {}, pod_resize_supported=False
    )
    session = wiring.session
    assert session is not None
    before = session.policy
    reference = session.evidence.record(
        "get_logs", {"namespace": "prod", "name": "api"}, "OOMKilled"
    )
    assert reference is not None

    # The live provider now serves a different model, so re-resolving
    # produces a policy only a rebuilt session can adopt.
    providers[0]._descriptor = ModelDescriptor("test", "another-model")

    with pytest.raises(SessionRetargetError, match="rebuild the session"):
        wiring.retarget(session, True, ClusterFacts(provider="aws", distribution="eks"))

    # Nothing half-moved: the session still holds the cluster it was on,
    # which is exactly why the caller must not present the switch as done.
    assert session.policy is before
    assert session.evidence.resolve(reference) is not None
    await session.aclose()


async def test_a_failed_policy_resolution_propagates_out_of_the_retarget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The router refusing the new environment is the same failure: the
    composition root re-raises instead of logging and carrying on."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.interaction import ClusterFacts
    from korvid.agent.model_policy import ModelRoutingError

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _stub_providers(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
    session = wiring.session
    assert session is not None

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise ModelRoutingError("no tool support in this environment")

    monkeypatch.setattr(korvid.__main__, "_resolve_agent_policy", _refuse)
    with pytest.raises(ModelRoutingError, match="no tool support"):
        wiring.retarget(session, True, ClusterFacts(provider="aws", distribution="eks"))
    await session.aclose()


async def test_a_refused_retarget_aborts_the_context_switch_closure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure has to reach the `:ctx` transaction, which owns rollback
    and the user-visible error — `switch_context` must not return a result
    describing a switch whose agent half never happened."""
    import contextlib

    import korvid.__main__ as main_mod
    from korvid.__main__ import _make_switch_context
    from korvid.agent.session import SessionRetargetError
    from korvid.core.config import KorvidConfig
    from korvid.k8s.csp import ProviderInfo

    class FakeKube:
        async def switch_context(self, name: str | None) -> None:
            return None

        async def supports_pod_resize(self) -> bool:
            return False

        async def detect_cloud_provider(self) -> ProviderInfo:
            return ProviderInfo("azure", "aks")

        async def discover_resources(self) -> list[Any]:
            return []

    monkeypatch.setattr(main_mod, "resolve_context_namespace", lambda name: None)
    startup = KorvidConfig(namespace="default")
    app_stub = SimpleNamespace(agent_session=object(), config=startup)
    discovery_box: list[asyncio.Task[None]] = []

    def _refuse(session: Any, resize: bool, cluster: Any) -> None:
        raise SessionRetargetError(
            "cannot retarget a live session onto a policy that changes model"
        )

    switch = _make_switch_context(
        startup,
        cast("Any", FakeKube()),
        {},
        cast("Any", [app_stub]),
        discovery_box,
        _refuse,
    )
    try:
        with pytest.raises(SessionRetargetError, match="cannot retarget"):
            await switch("ctx-b")
    finally:
        for task in discovery_box:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def test_a_switch_retargets_the_session_with_typed_cluster_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`:ctx` converts the probed `ProviderInfo` into `ClusterFacts` and
    hands it to `retarget` — the session, not the composition root, decides
    what that means for the prompt."""
    import contextlib

    import korvid.__main__ as main_mod
    from korvid.__main__ import _make_switch_context
    from korvid.agent.interaction import ClusterFacts
    from korvid.core.config import KorvidConfig
    from korvid.k8s.csp import ProviderInfo

    class FakeKube:
        async def switch_context(self, name: str | None) -> None:
            return None

        async def detect_cloud_provider(self) -> ProviderInfo:
            return ProviderInfo("azure", "aks")

        async def discover_resources(self) -> list[Any]:
            return []

    monkeypatch.setattr(main_mod, "resolve_context_namespace", lambda name: None)
    calls: list[tuple[Any, bool, Any]] = []
    startup = KorvidConfig(namespace="default", readonly=True)
    session = object()
    app_stub = SimpleNamespace(agent_session=session, config=startup)
    discovery_box: list[asyncio.Task[None]] = []
    switch = _make_switch_context(
        startup,
        cast("Any", FakeKube()),
        {},
        cast("Any", [app_stub]),
        discovery_box,
        lambda target, resize, cluster: calls.append((target, resize, cluster)),
    )
    try:
        await switch("ctx-b")
    finally:
        for task in discovery_box:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    assert calls
    target, _resize, cluster = calls[-1]
    assert target is session
    assert cluster == ClusterFacts(provider="azure", distribution="aks")


async def test_ctx_switch_result_carries_the_context_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch result reports the target context's kubeconfig namespace so
    the app can adopt it as the session default (issue #36); no fallback
    namespace set is derived from config (issue #108)."""
    import asyncio
    import contextlib

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
    app_stub = SimpleNamespace(agent_session=None, config=startup)
    discovery_box: list[asyncio.Task[None]] = []
    switch = _make_switch_context(
        startup,
        cast("Any", FakeKube()),
        {},
        cast("Any", [app_stub]),
        discovery_box,
        lambda session, resize, cluster: None,
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


def test_load_startup_config_wraps_config_migration_error_as_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removed `agent.profile`/`agent.prompts` key must fail startup with
    one clear, actionable line — not an unfiltered traceback."""
    import korvid.__main__ as main_mod
    from korvid.core.config import ConfigMigrationError

    def _raise() -> Any:
        raise ConfigMigrationError(
            "agent.profile was removed; use agent.model_tier instead (absent/low/high)."
        )

    monkeypatch.setattr(main_mod, "load_config", _raise)
    with pytest.raises(SystemExit) as exc_info:
        main_mod._load_startup_config(False)
    message = str(exc_info.value)
    assert "\n" not in message  # one-line, actionable
    assert "agent.profile" in message
    assert "agent.model_tier" in message


def test_load_startup_config_wraps_migration_error_even_when_agent_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The removed-key check is unconditional: startup must not silently
    ignore a leftover `agent.profile` just because `agent.enabled: false`."""
    import korvid.__main__ as main_mod
    from korvid.core.config import load_config as real_load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  enabled: false\n  profile: full\n")
    monkeypatch.setattr(main_mod, "load_config", lambda: real_load_config(config_path))
    with pytest.raises(SystemExit) as exc_info:
        main_mod._load_startup_config(False)
    assert "agent.profile" in str(exc_info.value)


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


def test_main_module_version_exits_before_startup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import korvid.__main__ as main_mod

    monkeypatch.setattr(sys, "argv", ["korvid", "--version"])
    monkeypatch.setattr(
        main_mod,
        "_run",
        lambda *args, **kwargs: pytest.fail("startup must not run"),
    )

    with pytest.raises(SystemExit, match="0"):
        main_mod.main()

    assert capsys.readouterr().out.strip() == f"korvid {korvid.__version__}"


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
    requirement = f"korvid[all,entra]=={korvid.__version__}"
    with pytest.raises(
        SystemExit,
        match=(
            r"MCP support was requested.*"
            r"including mcp.*"
            rf"uv tool install --force '{re.escape(requirement)}'.*"
            rf"pipx install --force '{re.escape(requirement)}'"
        ),
    ):
        _build_mcp_controller(
            KorvidConfig(mcp_enabled=True), cast("KubeClient", object()), {}, None
        )


def test_missing_agent_extra_degrades_when_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the [agent] extra and without agent.provider configured,
    the wiring is session-less and the retarget hook is a safe no-op."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.interaction import ClusterFacts
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, *_AGENT_ROOTS)
    wiring = _build_agent_wiring(KorvidConfig(), cast("KubeClient", object()), {})
    session = wiring.session
    configurator = wiring.configurator
    rebuild = wiring.rebuild
    retarget = wiring.retarget
    provider_box = wiring.provider_box
    assert session is None
    assert configurator is None
    assert rebuild is None
    assert provider_box == [None]
    retarget(None, True, ClusterFacts(provider="aws", distribution=None))  # must not raise


def test_missing_agent_extra_fails_actionably_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agent.provider in config.yaml with the extra missing must exit with
    an install hint, never a bare ImportError traceback."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, *_AGENT_ROOTS)
    requirement = f"korvid[all,entra]=={korvid.__version__}"
    with pytest.raises(
        SystemExit,
        match=(
            r"the embedded agent is enabled.*"
            r"including agent.*"
            rf"uv tool install --force '{re.escape(requirement)}'.*"
            rf"pipx install --force '{re.escape(requirement)}'"
        ),
    ):
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


def test_httpx_without_keyring_does_not_compose_the_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An observability-only install has httpx but not keyring.

    The agent wiring must still degrade without loading the embedded-agent
    loop, and TokenStore's lazy keyring import must not fool the capability
    probe.
    """
    import sys

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, "keyring")  # observability keeps httpx importable
    for cached in list(sys.modules):
        if cached in ("korvid.agent.session", "korvid.agent.native_engine"):
            monkeypatch.delitem(sys.modules, cached)

    wiring = _build_agent_wiring(KorvidConfig(), cast("KubeClient", object()), {})
    session = wiring.session
    configurator = wiring.configurator
    rebuild = wiring.rebuild
    provider_box = wiring.provider_box
    assert session is None
    assert configurator is None
    assert rebuild is None
    assert provider_box == [None]
    assert "korvid.agent.session" not in sys.modules
    assert "korvid.agent.native_engine" not in sys.modules


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

    class _Integrations:
        follow_enabled = True

        def __init__(self) -> None:
            self.notes: list[str] = []

        def note_activity(self, line: str) -> None:
            self.notes.append(line)

    class _App:
        def __init__(self) -> None:
            self.integrations = _Integrations()

    fake_app = _App()
    hooks.app = cast("Any", fake_app)  # duck-typed stand-in for KorvidApp
    assert hooks.follow_enabled() is True
    hooks.note_activity("seen")
    assert fake_app.integrations.notes == ["seen"]


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
    wiring = _build_agent_wiring(config, kube_stub, {})
    session = wiring.session
    disconnect = wiring.disconnect
    provider_box = wiring.provider_box
    assert session is not None
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


async def test_a_plugin_property_that_raises_degrades_the_start_to_a_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A third-party `descriptor`/`capabilities` read is plugin code too.

    `ValidatedPluginProvider` reads both while wrapping the plugin's
    provider, before korvid has any use for them. A plugin that raises
    there — a lazy credential read, a probe — must degrade exactly like
    every other plugin failure: a startup warning, provider None, a
    usable TUI. Anything the exception carries stays out of the warning.
    """
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    config = dataclasses.replace(
        _company_plugin_config(), agent_options={"raise_in_property": "descriptor"}
    )
    warnings: list[str] = []

    wiring = _build_agent_wiring(config, cast("Any", object()), {}, startup_warnings=warnings)

    assert wiring.session is None
    assert wiring.provider_box[0] is None
    assert wiring.configurator is not None
    assert len(warnings) == 1
    assert "Provider plugin failed" in warnings[0]
    assert "PLUGIN_SECRET" not in warnings[0]


async def test_a_plugin_capabilities_property_that_raises_degrades_the_same_way(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The capability read is wrapped separately from the descriptor read."""
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    config = dataclasses.replace(
        _company_plugin_config(), agent_options={"raise_in_property": "capabilities"}
    )
    warnings: list[str] = []

    wiring = _build_agent_wiring(config, cast("Any", object()), {}, startup_warnings=warnings)

    assert wiring.session is None
    assert wiring.provider_box[0] is None
    assert len(warnings) == 1
    assert "Provider plugin failed" in warnings[0]
    assert "PLUGIN_SECRET" not in warnings[0]


async def test_plugin_rebuild_failure_keeps_the_previous_provider_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from korvid.__main__ import _build_agent_wiring
    from korvid.providers.plugin_registry import ProviderPluginError

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    wiring = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    session = wiring.session
    rebuild = wiring.rebuild
    provider_box = wiring.provider_box
    assert session is not None
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
    wiring = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    session = wiring.session
    rebuild = wiring.rebuild
    provider_box = wiring.provider_box
    assert session is not None
    assert rebuild is not None
    old_provider = provider_box[0]
    assert old_provider is not None

    new_session = rebuild(_company_plugin_settings())

    assert new_session is not None
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
    wiring = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    session = wiring.session
    disconnect = wiring.disconnect
    provider_box = wiring.provider_box
    assert session is not None
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
    wiring = _build_agent_wiring(
        _company_plugin_config(),
        cast("Any", object()),
        {},
    )
    session = wiring.session
    provider_box = wiring.provider_box
    assert session is not None
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
    wiring = _build_agent_wiring(
        config,
        kube_stub,
        {},
        startup_warnings=warnings,
    )
    session = wiring.session
    configurator = wiring.configurator
    _rebuild = wiring.rebuild
    provider_box = wiring.provider_box
    assert session is None  # provider disabled, not a crash
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
    wiring = _build_agent_wiring(config, kube_stub, {})
    rebuild = wiring.rebuild
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
# Finding #8: Rebuild transactional — profile/session failures
# ---------------------------------------------------------------------------


async def test_a_failed_tool_wiring_during_rebuild_keeps_the_old_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Rebuild is a transaction: the whole new provider *and* session are
    built before anything is swapped. If the tool wiring raises, only the
    new provider is released and the live session keeps running."""
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    wiring = _build_agent_wiring(_company_plugin_config(), cast("Any", object()), {})
    session = wiring.session
    provider_box = wiring.provider_box
    session_box = wiring.session_box
    assert session is not None
    old_provider = provider_box[0]
    assert old_provider is not None

    from korvid.tools import executor as executor_mod

    def _boom_te_init(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("tool executor construction failed")

    monkeypatch.setattr(executor_mod.ToolExecutor, "__init__", _boom_te_init)

    rebuild = wiring.rebuild
    assert rebuild is not None
    with pytest.raises(RuntimeError, match="tool executor construction failed"):
        rebuild(_company_plugin_settings())

    assert provider_box[0] is old_provider
    assert session_box[0] is session
    assert cast("Any", old_provider)._provider.close_calls == 0
    assert cast("Any", session).finalization_pending is False


async def test_a_failed_session_build_during_rebuild_closes_only_the_new_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _install_company_plugin_site(monkeypatch, tmp_path)
    wiring = _build_agent_wiring(_company_plugin_config(), cast("Any", object()), {})
    session = wiring.session
    provider_box = wiring.provider_box
    assert session is not None
    old_provider = provider_box[0]
    assert old_provider is not None

    from korvid.agent import session as session_mod

    def _boom_init(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("session construction failed")

    monkeypatch.setattr(session_mod.DefaultAgentSession, "__init__", _boom_init)

    rebuild = wiring.rebuild
    assert rebuild is not None
    with pytest.raises(RuntimeError, match="session construction failed"):
        rebuild(_company_plugin_settings())

    assert provider_box[0] is old_provider
    assert wiring.session_box[0] is session
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
    wiring = _build_agent_wiring(
        config,
        cast("Any", object()),
        {},
        startup_warnings=warnings,
    )
    session = wiring.session
    provider_box = wiring.provider_box
    # The agent must be disabled (None session) and the warning must be surfaced.
    assert session is None
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
    wiring = _build_agent_wiring(
        config,
        cast("Any", object()),
        {},
    )
    session = wiring.session
    # Built-in provider starts fine despite options_error
    assert session is not None


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


# ---------------------------------------------------------------------------
# Finding #5: github_copilot variant loads OAuth via canonical name
# ---------------------------------------------------------------------------


def test_github_copilot_variant_loads_oauth_token(monkeypatch: object) -> None:
    """A config with 'github_copilot' (underscore) must canonicalize to
    'github-copilot' so the composition root loads the OAuth token."""
    import pytest

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)

    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    # Config with the canonical name (produced by load_config canonicalization)
    config = KorvidConfig(
        agent_enabled=True,
        agent_provider="github-copilot",
        agent_auth_method="device-login",
        agent_model="gpt-4o",
    )
    kube_stub = cast("Any", object())

    # Patch TokenStore.load to track what key is requested and return None
    # (simulating no stored token).
    loaded_keys: list[str] = []
    from korvid.providers import token_store as ts_mod

    def _tracking_load(self: Any, key: str) -> str | None:
        loaded_keys.append(key)
        return None  # no token stored

    mp.setattr(ts_mod.TokenStore, "load", _tracking_load)

    wiring = _build_agent_wiring(config, kube_stub, {})
    session = wiring.session
    provider_box = wiring.provider_box
    # The composition root must have asked for "github-oauth" because the
    # canonical name matched "github-copilot".
    assert "github-oauth" in loaded_keys
    # No token stored → provider is None.
    assert session is None
    assert provider_box[0] is None


class _FakeAppCapturesKwargs:
    """Records every `KorvidApp` constructor kwarg `_wire_and_run` passes,
    without building a real Textual app (issue #281 task 7). `instances`
    lets the test reach the one instance `_wire_and_run` built even though
    nothing else in the wiring path hands it back to the caller."""

    instances: ClassVar[list[_FakeAppCapturesKwargs]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.captured = kwargs
        # `AppUIBridge(app)` reads exactly these two collaborators right
        # after construction: the agent controller it delegates every UI
        # tool to, and the dispatcher that marshals the call onto the app
        # context. Sentinels are enough - the bridge only stores them.
        # `agent_ui` is also read directly: the composition root binds the
        # session's workspace port to the controller's bridge.
        self._agent_ui: Any = SimpleNamespace(workspace_bridge=object())
        self._bridge_dispatch: Any = object()
        _FakeAppCapturesKwargs.instances.append(self)

    @property
    def agent_ui(self) -> Any:
        return self._agent_ui

    def on_aliases_updated(self) -> None:
        pass

    async def run_async(self) -> None:
        return None


class _FakeKubeForWiring:
    """Minimal double: only the attributes `_wire_and_run` touches before
    constructing `KorvidApp`. Most are referenced but never called during
    wiring (they become bound-method kwargs), so a cheap stub is enough -
    only `detect_cloud_provider` (the bounded cloud-provider probe) and
    `list_relationship_objects` (asserted below) are actually invoked."""

    def __init__(self) -> None:
        self.list_calls: list[tuple[Any, str | None]] = []
        self.relationship_list_calls: list[tuple[Any, str | None]] = []

    async def detect_cloud_provider(self) -> Any:
        from korvid.k8s.csp import detect_provider

        return detect_provider([])

    async def discover_resources(self) -> list[Any]:
        return []

    async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
        self.list_calls.append((meta, namespace))
        return []

    async def list_relationship_objects(self, meta: Any, namespace: str | None) -> list[Any]:
        self.relationship_list_calls.append((meta, namespace))
        return []

    def list_namespaces(self) -> Any: ...
    def get_helm_release_components(self, *a: Any, **k: Any) -> Any: ...
    def stream_logs(self, *a: Any, **k: Any) -> Any: ...
    def can_i(self, *a: Any, **k: Any) -> Any: ...
    def open_pod_exec(self, *a: Any, **k: Any) -> Any: ...
    def probe_context(self, *a: Any, **k: Any) -> Any: ...
    def list_pod_metrics(self, *a: Any, **k: Any) -> Any: ...
    def watch_warning_events(self, *a: Any, **k: Any) -> Any: ...

    async def switch_context(self, name: str | None) -> None:
        return None


async def test_wire_and_run_wires_relationship_lister_from_kube(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition root must hand KorvidApp a relationship lister backed
    by the connected client's own `list_relationship_objects` (issue #281
    task 7): the `g` binding's exclusive worker calls exactly this callable
    to resolve a root's dependents/dependencies for the relationship graph."""
    import korvid.__main__ as main_mod
    from korvid.core.config import KorvidConfig

    monkeypatch.setattr(main_mod, "KorvidApp", _FakeAppCapturesKwargs)
    _FakeAppCapturesKwargs.instances.clear()

    kube = _FakeKubeForWiring()
    config = KorvidConfig(readonly=True)  # skips the pods/resize probe round trip
    state = main_mod._RunState()
    await main_mod._wire_and_run(config, cast("Any", kube), state)

    # Discovery is fire-and-forget (issue #27's background task): drain it
    # so it doesn't outlive the test as an orphaned pending task.
    if state.discovery_box:
        await state.discovery_box[0]

    assert len(_FakeAppCapturesKwargs.instances) == 1
    captured = _FakeAppCapturesKwargs.instances[0].captured
    assert "approval_timeout_seconds" not in captured
    wired = captured["list_relationship_objects"]
    result = await wired("meta", "ns")
    assert result == []
    assert kube.relationship_list_calls == [("meta", "ns")]
    assert kube.list_calls == []


async def test_wire_and_run_passes_session_timeline_and_warning_watch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition root owns the timeline's bounds and its only live
    producer that is not already wired through the store: a session with no
    Warning feed would silently lose half the record (issue #282 task 3)."""
    import korvid.__main__ as main_mod
    from korvid.core.config import KorvidConfig
    from korvid.core.session_timeline import SessionTimeline

    monkeypatch.setattr(main_mod, "KorvidApp", _FakeAppCapturesKwargs)
    _FakeAppCapturesKwargs.instances.clear()

    kube = _FakeKubeForWiring()
    config = KorvidConfig(readonly=True, timeline_max_entries=7, timeline_max_bytes=4096)
    state = main_mod._RunState()
    await main_mod._wire_and_run(config, cast("Any", kube), state)
    if state.discovery_box:
        await state.discovery_box[0]

    captured = _FakeAppCapturesKwargs.instances[0].captured
    timeline = captured["session_timeline"]
    assert isinstance(timeline, SessionTimeline)
    assert captured["watch_warning_events"] == kube.watch_warning_events
    # The configured bounds reach the timeline, not just its constructor.
    for index in range(9):
        timeline.append_context_switch(
            epoch=0, phase="started", from_context=None, to_context=f"ctx-{index}"
        )
    assert timeline.snapshot(epoch=None, source=None, resource=None).stats.entry_count == 7


# ---------------------------------------------------------------------------
# Task 12: session ownership, rebuild/disconnect transactions, end-to-end
# ---------------------------------------------------------------------------


class _RecordingProvider:
    """A provider that streams one scripted turn and records its requests."""

    order: ClassVar[list[str]] = []

    def __init__(self, model: str = "m") -> None:
        from korvid.agent.model_policy import ModelDescriptor

        self._descriptor = ModelDescriptor("test", model)
        self.requests: list[list[dict[str, Any]]] = []
        self.surfaces: list[list[dict[str, Any]]] = []
        self.closed = 0

    @property
    def descriptor(self) -> Any:
        return self._descriptor

    @property
    def capabilities(self) -> Any:
        from korvid.agent.model_policy import ModelCapabilities

        return ModelCapabilities.unknown()

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool = True
    ) -> Any:
        import copy

        self.requests.append(copy.deepcopy(messages))
        self.surfaces.append(copy.deepcopy(tools))
        for event in self.script:
            yield event

    script: ClassVar[list[dict[str, Any]]] = [
        {"type": "text", "text": "ok"},
        {"type": "done"},
    ]

    async def aclose(self) -> None:
        self.closed += 1
        _RecordingProvider.order.append("provider")


def _stub_providers(monkeypatch: pytest.MonkeyPatch) -> list[_RecordingProvider]:
    """Make every `create_provider` call hand back a recording provider."""
    built: list[_RecordingProvider] = []

    def _create(**kwargs: Any) -> Any:
        if not kwargs.get("enabled", False):
            return None
        provider = _RecordingProvider(str(kwargs.get("model") or "m"))
        built.append(provider)
        return provider

    monkeypatch.setattr("korvid.providers.registry.create_provider", _create)
    return built


def _provider_closed(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """An event the background provider close sets when it completes.

    Rebuild and disconnect hand the old pair to background tasks, so the
    close order only exists once those tasks have run. Awaiting the event
    they set is the outcome itself; polling the clock for it would test
    how fast this machine happens to be, and flake on a loaded runner.
    """
    finished = asyncio.Event()
    original = _RecordingProvider.aclose

    async def _closed(self: _RecordingProvider) -> None:
        await original(self)
        finished.set()

    monkeypatch.setattr(_RecordingProvider, "aclose", _closed)
    return finished


async def _await_closes(finished: asyncio.Event) -> None:
    """Wait for the background close, bounded so a regression fails fast."""
    await asyncio.wait_for(finished.wait(), timeout=_CLOSE_TIMEOUT)


#: Upper bound on a background close, generous enough that only a real
#: regression (a close that never happens) can reach it. Nothing asserts
#: on how long the close actually took.
_CLOSE_TIMEOUT = 10.0


def _agent_config(**overrides: Any) -> Any:
    from korvid.core.config import KorvidConfig

    base: dict[str, Any] = {
        "agent_enabled": True,
        "agent_provider": "openai",
        "agent_auth_method": "api_key",
        "agent_base_url": "http://localhost:9999/v1",
        "agent_model": "m",
        "agent_api_key_env": "KORVID_TEST_KEY",
    }
    base.update(overrides)
    return KorvidConfig(**base)


def _settings(model: str = "m2") -> AgentSettings:
    return AgentSettings(
        provider="openai",
        auth_method="api_key",
        base_url="http://localhost:9999/v1",
        model=model,
        api_key_env="KORVID_TEST_KEY",
    )


class _CountingBridge:
    """The agent-layer UI port a bound proxy forwards to."""

    def __init__(self) -> None:
        self.snapshots = 0
        self.applied: list[Any] = []

    def snapshot(self) -> Any:
        from korvid.agent.interaction import InteractionContext, PaneContext

        self.snapshots += 1
        return InteractionContext(
            kube_context="kind-dev",
            context_epoch=1,
            focused_pane=PaneContext(
                kind="pods", scope="default", filter_pattern=None, selected=None
            ),
            secondary_pane=None,
            timeline_cursor=None,
        )

    async def apply(self, action: Any) -> Any:
        from korvid.agent.interaction import UiActionResult

        self.applied.append(action)
        return UiActionResult(ok=True, message="done", context=self.snapshot())


async def test_a_turn_carries_the_cluster_facts_and_the_configured_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end through the production wiring: the cluster the composition
    root probed and the operator's `agent.rules` both reach the wire, and
    they get there as *composed prompt state*, never as a prose parameter."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.agent.interaction import ClusterFacts

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    providers = _stub_providers(monkeypatch)
    config = _agent_config(agent_rules=("never touch kube-system",))
    wiring = _build_agent_wiring(
        config,
        cast("Any", object()),
        {},
        cluster=ClusterFacts(provider="azure", distribution="aks"),
    )
    assert wiring.session is not None
    wiring.ui_bridge.target = cast("Any", _CountingBridge())

    events = [event async for event in wiring.session.run_turn("what is wrong?")]
    assert events
    system = providers[0].requests[0][0]["content"]
    assert "aks" in system.lower()
    assert "never touch kube-system" in system
    await wiring.session.aclose()


async def test_the_session_reads_the_workspace_through_the_bound_ui_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _stub_providers(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
    assert wiring.session is not None
    bridge = _CountingBridge()
    wiring.ui_bridge.target = cast("Any", bridge)

    [event async for event in wiring.session.run_turn("hi")]
    assert bridge.snapshots >= 1
    await wiring.session.aclose()


async def test_a_turn_before_the_ui_is_bound_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fabricated workspace: a turn started before the app exists must
    surface the wiring bug, not invent a screen for the model."""
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _stub_providers(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
    assert wiring.session is not None
    with pytest.raises(RuntimeError, match="agent UI not ready"):
        [event async for event in wiring.session.run_turn("hi")]
    await wiring.session.aclose()


async def test_rebuild_swaps_both_boxes_and_closes_the_session_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old session is closed *before* the old provider: closing the
    provider first would tear the transport out from under a turn the
    session is still winding down."""
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _RecordingProvider.order.clear()
    _stub_providers(monkeypatch)
    closed = _provider_closed(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
    old_session = wiring.session
    old_provider = wiring.provider_box[0]
    assert old_session is not None
    assert old_provider is not None

    closes: list[str] = []
    original = type(old_session).aclose

    async def _record_close(self: Any) -> None:
        closes.append("session")
        _RecordingProvider.order.append("session")
        await original(self)

    monkeypatch.setattr(type(old_session), "aclose", _record_close)

    rebuild = wiring.rebuild
    assert rebuild is not None
    new_session = rebuild(_settings())
    assert new_session is not None
    assert new_session is not old_session
    assert wiring.session_box[0] is new_session
    assert wiring.provider_box[0] is not old_provider

    await _await_closes(closed)
    assert _RecordingProvider.order[:2] == ["session", "provider"]
    assert closes == ["session"]
    await new_session.aclose()


async def test_disconnect_clears_both_boxes_and_closes_session_then_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    _RecordingProvider.order.clear()
    _stub_providers(monkeypatch)
    closed = _provider_closed(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
    session = wiring.session
    assert session is not None
    original = type(session).aclose

    async def _record_close(self: Any) -> None:
        _RecordingProvider.order.append("session")
        await original(self)

    monkeypatch.setattr(type(session), "aclose", _record_close)

    wiring.disconnect()
    assert wiring.provider_box[0] is None
    assert wiring.session_box[0] is None

    await _await_closes(closed)
    assert _RecordingProvider.order[:2] == ["session", "provider"]
    wiring.disconnect()  # idempotent when already off
    assert wiring.session_box[0] is None


async def test_teardown_closes_the_session_before_the_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure between building the agent and mounting the app leaves the
    session owned by `_RunState`; global teardown must release it in the
    same order a normal shutdown would."""
    import korvid.__main__ as main_mod

    order: list[str] = []

    class _Session:
        async def aclose(self) -> None:
            order.append("session")

    class _Provider:
        async def aclose(self) -> None:
            order.append("provider")

    class _Kube:
        async def close(self) -> None:
            order.append("kube")

    state = main_mod._RunState()
    state.provider_box[0] = cast("Any", _Provider())
    state.session_box[0] = cast("Any", _Session())
    await main_mod._teardown(state, cast("Any", _Kube()))
    assert order == ["session", "provider", "kube"]


async def test_teardown_after_a_normal_shutdown_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The app closes the session on unmount and teardown may close it
    again: `AgentSession.aclose` is idempotent, so this must be inert."""
    import korvid.__main__ as main_mod

    closes: list[str] = []

    class _Session:
        def __init__(self) -> None:
            self.closed = 0

        async def aclose(self) -> None:
            self.closed += 1
            closes.append("session")

    class _Kube:
        async def close(self) -> None:
            return None

    session = _Session()
    state = main_mod._RunState()
    state.session_box[0] = cast("Any", session)
    await main_mod._teardown(state, cast("Any", _Kube()))
    await main_mod._teardown(state, cast("Any", _Kube()))
    assert session.closed == 1  # the box is cleared after the first close
    assert closes == ["session"]


async def test_a_model_that_reports_no_tool_support_warns_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that says it cannot call tools has no usable session — but
    that is a configuration problem, not a reason to refuse to start korvid.

    The wiring degrades to no session and a startup warning; the panel then
    shows the setup hint and `:ai` can point the agent somewhere workable.
    The provider is still owned by the box, so teardown releases it.
    """
    from korvid.__main__ import _build_agent_wiring

    class _ToollessProvider(_RecordingProvider):
        @property
        def capabilities(self) -> Any:
            from korvid.agent.model_policy import ModelCapabilities

            return dataclasses.replace(ModelCapabilities.unknown(), supports_tools=False)

    def _create(**kwargs: Any) -> Any:
        return _ToollessProvider() if kwargs.get("enabled", False) else None

    monkeypatch.setattr("korvid.providers.registry.create_provider", _create)
    warnings: list[str] = []
    wiring = _build_agent_wiring(
        _agent_config(), cast("Any", object()), {}, startup_warnings=warnings
    )

    assert wiring.session is None
    assert wiring.session_box == [None]
    assert isinstance(wiring.provider_box[0], _ToollessProvider)
    assert any("tool" in warning for warning in warnings)
