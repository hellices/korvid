"""Resource metadata + API discovery (any kind incl. CRDs, spec §5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")


@dataclass(frozen=True)
class ResourceMeta:
    kind: str  # "Deployment"
    plural: str  # "deployments"
    group: str  # "" for core
    version: str  # "v1"
    namespaced: bool
    shortnames: tuple[str, ...] = ()
    #: korvid-invented view kinds (e.g. the helm browser) that have no API
    #: endpoint: navigation may use them, API-path consumers must not.
    synthetic: bool = False
    #: The real (plural, group) a synthetic view LISTs/WATCHes under the hood
    #: (e.g. the helm browser reads Secrets). Permission probes target this;
    #: None means the view has no backing API resource to probe.
    backing: tuple[str, str] | None = None
    #: False for kinds whose server offers `list` but not `watch` (aggregated
    #: APIs like OLM's packageserver, issue #141): the watch source keeps
    #: them fresh by periodic re-LIST diffing instead of a watch stream.
    watchable: bool = True

    @property
    def api_base(self) -> str:
        return f"/apis/{self.group}/{self.version}" if self.group else "/api/v1"

    @property
    def identity(self) -> tuple[str, str, bool]:
        """Resource identity, independent of aliases and served API version."""
        return self.group, self.plural, self.synthetic

    @property
    def qualified_name(self) -> str:
        """The unambiguous API name, or the name of a synthetic view."""
        return f"{self.plural}.{self.group}" if self.group else self.plural

    def configured_value(self, values: Mapping[str, _T]) -> _T | None:
        """Select identity-specific configuration before a plural-wide default."""
        return values.get(self.qualified_name, values.get(self.plural))


PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))


def build_alias_map(metas: list[ResourceMeta]) -> dict[str, ResourceMeta]:
    """Index qualified resources and first-wins human-facing aliases."""
    aliases: dict[str, ResourceMeta] = {}
    for meta in metas:
        if meta.group and not meta.synthetic:
            aliases.setdefault(meta.qualified_name.lower(), meta)
    for meta in metas:
        for alias in (meta.plural, meta.kind.lower(), *meta.shortnames):
            aliases.setdefault(alias.lower(), meta)
    return aliases


def resolve_resource(
    aliases: Mapping[str, ResourceMeta],
    group: str,
    plural: str,
    *,
    synthetic: bool = False,
) -> ResourceMeta | None:
    """Find a discovered identity without trusting a colliding display alias."""
    identity = group, plural, synthetic
    qualified = f"{plural}.{group}" if group else plural
    candidate = aliases.get(qualified)
    if candidate is not None and candidate.identity == identity:
        return candidate
    return next((meta for meta in aliases.values() if meta.identity == identity), None)


def canonical_resource_alias(aliases: Mapping[str, ResourceMeta], meta: ResourceMeta) -> str:
    """Select one registered view key for a resource, preserving its identity."""
    for alias in (meta.plural, meta.qualified_name):
        candidate = aliases.get(alias)
        if candidate is not None and candidate.identity == meta.identity:
            return alias
    fallback = min(
        (alias for alias, candidate in aliases.items() if candidate.identity == meta.identity),
        default=None,
    )
    if fallback is not None:
        return fallback
    raise ValueError(f"Resource {meta.qualified_name!r} is not discovered")
