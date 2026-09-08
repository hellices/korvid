"""Special-flow registry — selected-only lazy loading over the `korvid.provider` entry-point group.

A special flow is the one concession to reality: it is data that claims
exactly one reference prefix (or one named boolean option), and it may
only supply what LiteLLM structurally cannot. The registry is deliberately
shaped so it cannot grow back into a provider list:

- No enumeration API (a test asserts that).
- An empty registry is fully functional (a test asserts that too).
- Loading is selected-only and lazy: `from_entry_points()` reads entry-point
  *names* only; `claim()` calls `EntryPoint.load()` for exactly the one
  entry point that matches, the first time it is claimed.
"""

from __future__ import annotations

import importlib.metadata
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from korvid.agent.model_profiles import SpecialFlow, split_reference
from korvid.providers.litellm_settings import (
    DEVICE_LOGIN_PREFIXES,
    RESERVED_PROVIDER_NAMES,
    RETIRED_PROVIDER_ALIASES,
)

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP: str = "korvid.provider"

#: korvid's own distribution name, normalized. Entry points shipped by it
#: are exempt from the reserved-prefix rule (see `is_korvids_own`).
_OWN_DISTRIBUTION: str = "korvid"

# Applied to the *declared* (un-normalized) spelling.
_PREFIX_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Normalized retired aliases — claimable by no one, so operators can never
# be confused by a third party squatting a name they read as korvid's own.
_FORBIDDEN_PREFIXES: frozenset[str] = frozenset(
    raw.lower().replace("_", "-") for raw in RETIRED_PROVIDER_ALIASES
)

#: Prefixes no flow may hand back to the standard transport, whatever it
#: declares. A retired alias must stay unroutable, and a device-login
#: prefix starts an interactive login inside the SDK's own routing call.
_ALWAYS_CLAIMED: frozenset[str] = _FORBIDDEN_PREFIXES | frozenset(
    prefix.lower().replace("_", "-") for prefix in DEVICE_LOGIN_PREFIXES
)

#: Names korvid ships a route or a flow for, normalized. A third party
#: may not register one; korvid's own distribution still may, and every
#: one of them stays routable — this decides who may *declare* a prefix,
#: never whether korvid will dispatch it. Held here rather than inferred
#: from the standard transport's published table, because that table is
#: a vendor's release artefact and a dropped row would silently hand an
#: operator's `provider: openai` to whoever registered the entry point.
_RESERVED_NAMES: frozenset[str] = frozenset(
    name.lower().replace("_", "-") for name in RESERVED_PROVIDER_NAMES
)


def normalize_prefix(prefix: str) -> str:
    """The canonical spelling of a reference prefix.

    Lowercased, with `_` folded to `-`. LiteLLM's own tables publish
    `github_copilot/...` while korvid's flow claims `github-copilot/`; if
    those two do not fold together, the underscore spelling is unclaimed,
    falls through to `get_llm_provider`, and starts the interactive device
    login the registry exists to prevent.
    """
    return prefix.strip().lower().replace("_", "-")


def _iter_entry_points() -> Iterable[importlib.metadata.EntryPoint]:
    """Enumerate the `korvid.provider` entry-point group without loading anything.

    Module-level so tests can substitute it via `monkeypatch.setattr`.
    """
    try:
        return importlib.metadata.entry_points(group=_ENTRY_POINT_GROUP)
    except Exception:  # metadata read can fail for any reason
        return ()


def is_korvids_own(entry_point: importlib.metadata.EntryPoint) -> bool:
    """Was this entry point declared by korvid's own distribution?

    The reserved-prefix rule protects routing from third parties, not
    korvid from itself: the references korvid's own flows claim are
    exactly the ones the SDK would otherwise route into an interactive
    device login. The exemption is distribution *identity*, never the
    flow's own say-so, so a plugin cannot buy it by choosing a name.

    Public within `providers/` because `provider_default.py` makes the
    same exemption on the same reserved set, and two copies of a security
    check are two checks that will eventually differ.
    """
    try:
        name = getattr(getattr(entry_point, "dist", None), "name", None)
    except Exception:  # metadata read can fail for any reason
        return False
    if not isinstance(name, str):
        return False
    return name.strip().lower().replace("_", "-") == _OWN_DISTRIBUTION


def _load_declared_flow(
    entry_point: importlib.metadata.EntryPoint,
) -> SpecialFlow | Exception:
    """Load one selected entry point and extract its first declared flow."""
    try:
        obj = entry_point.load()
        if isinstance(obj, SpecialFlow):
            return obj
        factory = getattr(obj, "korvid_special_flows", None)
        if callable(factory):
            for candidate in factory():
                if isinstance(candidate, SpecialFlow):
                    return candidate
    except Exception as exc:
        return exc
    return ValueError("no SpecialFlow found in loaded object")


