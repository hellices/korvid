"""Tests for composition-root helpers in korvid.__main__."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import re
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
import yaml

import korvid
import korvid.__main__
from korvid.__main__ import _close_provider_in_background
from korvid.agent.model_policy import ModelCapabilities, ModelDescriptor
from korvid.agent.model_profiles import split_reference
from korvid.agent.provider import LLMProvider
from korvid.core.config import (
    ConnectionAuthConfig,
    ModelConnectionConfig,
    ModelConnectionsConfig,
)


@pytest.fixture(autouse=True)
def _cache_home_away_from_the_operator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep composition-root tests out of the real user cache.

    `_build_model_catalog()` constructs the models.dev source, and that
    source reads whatever cache is already on disk. Run under a developer's
    own account that is `~/Library/Caches/korvid/models-dev.json`: a real
    file whose presence, contents or staleness would decide what these
    tests see, and which a test has no business reading or writing. The
    subprocess probes below inherit the redirected environment too, so the
    isolation holds across the process boundary.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


class _BoomProvider:
    async def aclose(self) -> None:
        raise RuntimeError("boom")


class _OkProvider:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


async def test_close_task_reference_is_retained_until_done() -> None:
    provider = _OkProvider()
    tasks: set[asyncio.Future[Any]] = set()
    _close_provider_in_background(cast("LLMProvider", provider), tasks)
    assert len(tasks) == 1  # strong reference held while pending
    for _ in range(3):
        await asyncio.sleep(0)
    assert provider.closed
    assert not tasks  # reaped once complete


async def test_close_errors_are_consumed() -> None:
    tasks: set[asyncio.Future[Any]] = set()
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

    class _SecretBoomProvider:
        async def aclose(self) -> None:
            raise RuntimeError("SUPER_SECRET_API_KEY_leak_attempt" * 5)

    tasks: set[asyncio.Future[Any]] = set()
    _close_provider_in_background(cast("LLMProvider", _SecretBoomProvider()), tasks)
    for _ in range(10):
        await asyncio.sleep(0)
    assert not tasks
    assert "SUPER_SECRET_API_KEY" not in caplog.text


#: Model-facing prompt material the composition root must never touch. Wiring
#: a `PromptHarness` is the whole job; reaching past it into the layer text,
#: tier definitions, or the cluster-note formatter would make `__main__.py`
#: a second author of what the model reads — which is exactly the split
#: `prompt_harness.py` exists to hold (issue #316 task 6).
_FORBIDDEN_PROMPT_COMPOSITION = (
    "cluster_context_note",
    "SAFETY_CONTRACT",
    "COMMON_ROLE",
    "BEHAVIOR",
    "PROMPT",
    "TOOL_DESCRIPTIONS",
    "get_behavior",
    "tier_prompt",
    "extra_layers",
    "ComposedPrompt",
    "PromptInputs",
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


def test_composition_support_defines_the_ca_validator_once() -> None:
    """Merge conflict cleanup must not silently shadow a hardened helper."""
    path = Path(korvid.__main__.__file__).with_name("composition_support.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
    definitions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "_validate_ca_bundle"
    ]

    assert len(definitions) == 1


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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from korvid.agent.tiers.low import TOOL_DESCRIPTIONS as descriptions",
            {"TOOL_DESCRIPTIONS"},
        ),
        ("import korvid.agent.tiers.high as high; text = high.PROMPT", {"PROMPT"}),
        ("# BEHAVIOR\nextra_layers_note = 'tier_prompt TOOL_DESCRIPTIONS'", set()),
    ],
    ids=["aliased-descriptions", "qualified-prompt", "unrelated-text"],
)
def test_prompt_composition_guard_recognizes_tier_symbols(source: str, expected: set[str]) -> None:
    referenced = _referenced_names(ast.parse(source))

    assert referenced.intersection(_FORBIDDEN_PROMPT_COMPOSITION) == expected


def test_the_composition_root_never_imports_the_prompt_pack_registry() -> None:
    """Layer text reaches the model through the harness or not at all."""
    banned_modules = ("korvid.agent.prompt_packs",)
    for node in ast.walk(_main_tree()):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in banned_modules, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in banned_modules, alias.name


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
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        model_connections=_profiles(_profile()),
    )
    kube_stub = cast("Any", object())
    azure = ClusterFacts(provider="azure", distribution="aks")
    wiring = _build_agent_wiring(config, kube_stub, {}, cluster=azure)
    assert wiring.session is not None
    assert wiring.rebuild is not None

    rebuilt = wiring.rebuild(_profile(), None)
    assert rebuilt is not None
    # A rebuild inherits the cluster the wiring last learned about; what
    # that produces on the wire is pinned by the end-to-end test below.
    assert rebuilt.policy.model == wiring.session.policy.model


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
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        model_connections=_profiles(_profile()),
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
    high = rebuild(_profile(), "high")
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
        model_connections=_profiles(_profile()),
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
        model_connections=_profiles(_profile()),
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
    from korvid.core.config import KorvidConfig

    config = KorvidConfig(
        agent_enabled=True,
        model_connections=_profiles(_profile()),
        agent_model_tier="low",
    )
    kube_stub = cast("Any", object())
    wiring = _build_agent_wiring(config, kube_stub, {}, pod_resize_supported=False)
    session = wiring.session
    assert session is not None
    wiring.retarget(session, True, ClusterFacts(provider="aws", distribution="eks"))

    rebuild = wiring.rebuild
    assert rebuild is not None
    rebuilt = rebuild(_profile(), "low")
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
    assert wiring.available is True
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


def test_a_non_budget_prompt_error_is_not_blamed_on_the_operators_rules() -> None:
    from korvid.__main__ import _warn_agent_disabled
    from korvid.agent.prompt_harness import PromptCompositionError

    warnings: list[str] = []
    _warn_agent_disabled(PromptCompositionError("prompt composition failed"), warnings)

    assert warnings == ["agent disabled: prompt composition failed"]


def test_the_prompt_budget_hint_is_fixed_text() -> None:
    from korvid.__main__ import _PROMPT_DEGRADE_HINT, _warn_agent_disabled
    from korvid.agent.prompt_harness import StaticPromptTooLargeError

    over_budget: list[str] = []
    _warn_agent_disabled(StaticPromptTooLargeError("static system prompt too large"), over_budget)

    assert over_budget == [
        f"agent disabled: static system prompt too large — {_PROMPT_DEGRADE_HINT}"
    ]


def test_a_model_that_cannot_call_tools_still_gets_no_prompt_advice() -> None:
    """Model incompatibility must not be blamed on the prompt budget."""
    from korvid.__main__ import _PROMPT_DEGRADE_HINT, _warn_agent_disabled
    from korvid.agent.model_policy import ModelRoutingError

    warnings: list[str] = []
    _warn_agent_disabled(ModelRoutingError("model reports no tool support"), warnings)

    assert len(warnings) == 1
    assert _PROMPT_DEGRADE_HINT not in warnings[0]


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
        rebuild(_profile(), "low")

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


def test_startup_preserves_qualified_custom_column_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import korvid.__main__ as main_mod
    from korvid.__main__ import _custom_column_names, _load_startup_config
    from korvid.core.config import KorvidConfig, ViewConfig
    from korvid.k8s.columns import CustomColumn

    key = "helmreleases.helm.toolkit.fluxcd.io"
    view = ViewConfig(columns=(CustomColumn("TEAM", "label", "team"),))
    config = KorvidConfig(namespace="default", views={key: view})
    monkeypatch.setattr(main_mod, "load_config", lambda: config)
    monkeypatch.setattr(main_mod, "resolve_context_name", lambda name: name)
    monkeypatch.setattr(main_mod, "resolve_context_namespace", lambda name: None)

    loaded = _load_startup_config(False)

    assert loaded.views == {key: view}
    assert _custom_column_names(loaded) == {key: ("TEAM",)}


def test_load_startup_config_wraps_config_error_as_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported config key must fail startup with
    one clear, actionable line — not an unfiltered traceback."""
    import korvid.__main__ as main_mod
    from korvid.core.config import ConfigError

    def _raise() -> Any:
        raise ConfigError("unsupported agent key: 'profile'")

    monkeypatch.setattr(main_mod, "load_config", _raise)
    with pytest.raises(SystemExit) as exc_info:
        main_mod._load_startup_config(False)
    message = str(exc_info.value)
    assert "\n" not in message  # one-line, actionable
    assert message == "korvid: unsupported agent key: 'profile'"


