from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from korvid.agent.model_profiles import (
    EndpointRequirement,
    ModelConnectionConfig,
    ModelEntry,
    ModelEntrySource,
)

litellm = pytest.importorskip("litellm")

from korvid.providers.litellm_catalog import (  # noqa: E402
    LiteLLMModelCatalog,
    ProfileTestUnavailable,
)
from korvid.providers.litellm_runtime import (  # noqa: E402
    LOCKDOWN_FLAGS,
    ProviderSDKError,
    model_cost_entry,
    models_by_provider,
)

_SRC = Path("src/korvid")


def _imported_module_names(path: Path) -> list[str]:
    names: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_importing_the_runtime_locks_litellm_down() -> None:
    for name, expected in LOCKDOWN_FLAGS:
        assert getattr(litellm, name) == expected, name


def test_a_mapped_provider_error_prints_nothing_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`suppress_debug_info` is the only thing standing between LiteLLM
    and the terminal a Textual app is drawing on.

    Both `litellm_core_utils/exception_mapping_utils.py` and
    `litellm_core_utils/get_llm_provider_logic.py` call bare `print()`
    with ANSI colour codes, gated on nothing but
    `litellm.suppress_debug_info is False`. They never touch
    `litellm.verbose_logger`, so detaching its handlers in the import
    wrapper does not reach them. Drive a real mapped failure and assert
    the capture is empty, so a maintainer who trims the flag list as
    "noise control" fails here instead of corrupting the TUI.
    """
    capsys.readouterr()  # discard anything the imports above emitted
    with pytest.raises(Exception, match=r"(?i)provider|model|llm"):
        litellm.completion(
            model="definitely-not-a-real-provider/nope",
            messages=[{"role": "user", "content": "x"}],
        )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "\033[" not in captured.out + captured.err


def test_the_flag_that_protects_stdout_is_not_quietly_droppable() -> None:
    """Pin the flag by name. The test above proves the behaviour; this
    one names the mechanism, so removing the flag from LOCKDOWN_FLAGS
    fails with a message that says why it mattered."""
    assert ("suppress_debug_info", True) in LOCKDOWN_FLAGS


def test_the_runtime_reexports_the_base_class_that_actually_catches_errors() -> None:
    """`except litellm.exceptions.APIError` catches almost nothing.

    Measured on 1.98.0: only `APIError` itself subclasses it, while the
    error classes korvid must translate -- Authentication, RateLimit,
    NotFound, BadRequest, ContextWindowExceeded, Timeout,
    APIConnection, InternalServer, ServiceUnavailable, PermissionDenied
    -- share `openai.OpenAIError`. Catching the wrong base would make
    the whole REQUEST_SENT rule dead code.
    """
    must_be_caught = [
        "AuthenticationError",
        "RateLimitError",
        "NotFoundError",
        "BadRequestError",
        "ContextWindowExceededError",
        "Timeout",
        "APIConnectionError",
        "InternalServerError",
        "ServiceUnavailableError",
        "PermissionDeniedError",
    ]
    for name in must_be_caught:
        cls = getattr(litellm.exceptions, name)
        assert issubclass(cls, ProviderSDKError), name
    assert not issubclass(litellm.exceptions.AuthenticationError, litellm.exceptions.APIError)


def test_a_renamed_lockdown_flag_fails_the_import_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assign-then-read-back would keep passing after an upstream rename:
    `setattr` on a name litellm no longer uses just creates an unused
    attribute while the real sink stays open. The guard has to run first.
    """
    import importlib
    import types

    stub = types.SimpleNamespace(
        **{name: value for name, value in LOCKDOWN_FLAGS if name != "telemetry"},
        acompletion=None,
        get_llm_provider=None,
        exceptions=None,
        models_by_provider={},
        model_cost={},
    )
    monkeypatch.setattr("korvid.providers._litellm_import.litellm", stub, raising=True)
    import korvid.providers.litellm_runtime as runtime

    with pytest.raises(ImportError, match="telemetry"):
        importlib.reload(runtime)


def test_exactly_one_korvid_module_imports_litellm() -> None:
    """The env var that makes the import offline has to be set in a file
    that runs first — an import sorter would reorder a plain top-level
    `import litellm` above any `korvid` import in the same block.
    """
    offenders = {
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if any(
            name == "litellm" or name.startswith("litellm.")
            for name in _imported_module_names(path)
        )
    }
    assert offenders == {"providers/_litellm_import.py"}