class SpecialFlowRegistry:
    """Loads `SpecialFlow` declarations from the `korvid.provider` entry-point group.

    Loading is **selected-only and lazy**: construction reads entry-point
    *names* from installed distribution metadata and calls `EntryPoint.load()`
    for nothing. A name is loaded the first time a reference resolving to it
    is claimed, and only that one. Loading every declared entry point at
    construction would execute arbitrary third-party module-level code on
    every korvid startup and let one broken plugin break TUI wiring.

    Declared prefixes are stored normalized, so two flows differing only in
    separator collide and the second is rejected rather than silently shadowing
    the first.
    """

    def __init__(self, flows: Sequence[Any] = ()) -> None:
        # {normalized_prefix: SpecialFlow}
        self._claims: dict[str, SpecialFlow] = {}
        self._errors: list[str] = []
        # For entry-point lazy loading: {normalized_name: EntryPoint}
        self._ep_map: dict[str, importlib.metadata.EntryPoint] = {}
        # {normalized_name: SpecialFlow | Exception} — memoized load results
        self._loaded: dict[str, SpecialFlow | Exception] = {}
        # Prefixes the standard transport publishes and routes on its own;
        # filled in by `from_entry_points`. See `_falls_back_to_the_transport`.
        self._routable_prefixes: frozenset[str] = frozenset()

        for item in flows:
            self._register(item)

    def _register(self, item: object) -> None:
        """Validate and register one flow declaration."""
        try:
            if not isinstance(item, SpecialFlow):
                self._errors.append(f"rejected a non-SpecialFlow object: {type(item).__name__!r}")
                return
            declared = item.prefix
        except Exception as exc:  # third-party code can raise anything
            self._errors.append(f"flow raised on access: {type(exc).__name__}")
            return

        if not _PREFIX_PATTERN.match(declared):
            self._errors.append(
                f"flow prefix {declared!r} is not a valid reference prefix (pattern: [a-z0-9][a-z0-9_-]*)"
            )
            return

        normalized = normalize_prefix(declared)
        if normalized in _FORBIDDEN_PREFIXES:
            self._errors.append(
                f"flow prefix {declared!r} (normalized: {normalized!r}) is a retired or reserved name"
            )
            return

        if normalized in self._claims:
            self._errors.append(
                f"flow prefix {normalized!r} already claimed; second declaration ignored"
            )
            return

        self._claims[normalized] = item

    @classmethod
    def from_entry_points(cls, *, reserved_prefixes: Iterable[str] = ()) -> SpecialFlowRegistry:
        """Build from entry-point **names only**; load nothing yet.

        *reserved_prefixes* is the standard transport's own provider
        table. It does two jobs: a third party may not shadow a name in
        it, and a name in it is one korvid can hand back to routing if
        the flow sharing it turns out not to be loadable.

        `_ALWAYS_CLAIMED` and `_RESERVED_NAMES` are checked here too, and
        not left to overlap with *reserved_prefixes* by luck. Every name
        on them is one korvid must keep away from a third party whatever
        the transport publishes — a retired alias an operator still reads
        as korvid's own, a prefix whose routing starts an interactive
        device login, and a name korvid ships a route or a flow for.
        `github_copilot`, `openai`, `azure`, `anthropic` and `ollama` all
        happen to be in `models_by_provider()` today; the refusal must
        not be a consequence of that.

        The two lists are not interchangeable. `_ALWAYS_CLAIMED` also
        keeps its names away from *routing*; `_RESERVED_NAMES` never
        does, so `openai/gpt-4o` stays dispatchable while `openai`
        remains unregistrable by anyone but korvid.
        """
        registry = cls()
        reserved = {normalize_prefix(prefix) for prefix in reserved_prefixes}
        registry._routable_prefixes = frozenset(reserved)
        exclusive = reserved | _ALWAYS_CLAIMED | _RESERVED_NAMES

        for ep in _iter_entry_points():
            try:
                name = ep.name
            except Exception:
                continue
            normalized = normalize_prefix(name)
            if normalized in _FORBIDDEN_PREFIXES or (
                normalized in exclusive and not is_korvids_own(ep)
            ):
                registry._errors.append(
                    f"entry-point prefix {name!r} (normalized: {normalized!r}) is reserved"
                )
                continue
            if normalized not in registry._ep_map:
                registry._ep_map[normalized] = ep

        return registry

    def _falls_back_to_the_transport(self, normalized: str) -> bool:
        """Would this prefix still be served if its flow were missing?

        Only if the standard transport publishes it — a flow can *share*
        a prefix, never invent one for the transport — and only if it is
        not on the list that must never reach routing at all.
        """
        return normalized in self._routable_prefixes and normalized not in _ALWAYS_CLAIMED

    def _fail_load(
        self, normalized: str, name: str, result: Exception, reason: str
    ) -> SpecialFlow | None:
        """Memoize a failed load, report it, and name what it costs.

        A flow that could not be loaded claims only what it must: a
        prefix it *shared* with the standard transport goes back to being
        routed, because refusing it would disable every ordinary
        reference under that prefix over an optional module that raised.
        A prefix nothing else can serve — a retired alias, a device-login
        trap, a name the transport does not publish — stays claimed and
        is refused.

        Reported either way: the result is memoized, so a repeated claim
        neither reloads nor re-reports.
        """
        self._loaded[normalized] = result
        self._errors.append(f"entry point {name!r} {reason}")
        logger.warning(
            "special flow %r %s; references under that prefix %s",
            name,
            reason,
            "fall back to the standard transport"
            if self._falls_back_to_the_transport(normalized)
            else "are refused",
        )
        return None

    def _load_ep(self, normalized: str) -> SpecialFlow | None:
        """Load the entry point for *normalized* if not yet loaded.

        Returns the flow on success, None on failure. Memoizes both.
        """
        if normalized in self._loaded:
            result = self._loaded[normalized]
            return result if isinstance(result, SpecialFlow) else None

        ep = self._ep_map.get(normalized)
        if ep is None:
            return None

        loaded = _load_declared_flow(ep)
        if isinstance(loaded, Exception):
            return self._fail_load(
                normalized, ep.name, loaded, f"raised on load: {type(loaded).__name__}"
            )

        flow = loaded
        if normalize_prefix(flow.prefix) != normalized:
            return self._fail_load(
                normalized,
                ep.name,
                ValueError("entry-point prefix mismatch"),
                f"returned flow prefix {flow.prefix!r}",
            )
        self._loaded[normalized] = flow
        # Register it properly (validates prefix etc.)
        before = len(self._errors)
        self._register(flow)
        if len(self._errors) > before:
            # Validation rejected it — do not expose. `_register` already
            # said why, so this only records that nothing was loaded.
            self._loaded[normalized] = ValueError("rejected after load")
            return None
        return self._claims.get(normalize_prefix(flow.prefix))

    def claim(self, reference: str) -> SpecialFlow | None:
        """The flow owning this reference's prefix, or None.

        Normalizes the prefix first, then loads the one matching entry
        point if it has not been loaded yet.
        """
        prefix, _ = split_reference(reference)
        if not prefix:
            return None
        normalized = normalize_prefix(prefix)

        # Already registered from constructor-time flows
        if normalized in self._claims:
            return self._claims[normalized]

        # Lazy-load from entry points
        return self._load_ep(normalized)

    def claim_by_option(self, reference: str, options: Mapping[str, object]) -> SpecialFlow | None:
        """A flow that shares a reference but activates on a named boolean option.

        The option value must be strictly `True` (not just truthy) to prevent a
        truthy string from silently switching transports.
        """
        prefix, _ = split_reference(reference)
        if not prefix:
            return None
        normalized = normalize_prefix(prefix)

        for flow in self._claims.values():
            if (
                flow.claims_option is not None
                and normalize_prefix(flow.prefix) == normalized
                and options.get(flow.claims_option) is True
            ):
                return flow

        # Also check lazy-loaded entry points for the prefix
        candidate = self._load_ep(normalized)
        if (
            candidate is not None
            and candidate.claims_option is not None
            and options.get(candidate.claims_option) is True
        ):
            return candidate

        return None

    @property
    def claimed_prefixes(self) -> frozenset[str]:
        """Every normalized prefix that is claimed, whatever is installed.

        Declared flows, entry-point names, the retired aliases and the
        device-login prefixes. The last group is why an *empty* registry
        still claims: those references start an interactive login inside
        LiteLLM's own routing call, so the factory has to be able to
        refuse one before it routes, and it must still refuse when the
        flow that serves it was never installed.

        Two kinds of prefix are subtracted, and both are prefixes the
        standard transport already serves:

        - one whose only claim is a named *option*: the flow shares the
          prefix rather than owning it, and a shared prefix in this set
          would make the option permanently on — the factory refuses a
          claimed prefix nothing served;
        - one whose entry point was loaded and *failed*: nothing is left
          to serve the option, so the ordinary references under it go
          back to being routed instead of being disabled by an optional
          module that raised.

        Neither subtraction can touch `_ALWAYS_CLAIMED`, so a retired
        alias and a device-login trap stay refused however badly a flow
        misbehaves.

        Available *without* loading anything.
        """
        shared = {
            prefix
            for prefix, flow in self._known_flows()
            if flow.claims_option is not None and prefix not in _ALWAYS_CLAIMED
        }
        return (
            (
                frozenset(self._claims.keys())
                | frozenset(self._ep_map.keys())
                | frozenset(normalize_prefix(a) for a in RETIRED_PROVIDER_ALIASES)
                | frozenset(normalize_prefix(p) for p in DEVICE_LOGIN_PREFIXES)
            )
            - shared
            - self._unloadable_prefixes()
        )

    def _unloadable_prefixes(self) -> frozenset[str]:
        """Prefixes whose flow was loaded, failed, and has a fallback."""
        return frozenset(
            prefix
            for prefix, result in self._loaded.items()
            if isinstance(result, Exception) and self._falls_back_to_the_transport(prefix)
        )

    def _known_flows(self) -> Iterable[tuple[str, SpecialFlow]]:
        """Every flow already registered or loaded, without loading more."""
        seen: dict[str, SpecialFlow] = dict(self._claims)
        for prefix, result in self._loaded.items():
            if isinstance(result, SpecialFlow):
                seen.setdefault(prefix, result)
        return tuple(seen.items())

    @property
    def errors(self) -> tuple[str, ...]:
        """Human-readable rejection reasons, for the setup UI's banner."""
        return tuple(self._errors)
