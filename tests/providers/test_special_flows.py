from __future__ import annotations

import pytest

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    SetupField,
    SetupFieldKind,
    SpecialFlow,
)
from korvid.providers.special_flows import SpecialFlowRegistry


def _flow(prefix: str, **kwargs: object) -> SpecialFlow:
    return SpecialFlow(
        prefix=prefix,
        display_name=prefix,
        auth_methods=(AuthMethodDescriptor(id="none", display_name="None"),),
        **kwargs,  # type: ignore[arg-type]  # test builder, exercised by mypy on the real call sites
    )


def test_an_empty_registry_is_fully_functional() -> None:
    """No flow declared is the normal case, not a degraded one."""
    registry = SpecialFlowRegistry()
    assert registry.claim("openai/gpt-4o") is None
    assert registry.errors == ()


def test_a_flow_claims_only_its_own_prefix() -> None:
    registry = SpecialFlowRegistry([_flow("github-copilot")])
    assert registry.claim("github-copilot/gpt-4o") is not None
    assert registry.claim("github-copilot-extra/gpt-4o") is None
    assert registry.claim("openai/gpt-4o") is None
    assert registry.claim("gpt-4o") is None


def test_the_first_claim_of_a_prefix_wins_and_the_second_is_reported() -> None:
    registry = SpecialFlowRegistry([_flow("dup"), _flow("dup")])
    assert registry.claim("dup/x") is registry.claim("dup/x")
    assert any("dup" in message for message in registry.errors)


@pytest.mark.parametrize("prefix", ["", "  ", "a/b", "UPPER", "with space", "sla\\sh"])
def test_a_malformed_prefix_is_refused(prefix: str) -> None:
    registry = SpecialFlowRegistry([_flow(prefix)])
    assert registry.claim(f"{prefix}/x") is None
    assert registry.errors


@pytest.mark.parametrize("prefix", ["openai-compat", "vllm", "github", "claude"])
def test_a_retired_builtin_alias_cannot_be_claimed(prefix: str) -> None:
    """Deleting the built-ins must not free the names for a third party
    to squat on: an operator still reads them as korvid's own."""
    registry = SpecialFlowRegistry([_flow(prefix)])
    assert registry.claim(f"{prefix}/x") is None
    assert registry.errors


def test_a_flow_may_claim_a_named_option_instead_of_a_prefix() -> None:
    flow = _flow(
        "ollama",
        claims_option="native_thinking",
        option_fields=(
            SetupField(key="native_thinking", label="Native thinking", kind=SetupFieldKind.BOOLEAN),
        ),
    )
    registry = SpecialFlowRegistry([flow])
    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": True}) is flow
    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": False}) is None
    assert registry.claim_by_option("ollama/qwen3:8b", {}) is None
    assert registry.claim_by_option("openai/gpt-4o", {"native_thinking": True}) is None


def test_a_broken_declaration_disables_only_itself() -> None:
    class Exploding:
        @property
        def prefix(self) -> str:
            raise RuntimeError("boom")

    registry = SpecialFlowRegistry([Exploding(), _flow("good")])  # type: ignore[list-item]  # deliberately invalid
    assert registry.claim("good/x") is not None
    assert registry.errors


def test_the_registry_is_not_a_provider_list() -> None:
    """No enumeration API: nothing may iterate flows to render a vendor
    picker, which is the shape this design removes."""
    public = {name for name in vars(SpecialFlowRegistry) if not name.startswith("_")}
    assert public == {
        "claim",
        "claim_by_option",
        "claimed_prefixes",
        "errors",
        "from_entry_points",
    }


@pytest.mark.parametrize(
    "reference",
    ["github-copilot/gpt-4o", "github_copilot/gpt-4o", "GitHub-Copilot/gpt-4o"],
)
def test_a_claim_folds_underscores_hyphens_and_case(reference: str) -> None:
    """LiteLLM's own tables publish `github_copilot/...`. If that spelling
    does not fold onto korvid's `github-copilot/` claim, it is unclaimed,
    reaches `get_llm_provider`, and starts an interactive device login."""
    flow = _flow("github-copilot")
    registry = SpecialFlowRegistry([flow])
    assert registry.claim(reference) is flow


def test_two_flows_differing_only_by_separator_collide() -> None:
    registry = SpecialFlowRegistry([_flow("github-copilot"), _flow("github_copilot")])
    assert registry.claim("github_copilot/x") is registry.claim("github-copilot/x")
    assert any("github-copilot" in message for message in registry.errors)


