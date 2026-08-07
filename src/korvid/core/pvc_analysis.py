"""Deterministic PVC binding analyzer (pvc.binding)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from korvid.core.findings import (
    AnalysisReport,
    Evidence,
    EvidenceGap,
    Finding,
    ResourceIdentity,
)

__all__ = [
    "AnalysisReport",
    "Evidence",
    "EvidenceGap",
    "Finding",
    "PVCBindingSnapshot",
    "ResourceIdentity",
    "StorageClassSnapshot",
    "WarningEventSnapshot",
    "analyze_pvc_binding",
]

_ANALYZER = "pvc.binding"
_VERSION = "1"
_RULE_VERSION = "1"
_PROVISIONING_FAILURE_REASONS = frozenset({"ProvisioningFailed", "FailedBinding", "VolumeMismatch"})


@dataclass(frozen=True, slots=True)
class PVCBindingSnapshot:
    """Immutable PVC input for the analyzer."""

    identity: ResourceIdentity
    phase: str
    volume_name: str
    storage_class_name: str | None
    requested_storage: str
    access_modes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StorageClassSnapshot:
    """Immutable StorageClass input for the analyzer."""

    identity: ResourceIdentity
    provisioner: str
    volume_binding_mode: str
    is_default: bool


@dataclass(frozen=True, slots=True)
class WarningEventSnapshot:
    """Immutable Warning event input for the analyzer."""

    reason: str
    message: str
    count: int
    last_seen: str


def analyze_pvc_binding(
    pvc: PVCBindingSnapshot,
    storage_classes: Sequence[StorageClassSnapshot] = (),
    warning_events: Sequence[WarningEventSnapshot] = (),
    gaps: Sequence[EvidenceGap] = (),
) -> AnalysisReport:
    """Analyze a PVC binding snapshot against available evidence.

    Rules are evaluated in strict priority order:
    1. Bound/Lost and internally inconsistent Bound.
    2. Any provisioning-failure Warning event.
    3. Explicit empty class (static binding).
    4. storageclasses gap blocks class-resolution rules (returns incomplete).
    5. Named/default StorageClass resolution.
    6. WaitForFirstConsumer.
    7. Generic Immediate pending.
    8. Fallback incomplete when gaps exist.
    """
    pvc_id = pvc.identity
    gaps_tuple = tuple(gaps)

    result = _check_phase(pvc, pvc_id, gaps_tuple)
    if result is not None:
        return result

    result = _check_failure_events(pvc, pvc_id, gaps_tuple, warning_events)
    if result is not None:
        return result

    result = _check_static_binding(pvc, pvc_id, gaps_tuple)
    if result is not None:
        return result

    return _resolve_storage_class(pvc, pvc_id, gaps_tuple, storage_classes)


def _check_phase(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
) -> AnalysisReport | None:
    """Handle Bound (healthy or inconsistent) and Lost phases."""
    if pvc.phase == "Bound":
        if not pvc.volume_name:
            return _findings_report(
                pvc_id,
                gaps,
                _finding(
                    "pvc.bound_without_volume",
                    "critical",
                    "high",
                    pvc_id,
                    explanation=(
                        "PVC reports phase=Bound but spec.volumeName is empty, "
                        "which is internally inconsistent."
                    ),
                    evidence=(_evidence(pvc_id, "status.phase", "Bound"),),
                    next_checks=("inspect PV and binding controller logs",),
                ),
            )
        return AnalysisReport(
            analyzer=_ANALYZER,
            version=_VERSION,
            outcome="healthy",
            primary=pvc_id,
            evidence=(_evidence(pvc_id, "spec.volumeName", pvc.volume_name),),
            gaps=gaps,
        )
    if pvc.phase == "Lost":
        return _findings_report(
            pvc_id,
            gaps,
            _finding(
                "pvc.lost",
                "critical",
                "high",
                pvc_id,
                explanation="PVC is in phase=Lost; the backing PV has been deleted or reclaimed.",
                evidence=(_evidence(pvc_id, "status.phase", "Lost"),),
                next_checks=("check PV existence", "check reclaim policy"),
            ),
        )
    return None


def _check_failure_events(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    warning_events: Sequence[WarningEventSnapshot],
) -> AnalysisReport | None:
    """Return a finding if any provisioning-failure event exists."""
    sorted_events = sorted(warning_events, key=lambda e: (e.reason, e.message))
    failure_events = [e for e in sorted_events if e.reason in _PROVISIONING_FAILURE_REASONS]
    if not failure_events:
        return None
    ev = failure_events[0]
    return _findings_report(
        pvc_id,
        gaps,
        _finding(
            "pvc.provisioning_failed",
            "warning",
            "high",
            pvc_id,
            explanation=f"Provisioning failure event: {ev.reason}",
            evidence=(
                _evidence(pvc_id, "status.phase", pvc.phase),
                _evidence(pvc_id, "event.reason", ev.reason),
                _evidence(pvc_id, "event.message", ev.message),
            ),
            next_checks=("check provisioner logs", "verify quota and permissions"),
        ),
    )


def _check_static_binding(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
) -> AnalysisReport | None:
    """Return a finding if storageClassName is explicitly empty."""
    if pvc.storage_class_name != "":
        return None
    return _findings_report(
        pvc_id,
        gaps,
        _finding(
            "pvc.awaiting_static_volume",
            "info",
            "high",
            pvc_id,
            explanation=(
                "storageClassName is explicitly set to '' so the PVC awaits "
                "manual binding to a static PersistentVolume."
            ),
            evidence=(_evidence(pvc_id, "spec.storageClassName", ""),),
            next_checks=("create a matching PersistentVolume",),
        ),
    )


def _resolve_storage_class(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    storage_classes: Sequence[StorageClassSnapshot],
) -> AnalysisReport:
    """Resolve the StorageClass and return a finding for the pending state."""
    has_sc_gap = any(g.source == "storageclasses" for g in gaps)
    sorted_classes = sorted(storage_classes, key=lambda sc: sc.identity.name)

    if pvc.storage_class_name is None:
        if has_sc_gap:
            return _incomplete_report(pvc_id, gaps)
        return _resolve_default_class(pvc_id, gaps, sorted_classes)

    # Named class
    named = [sc for sc in sorted_classes if sc.identity.name == pvc.storage_class_name]
    if not named:
        if has_sc_gap:
            return _incomplete_report(pvc_id, gaps)
        return _findings_report(
            pvc_id,
            gaps,
            _finding(
                "pvc.storage_class_not_found",
                "warning",
                "high",
                pvc_id,
                explanation=(
                    f"StorageClass '{pvc.storage_class_name}' referenced by the PVC "
                    "does not exist in the cluster."
                ),
                evidence=(_evidence(pvc_id, "spec.storageClassName", pvc.storage_class_name),),
                next_checks=(
                    f"create StorageClass '{pvc.storage_class_name}'",
                    "or correct spec.storageClassName",
                ),
            ),
        )
    return _resolve_binding_mode(pvc_id, gaps, named[0])


def _resolve_default_class(
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    sorted_classes: list[StorageClassSnapshot],
) -> AnalysisReport:
    """Handle PVCs that require a default StorageClass."""
    defaults = [sc for sc in sorted_classes if sc.is_default]
    if len(defaults) > 1:
        return _findings_report(
            pvc_id,
            gaps,
            _finding(
                "pvc.multiple_default_storage_classes",
                "warning",
                "high",
                pvc_id,
                explanation=(
                    "Multiple StorageClasses are marked as default; only one should be default."
                ),
                evidence=tuple(
                    _evidence(sc.identity, "metadata.annotations", "is-default") for sc in defaults
                ),
                related=tuple(sc.identity for sc in defaults),
                next_checks=("remove the extra default annotation",),
            ),
        )
    if not defaults:
        return _findings_report(
            pvc_id,
            gaps,
            _finding(
                "pvc.no_default_storage_class",
                "warning",
                "high",
                pvc_id,
                explanation=(
                    "No default StorageClass exists in the cluster; "
                    "a PVC without an explicit storageClassName cannot be provisioned."
                ),
                evidence=(_evidence(pvc_id, "spec.storageClassName", "<none>"),),
                next_checks=(
                    "create a StorageClass and annotate it as default",
                    "or set spec.storageClassName explicitly",
                ),
            ),
        )
    return _resolve_binding_mode(pvc_id, gaps, defaults[0])


def _resolve_binding_mode(
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    resolved_sc: StorageClassSnapshot,
) -> AnalysisReport:
    """Emit WaitForFirstConsumer or generic provisioning-pending finding."""
    if resolved_sc.volume_binding_mode == "WaitForFirstConsumer":
        return _findings_report(
            pvc_id,
            gaps,
            _finding(
                "pvc.waiting_for_first_consumer",
                "info",
                "high",
                pvc_id,
                explanation=(
                    f"StorageClass '{resolved_sc.identity.name}' uses "
                    "volumeBindingMode=WaitForFirstConsumer; "
                    "the PVC will bind when a Pod consumes it."
                ),
                evidence=(
                    _evidence(
                        resolved_sc.identity,
                        "spec.volumeBindingMode",
                        "WaitForFirstConsumer",
                    ),
                ),
                next_checks=("schedule a Pod that uses this PVC",),
            ),
        )
    return _findings_report(
        pvc_id,
        gaps,
        _finding(
            "pvc.provisioning_pending",
            "info",
            "medium",
            pvc_id,
            explanation=(
                f"PVC is Pending with StorageClass '{resolved_sc.identity.name}' "
                "(volumeBindingMode=Immediate); provisioning has not yet completed."
            ),
            evidence=(
                _evidence(pvc_id, "status.phase", "Pending"),
                _evidence(
                    resolved_sc.identity,
                    "spec.volumeBindingMode",
                    resolved_sc.volume_binding_mode,
                ),
            ),
            next_checks=("check provisioner pod logs", "describe PVC for events"),
        ),
    )


def _incomplete_report(
    primary: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
) -> AnalysisReport:
    return AnalysisReport(
        analyzer=_ANALYZER,
        version=_VERSION,
        outcome="incomplete",
        primary=primary,
        findings=(),
        gaps=gaps,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence(resource: ResourceIdentity, field: str, value: str) -> Evidence:
    return Evidence(resource=resource, field=field, value=value)


def _finding(
    rule_id: str,
    severity: str,
    confidence: str,
    primary: ResourceIdentity,
    *,
    explanation: str,
    evidence: tuple[Evidence, ...],
    next_checks: tuple[str, ...],
    related: tuple[ResourceIdentity, ...] = (),
) -> Finding:
    return Finding(
        rule_id=rule_id,
        rule_version=_RULE_VERSION,
        severity=severity,  # type: ignore[arg-type]  # literal checked by tests
        confidence=confidence,  # type: ignore[arg-type]
        primary=primary,
        related=related,
        evidence=evidence,
        explanation=explanation,
        next_checks=next_checks,
    )


def _findings_report(
    primary: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    *findings: Finding,
) -> AnalysisReport:
    return AnalysisReport(
        analyzer=_ANALYZER,
        version=_VERSION,
        outcome="findings",
        primary=primary,
        findings=findings,
        gaps=gaps,
    )
