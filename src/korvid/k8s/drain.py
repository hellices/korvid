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

from korvid.k8s.errors import ApiStatusError

_MIRROR_ANNOTATION = "kubernetes.io/config.mirror"
#: Phases whose pods no longer count against a PodDisruptionBudget.
_TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})


def is_pdb_denial(exc: ApiStatusError) -> bool:
    """Whether a 429 from the Eviction API is a PodDisruptionBudget
    admission denial. Not every 429 is: API Priority and Fairness (and
    other apiserver throttling) answers 429 too, and must be treated as
    transient overload rather than "blocked by budget". A PDB denial's
    ``Status`` body carries a ``DisruptionBudget`` cause and the message
    "would violate the pod's disruption budget"."""
    if exc.status != 429:
        return False
    detail = f"{exc.body} {exc.reason}"
    return "DisruptionBudget" in detail or "disruption budget" in detail.lower()


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
    #: No controller owns this pod - eviction deletes it permanently
    #: (kubectl drain refuses these without ``--force``).
    unmanaged: bool = False

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
            # "- " prefix: the approval dialog styles removals red.
            lines.extend(f"- {target.ref}{_target_flags(target)}" for target in evictable)
        else:
            lines.append("No pods to evict.")
        if blocked:
            lines.append("")
            lines.append(
                f"Blocked by PodDisruptionBudget ({len(blocked)})"
                " - the eviction API currently refuses these:"
            )
            # "~ " prefix: styled yellow, these need operator attention.
            lines.extend(
                f"~ {target.ref} (pdb: {target.pdb_blocked}){_target_flags(target)}"
                for target in blocked
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


def _target_flags(target: DrainTarget) -> str:
    """Warning suffixes for one preview line."""
    flags = ""
    if target.unmanaged:
        flags += "  [no controller: pod will not be recreated]"
    if target.local_storage:
        flags += "  [local storage: emptyDir - data will be lost]"
    return flags


def _expression_matches(expr: dict[str, Any], labels: dict[str, str]) -> bool:
    """One matchExpressions entry against a pod's labels."""
    key = str(expr.get("key", ""))
    operator = expr.get("operator")
    values = [str(v) for v in expr.get("values") or []]
    if operator == "In":
        return labels.get(key) in values
    if operator == "NotIn":
        # apimachinery semantics: a pod *without* the key matches NotIn
        # (labels.Requirement.Matches returns true when the key is absent).
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


class _BudgetTracker:
    """Allocates each PDB's ``disruptionsAllowed`` across the planned
    eviction order: a budget of 1 lets exactly one matching pod through
    and blocks the rest, matching what the Eviction API would do as the
    drain consumes the allowance."""

    def __init__(self, pdbs: list[dict[str, Any]]) -> None:
        self._pdbs = pdbs
        self._remaining = {
            id(pdb): int((pdb.get("status") or {}).get("disruptionsAllowed", 0) or 0)
            for pdb in pdbs
        }

    def blocking_reason(self, pod: dict[str, Any]) -> str | None:
        """Why evicting *pod* would currently be refused, or None (and one
        unit of the matching PDB's allowance is consumed). More than one
        matching PDB blocks unconditionally - the Eviction API rejects such
        pods with a 500 regardless of budget (kubectl drain fails the same
        way)."""
        metadata = pod.get("metadata") or {}
        if _pdb_exempt(pod):
            return None
        namespace = str(metadata.get("namespace", ""))
        labels = {str(k): str(v) for k, v in (metadata.get("labels") or {}).items()}
        matches = [
            pdb
            for pdb in self._pdbs
            if str((pdb.get("metadata") or {}).get("namespace", "")) == namespace
            and _pdb_selector_matches(pdb, labels)
        ]
        if len(matches) > 1:
            names = ", ".join(
                sorted(str((m.get("metadata") or {}).get("name", "")) for m in matches)
            )
            return f"multiple PDBs match ({names}) - the eviction API rejects this"
        if matches:
            pdb = matches[0]
            if _always_allows_unhealthy(pdb) and not _pod_is_ready(pod):
                # spec.unhealthyPodEvictionPolicy: AlwaysAllow admits a
                # non-Ready pod without consuming the PDB allowance.
                return None
            pdb_name = str((pdb.get("metadata") or {}).get("name", ""))
            if _status_is_stale(pdb):
                # Eviction admission refuses disruptions while the PDB's
                # status lags its spec (observedGeneration < generation),
                # regardless of the stale disruptionsAllowed - fail safe.
                return f"{pdb_name} (status not up to date)"
            if self._remaining[id(pdb)] <= 0:
                return pdb_name
            self._remaining[id(pdb)] -= 1
        return None


def _pdb_exempt(pod: dict[str, Any]) -> bool:
    """Pods the Eviction API disrupts without PDB admission: terminal
    (Succeeded/Failed) and Pending pods are not counted as disruptions,
    and a pod already carrying ``metadata.deletionTimestamp`` is being
    deleted anyway - none of these block or consume the budget."""
    phase = str((pod.get("status") or {}).get("phase", ""))
    if phase in _TERMINAL_PHASES or phase == "Pending":
        return True
    return (pod.get("metadata") or {}).get("deletionTimestamp") is not None


def _status_is_stale(pdb: dict[str, Any]) -> bool:
    generation = (pdb.get("metadata") or {}).get("generation")
    observed = (pdb.get("status") or {}).get("observedGeneration")
    if not isinstance(generation, int) or not isinstance(observed, int):
        return False
    return observed < generation


def _always_allows_unhealthy(pdb: dict[str, Any]) -> bool:
    return (pdb.get("spec") or {}).get("unhealthyPodEvictionPolicy") == "AlwaysAllow"


def _pod_is_ready(pod: dict[str, Any]) -> bool:
    return any(
        cond.get("type") == "Ready" and cond.get("status") == "True"
        for cond in (pod.get("status") or {}).get("conditions") or []
        if isinstance(cond, dict)
    )


def _pdb_selector_matches(pdb: dict[str, Any], labels: dict[str, str]) -> bool:
    """policy/v1 semantics: a null/missing selector matches no pods, while
    an explicitly empty ``{}`` selector matches every pod in the namespace."""
    selector = (pdb.get("spec") or {}).get("selector")
    if selector is None:
        return False
    return _selector_matches(selector, labels)


def _is_daemonset_pod(pod: dict[str, Any]) -> bool:
    """Only a *controlling* DaemonSet reference exempts a pod from
    eviction (kubectl drain semantics); a non-controller reference to a
    DaemonSet leaves the pod evictable."""
    return any(
        ref.get("kind") == "DaemonSet" and ref.get("controller") is True
        for ref in (pod.get("metadata") or {}).get("ownerReferences") or []
    )


def _has_controller(pod: dict[str, Any]) -> bool:
    return any(
        ref.get("controller") is True
        for ref in (pod.get("metadata") or {}).get("ownerReferences") or []
        if isinstance(ref, dict)
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
    eviction plan shown to the user for approval. PDB allowances are
    allocated across the planned eviction order: a budget of 1 covering
    two pods on this node marks only the first as evictable."""
    targets: list[DrainTarget] = []
    skipped_daemonset: list[str] = []
    skipped_mirror: list[str] = []
    budgets = _BudgetTracker(pdbs)
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
                pdb_blocked=budgets.blocking_reason(pod),
                unmanaged=not _has_controller(pod),
            )
        )
    return DrainPlan(
        targets=tuple(targets),
        skipped_daemonset=tuple(skipped_daemonset),
        skipped_mirror=tuple(skipped_mirror),
    )
