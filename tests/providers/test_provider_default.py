"""`provider-default` must be able to resolve through a declared chain.

The measurement this whole module exists for, taken against
litellm 1.98.0 with every `AZURE_*` variable unset:

    initialize_azure_sdk_client(api_key=None, ...)
        -> {'api_key': None, 'azure_ad_token': None,
            'azure_ad_token_provider': None}

An `azure/...` profile with `auth.method: provider-default` therefore
authenticates with nothing at all, while `docs/agent.md` promises "Entra
ID - `az login` or managed identity". LiteLLM does reach
`DefaultAzureCredential`, but only behind the module-global
`litellm.enable_azure_ad_token_refresh`, which defaults to `False` and
would apply to every profile in the process at once.

So korvid declares the chain instead, as data, keyed by reference prefix.
The factory asks a registry; it never names a vendor.
"""

from __future__ import annotations

import pytest

from korvid.providers.provider_default import (
    _NO_PARAMETERS,
    CredentialUnavailable,
    ProviderDefaultCredential,
    ProviderDefaultRegistry,
    ResolvedCredential,
)


def _declaration(prefix: str, **kwargs: object) -> ProviderDefaultCredential:
    resolve = kwargs.pop("resolve", None)
    return ProviderDefaultCredential(
        prefix=prefix,
        display_name=kwargs.pop("display_name", prefix),  # type: ignore[arg-type]  # test builder
        resolve=resolve or (lambda: ResolvedCredential(parameters={"token": prefix})),  # type: ignore[arg-type]  # test builder
        **kwargs,  # type: ignore[arg-type]  # test builder
    )


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_an_empty_registry_answers_for_every_reference() -> None:
    """Nothing declared is the normal case, not a degraded one.

    Every profile that is not covered by a declaration keeps today's
    behaviour: `provider-default` omits the credential and the transport
    consults its own chain.
    """
    registry = ProviderDefaultRegistry()

    assert registry.resolve("openai/gpt-4o") is None
    assert registry.errors == ()


def test_a_declaration_answers_only_for_its_own_prefix() -> None:
    declared = _declaration("azure")
    registry = ProviderDefaultRegistry((declared,))

    assert registry.resolve("azure/gpt-4o") is declared
    assert registry.resolve("openai/gpt-4o") is None
    assert registry.resolve("gpt-4o") is None


@pytest.mark.parametrize("reference", ["Azure/x", "AZURE/x", "azure/x"])
def test_a_declaration_folds_case_and_separators(reference: str) -> None:
    """The same folding `special_flows` uses, for the same reason: a
    prefix that does not fold is a prefix the declaration never sees."""
    declared = _declaration("azure")
    registry = ProviderDefaultRegistry((declared,))

    assert registry.resolve(reference) is declared


def test_a_malformed_declaration_disables_only_itself() -> None:
    good = _declaration("acme")
    registry = ProviderDefaultRegistry((object(), _declaration("Not A Prefix"), good))

    assert registry.resolve("acme/x") is good
    assert len(registry.errors) == 2


def test_the_second_declaration_of_a_prefix_is_refused() -> None:
    """Two chains for one prefix is ambiguity, and the wrong answer to it
    is silently picking one: whichever loads first would decide which
    credential an operator's profile authenticates with."""
    first = _declaration("acme")
    registry = ProviderDefaultRegistry((first, _declaration("acme")))

    assert registry.resolve("acme/x") is first
    assert any("already" in message for message in registry.errors)


def test_the_registry_is_not_a_provider_list() -> None:
    """No enumeration API. A credential registry that can be listed is a
    vendor table waiting to be rendered in a picker."""
    for name in ("declarations", "prefixes", "all", "items", "keys"):
        assert not hasattr(ProviderDefaultRegistry(), name)


# ---------------------------------------------------------------------------
# Entry points: selected-only, lazy, and reserved-name aware
# ---------------------------------------------------------------------------


