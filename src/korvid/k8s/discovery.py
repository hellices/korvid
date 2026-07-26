"""Resource metadata + API discovery (any kind incl. CRDs, spec §5)."""

from __future__ import annotations

from dataclasses import dataclass


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

    @property
    def api_base(self) -> str:
        return f"/apis/{self.group}/{self.version}" if self.group else "/api/v1"


PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))


def build_alias_map(metas: list[ResourceMeta]) -> dict[str, ResourceMeta]:
    """lowercase plural / kind / shortnames -> meta; first meta wins on conflict."""
    aliases: dict[str, ResourceMeta] = {}
    for meta in metas:
        for alias in (meta.plural, meta.kind.lower(), *meta.shortnames):
            aliases.setdefault(alias.lower(), meta)
    return aliases