def test_claimed_prefixes_are_known_without_loading_anything() -> None:
    """The factory has to refuse a claimed reference *before* it routes,
    and it must be able to do that without importing plugin code."""
    registry = SpecialFlowRegistry([_flow("github-copilot")])
    assert "github-copilot" in registry.claimed_prefixes
    assert "openai-compat" in registry.claimed_prefixes


def test_only_the_resolved_entry_point_is_ever_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading every declared entry point at construction would execute
    arbitrary third-party module-level code on every korvid startup, and
    one broken plugin would break TUI wiring. `plugin_registry.py`
    already loads only the selected entry point; this must not be weaker.
    """
    loaded: list[str] = []

    class _FakeEntryPoint:
        def __init__(self, name: str, flow: SpecialFlow | None) -> None:
            self.name = name
            self.group = "korvid.provider"
            self._flow = flow

        def load(self) -> SpecialFlow:
            loaded.append(self.name)
            if self._flow is None:
                raise AssertionError(f"{self.name} must not be loaded")
            return self._flow

    wanted = _flow("wanted")
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (
            _FakeEntryPoint("wanted", wanted),
            _FakeEntryPoint("landmine", None),
        ),
    )

    registry = SpecialFlowRegistry.from_entry_points()
    assert loaded == [], "construction must load nothing"

    assert registry.claim("wanted/x") is wanted
    assert loaded == ["wanted"]
    assert registry.claim("unrelated/x") is None
    assert loaded == ["wanted"]


def test_entry_point_cannot_shadow_a_reserved_litellm_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []

    class _FakeEntryPoint:
        name = "openai"
        group = "korvid.provider"

        def load(self) -> SpecialFlow:
            loaded.append(self.name)
            return _flow(self.name)

    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_FakeEntryPoint(),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"openai"})

    assert registry.claim("openai/gpt-4o") is None
    assert loaded == []
    assert any("openai" in message and "reserved" in message for message in registry.errors)


def test_entry_point_flow_prefix_must_match_its_registered_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MismatchedEntryPoint:
        name = "harmless"
        group = "korvid.provider"

        def load(self) -> SpecialFlow:
            return _flow("openai")

    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_MismatchedEntryPoint(),),
    )

    registry = SpecialFlowRegistry.from_entry_points()

    assert registry.claim("harmless/x") is None
    assert registry.claim("openai/gpt-4o") is None
    assert any("harmless" in message and "openai" in message for message in registry.errors)


class _DistEntryPoint:
    """An entry point that knows which distribution declared it."""

    def __init__(self, name: str, flow: SpecialFlow, distribution: str | None) -> None:
        self.name = name
        self.group = "korvid.provider"
        self._flow = flow
        self.dist = None if distribution is None else _Distribution(distribution)

    def load(self) -> SpecialFlow:
        return self._flow


class _Distribution:
    def __init__(self, name: str) -> None:
        self.name = name


def test_korvids_own_flow_may_claim_a_prefix_litellm_also_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reservation protects routing from third parties, not korvid
    from itself. `github_copilot/...` resolves through an interactive
    device login inside `get_llm_provider`, so taking that prefix away
    from routing is the entire purpose of the flow korvid ships."""
    flow = _flow("github-copilot")
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_DistEntryPoint("github-copilot", flow, "korvid"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"github_copilot"})

    assert registry.claim("github_copilot/gpt-4o") is flow
    assert registry.claim("github-copilot/gpt-4o") is flow


def test_a_third_party_distribution_still_cannot_take_a_reserved_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption is distribution identity, not the flow's own say-so:
    a plugin that named itself korvid's flow would otherwise be able to
    intercept every reference for a provider LiteLLM routes."""
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_DistEntryPoint("openai", _flow("openai"), "acme-korvid-plugin"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"openai"})

    assert registry.claim("openai/gpt-4o") is None
    assert any("reserved" in message for message in registry.errors)


def test_korvids_own_flow_still_cannot_claim_a_retired_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retired names are claimable by nobody, korvid included: an
    operator reads `openai-compat/` as the adapter that was deleted."""
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_DistEntryPoint("openai-compat", _flow("openai-compat"), "korvid"),),
    )

    registry = SpecialFlowRegistry.from_entry_points()

    assert registry.claim("openai-compat/x") is None
    assert registry.errors


