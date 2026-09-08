from __future__ import annotations

import ast
import inspect
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, cast

import pytest

from korvid.agent.model_profiles import (
    EndpointRequirement,
    MetadataRefresh,
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
from korvid.providers.models_dev import (  # noqa: E402
    ModelMetadata,
    ModelMetadataSource,
    RefreshOutcome,
)

_SRC = Path("src/korvid")


def _module_id(path: PurePath, root: PurePath = _SRC) -> str:
    """*path* relative to *root*, spelled with forward slashes.

    Every structural assertion below compares a set of module paths
    against POSIX literals. `str(Path)` renders the *host* separator, so
    the same code that passes on Linux and macOS builds
    `providers\\_litellm_import.py` on the Windows runner and never
    matches. `as_posix` is the one spelling that means the same thing on
    every platform, and it is spelled here once so a new structural
    check cannot reintroduce the difference.
    """
    return path.relative_to(root).as_posix()


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


def test_module_ids_are_spelled_with_forward_slashes_on_every_platform() -> None:
    """The Windows failure, reproduced on every runner.

    `str(Path)` renders the host separator, so the two structural checks
    below built `providers\\_litellm_import.py` on the Windows job and
    compared it against a POSIX literal. Driving the shared helper with
    a `PureWindowsPath` reproduces that without a Windows host.
    """
    root = PureWindowsPath(r"C:\repo\src\korvid")
    module = root / "providers" / "_litellm_import.py"

    assert _module_id(module, root) == "providers/_litellm_import.py"
    assert "\\" not in _module_id(module, root)


def test_exactly_one_korvid_module_imports_litellm() -> None:
    """The env var that makes the import offline has to be set in a file
    that runs first — an import sorter would reorder a plain top-level
    `import litellm` above any `korvid` import in the same block.
    """
    offenders = {
        _module_id(path)
        for path in sorted(_SRC.rglob("*.py"))
        if any(
            name == "litellm" or name.startswith("litellm.")
            for name in _imported_module_names(path)
        )
    }
    assert offenders == {"providers/_litellm_import.py"}


def test_exactly_one_korvid_module_imports_the_wrapper() -> None:
    importers = {
        _module_id(path)
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


def test_no_device_login_prefix_is_offered_when_nothing_serves_it() -> None:
    """The exclusion is the deny-list's, not one vendor's.

    Every prefix in `DEVICE_LOGIN_PREFIXES` resolves through an
    interactive login, so with no flow installed the catalog must offer
    none of their ids under any spelling. Offering one would put a
    blocking device-code poll behind a keystroke in the picker, and the
    factory would refuse the saved profile afterwards anyway.
    """
    from korvid.providers.litellm_settings import DEVICE_LOGIN_PREFIXES
    from korvid.providers.special_flows import normalize_prefix

    published = {
        provider
        for provider in models_by_provider()
        if normalize_prefix(provider) in {normalize_prefix(p) for p in DEVICE_LOGIN_PREFIXES}
    }
    assert published, "litellm publishes no device-login provider; the exclusion is dead code"

    catalog = LiteLLMModelCatalog()
    offenders = {
        entry.reference
        for provider in published
        for entry in catalog.search(provider, limit=500)
        if normalize_prefix(entry.reference.split("/", 1)[0]) == normalize_prefix(provider)
    }
    assert offenders == set()


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


class _RecordingDiscovery:
    """Records the prefix `discover` resolved, and lists nothing."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_models(
        self, *, base_url: str, api_key: str | None, prefix: str
    ) -> tuple[ModelEntry, ...]:
        self.calls.append(prefix)
        return ()


async def test_discovery_uses_the_profile_prefix() -> None:
    discovery = _RecordingDiscovery()
    catalog = LiteLLMModelCatalog(discovery=cast("Any", discovery))

    await catalog.discover(
        ModelConnectionConfig(model="hosted_vllm/qwen", endpoint="https://gpu.internal/v1")
    )

    assert discovery.calls == ["hosted_vllm"]


async def test_a_prefixless_reference_is_not_discovered_under_an_invented_vendor() -> None:
    """A bare reference names no provider, and korvid must not pick one.

    Labelling an unknown endpoint's models `openai/...` writes a provider
    prefix the operator never chose into every entry the wizard offers,
    and the resulting reference points at a vendor that may have nothing
    to do with the endpoint. Discovering nothing is the honest answer.
    """
    discovery = _RecordingDiscovery()
    catalog = LiteLLMModelCatalog(discovery=cast("Any", discovery))

    assert (
        await catalog.discover(
            ModelConnectionConfig(model="bare-model", endpoint="https://gpu.internal/v1")
        )
        == ()
    )
    assert discovery.calls == []


# ---------------------------------------------------------------------------
# Task 7 — models.dev enrichment and provenance
# ---------------------------------------------------------------------------


class _FakeMetadataSource(ModelMetadataSource):
    """Minimal stub of `ModelMetadataSource` for catalog overlay tests."""

    def __init__(self, entries: dict[str, ModelMetadata]) -> None:
        self._entries = entries

    def metadata(self, reference: str) -> ModelMetadata | None:
        return self._entries.get(reference)

    def env_hints(self, provider_id: str) -> tuple[str, ...]:
        return ()

    async def refresh(self, *, force: bool = False) -> RefreshOutcome:
        return RefreshOutcome.CACHED


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


# ---------------------------------------------------------------------------
# Task 17 — sign-in delegated to the claiming flow
# ---------------------------------------------------------------------------


async def test_begin_auth_delegates_to_the_claiming_flow() -> None:
    """The catalog owns no vendor knowledge. A device login belongs to the
    flow that declared it, and the wizard only ever talks to the catalog."""
    from korvid.agent.model_profiles import DeviceLoginPrompt
    from korvid.providers.special_flows import SpecialFlowRegistry

    seen: list[ModelConnectionConfig] = []
    prompt = DeviceLoginPrompt(
        user_code="ABCD-1234", verification_uri="https://host/login", expires_in_seconds=900
    )

    async def _begin(profile: ModelConnectionConfig) -> DeviceLoginPrompt:
        seen.append(profile)
        return prompt

    registry = SpecialFlowRegistry([_make_flow("acme", begin_auth=_begin)])
    catalog = LiteLLMModelCatalog(flows=registry)
    profile = ModelConnectionConfig(model="acme/model")

    assert await catalog.begin_auth(profile) is prompt
    assert seen == [profile]


async def test_finish_auth_delegates_to_the_claiming_flow() -> None:
    """What comes back is the credential *key* the profile will name — a
    token would put a secret on the wizard's screen and in its state."""
    from korvid.providers.special_flows import SpecialFlowRegistry

    async def _finish(profile: ModelConnectionConfig) -> str:
        return "github-oauth"

    registry = SpecialFlowRegistry([_make_flow("acme", finish_auth=_finish)])
    catalog = LiteLLMModelCatalog(flows=registry)

    assert await catalog.finish_auth(ModelConnectionConfig(model="acme/model")) == "github-oauth"


async def test_a_reference_no_flow_claims_needs_no_sign_in() -> None:
    """`None` is the wizard's signal to skip the stage; an exception would
    make every ordinary API-key profile look broken."""
    from korvid.providers.special_flows import SpecialFlowRegistry

    catalog = LiteLLMModelCatalog(flows=SpecialFlowRegistry([_make_flow("acme")]))
    profile = ModelConnectionConfig(model="openai/gpt-4o")

    assert await catalog.begin_auth(profile) is None
    assert await catalog.finish_auth(profile) is None


async def test_a_flow_that_declares_no_sign_in_is_not_invented() -> None:
    from korvid.providers.special_flows import SpecialFlowRegistry

    catalog = LiteLLMModelCatalog(flows=SpecialFlowRegistry([_make_flow("acme")]))

    assert await catalog.begin_auth(ModelConnectionConfig(model="acme/model")) is None
    assert await catalog.finish_auth(ModelConnectionConfig(model="acme/model")) is None


# ---------------------------------------------------------------------------
# Task 17 review round 2 — an option flow's sign-in follows the option
# ---------------------------------------------------------------------------
#
# `litellm_factory._claim` refuses to answer a bare `registry.claim()` hit
# whose flow declares `claims_option`: such a flow *shares* its prefix
# rather than owning it, so only `claim_by_option` may select it. The
# catalog drives the same flows through the wizard's sign-in stages, so it
# has to read a claim exactly the same way — otherwise the wizard runs a
# device login for a profile the factory will then build on the ordinary
# transport.


def _option_flow(**kwargs: object) -> object:
    """A flow that shares `ollama/` and activates on `native_thinking`."""
    from korvid.agent.model_profiles import SetupField, SetupFieldKind

    return _make_flow(
        "ollama",
        claims_option="native_thinking",
        option_fields=(
            SetupField(key="native_thinking", label="Native thinking", kind=SetupFieldKind.BOOLEAN),
        ),
        **kwargs,
    )


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({}, id="absent"),
        pytest.param({"native_thinking": False}, id="off"),
        pytest.param({"native_thinking": "true"}, id="a-truthy-string"),
        pytest.param({"native_thinking": 1}, id="a-truthy-int"),
    ],
)
async def test_an_option_flow_is_not_signed_into_when_the_option_is_not_on(
    options: dict[str, object],
) -> None:
    """The catalog must mirror the factory's claim semantics.

    A flow that declares `claims_option` shares a prefix the standard
    transport already routes. `registry.claim("ollama/x")` still resolves
    it — the wizard needs that to render the option's own fields — but
    answering the *sign-in* there would start a login for every ordinary
    `ollama/*` profile, including ones that never turned the option on
    and that the factory will build on the shared transport.
    """
    from korvid.agent.model_profiles import DeviceLoginPrompt
    from korvid.providers.special_flows import SpecialFlowRegistry

    started: list[ModelConnectionConfig] = []
    finished: list[ModelConnectionConfig] = []

    async def _begin(profile: ModelConnectionConfig) -> DeviceLoginPrompt:
        started.append(profile)
        return DeviceLoginPrompt(
            user_code="ABCD-1234", verification_uri="https://host/login", expires_in_seconds=900
        )

    async def _finish(profile: ModelConnectionConfig) -> str:
        finished.append(profile)
        return "some-credential"

    registry = SpecialFlowRegistry([_option_flow(begin_auth=_begin, finish_auth=_finish)])
    catalog = LiteLLMModelCatalog(flows=registry)
    profile = ModelConnectionConfig(model="ollama/qwen3:8b", options=options)

    assert await catalog.begin_auth(profile) is None
    assert await catalog.finish_auth(profile) is None
    assert started == [], "no login may be started for an option that is not on"
    assert finished == [], "no credential may be stored for an option that is not on"


