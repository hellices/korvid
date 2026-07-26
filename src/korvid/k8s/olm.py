"""OLM (Operator Lifecycle Manager) pure helpers - issue #29.

Where OLM is installed, `packages.operators.coreos.com` serves PackageManifest
objects describing every installable operator, and `operators.coreos.com`
carries Subscriptions / CSVs / InstallPlans. Everything korvid shows comes
from those objects - no hardcoded operator knowledge. This module holds the
API-free pieces: extracting install facts from a PackageManifest and building
the Subscription manifest that the approval dialog shows in full.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Aggregated API group serving PackageManifest (the operator catalog).
PACKAGES_GROUP = "packages.operators.coreos.com"

#: CRD group carrying Subscription / ClusterServiceVersion / InstallPlan.
OPERATORS_GROUP = "operators.coreos.com"

#: Valid values for ``spec.installPlanApproval``.
APPROVAL_MODES = ("Automatic", "Manual")


def _mapping(value: Any) -> dict[str, Any]:
    """*value* if it is a mapping, else {} - catalog objects come from the
    cluster and nested fields may be missing or mistyped."""
    return value if isinstance(value, dict) else {}


def channel_names(status: dict[str, Any]) -> tuple[str, ...]:
    """Channel names from a PackageManifest ``status``, tolerating a missing
    or mistyped ``channels`` field (a malformed catalog entry yields an empty
    tuple - never a crash - leaving the server as the sole validator)."""
    channels = status.get("channels")
    if not isinstance(channels, list):
        return ()
    return tuple(
        str(entry["name"]) for entry in channels if isinstance(entry, dict) and entry.get("name")
    )


@dataclass(frozen=True)
class PackageInstallFacts:
    """What the install wizard needs from one PackageManifest."""

    package: str
    channels: tuple[str, ...]
    default_channel: str
    catalog_source: str
    catalog_source_namespace: str


def package_install_facts(manifest: dict[str, Any]) -> PackageInstallFacts:
    """Extract wizard inputs from a PackageManifest, tolerating malformed
    nested fields (empty facts mean nothing to preselect, never a crash)."""
    status = _mapping(manifest.get("status"))
    return PackageInstallFacts(
        package=str(_mapping(manifest.get("metadata")).get("name") or ""),
        channels=channel_names(status),
        default_channel=str(status.get("defaultChannel") or ""),
        catalog_source=str(status.get("catalogSource") or ""),
        catalog_source_namespace=str(status.get("catalogSourceNamespace") or ""),
    )


def build_subscription(
    *,
    package: str,
    namespace: str,
    channel: str,
    source: str,
    source_namespace: str,
    approval: str,
) -> dict[str, Any]:
    """Subscription manifest for installing *package* - shown in full in the
    approval dialog before it is created.

    Raises ValueError on a blank required field or an unknown approval mode
    so a half-filled wizard can never reach the confirmation screen.
    """
    fields = {
        "package": package,
        "namespace": namespace,
        "channel": channel,
        "source": source,
        "source namespace": source_namespace,
    }
    for label, value in fields.items():
        if not value.strip():
            raise ValueError(f"{label} must not be blank")
    if approval not in APPROVAL_MODES:
        raise ValueError(f"approval must be one of {APPROVAL_MODES}, got {approval!r}")
    return {
        "apiVersion": f"{OPERATORS_GROUP}/v1alpha1",
        "kind": "Subscription",
        "metadata": {"name": package.strip(), "namespace": namespace.strip()},
        "spec": {
            "name": package.strip(),
            "channel": channel.strip(),
            "source": source.strip(),
            "sourceNamespace": source_namespace.strip(),
            "installPlanApproval": approval,
        },
    }