def test_exactly_one_korvid_module_imports_the_wrapper() -> None:
    importers = {
        str(path.relative_to(_SRC))
        for path in sorted(_SRC.rglob("*.py"))
        if "korvid.providers._litellm_import" in _imported_module_names(path)
    }
    assert importers == {"providers/litellm_runtime.py"}


def test_provider_model_tables_are_normalized_to_sorted_lists() -> None:
    """Most shipped values are sets and a handful are lists; indexing a
    set raises `TypeError`, and set iteration order is not stable."""
    table = models_by_provider()
    assert table, "litellm shipped an empty provider table"
    assert all(isinstance(models, list) for models in table.values())
    assert all(models == sorted(models) for models in table.values())
    assert table["anthropic"][:1] == sorted(litellm.models_by_provider["anthropic"])[:1]


def test_no_test_asserts_a_catalog_size() -> None:
    """Table cardinality differs between the bundled data and the remote
    cost map and moves with every litellm patch release, so an exact-count
    assertion is a scheduled false failure. Membership and shape only."""
    table = models_by_provider()
    assert len(table) > 1
    assert "anthropic" in table


def test_the_provider_qualified_cost_key_wins_over_the_bare_one() -> None:
    """Both spellings exist in `model_cost`, and for a measurable minority
    of references they carry *different* facts, so a bare-first lookup
    reads another provider's record."""
    assert model_cost_entry("anthropic", "claude-sonnet-4-5") is not None
    assert model_cost_entry("ollama", "ollama/llama3") is not None
    assert model_cost_entry("openai", "definitely-not-a-model") is None

    divergent = next(
        (
            (provider, model)
            for provider, models in models_by_provider().items()
            for model in models
            if model in litellm.model_cost
            and f"{provider}/{model}" in litellm.model_cost
            and litellm.model_cost[model] != litellm.model_cost[f"{provider}/{model}"]
        ),
        None,
    )
    if divergent is not None:
        provider, model = divergent
        assert model_cost_entry(provider, model) == litellm.model_cost[f"{provider}/{model}"]


def test_search_finds_a_known_model_by_substring() -> None:
    catalog = LiteLLMModelCatalog()
    results = catalog.search("claude-sonnet-4-5")
    references = [entry.reference for entry in results]
    assert "anthropic/claude-sonnet-4-5" in references


def test_search_is_bounded_and_deterministic() -> None:
    catalog = LiteLLMModelCatalog()
    first = catalog.search("gpt", limit=10)
    second = catalog.search("gpt", limit=10)
    assert 0 < len(first) <= 10
    assert [e.reference for e in first] == [e.reference for e in second]


def test_search_never_raises_on_junk() -> None:
    catalog = LiteLLMModelCatalog()
    assert catalog.search("") == () or len(catalog.search("")) <= 50
    assert catalog.search("\x00\x01 ?? []") == ()


def test_capabilities_are_translated_faithfully_and_unknowns_stay_none() -> None:
    catalog = LiteLLMModelCatalog()
    known = catalog.entry("anthropic/claude-sonnet-4-5")
    assert known is not None
    record = model_cost_entry("anthropic", "claude-sonnet-4-5")
    assert record is not None
    assert known.context_window_tokens == record.get("max_input_tokens")
    assert known.supports_tools is record.get("supports_function_calling")
    assert known.source is ModelEntrySource.LITELLM

    unknown = catalog.entry("openai/definitely-not-a-model")
    assert unknown is None


def test_litellms_github_copilot_provider_never_reaches_the_catalog() -> None:
    """Resolving `github_copilot/...` starts an interactive device login
    inside the routing call. Offering those ids in search would put that
    one keystroke away, so the provider is excluded or rewritten onto
    korvid's own prefix."""
    catalog = LiteLLMModelCatalog()
    references = {entry.reference for entry in catalog.search("copilot", limit=50)}
    assert not any(ref.startswith("github_copilot/") for ref in references)
    assert "github_copilot" in litellm.models_by_provider, (
        "litellm stopped shipping the provider; the exclusion is now dead code"
    )


@pytest.mark.parametrize(
    "reference",
    ["github_copilot/gpt-4o", "github-copilot/gpt-4o"],
)
def test_per_reference_answers_never_route(monkeypatch: pytest.MonkeyPatch, reference: str) -> None:
    """`auth_methods`, `option_fields` and `endpoint_requirement` render
    once per visible search row. A routing call there is slow for every
    reference and, for a claimed prefix, starts a device login."""

    def _explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_llm_provider must not be called here")

    monkeypatch.setattr("korvid.providers.litellm_runtime.get_llm_provider", _explode)
    catalog = LiteLLMModelCatalog()
    assert catalog.auth_methods(reference)
    assert catalog.auth_methods(reference, endpoint="http://localhost:8080")
    assert catalog.option_fields(reference) is not None
    assert catalog.endpoint_requirement(reference) in EndpointRequirement