def test_an_option_claiming_entry_point_does_not_deny_the_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flow that shares a prefix and activates on an option must leave
    the prefix itself routable: the factory refuses a *claimed* prefix
    nothing served, so a shared prefix in that set would make the option
    permanently on."""
    flow = _flow("ollama", claims_option="native_thinking")
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_DistEntryPoint("ollama", flow, "korvid"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"ollama"})

    assert registry.claim_by_option("ollama/qwen3:8b", {"native_thinking": True}) is flow
    assert registry.claim_by_option("ollama/qwen3:8b", {}) is None
    assert "ollama" not in registry.claimed_prefixes


def test_a_declared_option_flow_does_not_deny_the_prefix_either() -> None:
    """Same rule for a flow handed to the constructor, so the two ways of
    registering cannot disagree about what a shared prefix means."""
    registry = SpecialFlowRegistry([_flow("ollama", claims_option="native_thinking")])

    assert "ollama" not in registry.claimed_prefixes


def test_an_option_flow_is_still_the_setup_answer_for_its_shared_prefix() -> None:
    """The wizard reads `option_fields` off the flow the prefix resolves
    to. An operator can only opt in to a field that is rendered, so the
    lookup has to find the flow before the option is set."""
    flow = _flow("ollama", claims_option="native_thinking")
    registry = SpecialFlowRegistry([flow])

    assert registry.claim("ollama/qwen3:8b") is flow


class _ExplodingEntryPoint:
    """An entry point whose module cannot be imported."""

    def __init__(self, name: str, distribution: str | None = "korvid") -> None:
        self.name = name
        self.group = "korvid.provider"
        self.dist = None if distribution is None else _Distribution(distribution)

    def load(self) -> SpecialFlow:
        raise ImportError(f"No module named 'korvid.providers.flow_{self.name}'")


def test_a_flow_that_cannot_load_does_not_take_a_prefix_the_transport_already_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken plugin may disable itself; it may not disable `ollama/*`.

    The prefix the thinking flow *shares* is one LiteLLM publishes and
    routes on its own. Keeping it claimed after the load failed means the
    factory refuses a claimed prefix nothing served — so every ordinary
    `ollama/*` profile, including ones that never asked for the native
    route, stops working because an unrelated optional module raised on
    import.
    """
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_ExplodingEntryPoint("ollama"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"ollama", "openai"})

    assert registry.claim("ollama/qwen3:8b") is None
    assert "ollama" not in registry.claimed_prefixes


def test_a_flow_that_cannot_load_still_denies_a_device_login_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opposite case, and the reason the rule is not "unclaim on
    failure". Routing `github_copilot/...` starts an interactive device
    login inside `get_llm_provider`. If the flow that exists to prevent
    that cannot be loaded, the reference must be refused, not handed to
    the SDK.
    """
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_ExplodingEntryPoint("github-copilot"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"github_copilot", "ollama"})

    assert registry.claim("github-copilot/gpt-4o") is None
    assert "github-copilot" in registry.claimed_prefixes


def test_a_flow_that_cannot_load_keeps_a_prefix_nothing_else_can_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exclusive claim on a name the standard transport does not
    publish has no fallback: handing `acme/model` to routing produces the
    SDK's own confusion instead of korvid's reason, so the claim stands
    and the factory refuses it.
    """
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_ExplodingEntryPoint("acme", distribution="acme-korvid-plugin"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"ollama", "openai"})

    assert registry.claim("acme/model") is None
    assert "acme" in registry.claimed_prefixes


def test_a_flow_that_cannot_load_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handing the prefix back to the standard transport is a fallback,
    not a silence: an operator who opted in to a flow has to be able to
    find out that it was never loaded."""
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_ExplodingEntryPoint("ollama"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"ollama"})
    assert registry.claim("ollama/qwen3:8b") is None

    assert any("ollama" in message and "ImportError" in message for message in registry.errors)


def test_a_failed_load_is_reported_once_however_often_it_is_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claimed_prefixes` is read on every profile build. A failure that
    re-appended would grow the banner without bound."""
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_ExplodingEntryPoint("ollama"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"ollama"})
    for _ in range(3):
        assert registry.claim("ollama/qwen3:8b") is None
        assert "ollama" not in registry.claimed_prefixes

    assert len([message for message in registry.errors if "ollama" in message]) == 1


def test_an_unasked_for_prefix_is_still_claimed_before_anything_is_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is only for a load that was *attempted and failed*.

    A declared entry point that has not been loaded yet is still a claim,
    because the factory has to be able to refuse before it routes and it
    must not have to import plugin code to find that out.
    """
    monkeypatch.setattr(
        "korvid.providers.special_flows._iter_entry_points",
        lambda: (_ExplodingEntryPoint("ollama"),),
    )

    registry = SpecialFlowRegistry.from_entry_points(reserved_prefixes={"ollama"})

    assert "ollama" in registry.claimed_prefixes
