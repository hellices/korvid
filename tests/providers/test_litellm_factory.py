"""The factory: a profile becomes a live provider, or it is refused.

Every refusal in here is a security or correctness rule, so the tests are
written against the *real* LiteLLM routing wherever routing is not the
thing being isolated. `litellm` ships with the `[agent]` extra from Task
5B onward, so nothing here skips: a skip means the environment is wrong.
"""

from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path
from typing import Any, cast

import pytest

import korvid.providers.litellm_factory
from korvid.agent.model_policy import CapabilitySource, ModelCapabilities, ModelDescriptor
from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    EndpointRequirement,
    ModelEntry,
    ModelEntrySource,
    SpecialFlow,
)
from korvid.agent.provider import LLMProvider
from korvid.core.config import ConnectionAuthConfig, ModelConnectionConfig
from korvid.providers.litellm_factory import (
    OMIT_API_KEY,
    create_provider_from_profile,
)
from korvid.providers.litellm_provider import LiteLLMProvider
from korvid.providers.litellm_request import RequestPlan
from korvid.providers.special_flows import SpecialFlowRegistry

FACTORY_LOGGER = "korvid.providers.litellm_factory"
SRC_ROOT = Path(korvid.providers.litellm_factory.__file__).resolve().parents[2]
FACTORY_SOURCE = SRC_ROOT / "korvid" / "providers" / "litellm_factory.py"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class _FakeFlowProvider(LLMProvider):
    """What a special flow's own builder returns."""

    @property
    def descriptor(self) -> ModelDescriptor:
        return ModelDescriptor(provider="flow", model="flow-model")

    @property
    def capabilities(self) -> ModelCapabilities:
        return ModelCapabilities.unknown()

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        yield {"type": "done"}


def _flow(
    prefix: str,
    *,
    claims_option: str | None = None,
    builder: Any = None,
) -> SpecialFlow:
    return SpecialFlow(
        prefix=prefix,
        display_name=prefix,
        auth_methods=(AuthMethodDescriptor(id="device-login", display_name="Device login"),),
        endpoint=EndpointRequirement.UNSUPPORTED,
        claims_option=claims_option,
        build_provider=builder if builder is not None else (lambda _profile: _FakeFlowProvider()),
    )


def _none_auth() -> ConnectionAuthConfig:
    return ConnectionAuthConfig(method="none")


def _env_auth(name: str) -> ConnectionAuthConfig:
    return ConnectionAuthConfig(method="environment", settings={"key": name})


def _profile(
    reference: str,
    *,
    base_url: str | None = None,
    auth: ConnectionAuthConfig | None = None,
    options: dict[str, object] | None = None,
) -> ModelConnectionConfig:
    """A profile whose auth needs neither a secret nor an endpoint by default."""
    return ModelConnectionConfig(
        model=reference,
        endpoint=base_url,
        auth=auth if auth is not None else ConnectionAuthConfig(method="provider-default"),
        options=options or {},
    )


def _plan_for(profile: ModelConnectionConfig, **kwargs: Any) -> RequestPlan:
    provider = create_provider_from_profile(profile, **kwargs)
    assert isinstance(provider, LiteLLMProvider)
    return provider._plan


class _Catalog:
    """The two catalog methods the factory uses, and nothing else."""

    def __init__(self, entry: ModelEntry | None) -> None:
        self._entry = entry

    def entry(self, reference: str) -> ModelEntry | None:
        return self._entry


class _RaisingStore:
    def load(self, key: str) -> str | None:
        raise RuntimeError("keyring backend is unavailable")


class _Store:
    def __init__(self, value: str | None) -> None:
        self._value = value
        self.asked: list[str] = []

    def load(self, key: str) -> str | None:
        self.asked.append(key)
        return self._value


# ---------------------------------------------------------------------------
# Refusals that cost nothing: a profile the parser already rejected
# ---------------------------------------------------------------------------