def test_the_profile_writer_updates_only_the_active_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The composition root's writer edits one profile and leaves the file
    otherwise intact — unrelated keys, sibling profiles, and the raw block
    of a profile korvid rejected, which is the operator's only copy of the
    thing they have to fix."""
    import korvid.__main__ as main_mod
    from korvid.__main__ import _profile_writer
    from korvid.core.config import load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
kube_context: prod
ui:
  topbar: expanded
agent:
  active: production
  profiles:
    default:
      model: openai/gpt-3.5
      endpoint: https://old.default.example
      auth:
        method: environment
        key: OLD_KEY
      options:
        temperature: 0.1
    production:
      model: azure/gpt-4o
      endpoint: https://prod.example
      auth:
        method: provider-default
      options:
        azure_deployment: prod
    rejected:
      model: openai/gpt-4o
      options:
        api_key: inline-secret
    bad name:
      model: openai/gpt-4o
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(main_mod, "DEFAULT_CONFIG_PATH", config_path)
    loaded = load_config(config_path)

    _profile_writer()(
        ModelConnectionsConfig(
            active="default",
            profiles={
                **loaded.model_connections.profiles,
                "default": _profile("m2"),
            },
            unparsed=loaded.model_connections.unparsed,
        )
    )

    persisted = load_config(config_path)
    active = persisted.model_connections.active_profile
    assert persisted.model_connections.active == "default"
    assert active is not None
    assert active.model == "openai/m2"
    assert active.endpoint == "http://localhost:9999/v1"
    assert active.auth.method == "environment"
    assert active.auth.settings["key"] == "KORVID_TEST_KEY"
    assert persisted.model_connections.profiles["production"].endpoint == "https://prod.example"
    assert persisted.model_connections.profiles["rejected"].config_error is not None
    assert "bad name" in persisted.model_connections.unparsed
    assert "rejected" in persisted.model_connections.unparsed

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw["kube_context"] == "prod"
    assert raw["ui"]["topbar"] == "expanded"
    assert set(raw["agent"]["profiles"]) == {"default", "production", "rejected", "bad name"}
    # The rejected profile's raw block is the operator's only copy of the
    # thing they have to fix; an unrelated write must not strip it.
    assert raw["agent"]["profiles"]["rejected"]["options"] == {"api_key": "inline-secret"}


def test_the_profile_writer_never_writes_a_secret_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A profile stores the *name* of the environment variable, never what
    it holds. The writer reads no secret, so none can reach config.yaml."""
    import korvid.__main__ as main_mod
    from korvid.__main__ import _profile_writer
    from korvid.core.config import load_config

    monkeypatch.setenv("KORVID_TEST_KEY", "sk-secret-value")
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(main_mod, "DEFAULT_CONFIG_PATH", config_path)

    _profile_writer()(_profiles(_profile("m1")))

    active = load_config(config_path).model_connections.active_profile
    assert active is not None
    assert active.auth.settings["key"] == "KORVID_TEST_KEY"
    assert "sk-secret-value" not in config_path.read_text(encoding="utf-8")


