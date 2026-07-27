"""Cluster context notes injected into the agent system prompt (issue #30).

Pure formatting: turns a detected `ProviderInfo` into a one-sentence system
prompt addition. No annotation catalog is shipped — the note only tells the
model *which* provider to reason about; the model supplies the CSP-specific
knowledge (annotations, LB classes, ingress flags) itself.
"""

from __future__ import annotations

from korvid.k8s.csp import ProviderInfo

#: Human-facing provider names for the prompt.
_PROVIDER_NAMES: dict[str, str] = {
    "azure": "Azure",
    "aws": "AWS",
    "gcp": "Google Cloud",
    "openstack": "OpenStack",
    "vsphere": "vSphere",
    "digitalocean": "DigitalOcean",
    "hetzner": "Hetzner",
    "oracle": "Oracle Cloud",
    "ibm": "IBM Cloud",
    "alibaba": "Alibaba Cloud",
}

_DISTRIBUTION_NAMES: dict[str, str] = {
    "aks": "AKS",
    "eks": "EKS",
    "gke": "GKE",
}


def cluster_context_note(info: ProviderInfo) -> str | None:
    """Build the system prompt note for a detected cloud provider.

    Args:
        info: Detection result from `korvid.k8s.csp.detect_provider`.

    Returns:
        A one-sentence note naming the provider (and managed distribution
        when known) and directing the model to answer provider-specific
        requests with appropriate annotations — or None when the provider
        is unknown (no note beats a wrong note).
    """
    if not info.known:
        return None
    provider_name = _PROVIDER_NAMES.get(info.provider, info.provider)
    if info.distribution:
        dist_name = _DISTRIBUTION_NAMES.get(info.distribution, info.distribution)
        where = f"{provider_name} ({dist_name} managed)"
    else:
        where = provider_name
    return (
        f"This cluster runs on {where}. When the user asks for "
        "provider-specific behavior — exposing services publicly or "
        "internally, load balancer or ingress annotations, storage classes — "
        f"give {where}-appropriate annotations and settings without making "
        "them name the cloud provider, and verify current resource state "
        "with tools before suggesting changes."
    )
