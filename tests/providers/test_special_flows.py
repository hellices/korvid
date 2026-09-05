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