def test_load_startup_config_wraps_config_error_unconditionally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unsupported-key check is unconditional: startup must not silently
    ignore a leftover `agent.profile` just because no profile is active."""
    import korvid.__main__ as main_mod
    from korvid.core.config import load_config as real_load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent:\n  active: null\n  profile: full\n")
    monkeypatch.setattr(main_mod, "load_config", lambda: real_load_config(config_path))
    with pytest.raises(SystemExit) as exc_info:
        main_mod._load_startup_config(False)
    message = str(exc_info.value)
    assert "unsupported" in message
    assert "'profile'" in message


def test_cli_namespace_flag_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`korvid -n team-a` and `--namespace team-a` select the startup
    namespace (issue #108)."""
    import korvid.__main__ as main_mod

    calls: list[str | None] = []
    loops: list[asyncio.AbstractEventLoop] = []

    async def fake_run_app(
        readonly: bool = False, mcp: bool = False, namespace: str | None = None
    ) -> None:
        calls.append(namespace)
        loops.append(asyncio.get_running_loop())

    monkeypatch.setattr(main_mod, "_run", fake_run_app)
    monkeypatch.setattr("sys.argv", ["korvid", "-n", "team-a"])
    main_mod.main()
    monkeypatch.setattr("sys.argv", ["korvid", "--namespace", "team-b"])
    main_mod.main()
    monkeypatch.setattr("sys.argv", ["korvid"])
    main_mod.main()
    assert calls == ["team-a", "team-b", None]
    assert len(set(loops)) == 3
    assert all(loop.is_closed() for loop in loops)


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
    rebuild = wiring.rebuild
    retarget = wiring.retarget
    provider_box = wiring.provider_box
    assert session is None
    assert wiring.available is False
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
            KorvidConfig(
                agent_enabled=True,
                model_connections=_profiles(
                    ModelConnectionConfig(model="ollama/m", endpoint="http://x:11434")
                ),
            ),
            cast("KubeClient", object()),
            {},
        )


def test_the_missing_agent_extra_hint_names_a_key_that_still_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint used to point at `agent.provider`, which startup rejects.

    `agent_enabled` is derived from `agent.active` naming a parsed
    profile; the flat `agent.provider` scalar was retired and is migrated
    away on load. An operator told to look at `agent.provider` is sent to
    a key their config.yaml is not supposed to contain.
    """
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.k8s.client import KubeClient

    _uninstall_packages(monkeypatch, *_AGENT_ROOTS)
    with pytest.raises(SystemExit) as excinfo:
        _build_agent_wiring(
            KorvidConfig(
                agent_enabled=True,
                model_connections=_profiles(
                    ModelConnectionConfig(model="ollama/m", endpoint="http://x:11434")
                ),
            ),
            cast("KubeClient", object()),
            {},
        )
    message = str(excinfo.value)
    assert "agent.active" in message
    assert "agent.provider" not in message


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
    rebuild = wiring.rebuild
    provider_box = wiring.provider_box
    assert session is None
    assert wiring.available is False
    assert rebuild is None
    assert provider_box == [None]
    assert "korvid.agent.session" not in sys.modules
    assert "korvid.agent.native_engine" not in sys.modules


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
        model_connections=_profiles(_profile()),
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


# ---------------------------------------------------------------------------
# Third-party extension failures degrade the start, they never stop it
# ---------------------------------------------------------------------------


def _raising_flow_entry_point(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Install a `korvid.provider` entry point whose builder always raises."""
    from korvid.agent.model_profiles import SpecialFlow

    def _build(profile: Any) -> Any:
        raise error

    class _EntryPoint:
        name = "corp-llm"
        group = "korvid.provider"

        def load(self) -> SpecialFlow:
            return SpecialFlow(
                prefix="corp-llm",
                display_name="Corp",
                auth_methods=(),
                build_provider=_build,
            )

    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points", lambda: (_EntryPoint(),)
    )


def test_a_third_party_flow_that_raises_leaves_the_start_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Third-party code runs inside the provider factory. A flow that raises
    must disable the agent, not the TUI: no exception escapes the wiring and
    the wizard stays reachable so the operator can point korvid elsewhere."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    _raising_flow_entry_point(monkeypatch, RuntimeError("plugin factory failed"))
    config = KorvidConfig(
        agent_enabled=True,
        model_connections=_profiles(
            ModelConnectionConfig(model="corp-llm/m", endpoint="http://x/v1")
        ),
    )

    wiring = _build_agent_wiring(config, cast("Any", object()), {})

    assert wiring.session is None  # agent off, not a crash
    assert wiring.provider_box[0] is None
    assert wiring.available is True  # `:ai` must remain usable
    assert wiring.rebuild is not None


