"""What `provider-default` resolves to, declared as data.

`auth.method: provider-default` means "let the transport use its own
credential chain". For most references that is exactly right and korvid's
whole contribution is to pass no `api_key`, so the SDK's own lookup is
not suppressed by an explicit argument.

For some it is not enough. Measured on litellm 1.98.0 with every
`AZURE_*` variable unset, an `azure/...` reference built with no
`api_key` resolves `azure_ad_token_provider` to `None`: the chain that
would reach `DefaultAzureCredential` is behind the module-global
`litellm.enable_azure_ad_token_refresh`, which defaults to `False`. A
global is the wrong instrument — it changes credential resolution for
every profile in the process at once, and when the credential library is
missing it surfaces as a bare `ImportError` from inside the SDK rather
than as korvid's install hint.

So a chain is *declared*, keyed by reference prefix, and korvid asks. The
registry is shaped like `special_flows.SpecialFlowRegistry` and for the
same reasons:

- No enumeration API (a test asserts that).
- An empty registry is fully functional (a test asserts that too).
- Loading is selected-only and lazy: `from_entry_points()` reads
  entry-point *names* only; `resolve()` loads exactly the one that
  matches, the first time it is asked for.
- A third party may not declare a reserved prefix. A declaration
  contributes call parameters to every request made under its prefix, so
  one for `openai` would intercept the credential of a profile it has
  nothing to do with.

Nothing here names a vendor. The one declaration korvid ships lives in
the module that owns that vendor's protocol.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from korvid.agent.model_profiles import split_reference
from korvid.providers.litellm_settings import RESERVED_PROVIDER_NAMES
from korvid.providers.special_flows import is_korvids_own, normalize_prefix

logger = logging.getLogger(__name__)

__all__ = [
    "CredentialUnavailable",
    "ProviderDefaultCredential",
    "ProviderDefaultRegistry",
    "ResolvedCredential",
]

_ENTRY_POINT_GROUP: str = "korvid.credential"

# Applied to the *declared* (un-normalized) spelling, exactly as a flow
# prefix is: a declaration and a flow name the same namespace.
_PREFIX_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_RESERVED: frozenset[str] = frozenset(normalize_prefix(name) for name in RESERVED_PROVIDER_NAMES)

#: The empty contribution, shared. Immutable, so a caller cannot turn the
#: default into a channel for the next call's parameters.
_NO_PARAMETERS: Mapping[str, object] = MappingProxyType({})


def _no_parameters() -> Mapping[str, object]:
    """Return the shared empty parameter mapping.

    A factory rather than the constant itself because CPython 3.11's
    `dataclasses` rejects any field default whose class has no `__hash__`,
    and `mappingproxy` gained one only in 3.12 — so the constant imports
    on a new interpreter and raises `ValueError` on the oldest supported
    one. The one shared object is returned rather than a fresh proxy: it
    is immutable, so sharing it is safe, and a default costs no allocation.
    """
    return _NO_PARAMETERS


class _NoDeclaration(ValueError):
    """The loaded object carried no declaration.

    A distinct type because the reason has to reach the operator: a
    package that declares nothing did not *raise*, and reporting it as
    `raised on load: ValueError` sends whoever installed it looking for a
    traceback that does not exist. Third-party exception *messages* are
    never reported — they are unbounded and may carry a secret — so only
    korvid's own marker is quoted.
    """


class CredentialUnavailable(Exception):
    """A declared chain cannot supply a credential right now.

    The message is operator-facing and is logged verbatim as the reason
    the profile was refused, so it must say what to do — an install hint,
    a sign-in command — and must never carry a secret.
    """


@dataclass(frozen=True, slots=True)
class ResolvedCredential:
    """What a declared chain contributes to the call, and what it owns.

    `parameters` are transport call parameters, not model parameters:
    they are merged into the request after the per-provider allowlist has
    run, because the allowlist lists none of them. They carry a
    *refreshing* credential — a callable the transport invokes per
    request — never a resolved secret, so nothing here has a lifetime the
    profile outlives.
    """

    parameters: Mapping[str, object] = field(default_factory=_no_parameters)
    #: Releases whatever the chain opened, or None when it opened nothing.
    #: Awaited by the provider's own `aclose`, so a rebuilt agent does not
    #: leak a credential's HTTP client.
    aclose: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True, slots=True)
class ProviderDefaultCredential:
    """A credential chain for `provider-default` on one reference prefix.

    Not a provider entry. It supplies no transport, claims no reference
    away from routing, and is consulted only when the operator's profile
    already says `provider-default` — it cannot turn an unauthenticated
    profile into an authenticated one behind their back.
    """

    prefix: str
    display_name: str
    #: Resolves the chain. Called once per provider build, never per
    #: request. Raises `CredentialUnavailable` when it cannot.
    resolve: Callable[[], ResolvedCredential]
    #: Documentation for the refusal message and the operator's config;
    #: never read as a routing decision.
    requires_extra: str | None = field(default=None)


def _iter_entry_points() -> Iterable[importlib.metadata.EntryPoint]:
    """Enumerate the `korvid.credential` group without loading anything.

    Module-level so tests can substitute it via `monkeypatch.setattr`.
    """
    try:
        return importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
    except Exception:  # metadata read can fail for any reason
        return ()


def _load_declaration(
    entry_point: importlib.metadata.EntryPoint, normalized: str
) -> ProviderDefaultCredential | Exception:
    """Load one selected entry point and take the declaration it registered.

    A module may declare more than one chain, so the declaration matching
    the entry-point *name* is preferred over the first one found: the name
    is what the registry resolved without importing anything, and picking
    a different declaration would make the two disagree about what is
    covered. The first is returned only when nothing matches, so the
    prefix check in `_load_ep` still reports the mismatch.
    """
    try:
        obj: Any = entry_point.load()
        if isinstance(obj, ProviderDefaultCredential):
            return obj
        factory = getattr(obj, "korvid_provider_default_credentials", None)
        if not callable(factory):
            return _NoDeclaration("declares no ProviderDefaultCredential")
        declared = [
            candidate for candidate in factory() if isinstance(candidate, ProviderDefaultCredential)
        ]
    except Exception as exc:  # third-party module code can raise anything
        return exc
    for candidate in declared:
        if normalize_prefix(candidate.prefix) == normalized:
            return candidate
    if declared:
        return declared[0]
    return _NoDeclaration("declares no ProviderDefaultCredential")


class ProviderDefaultRegistry:
    """Declared `provider-default` credential chains, resolved by prefix."""

    def __init__(self, declarations: Sequence[Any] = ()) -> None:
        self._declared: dict[str, ProviderDefaultCredential] = {}
        self._errors: list[str] = []
        self._ep_map: dict[str, importlib.metadata.EntryPoint] = {}
        self._loaded: dict[str, ProviderDefaultCredential | Exception] = {}
        for item in declarations:
            self._register(item)

    def _register(self, item: object) -> None:
        """Validate and register one declaration."""
        try:
            if not isinstance(item, ProviderDefaultCredential):
                self._errors.append(
                    f"rejected a non-ProviderDefaultCredential object: {type(item).__name__!r}"
                )
                return
            declared = item.prefix
        except Exception as exc:  # third-party code can raise anything
            self._errors.append(f"credential declaration raised on access: {type(exc).__name__}")
            return

        if not _PREFIX_PATTERN.match(declared):
            self._errors.append(
                f"credential prefix {declared!r} is not a valid reference prefix "
                f"(pattern: [a-z0-9][a-z0-9_-]*)"
            )
            return

        normalized = normalize_prefix(declared)
        if normalized in self._declared:
            self._errors.append(
                f"credential prefix {normalized!r} is already declared; "
                f"the second declaration is ignored"
            )
            return

        self._declared[normalized] = item

    @classmethod
    def from_entry_points(cls) -> ProviderDefaultRegistry:
        """Build from entry-point **names only**; load nothing yet.

        A reserved prefix is refused to every distribution but korvid's
        own, before the entry point is loaded. The refusal is decided
        here rather than borrowed from the transport's published provider
        table, for the reason `litellm_settings` states: that table is a
        vendor's release artefact.
        """
        registry = cls()
        for entry_point in _iter_entry_points():
            try:
                name = entry_point.name
            except Exception:  # metadata read can fail for any reason
                continue
            normalized = normalize_prefix(name)
            if normalized in _RESERVED and not is_korvids_own(entry_point):
                registry._errors.append(
                    f"credential entry point {name!r} (normalized: {normalized!r}) is reserved"
                )
                continue
            if normalized not in registry._ep_map:
                registry._ep_map[normalized] = entry_point
        return registry

    def resolve(self, reference: str) -> ProviderDefaultCredential | None:
        """The chain declared for this reference's prefix, or None.

        None is not a failure: it is the ordinary answer for every
        reference whose transport already resolves its own credentials,
        and the factory reads it as "pass no `api_key` and let the SDK
        look".
        """
        prefix, _tag = split_reference(reference)
        if not prefix:
            return None
        normalized = normalize_prefix(prefix)
        declared = self._declared.get(normalized)
        if declared is not None:
            return declared
        return self._load_ep(normalized)

    def _load_ep(self, normalized: str) -> ProviderDefaultCredential | None:
        """Load the entry point for *normalized* if it has not been."""
        if normalized in self._loaded:
            result = self._loaded[normalized]
            return result if isinstance(result, ProviderDefaultCredential) else None

        entry_point = self._ep_map.get(normalized)
        if entry_point is None:
            return None

        loaded = _load_declaration(entry_point, normalized)
        if isinstance(loaded, Exception):
            reason = (
                str(loaded)
                if isinstance(loaded, _NoDeclaration)
                else f"raised on load: {type(loaded).__name__}"
            )
            self._fail(normalized, entry_point.name, reason)
            return None
        if normalize_prefix(loaded.prefix) != normalized:
            self._fail(
                normalized,
                entry_point.name,
                f"declared prefix {loaded.prefix!r}, which is not the name it registered",
            )
            return None

        self._loaded[normalized] = loaded
        before = len(self._errors)
        self._register(loaded)
        if len(self._errors) > before:
            self._loaded[normalized] = ValueError("rejected after load")
            return None
        return self._declared.get(normalized)

    def _fail(self, normalized: str, name: str, reason: str) -> None:
        """Memoize a failed load and report it once.

        A chain that cannot be loaded declares nothing, which leaves
        `provider-default` doing what it does everywhere else: passing no
        `api_key` so the transport consults its own chain. That is a
        weaker credential, never a wrong one, and it is the honest
        outcome for an optional module that raised.
        """
        self._loaded[normalized] = ValueError(reason)
        self._errors.append(f"credential entry point {name!r} {reason}")
        logger.warning(
            "provider-default credential %r %s; the transport's own chain is used", name, reason
        )

    @property
    def errors(self) -> tuple[str, ...]:
        """Human-readable rejection reasons, for the setup UI's banner."""
        return tuple(self._errors)