def test_a_profile_with_a_config_error_is_refused() -> None:
    """The parser already decided this profile is unusable. Building from
    it anyway would send a request shaped by values korvid could not
    validate."""
    profile = ModelConnectionConfig(model="openai/gpt-4o", options={"bad": object()})
    assert profile.config_error is not None  # the fixture is the precondition
    assert create_provider_from_profile(profile) is None


def test_a_profile_without_a_model_is_refused() -> None:
    assert create_provider_from_profile(_profile("")) is None


@pytest.mark.parametrize("model", ["", "   ", "/", "openai/", "/gpt-4o", "gpt 4o"])
def test_a_malformed_reference_is_refused_rather_than_guessed(model: str) -> None:
    assert create_provider_from_profile(_profile(model)) is None


def test_a_refusal_disables_the_agent_rather_than_raising() -> None:
    """A bad profile must never stop korvid from starting."""
    for model in ("", "openai/", "unroutable-bare-name"):
        assert create_provider_from_profile(_profile(model)) is None


# ---------------------------------------------------------------------------
# Routing is LiteLLM's, not korvid's
# ---------------------------------------------------------------------------


def test_routing_is_delegated_to_litellm_not_to_a_provider_table() -> None:
    """The point of the whole change: there is no dict mapping a provider
    name to a class."""
    source = FACTORY_SOURCE.read_text(encoding="utf-8")
    assert "get_llm_provider" in source
    for vendor in ("openai", "anthropic", "azure", "bedrock", "ollama", "copilot"):
        assert vendor not in source.lower()


def test_no_source_file_hands_litellms_copilot_spelling_to_routing() -> None:
    """The underscore spelling is the device-login trap. It may be named
    as data (the catalog re-prefixes it), but a *reference* built from it
    must exist nowhere."""
    for path in SRC_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "github_copilot/" in line:
                assert line.lstrip().startswith("#") or '"""' in line or "`" in line


@pytest.mark.parametrize("reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"])
def test_a_special_flow_claims_the_reference_before_litellm_routes_it(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    """Critical: LiteLLM's own `get_llm_provider("github_copilot/...")`
    starts an interactive device-login flow *inside the routing call* and
    writes a credential file. korvid must reach its own flow first, and it
    must do so for **both** spellings — the underscore form is the one
    LiteLLM's own tables publish."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be reached")

    monkeypatch.setattr("korvid.providers.litellm_runtime.get_llm_provider", _explode)
    flows = SpecialFlowRegistry([_flow("github-copilot")])
    provider = create_provider_from_profile(_profile(reference), flows=flows)
    assert isinstance(provider, _FakeFlowProvider)


def test_a_flow_claimed_by_a_named_option_is_reached_before_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be reached")

    monkeypatch.setattr("korvid.providers.litellm_runtime.get_llm_provider", _explode)
    flows = SpecialFlowRegistry([_flow("acme", claims_option="native_thinking")])
    provider = create_provider_from_profile(
        _profile("acme/qwen3:8b", options={"native_thinking": True}), flows=flows
    )
    assert isinstance(provider, _FakeFlowProvider)


@pytest.mark.parametrize("reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"])
def test_a_claimed_prefix_with_no_flow_installed_is_refused_not_routed(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    """The deny-list has to bite even when no flow is registered.
    Otherwise removing the Copilot plugin turns a claimed prefix back into
    a device-login trap."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be reached")

    monkeypatch.setattr("korvid.providers.litellm_runtime.get_llm_provider", _explode)
    assert create_provider_from_profile(_profile(reference), flows=SpecialFlowRegistry()) is None


@pytest.mark.parametrize("reference", ["github-copilot/gpt-4o", "github_copilot/gpt-4o"])
def test_a_claimed_prefix_is_refused_even_with_no_registry_at_all(reference: str) -> None:
    """`flows=None` is the default every caller gets before Task 17 wires
    the plugin. It must not be the one path that routes the trap."""
    assert create_provider_from_profile(_profile(reference)) is None