def test_a_third_party_flow_failure_never_reaches_the_operator_verbatim(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever the exception carries — a token, a URL with a key in it —
    stays out of what korvid shows. The refusal names the profile and points
    at the log; the plugin's own message is not echoed into the startup
    warnings the TUI renders."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    _raising_flow_entry_point(monkeypatch, RuntimeError("token=PLUGIN_SECRET"))
    config = KorvidConfig(
        agent_enabled=True,
        model_connections=_profiles(
            ModelConnectionConfig(model="corp-llm/m", endpoint="http://x/v1")
        ),
    )
    warnings: list[str] = []

    _build_agent_wiring(config, cast("Any", object()), {}, startup_warnings=warnings)

    assert not any("PLUGIN_SECRET" in warning for warning in warnings)


def test_profile_options_reach_the_provider_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """`agent.options` used to be a separate scalar the composition root
    forwarded. Options travel on the profile now, so the wiring test is that
    the profile the factory is handed still carries them untouched."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    factory = _RecordingFactory()
    monkeypatch.setattr("korvid.providers.litellm_factory.create_provider_from_profile", factory)
    config = KorvidConfig(
        agent_enabled=True,
        model_connections=_profiles(_profile(options={"tenant": "corp", "region": "us"})),
    )

    _build_agent_wiring(config, cast("Any", object()), {})

    assert dict(factory.profiles[0].options) == {"tenant": "corp", "region": "us"}


async def test_the_rebuild_builds_through_the_same_factory_as_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One factory, start and rebuild alike: a second construction path is
    exactly what Task 18 removed, and a rebuild that took one could serve a
    connection the start would have refused."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig

    monkeypatch.setenv("KORVID_TEST_KEY", "k")
    factory = _RecordingFactory()
    monkeypatch.setattr("korvid.providers.litellm_factory.create_provider_from_profile", factory)
    config = KorvidConfig(agent_enabled=True, model_connections=_profiles(_profile()))

    wiring = _build_agent_wiring(config, cast("Any", object()), {})
    rebuild = wiring.rebuild
    assert rebuild is not None
    new_session = rebuild(_profile("new-model"), None)

    assert [p.model for p in factory.profiles] == ["openai/m", "openai/new-model"]
    if new_session is not None:
        await new_session.aclose()


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
    providers = _stub_providers(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
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
        rebuild(_profile("m2"), None)

    assert provider_box[0] is old_provider
    assert session_box[0] is session
    assert cast("Any", old_provider).closed == 0
    assert providers[-1] is not old_provider  # only the replacement is released
    assert cast("Any", session).finalization_pending is False


async def test_a_failed_session_build_during_rebuild_closes_only_the_new_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from korvid.__main__ import _build_agent_wiring

    monkeypatch.setenv("KORVID_TEST_KEY", "fixture-token")
    _stub_providers(monkeypatch)
    wiring = _build_agent_wiring(_agent_config(), cast("Any", object()), {})
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
        rebuild(_profile("m2"), None)

    assert provider_box[0] is old_provider
    assert wiring.session_box[0] is session
    assert cast("Any", old_provider).closed == 0


# ---------------------------------------------------------------------------
# Finding #5: the copilot flow reads its OAuth token through the credential store
# ---------------------------------------------------------------------------


def test_github_copilot_profile_loads_oauth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The composition root hands the factory its `TokenStore`, and the
    `github-copilot` flow reads the OAuth token through it. Without a
    stored token the profile is refused: the agent is off, not crashed."""
    from korvid.__main__ import _build_agent_wiring
    from korvid.core.config import KorvidConfig
    from korvid.providers import token_store as ts_mod
    from korvid.providers.flow_copilot import CREDENTIAL_KEY

    loaded_keys: list[str] = []

    def _tracking_load(self: Any, key: str) -> str | None:
        loaded_keys.append(key)
        return None  # no token stored

    monkeypatch.setattr(ts_mod.TokenStore, "load", _tracking_load)
    config = KorvidConfig(
        agent_enabled=True,
        model_connections=_profiles(
            ModelConnectionConfig(
                model="github-copilot/gpt-4o",
                auth=ConnectionAuthConfig(method="device-login"),
            )
        ),
    )

    wiring = _build_agent_wiring(config, cast("Any", object()), {})

    assert CREDENTIAL_KEY in loaded_keys
    assert wiring.session is None
    assert wiring.provider_box[0] is None


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
    async def get_helm_release_identity(self, namespace: str, name: str) -> Any:
        return namespace, name

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
    monkeypatch.setattr(main_mod, "assemble_app_runtime", lambda app: app)
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


async def test_wire_and_run_wires_helm_release_identity_reader_from_kube(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import korvid.__main__ as main_mod
    from korvid.core.config import KorvidConfig

    monkeypatch.setattr(main_mod, "KorvidApp", _FakeAppCapturesKwargs)
    monkeypatch.setattr(main_mod, "assemble_app_runtime", lambda app: app)
    _FakeAppCapturesKwargs.instances.clear()

    kube = _FakeKubeForWiring()
    state = main_mod._RunState()
    await main_mod._wire_and_run(KorvidConfig(readonly=True), cast("Any", kube), state)
    if state.discovery_box:
        await state.discovery_box[0]

    reader = _FakeAppCapturesKwargs.instances[0].captured["get_helm_release_identity"]
    assert await reader("default", "web") == ("default", "web")


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
    monkeypatch.setattr(main_mod, "assemble_app_runtime", lambda app: app)
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


class _RecordingProvider(LLMProvider):
    """A provider that streams one scripted turn and records its requests."""

    order: ClassVar[list[str]] = []

    def __init__(self, model: str = "m") -> None:
        self._descriptor = ModelDescriptor("test", model)
        self.requests: list[list[dict[str, Any]]] = []
        self.surfaces: list[list[dict[str, Any]]] = []
        self.closed = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities.unknown()

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool = True
    ) -> AsyncIterator[dict[str, Any]]:
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
    """Make every profile the factory is handed yield a recording provider."""
    built: list[_RecordingProvider] = []

    def _create(profile: Any, **kwargs: Any) -> Any:
        provider = _RecordingProvider(split_reference(profile.model)[1])
        built.append(provider)
        return provider

    monkeypatch.setattr("korvid.providers.litellm_factory.create_provider_from_profile", _create)
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
        "model_connections": _profiles(_profile()),
    }
    base.update(overrides)
    return KorvidConfig(**base)


def _profile(model: str = "m", **overrides: Any) -> ModelConnectionConfig:
    """The one connection the wiring tests start from."""
    fields: dict[str, Any] = {
        "model": f"openai/{model}",
        "endpoint": "http://localhost:9999/v1",
        "auth": ConnectionAuthConfig(method="environment", settings={"key": "KORVID_TEST_KEY"}),
    }
    fields.update(overrides)
    return ModelConnectionConfig(**fields)


def _profiles(profile: ModelConnectionConfig, name: str = "default") -> ModelConnectionsConfig:
    return ModelConnectionsConfig(active=name, profiles={name: profile})


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
    new_session = rebuild(_profile("m2"), None)
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

    def _create(profile: Any, **kwargs: Any) -> Any:
        return _ToollessProvider()

    monkeypatch.setattr("korvid.providers.litellm_factory.create_provider_from_profile", _create)
    warnings: list[str] = []
    wiring = _build_agent_wiring(
        _agent_config(), cast("Any", object()), {}, startup_warnings=warnings
    )

    assert wiring.session is None
    assert wiring.session_box == [None]
    assert isinstance(wiring.provider_box[0], _ToollessProvider)
    assert any("tool" in warning for warning in warnings)


# ---------------------------------------------------------------------------
# Task 8 — catalog construction performs no I/O
# ---------------------------------------------------------------------------


def test_building_the_catalog_issues_no_http_request() -> None:
    """_build_model_catalog() must not make any HTTP requests.

    EndpointDiscovery construction must be I/O-free; only list_models
    triggers network access, and that is only called from discover().
    """
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog

    def _fail_if_called() -> None:
        raise AssertionError("HTTP client was constructed during catalog build")

    # Inject a client_factory that fails if called — EndpointDiscovery only
    # calls it inside list_models, so construction must not trigger it.
    import unittest.mock

    with unittest.mock.patch(
        "korvid.providers.endpoint_discovery._default_client_factory",
        side_effect=_fail_if_called,
    ):
        catalog = _build_model_catalog()

    assert catalog is not None


