"""Ownership relations between resource kinds for drill-down navigation.

The registry maps a parent kind (canonical lowercase plural) to the child
kind reached by drilling down. Matching is ownerReferences-based: a child
belongs to a parent when the parent's uid appears in the child's owner uids.

Slice 1 registers the Deployment rollout chain; later slices only add
entries (statefulsets/daemonsets -> pods, cronjobs -> jobs -> pods, ...).
"""

from __future__ import annotations

from typing import Any

_DRILL_CHILDREN: dict[str, str] = {
    "deployments": "replicasets",
    "replicasets": "pods",
    "helmreleases": "helmrevisions",
}


def drill_child(parent_kind: str) -> str | None:
    """Child kind (lowercase plural) shown when drilling into *parent_kind*."""
    return _DRILL_CHILDREN.get(parent_kind)


def owned_by(obj: Any, parent_uid: str) -> bool:
    """True when *obj*'s ownerReferences include *parent_uid*.

    Any summary type participates by exposing an ``owner_uids`` tuple;
    objects without one never match.
    """
    return parent_uid in getattr(obj, "owner_uids", ())