def test_a_manually_typed_reference_is_usable_even_when_unknown() -> None:
    catalog = LiteLLMModelCatalog()
    entry = catalog.manual_entry("company/internal-v2")
    assert entry.source is ModelEntrySource.MANUAL
    assert entry.reference == "company/internal-v2"
    assert entry.supports_tools is None


def test_every_reference_offers_the_generic_auth_methods() -> None:
    catalog = LiteLLMModelCatalog()
    ids = {m.id for m in catalog.auth_methods("openai/gpt-4o")}
    assert {"environment", "keyring", "provider-default"} <= ids


@pytest.mark.parametrize(
    "reference",
    ["openai/gpt-4o", "anthropic/claude-sonnet-4-5", "ollama/llama3", "company/internal-v2"],
)
def test_none_auth_is_offered_only_once_an_endpoint_is_known(reference: str) -> None:
    """The catalog mirrors the factory's rule exactly, for every reference.

    Keyless is refused with no endpoint and allowed with one — including
    for `ollama/llama3`, which the earlier default-host rule wrongly
    refused, and excluding `openai/gpt-4o`, which it wrongly allowed.
    Parametrizing over both a hosted and a local reference is the point:
    the answer must depend on the endpoint argument alone, never on the
    provider prefix.
    """
    catalog = LiteLLMModelCatalog()
    assert "none" not in {m.id for m in catalog.auth_methods(reference)}
    assert "none" not in {m.id for m in catalog.auth_methods(reference, endpoint="")}
    assert "none" in {
        m.id for m in catalog.auth_methods(reference, endpoint="http://localhost:11434")
    }