def test_building_the_catalog_opens_no_socket() -> None:
    """_build_model_catalog() must open no socket — same invariant as the
    litellm_offline_import test, now verified at the composition-root level.

    A regression in the wrapper's env-var ordering (LITELLM_LOCAL_MODEL_COST_MAP
    not set before import) would show up here as a real startup stall.
    """
    import subprocess
    import sys
    import textwrap

    pytest.importorskip("litellm")

    probe = textwrap.dedent(
        """
        import socket
        import sys

        attempts = []

        def _refuse(self, address):  # noqa: ANN001, ANN202
            attempts.append(address)
            raise OSError("network disabled for this probe")

        socket.socket.connect = _refuse
        socket.socket.connect_ex = lambda self, address: (attempts.append(address), 1)[1]

        from korvid.__main__ import _build_model_catalog

        catalog = _build_model_catalog()
        assert catalog is not None, "catalog was None"

        print(len(attempts))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0", result.stdout


# ---------------------------------------------------------------------------
# Task 11 review — the production catalog can actually probe a profile
# ---------------------------------------------------------------------------


class _RecordingFactory:
    """Stands in for the shared provider factory, recording what it built."""

    def __init__(self, reply: str = "ok", refuse: bool = False) -> None:
        self.profiles: list[ModelConnectionConfig] = []
        self.kwargs: list[dict[str, Any]] = []
        self._reply = reply
        self._refuse = refuse

    def __call__(self, profile: ModelConnectionConfig, **kwargs: Any) -> Any:
        self.profiles.append(profile)
        self.kwargs.append(kwargs)
        if self._refuse:
            return None
        return _ProbeProvider(profile, self._reply)


class _ProbeProvider:
    """The minimum surface `ProfileProbe` drives: descriptor, stream, close."""

    def __init__(self, profile: ModelConnectionConfig, reply: str) -> None:
        self._reply = reply
        self.closed = 0
        self.descriptor = ModelDescriptor("stub", split_reference(profile.model)[1])
        self.capabilities = ModelCapabilities.unknown()

    def complete(self, messages: Any, tools: Any, *, stream: bool = True) -> Any:
        async def gen() -> Any:
            yield {"type": "text_delta", "text": self._reply}

        return gen()

    async def aclose(self) -> None:
        self.closed += 1


def _azure_profile() -> ModelConnectionConfig:
    return ModelConnectionConfig(
        model="azure/gpt-4o",
        endpoint="https://x.openai.azure.com",
        auth=ConnectionAuthConfig(method="environment", settings={"key": "AZURE_OPENAI_API_KEY"}),
        options={"azure_deployment": "my-dep"},
    )


async def test_the_production_catalog_probes_a_profile_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wizard's last stage calls `catalog.test()`. A stub that raises
    `NotImplementedError` there makes every real first run end in failure."""
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog

    factory = _RecordingFactory(reply="connected")
    monkeypatch.setattr("korvid.providers.profile_probe.create_provider_from_profile", factory)
    catalog = _build_model_catalog()
    assert catalog is not None

    result = await catalog.test(ModelConnectionConfig(model="openai/gpt-4o"))

    assert result == "connected"
    assert [p.model for p in factory.profiles] == ["openai/gpt-4o"]


async def test_the_production_catalog_probes_a_vendor_prefix_the_transport_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing owns the vendor now. A prefix the interim transport had to
    refuse is an ordinary profile here — the probe builds it through the
    same factory as any other, with no vendor arm in between."""
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog

    factory = _RecordingFactory()
    monkeypatch.setattr("korvid.providers.profile_probe.create_provider_from_profile", factory)
    catalog = _build_model_catalog()
    assert catalog is not None

    assert await catalog.test(ModelConnectionConfig(model="anthropic/claude-sonnet-4-5")) == "ok"
    assert [p.model for p in factory.profiles] == ["anthropic/claude-sonnet-4-5"]


async def test_the_production_catalog_probes_the_profile_startup_would_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The probe must reach the host the runtime would. Both sides take the
    profile itself now, so the guarantee is that the object the wizard
    probes is the object startup would hand the factory — no projection in
    between that could disagree about the deployment path."""
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog, _create_initial_provider
    from korvid.core.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(
        "agent:\n"
        "  active: main\n"
        "  profiles:\n"
        "    main:\n"
        "      model: azure/gpt-4o\n"
        "      endpoint: https://x.openai.azure.com\n"
        "      auth:\n"
        "        method: environment\n"
        "        key: AZURE_OPENAI_API_KEY\n"
        "      options:\n"
        "        azure_deployment: my-dep\n",
        encoding="utf-8",
    )
    startup = load_config(path)

    factory = _RecordingFactory()
    monkeypatch.setattr("korvid.providers.profile_probe.create_provider_from_profile", factory)
    monkeypatch.setattr("korvid.providers.litellm_factory.create_provider_from_profile", factory)
    catalog = _build_model_catalog()
    assert catalog is not None

    await catalog.test(_azure_profile())
    _create_initial_provider(startup)

    probed, started = factory.profiles
    assert probed == started
    assert started.endpoint == "https://x.openai.azure.com"
    assert dict(started.options) == {"azure_deployment": "my-dep"}
    assert started.auth.settings["key"] == "AZURE_OPENAI_API_KEY"


async def test_a_profile_the_factory_refuses_reports_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal is the wizard's answer, not a crash: the operator is told
    the connection could not be built and where the reason is."""
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog
    from korvid.providers.profile_probe import ProbeFailed

    factory = _RecordingFactory(refuse=True)
    monkeypatch.setattr("korvid.providers.profile_probe.create_provider_from_profile", factory)
    catalog = _build_model_catalog()
    assert catalog is not None

    with pytest.raises(ProbeFailed, match="provider could not be created") as raised:
        await catalog.test(ModelConnectionConfig(model="openai/gpt-4o"))

    # The wizard renders this text, so it has to be a declared-safe one.
    assert raised.value.operator_message() == str(raised.value)


async def test_the_production_catalog_refuses_an_answer_past_the_probes_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`catalog.test()` is the wizard's whole verdict on a profile.

    Truncating an over-long answer and returning it made that verdict a
    lie for the case the bound exists for: the read stopped before the
    adapter could say whether the stream ever finished, so a provider that
    streamed 4 KiB and then died passed (issue #336 review). The catalog
    must raise instead, with a message it declared safe to render.
    """
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog
    from korvid.agent.provider import STREAM_LIMIT, ProviderStreamLimitError
    from korvid.providers.profile_probe import PROBE_MAX_RESPONSE_CHARS

    factory = _RecordingFactory(reply="z" * (PROBE_MAX_RESPONSE_CHARS + 1))
    monkeypatch.setattr("korvid.providers.profile_probe.create_provider_from_profile", factory)
    catalog = _build_model_catalog()
    assert catalog is not None

    with pytest.raises(ProviderStreamLimitError, match="grew past") as raised:
        await catalog.test(ModelConnectionConfig(model="openai/gpt-4o"))

    assert raised.value.operator_message() == STREAM_LIMIT


def test_the_app_is_wired_with_a_catalog_that_can_probe() -> None:
    """The composition root builds the catalog with the configured trust —
    without it the wizard's probe and the runtime could disagree on the CA."""
    source = Path("src/korvid/__main__.py").read_text(encoding="utf-8")
    assert "ca_bundle=config.network_ca_bundle" in source


def _profiles_config(path: Path, *, tier: str | None = None) -> None:
    tier_line = f"  model_tier: {tier}\n" if tier is not None else ""
    path.write_text(
        f"agent:\n{tier_line}  active: main\n  profiles:\n    main:\n      model: openai/gpt-4o\n",
        encoding="utf-8",
    )


def test_the_profile_writer_keeps_a_tier_it_was_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The composition root's writer is the seam the UI persists through.
    A save that never asked about the tier must not drop the override."""
    from korvid.__main__ import _profile_writer
    from korvid.core.config import load_config

    path = tmp_path / "config.yaml"
    _profiles_config(path, tier="high")
    monkeypatch.setattr(korvid.__main__, "DEFAULT_CONFIG_PATH", path)

    _profile_writer()(load_config(path).model_connections)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["model_tier"] == "high"


def test_the_profile_writer_persists_a_first_run_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the first-run wizard's answer lands in the same write as the
    profile it was chosen for — including Automatic, which clears it."""
    from korvid.__main__ import _profile_writer
    from korvid.core.config import load_config

    path = tmp_path / "config.yaml"
    _profiles_config(path)
    monkeypatch.setattr(korvid.__main__, "DEFAULT_CONFIG_PATH", path)
    profiles = load_config(path).model_connections

    _profile_writer()(profiles, model_tier="low")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["agent"]["model_tier"] == "low"
    assert raw["agent"]["active"] == "main"

    _profile_writer()(profiles, model_tier=None)
    assert "model_tier" not in yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]


