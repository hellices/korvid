"""OLM (Operator Lifecycle Manager) pure helpers - issue #29.

Where OLM is installed, `packages.operators.coreos.com` serves PackageManifest
objects describing every installable operator, and `operators.coreos.com`
carries Subscriptions / CSVs / InstallPlans. Everything korvid shows comes
from those objects - no hardcoded operator knowledge. This module holds the
API-free pieces: extracting install facts from a PackageManifest and building
the Subscription manifest that the approval dialog shows in full.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from korvid.k8s.discovery import ResourceMeta

#: Aggregated API group serving PackageManifest (the operator catalog).
PACKAGES_GROUP = "packages.operators.coreos.com"

#: CRD group carrying Subscription / ClusterServiceVersion / InstallPlan.
OPERATORS_GROUP = "operators.coreos.com"

#: Valid values for ``spec.installPlanApproval``.
APPROVAL_MODES = ("Automatic", "Manual")


def resolve_olm_meta(
    aliases: Mapping[str, ResourceMeta], plural: str, group: str
) -> ResourceMeta | None:
    """The OLM ResourceMeta for *plural* in *group*, or None when absent.

    Prefers the kubectl-style ``plural.group`` alias (kept by discovery even
    when a same-plural CRD from another group won the bare alias) and falls
    back to the bare plural, accepting it only when the group matches.
    """
    meta = aliases.get(f"{plural}.{group}") or aliases.get(plural)
    if meta is not None and meta.group == group:
        return meta
    return None


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


#: Row cap for the catalog's short description: catalog objects come from
#: the cluster and a hostile entry must not bloat the table.
MAX_DESCRIPTION_CHARS = 80

#: DNS-1123 subdomain (lowercase alphanumerics, '-' and '.'; 253-char cap
#: checked separately) - what the API server requires of metadata.name.
_DNS1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


def package_description(status: dict[str, Any]) -> str:
    """Short description for a catalog entry, from its own CSV metadata.

    Prefers the default channel's ``currentCSVDesc.annotations.description``
    (the catalog's one-liner), falling back to the first line of the long
    ``currentCSVDesc.description``. Capped at MAX_DESCRIPTION_CHARS.
    """
    channels = status.get("channels")
    if not isinstance(channels, list):
        return ""
    default = str(status.get("defaultChannel") or "")
    entries = [e for e in channels if isinstance(e, dict)]
    ordered = sorted(entries, key=lambda e: e.get("name") != default)
    for entry in ordered:
        desc = _mapping(entry.get("currentCSVDesc"))
        text = str(_mapping(desc.get("annotations")).get("description") or "")
        if not text:
            text = str(desc.get("description") or "")
        # Either source is catalog-controlled and may be multiline; a table
        # row must stay one line regardless of which one supplied the text.
        text = text.split("\n", 1)[0].strip()
        if text:
            if len(text) > MAX_DESCRIPTION_CHARS:
                # The ellipsis counts against the cap, keeping the bound exact.
                return text[: MAX_DESCRIPTION_CHARS - 1] + "\u2026"
            return text
    return ""


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
    # Catalog entries are cluster-supplied data: the package name becomes
    # metadata.name, so validate it as a DNS-1123 subdomain here rather than
    # letting a hostile entry ride into the API request path.
    if not _DNS1123_SUBDOMAIN.match(package.strip()) or len(package.strip()) > 253:
        raise ValueError(f"package name {package.strip()!r} is not a valid DNS-1123 subdomain")
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