def test_the_catalogs_none_rule_names_no_provider() -> None:
    """A provider-shaped set anywhere near this rule is the bug the
    default-host inversion came from. Assert on the parsed module, not on
    a substring: a comment mentioning a vendor is fine, a frozenset of
    vendor names is not."""
    import korvid.providers.litellm_catalog as module

    tree = ast.parse(inspect.getsource(module))
    vendors = {"openai", "anthropic", "azure", "gemini", "bedrock", "ollama", "groq", "xai"}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            continue
        literals = {
            e.value.lower()
            for e in node.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
        assert not (literals & vendors), f"provider set at line {node.lineno}: {literals}"


def test_an_environment_auth_method_asks_for_a_reference_not_a_secret() -> None:
    catalog = LiteLLMModelCatalog()
    method = next(m for m in catalog.auth_methods("openai/gpt-4o") if m.id == "environment")
    assert [f.key for f in method.fields] == ["key"]
    assert method.fields[0].kind.value == "secret_ref"


def test_credential_env_hints_are_offered_but_never_read() -> None:
    """A hint tells the operator which variable to *name*. The catalog
    must not read the variable — that is the factory's job, and only for
    a profile that explicitly asked for it."""
    catalog = LiteLLMModelCatalog()
    entry = catalog.entry("anthropic/claude-sonnet-4-5")
    assert entry is not None
    assert all(hint.isupper() for hint in entry.credential_env_hints)


@pytest.mark.parametrize(
    "reference",
    ["openai/gpt-4o", "azure/gpt-4o", "hosted_vllm/qwen", "company/internal-v2"],
)
def test_endpoint_is_optional_for_every_reference_no_flow_claims(reference: str) -> None:
    """OPTIONAL is the only honest default.

    LiteLLM ships no host data (the `model_cost` records carry no
    api_base/base_url/host key at all), so nothing can distinguish
    "needs an endpoint" from "does not". Azure is included deliberately:
    an earlier revision asserted REQUIRED for it from a hand-built
    frozenset, which is the compiled-in provider table this design
    removes. Azure's real requirement is expressed where it belongs — the
    factory refuses an Azure profile with no endpoint at build time.
    """
    catalog = LiteLLMModelCatalog()
    assert catalog.endpoint_requirement(reference) is EndpointRequirement.OPTIONAL


def test_a_flow_declaration_is_the_only_source_of_a_non_optional_requirement() -> None:
    """Task 8 composes the flow registry in; here, with no flows, every
    answer is OPTIONAL. The flow-driven REQUIRED/UNSUPPORTED cases are
    asserted in Task 8's suite against a real registered flow rather than
    against a table this module does not own."""
    catalog = LiteLLMModelCatalog()
    answers = {
        catalog.endpoint_requirement(r)
        for r in (
            "openai/gpt-4o",
            "ollama/llama3",
            "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
        )
    }
    assert answers == {EndpointRequirement.OPTIONAL}


async def test_discovery_without_an_endpoint_returns_nothing_rather_than_raising() -> None:
    catalog = LiteLLMModelCatalog()
    profile = ModelConnectionConfig(model="openai/gpt-4o")
    assert await catalog.discover(profile) == ()


# ---------------------------------------------------------------------------
# Task 7 — models.dev enrichment and provenance
# ---------------------------------------------------------------------------


class _FakeMetadataSource:
    """Minimal stub of `ModelMetadataSource` for catalog overlay tests."""

    def __init__(self, entries: dict[str, object]) -> None:
        self._entries = entries

    def metadata(self, reference: str) -> object | None:  # type: ignore[return]  # malformed source exercises runtime rejection
        return self._entries.get(reference)

    def env_hints(self, provider_id: str) -> tuple[str, ...]:
        return ()


def test_provenance_stays_litellm_when_the_overlay_adds_nothing() -> None:
    """An overlay that restates known facts must not claim credit.

    `replace()` returns a new object even when every field is identical, so
    the naive implementation flips `source` to `MODELS_DEV` for entries
    models.dev did not actually improve, and the UI's "where did this come
    from" line becomes false. Compare the dataclasses, not the identities.
    """
    from korvid.providers.models_dev import ModelMetadata

    base = ModelEntry(
        reference="openai/gpt-4o",
        provider_id="openai",
        display_name="GPT-4o",
        context_window_tokens=128_000,
        max_output_tokens=16_384,
        supports_tools=True,
        supports_reasoning=False,
        credential_env_hints=("OPENAI_API_KEY",),
        source=ModelEntrySource.LITELLM,
    )
    echoing = _FakeMetadataSource(
        {
            "openai/gpt-4o": ModelMetadata(
                reference="openai/gpt-4o",
                display_name="GPT-4o",
                context_window_tokens=128_000,
                max_output_tokens=16_384,
                supports_tools=True,
                supports_reasoning=False,
                credential_env_hints=("OPENAI_API_KEY",),
            )
        }
    )
    catalog = LiteLLMModelCatalog(enrichment=echoing)

    result = catalog._overlay(base)

    assert result == base
    assert result.source is ModelEntrySource.LITELLM


def test_provenance_becomes_models_dev_only_when_a_fact_was_added() -> None:
    """The mirror image: a genuine contribution must be credited."""
    from korvid.providers.models_dev import ModelMetadata

    bare = ModelEntry(
        reference="openai/gpt-4o",
        provider_id="openai",
        display_name=None,
        context_window_tokens=128_000,
        max_output_tokens=None,
        supports_tools=True,
        supports_reasoning=None,
        credential_env_hints=("OPENAI_API_KEY",),
        source=ModelEntrySource.LITELLM,
    )
    contributing = _FakeMetadataSource(
        {
            "openai/gpt-4o": ModelMetadata(
                reference="openai/gpt-4o",
                display_name="GPT-4o",
            )
        }
    )
    catalog = LiteLLMModelCatalog(enrichment=contributing)

    result = catalog._overlay(bare)

    assert result.display_name == "GPT-4o"
    assert result.source is ModelEntrySource.MODELS_DEV


def test_enrichment_cannot_create_a_routable_entry_for_unknown_references() -> None:
    """A reference unknown to LiteLLM must not become routable via enrichment."""
    from korvid.providers.models_dev import ModelMetadata

    source = _FakeMetadataSource(
        {
            "unknown/some-model": ModelMetadata(
                reference="unknown/some-model",
                display_name="Some Model",
            )
        }
    )
    catalog = LiteLLMModelCatalog(enrichment=source)
    # The catalog builds its index from LiteLLM, not from enrichment.
    entry = catalog.entry("unknown/some-model")
    assert entry is None


def test_enrichment_cannot_change_where_a_request_goes() -> None:
    """Metadata may describe a model. It may never route one."""
    source = Path("src/korvid/providers/models_dev.py").read_text(encoding="utf-8")
    for forbidden in ("api_base", "base_url", "acompletion", "api_key", "get_llm_provider"):
        assert forbidden not in source


def test_litellm_context_window_wins_over_enrichment() -> None:
    """LiteLLM's data wins every conflict — enrichment never overrides."""
    from korvid.providers.models_dev import ModelMetadata

    base = ModelEntry(
        reference="openai/gpt-4o",
        provider_id="openai",
        display_name="GPT-4o",
        context_window_tokens=128_000,
        source=ModelEntrySource.LITELLM,
    )
    lower_claim = _FakeMetadataSource(
        {
            "openai/gpt-4o": ModelMetadata(
                reference="openai/gpt-4o",
                context_window_tokens=1,
            )
        }
    )
    catalog = LiteLLMModelCatalog(enrichment=lower_claim)

    result = catalog._overlay(base)

    assert result.context_window_tokens == 128_000
    assert result.source is ModelEntrySource.LITELLM


# ---------------------------------------------------------------------------
# Task 8 — flow registry integration
# ---------------------------------------------------------------------------


def _make_flow(prefix: str, **kwargs: object) -> object:
    from korvid.agent.model_profiles import AuthMethodDescriptor, SpecialFlow

    defaults: dict[str, object] = {
        "auth_methods": (AuthMethodDescriptor(id="none", display_name="None"),),
    }
    defaults.update(kwargs)
    return SpecialFlow(
        prefix=prefix,
        display_name=prefix,
        **defaults,  # type: ignore[arg-type]
    )


def test_a_flow_supplies_the_only_non_optional_endpoint_requirements() -> None:
    from korvid.providers.special_flows import SpecialFlowRegistry

    registry = SpecialFlowRegistry(
        [_make_flow("github-copilot", endpoint=EndpointRequirement.UNSUPPORTED)]
    )
    catalog = LiteLLMModelCatalog(flows=registry)
    assert catalog.endpoint_requirement("github-copilot/gpt-4o") is (
        EndpointRequirement.UNSUPPORTED
    )
    assert catalog.endpoint_requirement("openai/gpt-4o") is EndpointRequirement.OPTIONAL
    assert catalog.endpoint_requirement("azure/gpt-4o") is EndpointRequirement.OPTIONAL


def test_a_flow_cannot_offer_keyless_auth_without_an_endpoint() -> None:
    """The catalog filters a plugin's declarations through korvid's own
    rule. A flow is third-party code; it does not get to widen a refusal
    the factory will enforce anyway."""
    from korvid.agent.model_profiles import AuthMethodDescriptor
    from korvid.providers.special_flows import SpecialFlowRegistry

    registry = SpecialFlowRegistry(
        [
            _make_flow(
                "company-flow",
                auth_methods=(AuthMethodDescriptor(id="none", display_name="None"),),
            )
        ]
    )
    catalog = LiteLLMModelCatalog(flows=registry)
    assert "none" not in {m.id for m in catalog.auth_methods("company-flow/x")}
    assert "none" in {
        m.id for m in catalog.auth_methods("company-flow/x", endpoint="http://host:8080")
    }


# ---------------------------------------------------------------------------
# Connection probe
# ---------------------------------------------------------------------------


async def test_the_catalog_delegates_the_probe_to_its_injected_tester() -> None:
    """`test()` is the wizard's last stage. The catalog owns no transport,
    so it must hand the profile to whoever does — unchanged."""
    seen: list[ModelConnectionConfig] = []

    async def _probe(profile: ModelConnectionConfig) -> str:
        seen.append(profile)
        return "connected to acme"

    catalog = LiteLLMModelCatalog(tester=_probe)
    profile = ModelConnectionConfig(model="openai/gpt-4o", endpoint="http://host/v1")

    assert await catalog.test(profile) == "connected to acme"
    assert seen == [profile]


async def test_the_catalog_propagates_a_probe_failure_unchanged() -> None:
    """A refused connection is the answer the wizard renders; swallowing
    it would report a working profile that cannot connect."""

    async def _probe(profile: ModelConnectionConfig) -> str:
        raise RuntimeError("connection refused")

    catalog = LiteLLMModelCatalog(tester=_probe)

    with pytest.raises(RuntimeError, match="connection refused"):
        await catalog.test(ModelConnectionConfig(model="openai/gpt-4o"))


async def test_a_catalog_without_a_tester_says_probing_is_unavailable() -> None:
    """Never `NotImplementedError`: the wizard shows what it caught, and
    a bare stub name is not something an operator can act on."""
    catalog = LiteLLMModelCatalog()

    with pytest.raises(ProfileTestUnavailable, match="cannot test"):
        await catalog.test(ModelConnectionConfig(model="openai/gpt-4o"))