async def test_an_option_flow_is_signed_into_once_the_option_is_on() -> None:
    """The narrowing must not remove the path the option exists for: with
    `native_thinking: true` the flow owns the reference, and its sign-in
    is the only one the wizard can offer."""
    from korvid.agent.model_profiles import DeviceLoginPrompt
    from korvid.providers.special_flows import SpecialFlowRegistry

    prompt = DeviceLoginPrompt(
        user_code="ABCD-1234", verification_uri="https://host/login", expires_in_seconds=900
    )
    seen: list[ModelConnectionConfig] = []

    async def _begin(profile: ModelConnectionConfig) -> DeviceLoginPrompt:
        seen.append(profile)
        return prompt

    async def _finish(profile: ModelConnectionConfig) -> str:
        seen.append(profile)
        return "some-credential"

    registry = SpecialFlowRegistry([_option_flow(begin_auth=_begin, finish_auth=_finish)])
    catalog = LiteLLMModelCatalog(flows=registry)
    profile = ModelConnectionConfig(model="ollama/qwen3:8b", options={"native_thinking": True})

    assert await catalog.begin_auth(profile) is prompt
    assert await catalog.finish_auth(profile) == "some-credential"
    assert seen == [profile, profile]


async def test_a_prefix_owning_flow_still_signs_in_without_any_option() -> None:
    """The narrowing is scoped to flows that declare `claims_option`. A
    flow that owns its prefix outright answers as it always did, whatever
    the profile's options say."""
    from korvid.agent.model_profiles import DeviceLoginPrompt
    from korvid.providers.special_flows import SpecialFlowRegistry

    prompt = DeviceLoginPrompt(
        user_code="ABCD-1234", verification_uri="https://host/login", expires_in_seconds=900
    )

    async def _begin(profile: ModelConnectionConfig) -> DeviceLoginPrompt:
        return prompt

    registry = SpecialFlowRegistry([_make_flow("acme", begin_auth=_begin)])
    catalog = LiteLLMModelCatalog(flows=registry)

    assert (
        await catalog.begin_auth(
            ModelConnectionConfig(model="acme/model", options={"native_thinking": False})
        )
        is prompt
    )