class _EntryPoint:
    def __init__(self, name: str, module: object, distribution: str | None) -> None:
        self.name = name
        self.group = "korvid.credential"
        self._module = module
        self.dist = None if distribution is None else _Distribution(distribution)
        self.loaded = 0

    def load(self) -> object:
        self.loaded += 1
        return self._module


class _Distribution:
    def __init__(self, name: str) -> None:
        self.name = name


class _Module:
    def __init__(self, *declarations: ProviderDefaultCredential) -> None:
        self._declarations = declarations

    def korvid_provider_default_credentials(self) -> tuple[ProviderDefaultCredential, ...]:
        return self._declarations


def test_only_the_resolved_entry_point_is_ever_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loading every declared entry point at construction would run
    arbitrary third-party module code on every korvid start."""
    wanted = _EntryPoint("acme", _Module(_declaration("acme")), "acme-korvid-creds")
    other = _EntryPoint("other", _Module(_declaration("other")), "other-korvid-creds")
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (wanted, other),
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert wanted.loaded == 0
    assert registry.resolve("acme/x") is not None
    assert wanted.loaded == 1
    assert other.loaded == 0


def test_a_third_party_cannot_declare_a_reserved_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sharpest rule on this boundary.

    A declaration contributes call parameters to every request an
    operator's profile makes under its prefix. A third party that could
    declare one for `openai` would be handed the traffic of a profile it
    has nothing to do with, which is the credential-interception risk
    `special_flows` already refuses for prefixes — the same reserved set
    decides it here.
    """
    entry = _EntryPoint("openai", _Module(_declaration("openai")), "acme-korvid-creds")
    monkeypatch.setattr("korvid.providers.provider_default._iter_entry_points", lambda: (entry,))

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("openai/gpt-4o") is None
    assert any("reserved" in message for message in registry.errors)
    assert entry.loaded == 0


def test_korvids_own_distribution_may_declare_a_reserved_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exemption is distribution identity, never the declaration's own
    say-so — the same rule, and the same reason, as for special flows."""
    declared = _declaration("azure")
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (_EntryPoint("azure", _Module(declared), "korvid"),),
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("azure/gpt-4o") is declared
    assert registry.errors == ()


def test_an_entry_point_that_raises_on_load_disables_only_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Exploding:
        name = "acme"
        group = "korvid.credential"
        dist = _Distribution("acme-korvid-creds")

        def load(self) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points", lambda: (_Exploding(),)
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("acme/x") is None
    assert any("acme" in message for message in registry.errors)


def test_a_failed_load_is_reported_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Exploding:
        name = "acme"
        group = "korvid.credential"
        dist = _Distribution("acme-korvid-creds")

        def load(self) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points", lambda: (_Exploding(),)
    )
    registry = ProviderDefaultRegistry.from_entry_points()

    registry.resolve("acme/x")
    registry.resolve("acme/y")

    assert len(registry.errors) == 1


def test_a_declaration_whose_prefix_disagrees_with_its_name_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry-point name is what the registry resolves without loading
    anything, so a declaration that answers under a different prefix would
    make the two disagree about what is covered."""
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (_EntryPoint("acme", _Module(_declaration("other")), "acme-korvid-creds"),),
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("acme/x") is None
    assert registry.errors


# ---------------------------------------------------------------------------
# ResolvedCredential
# ---------------------------------------------------------------------------


def test_a_resolved_credential_defaults_to_owning_nothing() -> None:
    resolved = ResolvedCredential(parameters={"a": 1})

    assert dict(resolved.parameters) == {"a": 1}
    assert resolved.aclose is None


def test_credential_unavailable_carries_an_operator_facing_message() -> None:
    with pytest.raises(CredentialUnavailable, match="install the extra"):
        raise CredentialUnavailable("install the extra")


