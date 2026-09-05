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
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from korvid.agent.model_profiles import SpecialFlow, split_reference
from korvid.providers.litellm_settings import DEVICE_LOGIN_PREFIXES, RETIRED_PROVIDER_ALIASES

_ENTRY_POINT_GROUP: str = "korvid.provider"

# Applied to the *declared* (un-normalized) spelling.
_PREFIX_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Normalized retired aliases — claimable by no one, so operators can never
# be confused by a third party squatting a name they read as korvid's own.
_FORBIDDEN_PREFIXES: frozenset[str] = frozenset(
    raw.lower().replace("_", "-") for raw in RETIRED_PROVIDER_ALIASES
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
        """Build from entry-point **names only**; load nothing yet."""
        registry = cls()
        forbidden = _FORBIDDEN_PREFIXES | {normalize_prefix(prefix) for prefix in reserved_prefixes}

        for ep in _iter_entry_points():
            try:
                name = ep.name
            except Exception:
                continue
            normalized = normalize_prefix(name)
            if normalized in forbidden:
                registry._errors.append(
                    f"entry-point prefix {name!r} (normalized: {normalized!r}) is reserved"
                )
                continue
            if normalized not in registry._ep_map:
                registry._ep_map[normalized] = ep

        return registry

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
            self._loaded[normalized] = loaded
            self._errors.append(f"entry point {ep.name!r} raised on load: {type(loaded).__name__}")
            return None

        flow = loaded
        if normalize_prefix(flow.prefix) != normalized:
            self._loaded[normalized] = ValueError("entry-point prefix mismatch")
            self._errors.append(f"entry point {ep.name!r} returned flow prefix {flow.prefix!r}")
            return None
        self._loaded[normalized] = flow
        # Register it properly (validates prefix etc.)
        before = len(self._errors)
        self._register(flow)
        if len(self._errors) > before:
            # Validation rejected it — do not expose
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

        Available *without* loading anything.
        """
        return (
            frozenset(self._claims.keys())
            | frozenset(self._ep_map.keys())
            | frozenset(normalize_prefix(a) for a in RETIRED_PROVIDER_ALIASES)
            | frozenset(normalize_prefix(p) for p in DEVICE_LOGIN_PREFIXES)
        )

    @property
    def errors(self) -> tuple[str, ...]:
        """Human-readable rejection reasons, for the setup UI's banner."""
        return tuple(self._errors)
