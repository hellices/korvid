"""Cloud provider detection from node metadata (issue #30).

Detection is dynamic — `spec.providerID` scheme prefix plus a handful of
well-known managed-distribution node labels. There is deliberately **no
annotation catalog** here: the detected provider only informs the agent
system context and a describe footer; the LLM supplies the CSP-specific
knowledge itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: providerID scheme -> canonical provider name.
_PROVIDER_SCHEMES: dict[str, str] = {
    "azure": "azure",
    "aws": "aws",
    "gce": "gcp",
    "openstack": "openstack",
    "vsphere": "vsphere",
    "digitalocean": "digitalocean",
    "hcloud": "hetzner",
    "oci": "oracle",
    "ibm": "ibm",
    "alicloud": "alibaba",
}

#: Managed-distribution node label -> (distribution, provider).
_MANAGED_LABELS: dict[str, tuple[str, str]] = {
    "kubernetes.azure.com/cluster": ("aks", "azure"),
    "eks.amazonaws.com/nodegroup": ("eks", "aws"),
    "cloud.google.com/gke-nodepool": ("gke", "gcp"),
}

UNKNOWN_PROVIDER = "unknown"


@dataclass(frozen=True)
class ProviderInfo:
    """Detected cloud provider for the connected cluster.

    Attributes:
        provider: Canonical provider name ("azure", "aws", "gcp", ...) or
            "unknown" when nothing recognizable was found.
        distribution: Managed distribution ("aks", "eks", "gke") when a
            well-known node label identified one, else None.
    """

    provider: str
    distribution: str | None

    @property
    def known(self) -> bool:
        """True when a provider was recognized."""
        return self.provider != UNKNOWN_PROVIDER

    @property
    def display(self) -> str:
        """Short human-facing name: the distribution when known, else the provider."""
        return self.distribution or self.provider


def _scheme(provider_id: str) -> str | None:
    head, sep, _ = provider_id.partition("://")
    return head.lower() if sep else None


def detect_provider(nodes: Iterable[Mapping[str, Any]]) -> ProviderInfo:
    """Detect the cloud provider from node manifests.

    Looks at each node's `spec.providerID` scheme and well-known managed
    labels; the first recognized signal decides. "unknown" is a fine answer
    (bare metal, kind, RBAC-limited, or an unrecognized cloud).

    Args:
        nodes: Node manifests as plain dicts (any subset of the cluster's
            nodes — one recognized node is enough).

    Returns:
        The detected ProviderInfo; `ProviderInfo("unknown", None)` when no
        node carries a recognizable signal.
    """
    provider: str | None = None
    distribution: str | None = None
    for node in nodes:
        labels: Mapping[str, Any] = (node.get("metadata") or {}).get("labels") or {}
        for label, (dist, prov) in _MANAGED_LABELS.items():
            if label in labels:
                return ProviderInfo(prov, dist)
        if provider is None:
            provider_id = (node.get("spec") or {}).get("providerID") or ""
            scheme = _scheme(str(provider_id))
            if scheme is not None:
                provider = _PROVIDER_SCHEMES.get(scheme)
    if provider is None:
        return ProviderInfo(UNKNOWN_PROVIDER, None)
    return ProviderInfo(provider, distribution)