async def test_wire_and_run_hands_the_ui_a_declared_profile_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The screens are injected with a `ModelConnectionsWriter`, not a callable.

    The seam crosses core → UI, so it is nominal (AGENTS.md): what the app
    receives declares itself an implementation of the interface `core`
    owns, and it is already bound to the one path the composition root
    chose — the UI never sees a location it could write to.
    """
    import korvid.__main__ as main_mod
    from korvid.core.config import KorvidConfig, ModelConnectionsWriter, load_config

    path = tmp_path / "config.yaml"
    _profiles_config(path, tier="high")
    monkeypatch.setattr(main_mod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(main_mod, "KorvidApp", _FakeAppCapturesKwargs)
    monkeypatch.setattr(main_mod, "assemble_app_runtime", lambda app: app)
    _FakeAppCapturesKwargs.instances.clear()

    kube = _FakeKubeForWiring()
    state = main_mod._RunState()
    await main_mod._wire_and_run(KorvidConfig(readonly=True), cast("Any", kube), state)
    if state.discovery_box:
        await state.discovery_box[0]

    writer = _FakeAppCapturesKwargs.instances[0].captured["agent_save_profiles"]
    assert isinstance(writer, ModelConnectionsWriter)

    # And it writes where the composition root said, with the tier
    # sentinel intact.
    writer(load_config(path).model_connections)
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["agent"]["model_tier"] == "high"


def _profile_connections(reference: str, **overrides: Any) -> Any:
    """One active profile, built the way the config loader builds it."""
    from korvid.core.config import (
        ConnectionAuthConfig,
        ModelConnectionConfig,
        ModelConnectionsConfig,
    )

    profile = ModelConnectionConfig(
        model=reference,
        auth=overrides.pop("auth", ConnectionAuthConfig(method="provider-default")),
        **overrides,
    )
    return ModelConnectionsConfig(active="main", profiles={"main": profile})


def test_an_active_profile_is_built_by_the_profile_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task 15's point, now the only path: no scalar projection on the way
    in. The profile reaches the factory as the operator wrote it, so a
    connection the removed transport could not express is built directly.
    """
    from korvid.__main__ import _create_initial_provider
    from korvid.core.config import KorvidConfig

    seen: list[Any] = []

    def _from_profile(profile: Any, **kwargs: Any) -> str:
        seen.append((profile, kwargs))
        return "built-from-profile"

    monkeypatch.setattr(
        "korvid.providers.litellm_factory.create_provider_from_profile", _from_profile
    )

    config = KorvidConfig(model_connections=_profile_connections("anthropic/claude-sonnet-4-5"))
    built = cast("Any", _create_initial_provider(config))

    assert built == "built-from-profile"
    assert seen[0][0].model == "anthropic/claude-sonnet-4-5"


class _StubCredentialStore:
    """The whole `CredentialStore` protocol: one `load`, and nothing else.

    Never consulted here — the factory is monkeypatched — but it is the
    declared parameter type, so the wiring is exercised with a value the
    real factory would accept rather than a bare `object`.
    """

    def load(self, key: str) -> str | None:
        return None


def test_the_profile_factory_is_given_the_credential_store_and_a_shared_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keyring auth is unresolvable without a store, and the catalog and
    the factory have to agree on which flows exist."""
    from korvid.__main__ import _create_initial_provider
    from korvid.core.config import KorvidConfig

    captured: dict[str, Any] = {}

    def _from_profile(profile: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "built"

    monkeypatch.setattr(
        "korvid.providers.litellm_factory.create_provider_from_profile", _from_profile
    )
    store = _StubCredentialStore()
    config = KorvidConfig(model_connections=_profile_connections("openai/gpt-4o"))

    built = cast("Any", _create_initial_provider(config, store))
    assert built == "built"
    assert captured["credentials"] is store
    assert captured["flows"] is not None
    assert captured["catalog"] is not None


def test_a_config_with_no_profiles_builds_no_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is one factory now. A config with no active profile has the
    agent off — a `None` provider, not a second construction path that
    could build a connection the profile factory would have refused."""
    from korvid.__main__ import _create_initial_provider
    from korvid.core.config import KorvidConfig

    def _must_not_run(profile: Any, **kwargs: Any) -> None:
        raise AssertionError("no profile exists to build from")

    monkeypatch.setattr(
        "korvid.providers.litellm_factory.create_provider_from_profile", _must_not_run
    )

    assert _create_initial_provider(KorvidConfig(agent_enabled=True)) is None