def test_a_module_declaring_several_chains_answers_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry-point name is the resolution key, so it decides.

    A module may declare more than one chain. Taking the first would let
    the *order of a tuple in third-party code* pick which credential an
    operator's profile authenticates with, while the registry went on
    reporting the entry-point name as what it covers.
    """
    wanted = _declaration("acme")
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (
            _EntryPoint(
                "acme",
                _Module(_declaration("other"), wanted),
                "acme-korvid-creds",
            ),
        ),
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("acme/x") is wanted
    assert registry.errors == ()


def test_an_entry_point_may_load_a_declaration_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module attribute that *is* a declaration is accepted, the same
    way a `SpecialFlow` object is on the flow group."""
    declared = _declaration("acme")
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (_EntryPoint("acme", declared, "acme-korvid-creds"),),
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("acme/x") is declared


def test_an_entry_point_that_declares_nothing_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (_EntryPoint("acme", object(), "acme-korvid-creds"),),
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("acme/x") is None
    assert any("declares no ProviderDefaultCredential" in message for message in registry.errors)
    assert not any("raised" in message for message in registry.errors), (
        "a package that declares nothing did not raise; saying so sends the "
        "operator looking for a traceback that does not exist"
    )


def test_an_entry_point_whose_name_cannot_be_read_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distribution metadata can be unreadable for any reason, and one
    broken package must not cost every other package its declaration."""

    class _Unreadable:
        group = "korvid.credential"

        @property
        def name(self) -> str:
            raise RuntimeError("unreadable metadata")

    good = _EntryPoint("acme", _Module(_declaration("acme")), "acme-korvid-creds")
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (_Unreadable(), good),
    )

    registry = ProviderDefaultRegistry.from_entry_points()

    assert registry.resolve("acme/x") is not None


def test_an_object_pretending_to_be_a_declaration_is_rejected_by_type() -> None:
    """Duck typing is not enough here.

    A declaration is consulted for a credential, so it is checked by type
    before its `prefix` is read at all — an object that merely *looks*
    like one never reaches the registry's prefix table.
    """

    class _Hostile:
        @property
        def prefix(self) -> str:
            raise RuntimeError("boom")

    good = _declaration("acme")
    registry = ProviderDefaultRegistry((_Hostile(), good))

    assert registry.resolve("acme/x") is good
    assert any("non-ProviderDefaultCredential" in message for message in registry.errors)


def test_a_loaded_declaration_rejected_by_validation_is_not_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declaration already made by the composition root wins, and the
    entry point that duplicates it is reported rather than shadowing it."""
    wired = _declaration("acme")
    monkeypatch.setattr(
        "korvid.providers.provider_default._iter_entry_points",
        lambda: (_EntryPoint("acme", _Module(_declaration("acme")), "acme-korvid-creds"),),
    )

    registry = ProviderDefaultRegistry.from_entry_points()
    registry._register(wired)

    assert registry.resolve("acme/x") is wired


def test_the_default_parameters_are_the_one_shared_immutable_mapping() -> None:
    """A chain that contributes nothing gets the shared read-only mapping,
    not a fresh dict a later caller could fill in for everyone. The field
    reaches it through a factory because Python 3.11 refuses an unhashable
    constant as a dataclass default."""
    first = ResolvedCredential()
    second = ResolvedCredential()

    assert first.parameters is _NO_PARAMETERS
    assert second.parameters is _NO_PARAMETERS
    assert first.aclose is None
    with pytest.raises(TypeError, match="does not support item assignment"):
        first.parameters["api_key"] = "leaked"  # type: ignore[index]  # read-only by design


def test_explicit_parameters_still_replace_the_default() -> None:
    """The factory must not intercept a constructor argument."""
    resolved = ResolvedCredential(parameters={"azure_ad_token_provider": "callable"})

    assert resolved.parameters == {"azure_ad_token_provider": "callable"}
    assert resolved.parameters is not _NO_PARAMETERS