# ---------------------------------------------------------------------------
# The explicit metadata refresh — the production wiring for Task 7's source
# ---------------------------------------------------------------------------


class _RecordingMetadataSource(ModelMetadataSource):
    """A metadata source whose refresh is counted and scripted."""

    def __init__(
        self,
        outcome: RefreshOutcome = RefreshOutcome.UPDATED,
        *,
        after: dict[str, ModelMetadata] | None = None,
    ) -> None:
        self.calls = 0
        #: How each call asked to be served — one entry per `refresh`.
        self.forced: list[bool] = []
        self._outcome = outcome
        self._after = after or {}
        self._entries: dict[str, ModelMetadata] = {}

    def metadata(self, reference: str) -> ModelMetadata | None:
        return self._entries.get(reference)

    def env_hints(self, provider_id: str) -> tuple[str, ...]:
        return ()

    async def refresh(self, *, force: bool = False) -> RefreshOutcome:
        self.calls += 1
        self.forced.append(force)
        if self._outcome is RefreshOutcome.UPDATED:
            self._entries = dict(self._after)
        return self._outcome


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (RefreshOutcome.UPDATED, MetadataRefresh.UPDATED),
        (RefreshOutcome.NOT_MODIFIED, MetadataRefresh.UNCHANGED),
        (RefreshOutcome.CACHED, MetadataRefresh.CACHED),
        (RefreshOutcome.UNAVAILABLE, MetadataRefresh.UNAVAILABLE),
    ],
)
async def test_every_source_outcome_maps_to_the_neutral_vocabulary(
    outcome: RefreshOutcome, expected: MetadataRefresh
) -> None:
    """Each provider-layer outcome has exactly one operator-facing answer.

    A missing arm would either raise into the UI worker or report a
    refresh that did not happen as one that did.
    """
    source = _RecordingMetadataSource(outcome)
    catalog = LiteLLMModelCatalog(enrichment=source)

    result = await catalog.refresh_metadata()

    assert result is expected
    assert source.calls == 1