def test_the_profile_factory_is_given_the_configured_trust_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`network.ca_bundle` is one trust decision for every korvid-owned
    HTTPS client. The profile factory has to take it, or a corporate
    endpoint stops verifying against the CA the operator named."""
    from korvid.__main__ import _create_initial_provider
    from korvid.core.config import KorvidConfig

    captured: dict[str, Any] = {}

    def _from_profile(profile: Any, **kwargs: Any) -> str:
        captured.update(kwargs)
        return "built"

    monkeypatch.setattr(
        "korvid.providers.litellm_factory.create_provider_from_profile", _from_profile
    )
    config = KorvidConfig(
        model_connections=_profile_connections("openai/gpt-4o"),
        network_ca_bundle="/etc/korvid/corporate-root.pem",
    )

    assert cast("Any", _create_initial_provider(config)) == "built"
    assert captured["ca_bundle"] == "/etc/korvid/corporate-root.pem"


def test_an_installed_flow_entry_point_builds_the_provider_instead_of_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry the composition root builds has to be the same kind
    the catalog and the wizard build.

    An empty `SpecialFlowRegistry()` claims the device-login prefixes and
    nothing else, so an installed flow is never asked and its references
    are refused instead of delegated. Here a real entry point is
    installed: the flow's own `build_provider` must be what answers, and
    LiteLLM routing must never see the reference.
    """
    from korvid.__main__ import _create_initial_provider
    from korvid.agent.model_profiles import SpecialFlow
    from korvid.core.config import KorvidConfig

    built: list[Any] = []

    class _FlowProvider(LLMProvider):
        @property
        def descriptor(self) -> ModelDescriptor:
            return ModelDescriptor(provider="acme", model="internal")

        @property
        def capabilities(self) -> ModelCapabilities:
            return ModelCapabilities.unknown()

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            yield {"type": "done"}

    def _build(profile: Any) -> LLMProvider:
        built.append(profile)
        return _FlowProvider()

    flow = SpecialFlow(
        prefix="acme",
        display_name="Acme",
        auth_methods=(),
        build_provider=_build,
    )

    class _EntryPoint:
        name = "acme"
        group = "korvid.provider"

        def load(self) -> SpecialFlow:
            return flow

    routed: list[str] = []

    def _record(model: str, **kwargs: object) -> tuple[str, str, None, None]:
        # Recorded rather than raised: the factory refuses on any routing
        # exception, so an AssertionError here would be swallowed into a
        # plain "cannot resolve" refusal and prove nothing.
        routed.append(model)
        return ("internal-v2", "acme", None, None)

    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points", lambda: (_EntryPoint(),)
    )
    monkeypatch.setattr("korvid.providers.litellm_runtime.get_llm_provider", _record)

    config = KorvidConfig(model_connections=_profile_connections("acme/internal-v2"))
    provider = _create_initial_provider(config)

    assert isinstance(provider, _FlowProvider)
    assert [profile.model for profile in built] == ["acme/internal-v2"]
    assert routed == [], "a claimed reference must never reach litellm routing"


def test_an_installed_credential_entry_point_resolves_provider_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composition root has to hand the factory a *discovered* registry.

    `provider_defaults=None` behaves as an empty registry, which is the
    same shape as a working one and therefore fails silently: the profile
    still builds, `provider-default` still omits the api key, and the
    request goes out with whatever ambient credential the SDK finds — or
    with none. A declared chain that is installed but never consulted is
    the exact failure this wiring exists to prevent.
    """
    from korvid.__main__ import _create_initial_provider
    from korvid.core.config import ConnectionAuthConfig, KorvidConfig
    from korvid.providers.litellm_provider import LiteLLMProvider
    from korvid.providers.provider_default import ProviderDefaultCredential, ResolvedCredential

    closed: list[int] = []

    async def _aclose() -> None:
        closed.append(1)

    async def _token() -> str:
        return "tok"

    declaration = ProviderDefaultCredential(
        prefix="acme",
        display_name="Acme identity",
        resolve=lambda: ResolvedCredential(
            parameters={"acme_token_provider": _token}, aclose=_aclose
        ),
    )

    class _Module:
        @staticmethod
        def korvid_provider_default_credentials() -> tuple[ProviderDefaultCredential, ...]:
            return (declaration,)

    class _EntryPoint:
        name = "acme"
        group = "korvid.credential"
        dist = None

        def load(self) -> type[_Module]:
            return _Module

    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points", lambda: (_EntryPoint(),)
    )
    monkeypatch.setattr(
        "korvid.providers.litellm_runtime.get_llm_provider",
        lambda model, **kwargs: ("internal-v2", "acme", None, None),
    )
    monkeypatch.setattr(
        "korvid.providers.litellm_runtime.requires_explicit_api_base",
        lambda *args, **kwargs: False,
    )

    config = KorvidConfig(
        model_connections=_profile_connections(
            "acme/internal-v2", auth=ConnectionAuthConfig(method="provider-default")
        )
    )
    provider = _create_initial_provider(config)

    assert isinstance(provider, LiteLLMProvider)
    kwargs = provider._plan.call_kwargs([], [], stream=True)
    assert kwargs["acme_token_provider"] is _token
    assert "api_key" not in kwargs


# ---------------------------------------------------------------------------
# models.dev is wired to one explicit action, and to a permanent kill switch
# ---------------------------------------------------------------------------


def test_the_catalog_is_built_with_the_configured_trust_and_kill_switch() -> None:
    """The composition root passes both decisions it owns: the CA bundle and
    whether the optional metadata source exists at all."""
    source = Path("src/korvid/__main__.py").read_text(encoding="utf-8")
    assert "_build_model_catalog(" in source
    assert "ca_bundle=config.network_ca_bundle" in source
    assert "models_dev=config.agent_model_search_models_dev" in source


def test_building_the_catalog_never_refreshes_the_metadata_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Never at startup" is enforced here, where startup happens.

    Constructing the source reads the on-disk cache; only the setup UI's
    explicit action may revalidate it over the network.
    """
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog
    from korvid.providers.models_dev import ModelsDevSource

    async def _refuse(self: ModelsDevSource) -> None:
        raise AssertionError("refresh must never run during startup wiring")

    monkeypatch.setattr(ModelsDevSource, "refresh", _refuse)

    catalog = _build_model_catalog()

    assert catalog is not None