def test_korvid_never_routes_to_litellms_own_copilot_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behavioural, not a source grep. A grep over korvid's own files
    cannot see a reference that arrived from LiteLLM's tables, from a
    config file, or from a search result — which is every way this
    reference actually shows up."""
    seen: list[str] = []

    def _record(model: str, **kwargs: object) -> tuple[str, str, None, None]:
        seen.append(model)
        return ("m", "p", None, None)

    monkeypatch.setattr("korvid.providers.litellm_runtime.get_llm_provider", _record)
    for reference in ("github_copilot/gpt-4o", "github-copilot/gpt-4o"):
        create_provider_from_profile(
            _profile(reference), flows=SpecialFlowRegistry([_flow("github-copilot")])
        )
    assert seen == []


def test_a_broken_flow_registry_does_not_prevent_normal_profiles() -> None:
    """A third-party plugin that raises must not take the agent with it."""

    class _Broken:
        def claim(self, reference: str) -> SpecialFlow | None:
            raise RuntimeError("plugin exploded")

        def claim_by_option(self, reference: str, options: Any) -> SpecialFlow | None:
            raise RuntimeError("plugin exploded")

        @property
        def claimed_prefixes(self) -> frozenset[str]:
            raise RuntimeError("plugin exploded")

    provider = create_provider_from_profile(_profile("openai/gpt-4o"), flows=cast("Any", _Broken()))
    assert isinstance(provider, LiteLLMProvider)


def test_a_flow_whose_builder_fails_disables_rather_than_crashes() -> None:
    def _boom(_profile_arg: ModelConnectionConfig) -> LLMProvider | None:
        raise RuntimeError("device login unavailable")

    flows = SpecialFlowRegistry([_flow("github-copilot", builder=_boom)])
    assert create_provider_from_profile(_profile("github-copilot/gpt-4o"), flows=flows) is None


def test_a_flow_with_no_builder_is_refused_not_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declaration-only flow is setup metadata. Falling through to
    routing would hand the claimed prefix straight to the trap."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be reached")

    monkeypatch.setattr("korvid.providers.litellm_runtime.get_llm_provider", _explode)
    flows = SpecialFlowRegistry(
        [SpecialFlow(prefix="github-copilot", display_name="c", auth_methods=())]
    )
    assert create_provider_from_profile(_profile("github-copilot/gpt-4o"), flows=flows) is None


# ---------------------------------------------------------------------------
# Auth resolution: five methods, none falling back to another
# ---------------------------------------------------------------------------


def test_an_environment_auth_method_reads_the_named_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_KEY", "sk-live")
    plan = _plan_for(_profile("openai/gpt-4o", auth=_env_auth("MY_KEY")))
    assert plan.api_key == "sk-live"


def test_an_unset_named_variable_is_refused_with_the_variable_name(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("MY_KEY", raising=False)
    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        assert (
            create_provider_from_profile(_profile("openai/gpt-4o", auth=_env_auth("MY_KEY")))
            is None
        )
    assert "MY_KEY" in caplog.text
    assert "sk-" not in caplog.text


def test_environment_auth_never_falls_back_to_another_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit name that is unset must fail loudly. Falling back to
    the SDK's own ambient variable would send a credential the operator
    did not choose."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-ambient")
    monkeypatch.delenv("MY_KEY", raising=False)
    assert create_provider_from_profile(_profile("openai/gpt-4o", auth=_env_auth("MY_KEY"))) is None


def test_environment_auth_without_a_variable_name_is_refused() -> None:
    profile = _profile("openai/gpt-4o", auth=ConnectionAuthConfig(method="environment"))
    assert create_provider_from_profile(profile) is None


def test_provider_default_passes_no_key_so_the_sdk_chain_applies() -> None:
    """This is the method that *deliberately* delegates: cloud profiles,
    managed identity and ADC all live below the SDK.

    Unconditional: `... not in kwargs or plan.api_key is None` would pass
    for an implementation that sets api_key=None, which does *not*
    delegate — the SDK sees an explicit argument and stops consulting its
    own chain."""
    plan = _plan_for(
        _profile("bedrock-x/model", auth=ConnectionAuthConfig(method="provider-default"))
    )
    assert "api_key" not in plan.call_kwargs([], [], stream=True)
    assert plan.api_key is OMIT_API_KEY


def test_a_keyring_lookup_reads_the_store_and_passes_the_value() -> None:
    store = _Store("sk-from-keyring")
    plan = _plan_for(
        _profile("openai/gpt-4o", auth=ConnectionAuthConfig(method="keyring")),
        credentials=cast("Any", store),
    )
    assert plan.api_key == "sk-from-keyring"
    assert store.asked == ["openai/gpt-4o"]


def test_a_keyring_entry_name_may_be_named_by_the_profile() -> None:
    store = _Store("sk-named")
    plan = _plan_for(
        _profile(
            "openai/gpt-4o",
            auth=ConnectionAuthConfig(method="keyring", settings={"key": "team-key"}),
        ),
        credentials=cast("Any", store),
    )
    assert plan.api_key == "sk-named"
    assert store.asked == ["team-key"]


def test_a_keyring_lookup_failure_disables_rather_than_crashes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile = _profile("openai/gpt-4o", auth=ConnectionAuthConfig(method="keyring"))
    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        assert (
            create_provider_from_profile(profile, credentials=cast("Any", _RaisingStore())) is None
        )
    assert "keyring" in caplog.text.lower()


def test_a_keyring_entry_that_does_not_exist_is_refused() -> None:
    profile = _profile("openai/gpt-4o", auth=ConnectionAuthConfig(method="keyring"))
    assert create_provider_from_profile(profile, credentials=cast("Any", _Store(None))) is None


def test_device_login_without_a_flow_is_refused(caplog: pytest.LogCaptureFixture) -> None:
    """`device-login` is only reachable when a flow claimed the reference;
    on any other reference there is nothing to log in to."""
    profile = _profile("openai/gpt-4o", auth=ConnectionAuthConfig(method="device-login"))
    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        assert create_provider_from_profile(profile) is None
    assert "device-login" in caplog.text


def test_an_unknown_auth_method_is_refused_rather_than_defaulted() -> None:
    profile = _profile("openai/gpt-4o", auth=ConnectionAuthConfig(method="bearer-magic"))
    assert create_provider_from_profile(profile) is None


def test_the_factory_never_logs_a_secret(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MY_KEY", "sk-super-secret")
    with caplog.at_level(logging.DEBUG):
        create_provider_from_profile(_profile("openai/gpt-4o", auth=_env_auth("MY_KEY")))
        create_provider_from_profile(_profile("nope/", auth=_env_auth("MY_KEY")))
    assert "sk-super-secret" not in caplog.text


# ---------------------------------------------------------------------------
# Keyless auth: allowed only against an endpoint the operator named
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-5",
        "azure/gpt-4o",
        "gemini/gemini-2.5-pro",
        "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
        "ollama/llama3",
        "groq/llama-3.3-70b-versatile",
        "xai/grok-4",
        "hosted_vllm/qwen",
        "company/internal-v2",
    ],
)
@pytest.mark.parametrize(
    ("endpoint", "allowed"),
    [
        (None, False),
        ("", False),
        ("   ", False),
        ("http://localhost:8000/v1", True),
    ],
)
def test_keyless_auth_requires_an_explicit_endpoint(
    reference: str, endpoint: str | None, allowed: bool
) -> None:
    """Keyless is allowed only when the operator named the host.

    Routing is deliberately **not** patched: these are real references
    resolved by the real `get_llm_provider`, so the test would catch a
    rule that had quietly acquired a provider dimension. Forty
    combinations, one answer per endpoint column, whatever the prefix.
    """
    profile = _profile(reference, base_url=endpoint, auth=_none_auth())
    assert (create_provider_from_profile(profile) is not None) is allowed


def test_the_keyless_refusal_names_the_missing_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The operator has to be able to act on it. "Refused" is not a
    message; "keyless auth needs an endpoint" is."""
    profile = _profile("openai/gpt-4o", base_url=None, auth=_none_auth())
    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        assert create_provider_from_profile(profile) is None
    assert "endpoint" in caplog.text.lower()
    assert "base_url" in caplog.text


