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
