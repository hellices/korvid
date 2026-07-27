"""Drain impact planning (issue #40).

Draining a node is the canonical high-blast-radius routine operation: the
approval dialog must show what will actually happen *before* the first
eviction is issued. This module is pure classification logic - it takes raw
pod and PodDisruptionBudget manifests and produces a ``DrainPlan`` that the
UI renders as the impact preview and then executes eviction by eviction.

Classification mirrors ``kubectl drain`` semantics:

- Mirror (static) pods are managed by the kubelet, not the API server -
  evicting them is a no-op, so they are skipped.
- DaemonSet-controlled pods would be immediately recreated on the still
  registered node - skipped (kubectl requires ``--ignore-daemonsets``).
- Pods with ``emptyDir`` volumes lose that data on eviction - flagged, but
  still evicted (kubectl requires ``--delete-emptydir-data``).
- Pods matched by a PodDisruptionBudget whose ``disruptionsAllowed`` is
  already zero are called out up front: their evictions will be refused
  (HTTP 429) until the budget frees up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_MIRROR_ANNOTATION = "kubernetes.io/config.mirror"
#: Phases whose pods no longer count against a PodDisruptionBudget.
_TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})


@dataclass(frozen=True)
class DrainTarget:
    """One pod the drain will try to evict."""

    namespace: str
    name: str
    uid: str | None
    #: The pod mounts an ``emptyDir`` volume - that data is lost on eviction.
    local_storage: bool
    #: Name of a PodDisruptionBudget with no disruptions left, if any:
    #: the eviction will be refused (429) until the budget allows it.
    pdb_blocked: str | None

    @property
    def ref(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass(frozen=True)
class DrainPlan:
    """Everything a drain would do, computed before any eviction is issued."""

    targets: tuple[DrainTarget, ...]
    skipped_daemonset: tuple[str, ...]
    skipped_mirror: tuple[str, ...]

    def preview_lines(self) -> list[str]:
        """Impact preview for the approval dialog, one category per block."""
        evictable = [t for t in self.targets if t.pdb_blocked is None]
        blocked = [t for t in self.targets if t.pdb_blocked is not None]
        lines: list[str] = []
        if evictable:
            lines.append(f"Pods to evict ({len(evictable)}):")
            for target in evictable:
                flag = "  [local storage: emptyDir - data will be lost]"
                # "- " prefix: the approval dialog styles removals red.
                lines.append(f"- {target.ref}{flag if target.local_storage else ''}")
        else:
            lines.append("No pods to evict.")
        if blocked:
            lines.append("")
            lines.append(
                f"Blocked by PodDisruptionBudget ({len(blocked)})"
                " - evictions will be refused until the budget allows:"
            )
            for target in blocked:
                flag = "  [local storage: emptyDir - data will be lost]"
                # "~ " prefix: styled yellow, these need operator attention.
                lines.append(
                    f"~ {target.ref} (pdb: {target.pdb_blocked})"
                    f"{flag if target.local_storage else ''}"
                )
        if self.skipped_daemonset:
            lines.append("")
            lines.append(f"DaemonSet pods skipped ({len(self.skipped_daemonset)}):")
            lines.extend(f"  {ref}" for ref in self.skipped_daemonset)
        if self.skipped_mirror:
            lines.append("")
            lines.append(f"Mirror (static) pods skipped ({len(self.skipped_mirror)}):")
            lines.extend(f"  {ref}" for ref in self.skipped_mirror)
        return lines


def _expression_matches(expr: dict[str, Any], labels: dict[str, str]) -> bool:
    """One matchExpressions entry against a pod's labels."""
    key = str(expr.get("key", ""))
    operator = expr.get("operator")
    values = [str(v) for v in expr.get("values") or []]
    if operator == "In":
        return labels.get(key) in values
    if operator == "NotIn":
        return labels.get(key) not in values
    if operator == "Exists":
        return key in labels
    if operator == "DoesNotExist":
        return key not in labels
    # Unknown operator: fail safe by treating the PDB as matching,
    # so the drain preview over-warns rather than under-warns.
    return True


def _selector_matches(selector: dict[str, Any], labels: dict[str, str]) -> bool:
    """LabelSelector semantics for PDBs (policy/v1): an empty selector
    matches every pod in the namespace; matchLabels and matchExpressions
    are AND-combined."""
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    return all(_expression_matches(expr, labels) for expr in selector.get("matchExpressions") or [])


def _blocking_pdb(pod: dict[str, Any], pdbs: list[dict[str, Any]]) -> str | None:
    """Why evicting *pod* would currently be refused, or None. A single
    matching PDB blocks when its budget is exhausted; more than one matching
    PDB blocks unconditionally - the Eviction API rejects such pods with a
    500 regardless of budget (kubectl drain fails the same way)."""
    metadata = pod.get("metadata") or {}
    if str((pod.get("status") or {}).get("phase", "")) in _TERMINAL_PHASES:
        return None
    namespace = str(metadata.get("namespace", ""))
    labels = {str(k): str(v) for k, v in (metadata.get("labels") or {}).items()}
    matches = [
        pdb
        for pdb in pdbs
        if str((pdb.get("metadata") or {}).get("namespace", "")) == namespace
        and _selector_matches((pdb.get("spec") or {}).get("selector") or {}, labels)
    ]
    if len(matches) > 1:
        names = ", ".join(sorted(str((m.get("metadata") or {}).get("name", "")) for m in matches))
        return f"multiple PDBs match ({names}) - the eviction API rejects this"
    if matches:
        allowed = (matches[0].get("status") or {}).get("disruptionsAllowed", 0)
        if int(allowed or 0) <= 0:
            return str((matches[0].get("metadata") or {}).get("name", ""))
    return None


def _is_daemonset_pod(pod: dict[str, Any]) -> bool:
    """Only a *controlling* DaemonSet reference exempts a pod from
    eviction (kubectl drain semantics); a non-controller reference to a
    DaemonSet leaves the pod evictable."""
    return any(
        ref.get("kind") == "DaemonSet" and ref.get("controller") is True
        for ref in (pod.get("metadata") or {}).get("ownerReferences") or []
    )


def _has_local_storage(pod: dict[str, Any]) -> bool:
    return any(
        "emptyDir" in volume
        for volume in (pod.get("spec") or {}).get("volumes") or []
        if isinstance(volume, dict)
    )


def build_drain_plan(pods: list[dict[str, Any]], pdbs: list[dict[str, Any]]) -> DrainPlan:
    """Classify *pods* (everything scheduled on the node being drained)
    against *pdbs* (cluster-wide PodDisruptionBudget list) into the
    eviction plan shown to the user for approval."""
    targets: list[DrainTarget] = []
    skipped_daemonset: list[str] = []
    skipped_mirror: list[str] = []
    for pod in pods:
        metadata = pod.get("metadata") or {}
        ref = f"{metadata.get('namespace', '')}/{metadata.get('name', '')}"
        if _MIRROR_ANNOTATION in (metadata.get("annotations") or {}):
            skipped_mirror.append(ref)
            continue
        if _is_daemonset_pod(pod):
            skipped_daemonset.append(ref)
            continue
        uid = metadata.get("uid")
        targets.append(
            DrainTarget(
                namespace=str(metadata.get("namespace", "")),
                name=str(metadata.get("name", "")),
                uid=str(uid) if uid else None,
                local_storage=_has_local_storage(pod),
                pdb_blocked=_blocking_pdb(pod, pdbs),
            )
        )
    return DrainPlan(
        targets=tuple(targets),
        skipped_daemonset=tuple(skipped_daemonset),
        skipped_mirror=tuple(skipped_mirror),
    )