def test_the_none_auth_rule_reads_one_profile_field_and_nothing_else() -> None:
    """A substring check on the source proves nothing; parse it.

    The rule must be a test of the profile's own endpoint. Anything that
    consults routing, a provider set, or a hostname is the inversion this
    revision removed.
    """
    import korvid.providers.litellm_factory as factory

    tree = ast.parse(inspect.getsource(factory))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_refuse_keyless_without_endpoint"
    )
    vendors = {"openai", "anthropic", "azure", "gemini", "bedrock", "ollama", "groq", "xai"}
    literals = {
        node.value.lower()
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert not (literals & vendors)
    called: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                called.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
    assert "get_llm_provider" not in called


def test_a_keyless_endpoint_still_sends_a_sentinel_rather_than_an_ambient_key() -> None:
    """Task 13's rule, pinned end to end: no resolved credential must not
    become the SDK's own `*_API_KEY` lookup."""
    plan = _plan_for(
        _profile("openai/gpt-4o", base_url="http://localhost:8000/v1", auth=_none_auth())
    )
    assert plan.api_key is None
    assert plan.call_kwargs([], [], stream=True)["api_key"] == "korvid-keyless"


# ---------------------------------------------------------------------------
# The endpoint a provider genuinely cannot be reached without
# ---------------------------------------------------------------------------


def test_a_provider_that_cannot_be_reached_without_an_endpoint_is_refused(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The build-time half of the endpoint rule: LiteLLM reports the base
    URL as an environment key it cannot find, and the profile names none."""
    monkeypatch.delenv("AZURE_API_BASE", raising=False)
    monkeypatch.setenv("MY_KEY", "sk-live")
    profile = _profile("azure/gpt-4o", auth=_env_auth("MY_KEY"))
    with caplog.at_level(logging.WARNING, logger=FACTORY_LOGGER):
        assert create_provider_from_profile(profile) is None
    assert "base_url" in caplog.text


def test_the_same_provider_builds_once_the_endpoint_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_API_BASE", raising=False)
    monkeypatch.setenv("MY_KEY", "sk-live")
    profile = _profile(
        "azure/gpt-4o", base_url="https://example.openai.azure.com", auth=_env_auth("MY_KEY")
    )
    provider = create_provider_from_profile(profile)
    assert isinstance(provider, LiteLLMProvider)


def test_a_provider_with_its_own_default_host_needs_no_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_KEY", "sk-live")
    provider = create_provider_from_profile(_profile("openai/gpt-4o", auth=_env_auth("MY_KEY")))
    assert isinstance(provider, LiteLLMProvider)


# ---------------------------------------------------------------------------
# The plan and the descriptor
# ---------------------------------------------------------------------------


def test_the_plan_carries_the_whole_reference_and_the_operator_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_KEY", "sk-live")
    plan = _plan_for(
        _profile("openai/gpt-4o", base_url="http://gw.internal/v1", auth=_env_auth("MY_KEY"))
    )
    assert plan.model == "openai/gpt-4o"
    assert plan.base_url == "http://gw.internal/v1"


def test_the_descriptor_names_the_routed_provider_and_the_model_tag() -> None:
    provider = create_provider_from_profile(_profile("openai/gpt-4o"))
    assert isinstance(provider, LiteLLMProvider)
    assert provider.descriptor.provider == "openai"
    assert provider.descriptor.model == "gpt-4o"


def test_options_the_provider_rejects_do_not_reach_the_wire() -> None:
    plan = _plan_for(_profile("openai/gpt-4o", options={"temperature": 0.5, "not_a_param": 1}))
    kwargs = plan.call_kwargs([], [], stream=False)
    assert kwargs["temperature"] == 0.5
    assert "not_a_param" not in kwargs


def test_korvid_owned_options_never_reach_the_wire() -> None:
    plan = _plan_for(_profile("openai/gpt-4o", options={"native_thinking": True}))
    assert "native_thinking" not in plan.call_kwargs([], [], stream=False)


# ---------------------------------------------------------------------------
# Capabilities: from the catalog, never from the reference
# ---------------------------------------------------------------------------


def test_capabilities_come_from_the_catalog_and_stay_unknown_without_one() -> None:
    provider = create_provider_from_profile(_profile("x/y"), catalog=None)
    assert provider is not None
    caps = provider.capabilities
    assert caps.supports_tools is None
    assert caps.context_window_tokens is None
    assert caps.provenance == {}


def test_provenance_records_where_each_fact_came_from() -> None:
    """`ModelCapabilities.provenance` is a per-fact mapping, not one
    source field."""
    entry = ModelEntry(
        reference="x/y",
        provider_id="x",
        context_window_tokens=128_000,
        supports_tools=True,
        supports_reasoning=False,
        source=ModelEntrySource.LITELLM,
    )
    provider = create_provider_from_profile(_profile("x/y"), catalog=cast("Any", _Catalog(entry)))
    assert provider is not None
    caps = provider.capabilities
    assert caps.context_window_tokens == 128_000
    assert caps.supports_tools is True
    assert caps.supports_reasoning is False
    assert caps.provenance["supports_tools"] is CapabilitySource.CATALOG
    assert caps.provenance["context_window_tokens"] is CapabilitySource.CATALOG


def test_a_catalog_fact_left_unknown_gets_no_provenance() -> None:
    entry = ModelEntry(reference="x/y", provider_id="x", supports_tools=True)
    provider = create_provider_from_profile(_profile("x/y"), catalog=cast("Any", _Catalog(entry)))
    assert provider is not None
    assert provider.capabilities.context_window_tokens is None
    assert "context_window_tokens" not in provider.capabilities.provenance


def test_a_profile_option_overrides_a_catalog_capability() -> None:
    """An operator who sets num_ctx knows their deployment better than a
    table does."""
    entry = ModelEntry(reference="x/y", provider_id="x", context_window_tokens=128_000)
    provider = create_provider_from_profile(
        _profile("x/y", options={"num_ctx": 8192}), catalog=cast("Any", _Catalog(entry))
    )
    assert provider is not None
    caps = provider.capabilities
    assert caps.context_window_tokens == 8192
    assert caps.provenance["context_window_tokens"] is CapabilitySource.USER


def test_a_catalogs_max_output_tokens_is_display_only() -> None:
    """`ModelEntry.max_output_tokens` has no capability counterpart; it
    must not be read as the context window."""
    entry = ModelEntry(reference="x/y", provider_id="x", max_output_tokens=4096)
    provider = create_provider_from_profile(_profile("x/y"), catalog=cast("Any", _Catalog(entry)))
    assert provider is not None
    assert provider.capabilities.context_window_tokens is None


def test_a_catalog_that_raises_does_not_prevent_the_build() -> None:
    class _Boom:
        def entry(self, reference: str) -> ModelEntry | None:
            raise RuntimeError("catalog exploded")

    provider = create_provider_from_profile(_profile("x/y"), catalog=cast("Any", _Boom()))
    assert isinstance(provider, LiteLLMProvider)
    assert provider.capabilities.provenance == {}


def test_capabilities_are_never_inferred_from_the_reference() -> None:
    provider = create_provider_from_profile(_profile("x/gpt-4o-with-tools-2000k"))
    assert provider is not None
    assert provider.capabilities == ModelCapabilities.unknown()