async def test_the_refresh_never_hands_a_provider_type_upward() -> None:
    """`RefreshOutcome` lives in `korvid.providers`, which `ui/` may not
    import. Returning one would make the UI's rendering depend on it."""
    catalog = LiteLLMModelCatalog(enrichment=_RecordingMetadataSource())

    result = await catalog.refresh_metadata()

    assert isinstance(result, MetadataRefresh)
    assert not isinstance(result, RefreshOutcome)


async def test_a_catalog_without_enrichment_reports_disabled_and_calls_nothing() -> None:
    """`agent.model_search.models_dev: false` (and a base install) leave the
    catalog with no source at all. The action must say so rather than
    pretending a refresh happened."""
    catalog = LiteLLMModelCatalog()

    assert await catalog.refresh_metadata() is MetadataRefresh.DISABLED


async def test_an_updated_refresh_is_visible_to_the_next_search() -> None:
    """The index is a cached property built from the enrichment overlay.

    Without invalidation the operator refreshes, is told it worked, and
    sees exactly the rows they saw before — the failure this action exists
    to avoid.
    """
    probe = LiteLLMModelCatalog()
    reference = probe.search("gpt", limit=1)[0].reference
    source = _RecordingMetadataSource(
        RefreshOutcome.UPDATED,
        after={reference: ModelMetadata(reference=reference, display_name="Refreshed Name")},
    )
    catalog = LiteLLMModelCatalog(enrichment=source)
    before = catalog.entry(reference)
    assert before is not None
    assert before.display_name != "Refreshed Name"

    assert await catalog.refresh_metadata() is MetadataRefresh.UPDATED

    after = catalog.entry(reference)
    assert after is not None
    assert after.display_name == "Refreshed Name"


async def test_an_unavailable_refresh_keeps_the_index_it_had() -> None:
    """A failed refresh is silent and total: nothing is dropped."""
    source = _RecordingMetadataSource(RefreshOutcome.UNAVAILABLE)
    catalog = LiteLLMModelCatalog(enrichment=source)
    before = catalog.search("gpt", limit=5)

    assert await catalog.refresh_metadata() is MetadataRefresh.UNAVAILABLE

    assert catalog.search("gpt", limit=5) == before


async def test_an_explicit_refresh_reaches_the_source_as_a_forced_one() -> None:
    """The operator's keypress has to survive the boundary.

    `ui/` cannot import `korvid.providers`, so the only way an explicit
    refresh can outrank the source's freshness window is if the catalog
    carries the request across. A catalog that dropped `force` would leave
    Ctrl-R answering "served from cache" for up to a day.
    """
    source = _RecordingMetadataSource(RefreshOutcome.NOT_MODIFIED)
    catalog = LiteLLMModelCatalog(enrichment=source)

    assert await catalog.refresh_metadata(force=True) is MetadataRefresh.UNCHANGED

    assert source.forced == [True]


async def test_a_default_refresh_leaves_the_freshness_window_in_place() -> None:
    """Anything korvid decides to refresh on its own keeps the TTL.

    `force` is opt-in at the call site: a future caller that is not an
    operator keypress inherits the bounded, cache-first behaviour rather
    than a fetch per call.
    """
    source = _RecordingMetadataSource(RefreshOutcome.CACHED)
    catalog = LiteLLMModelCatalog(enrichment=source)

    assert await catalog.refresh_metadata() is MetadataRefresh.CACHED

    assert source.forced == [False]


async def test_a_forced_refresh_without_a_source_still_reports_disabled() -> None:
    """Forcing cannot conjure a source an installation deliberately lacks."""
    catalog = LiteLLMModelCatalog()

    assert await catalog.refresh_metadata(force=True) is MetadataRefresh.DISABLED