async def test_the_kill_switch_constructs_no_metadata_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`agent.model_search.models_dev: false` must leave nothing that *could*
    reach the network — not a source that is merely never called, and not a
    client waiting for someone to hand it a URL."""
    pytest.importorskip("litellm")
    import korvid.providers.models_dev as models_dev_module
    from korvid.__main__ import _build_model_catalog
    from korvid.agent.model_profiles import MetadataRefresh
    from korvid.providers import net

    def _refuse(**kwargs: Any) -> None:
        raise AssertionError("ModelsDevSource must not be constructed when disabled")

    def _refuse_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no HTTPS client may be built when models.dev is disabled")

    monkeypatch.setattr(models_dev_module, "ModelsDevSource", _refuse)
    monkeypatch.setattr(net, "make_client", _refuse_client)

    catalog = _build_model_catalog(models_dev=False, ca_bundle="/etc/pki/corp-root.pem")

    assert catalog is not None
    assert await catalog.refresh_metadata() is MetadataRefresh.DISABLED
    assert await catalog.refresh_metadata(force=True) is MetadataRefresh.DISABLED


async def test_the_default_wiring_can_actually_refresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gap this closes: a source was constructed and nothing could call
    it. With the switch on, the action reaches the source."""
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog
    from korvid.agent.model_profiles import MetadataRefresh
    from korvid.providers.models_dev import ModelsDevSource, RefreshOutcome

    calls: list[bool] = []

    async def _record(self: ModelsDevSource, *, force: bool = False) -> RefreshOutcome:
        calls.append(force)
        return RefreshOutcome.NOT_MODIFIED

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setattr(ModelsDevSource, "refresh", _record)

    catalog = _build_model_catalog()
    assert catalog is not None
    assert calls == []

    assert await catalog.refresh_metadata(force=True) is MetadataRefresh.UNCHANGED
    assert calls == [True]


async def test_the_wired_source_trusts_the_configured_bundle(tmp_path: Path) -> None:
    """`network.ca_bundle` has to reach *this* client, at the composition root.

    models.dev is korvid's own outbound HTTPS call, so it goes through the
    same `net.make_client` as the live providers and the wizard's probe.
    Before this, a deployment behind a TLS-inspecting proxy got
    "unavailable" from the refresh key forever, while every other korvid
    client worked — with nothing in the UI to explain the difference.
    """
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog
    from korvid.providers.net import _CANamedClient
    from tests.providers.tls_ca import mint_ca_and_server_cert

    ca_pem, _, _ = mint_ca_and_server_cert(tmp_path)

    catalog = _build_model_catalog(ca_bundle=str(ca_pem))
    assert catalog is not None

    source = catalog._enrichment  # type: ignore[attr-defined]  # the wired catalog is the concrete one
    assert source is not None
    client = source._client_factory()
    try:
        assert isinstance(client, _CANamedClient)
        assert client._ca_bundle_path == str(ca_pem)
    finally:
        await client.aclose()


def test_building_the_catalog_constructs_no_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Never at startup` covers the client, not just the request.

    The trust configuration is *held* by the source and spent when an
    operator asks for a refresh. Building a client during wiring would
    read the CA bundle off disk on every start — and turn a misconfigured
    `network.ca_bundle` into a failure to launch the TUI at all.
    """
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog
    from korvid.providers import net

    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("no HTTPS client may be constructed during startup wiring")

    monkeypatch.setattr(net, "make_client", _refuse)

    assert _build_model_catalog(ca_bundle="/etc/pki/corp-root.pem") is not None


def test_the_wired_source_reads_no_cache_outside_the_test_sandbox(tmp_path: Path) -> None:
    """These tests must never touch the operator's own metadata cache.

    Constructing the source *reads* a cache file, so without the autouse
    redirect above this module would read — and a refresh would write —
    `~/Library/Caches/korvid/models-dev.json` on the machine running it.
    That makes the suite depend on state no test created, and leaves state
    behind for the next one. Pinned here so removing the fixture fails a
    test rather than quietly reaching into a home directory.
    """
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog

    catalog = _build_model_catalog()
    assert catalog is not None

    source = catalog._enrichment  # type: ignore[attr-defined]  # the wired catalog is the concrete one
    assert source is not None
    cache_path = source._cache_path
    assert (tmp_path / "cache") in cache_path.parents


async def test_the_wired_discovery_trusts_the_configured_bundle(tmp_path: Path) -> None:
    """Setup discovery has to reach the same endpoint the runtime will.

    A profile pointing at an internal endpoint behind a TLS-inspecting
    proxy tested green (the probe honours `network.ca_bundle`) and then
    listed no models at all, because discovery built a bare
    `httpx.AsyncClient` on default trust. One bundle, every korvid-owned
    HTTPS client, this one included.
    """
    pytest.importorskip("litellm")
    from korvid.__main__ import _build_model_catalog
    from korvid.providers.net import _CANamedClient
    from tests.providers.tls_ca import mint_ca_and_server_cert

    ca_pem, _, _ = mint_ca_and_server_cert(tmp_path)

    catalog = _build_model_catalog(ca_bundle=str(ca_pem))
    assert catalog is not None

    discovery = catalog._discovery  # type: ignore[attr-defined]  # the wired catalog is the concrete one
    assert discovery is not None
    client = discovery._client_factory()
    try:
        assert isinstance(client, _CANamedClient)
        assert client._ca_bundle_path == str(ca_pem)
    finally:
        await client.aclose()
