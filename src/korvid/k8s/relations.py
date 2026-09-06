"""Ownership relations between resource kinds for drill-down navigation.

The registry maps a discovered resource identity to its child's identity.
Matching is ownerReferences-based: a child
belongs to a parent when the parent's uid appears in the child's owner uids.

Slice 1 registers the Deployment rollout chain; later slices only add
entries (statefulsets/daemonsets -> pods, cronjobs -> jobs -> pods, ...).
"""

from __future__ import annotations

from typing import Any

from korvid.k8s.discovery import ResourceMeta

_DRILL_CHILDREN: dict[tuple[str, str, bool], tuple[str, str, bool]] = {
    ("apps", "deployments", False): ("apps", "replicasets", False),
    ("apps", "replicasets", False): ("", "pods", False),
    ("", "helmreleases", True): ("", "helmrevisions", True),
}


def drill_child(parent: ResourceMeta) -> tuple[str, str, bool] | None:
    """Return the child's group, plural, and synthetic flag for this resource."""
    return _DRILL_CHILDREN.get(parent.identity)


def owned_by(obj: Any, parent_uid: str) -> bool:
    """True when *obj*'s ownerReferences include *parent_uid*.

    Any summary type participates by exposing an ``owner_uids`` tuple;
    objects without one never match.
    """
    return parent_uid in getattr(obj, "owner_uids", ())
