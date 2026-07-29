"""Pure detection of who manages a Kubernetes object (issue #119).

A manual edit / scale / delete on a managed object is usually futile or
harmful: helm forgets it on the next `helm upgrade`, an operator's
reconcile loop reverts it within seconds.  `manager_of` reads the facts
straight off the object's own metadata — labels, annotations,
ownerReferences — with no hardcoded product knowledge, so the write
dialogs can warn *before* approval.  Detection is best-effort display
support: malformed metadata yields None, never an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["ManagedBy", "manager_of"]

#: Kubernetes' own controller kinds: ownership by these is normal workload
#: plumbing (pod -> ReplicaSet -> Deployment), not operator management.
_BUILTIN_GROUPS = frozenset({"", "apps", "batch", "autoscaling", "policy", "extensions"})

_OLM_GROUP = "operators.coreos.com"


@dataclass(frozen=True)
class ManagedBy:
    """Who manages the object and what the user should do instead.

    Attributes:
        manager: `"helm"`, `"olm"`, or `"controller"` (a custom-resource
            controller such as Strimzi or cert-manager).
        name: The manager's identity — release, CSV, or `Kind/name`.
        note: One display line for the confirmation dialog: what manages
            the object and which lever to use instead of a direct write.
    """

    manager: str
    name: str
    note: str


def manager_of(manifest: dict[str, Any]) -> ManagedBy | None:
    """Detect the manager of `manifest`, or None when it looks unmanaged.

    Precedence follows specificity: helm release annotations, then OLM
    ownership, then a controller ownerReference pointing at a custom
    resource.  Built-in controllers (ReplicaSet, Job, anything in a
    `*.k8s.io` group) are never reported — that would put a banner on
    every pod.
    """
    meta = manifest.get("metadata")
    if not isinstance(meta, dict):
        return None
    labels = meta.get("labels")
    labels = labels if isinstance(labels, dict) else {}
    annotations = meta.get("annotations")
    annotations = annotations if isinstance(annotations, dict) else {}
    return (
        _helm_manager(labels, annotations)
        or _olm_manager(labels, meta)
        or _cr_controller_manager(meta)
    )


def _helm_manager(labels: dict[str, Any], annotations: dict[str, Any]) -> ManagedBy | None:
    release = annotations.get("meta.helm.sh/release-name")
    release_ns = annotations.get("meta.helm.sh/release-namespace")
    if not isinstance(release, str) or not release:
        if labels.get("app.kubernetes.io/managed-by") != "Helm":
            return None
        release = ""
    ident = release
    if isinstance(release_ns, str) and release_ns and release:
        ident = f"{release_ns}/{release}"
    described = f"helm release {ident}" if ident else "helm"
    return ManagedBy(
        manager="helm",
        name=ident or "unknown",
        note=(
            f"managed by {described} — manual changes are overwritten by the"
            " next helm upgrade; change the chart values instead"
        ),
    )


def _olm_manager(labels: dict[str, Any], meta: dict[str, Any]) -> ManagedBy | None:
    csv = _olm_csv_owner(meta)
    if csv is None:
        owner = labels.get("olm.owner")
        if isinstance(owner, str) and owner:
            kind = labels.get("olm.owner.kind")
            if kind in (None, "ClusterServiceVersion"):
                csv = owner
    if csv is None and labels.get("olm.managed") == "true":
        csv = ""
    if csv is None:
        return None
    described = f"operator {csv} (CSV)" if csv else "an OLM operator"
    return ManagedBy(
        manager="olm",
        name=csv or "unknown",
        note=(
            f"managed by {described} — the operator will revert this change;"
            " work through its custom resources instead"
        ),
    )


def _olm_csv_owner(meta: dict[str, Any]) -> str | None:
    for ref in _owner_refs(meta):
        api_version = ref.get("apiVersion")
        name = ref.get("name")
        if (
            ref.get("kind") == "ClusterServiceVersion"
            and isinstance(api_version, str)
            and api_version.partition("/")[0] == _OLM_GROUP
            and isinstance(name, str)
            and name
        ):
            return name
    return None


def _cr_controller_manager(meta: dict[str, Any]) -> ManagedBy | None:
    for ref in _owner_refs(meta):
        if ref.get("controller") is not True:  # boolean in the API; strings are malformed
            continue
        api_version = ref.get("apiVersion")
        kind = ref.get("kind")
        name = ref.get("name")
        if not (
            isinstance(api_version, str)
            and isinstance(kind, str)
            and kind
            and isinstance(name, str)
            and name
        ):
            continue
        group = api_version.partition("/")[0] if "/" in api_version else ""
        if group in _BUILTIN_GROUPS or group.endswith(".k8s.io"):
            continue
        ident = f"{kind}/{name}"
        return ManagedBy(
            manager="controller",
            name=ident,
            note=(
                f"managed by {ident} — its controller will revert this change;"
                f" edit the {kind} custom resource instead"
            ),
        )
    return None


def _owner_refs(meta: dict[str, Any]) -> list[dict[str, Any]]:
    refs = meta.get("ownerReferences")
    if not isinstance(refs, list):
        return []
    return [ref for ref in refs if isinstance(ref, dict)]
