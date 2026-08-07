"""Deterministic PVC binding analyzer (pvc.binding)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from korvid.core.findings import (
    AnalysisReport,
    Confidence,
    Evidence,
    EvidenceGap,
    Finding,
    ResourceIdentity,
    Severity,
)

__all__ = [
    "AnalysisReport",
    "Confidence",
    "Evidence",
    "EvidenceGap",
    "Finding",
    "PVCBindingSnapshot",
    "ResourceIdentity",
    "Severity",
    "StorageClassSnapshot",
    "WarningEventSnapshot",
    "analyze_pvc_binding",
    "has_provisioning_failure_event",
]

_ANALYZER = "pvc.binding"
_VERSION = "1"
_RULE_VERSION = "1"
_PROVISIONING_FAILURE_REASONS = frozenset({"ProvisioningFailed", "FailedBinding", "VolumeMismatch"})
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_UTC = UTC


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
    default_annotation_key: str = ""
    default_annotation_value: str = ""
    created: str = ""


@dataclass(frozen=True, slots=True)
class WarningEventSnapshot:
    """Immutable Warning event input for the analyzer."""

    reason: str
    message: str
    count: int
    last_seen: str


def has_provisioning_failure_event(events: Sequence[WarningEventSnapshot]) -> bool:
    """Return True if any event reason indicates a decisive provisioning failure.

    Uses the same `_PROVISIONING_FAILURE_REASONS` constant as the analyzer
    rule so there is no duplication of the reason list.
    """
    return any(e.reason in _PROVISIONING_FAILURE_REASONS for e in events)


def analyze_pvc_binding(
    pvc: PVCBindingSnapshot,
    storage_classes: Sequence[StorageClassSnapshot] = (),
    warning_events: Sequence[WarningEventSnapshot] = (),
    gaps: Sequence[EvidenceGap] = (),
) -> AnalysisReport:
    """Analyze a PVC binding snapshot against available evidence.

    Rules are evaluated in strict priority order:
    1. Bound/Lost and internally inconsistent Bound.
    2. Any provisioning-failure Warning event (ProvisioningFailed, FailedBinding, VolumeMismatch).
    3. Pre-bound claim: spec.volumeName non-empty and phase not Bound/Lost — emit
       pvc.awaiting_prebound_volume; class/default resolution does not run.
    4. Explicit empty class (static binding).
    5. storageclasses gap blocks class-resolution rules (returns incomplete).
    6. Named/default StorageClass resolution.
    7. WaitForFirstConsumer.
    8. Generic Immediate pending.
    """
    pvc_id = pvc.identity
    gaps_tuple = tuple(gaps)

    result = _check_phase(pvc, pvc_id, gaps_tuple)
    if result is not None:
        return result

    result = _check_failure_events(pvc, pvc_id, gaps_tuple, warning_events)
    if result is not None:
        return result

    result = _check_prebound(pvc, pvc_id, gaps_tuple)
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
    """Return a finding if any provisioning-failure event exists.

    Selection: newest last_seen first, then highest count, then reason/message
    for determinism. Evidence includes event.count and event.last_seen (omitted
    when last_seen is empty).
    """
    failure_events = [e for e in warning_events if e.reason in _PROVISIONING_FAILURE_REASONS]
    if not failure_events:
        return None
    # Sort: newest instant (chronological) descending, count descending, reason/message ascending.
    # Events with unparsable last_seen fall back to epoch (sort last).
    failure_events.sort(key=lambda e: (-e.count, e.reason, e.message))
    failure_events.sort(key=_event_instant_from_snapshot, reverse=True)
    ev = failure_events[0]
    evidence: list[Evidence] = [
        _evidence(pvc_id, "status.phase", pvc.phase),
        _evidence(pvc_id, "event.reason", ev.reason),
        _evidence(pvc_id, "event.message", ev.message),
        _evidence(pvc_id, "event.count", str(ev.count)),
    ]
    if ev.last_seen:
        evidence.append(_evidence(pvc_id, "event.last_seen", ev.last_seen))
    return _findings_report(
        pvc_id,
        gaps,
        _finding(
            "pvc.provisioning_failed",
            "warning",
            "high",
            pvc_id,
            explanation=f"Provisioning failure event: {ev.reason}",
            evidence=tuple(evidence),
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


def _check_prebound(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
) -> AnalysisReport | None:
    """Return a finding if spec.volumeName is set and phase is not Bound/Lost.

    Class and default-class resolution must not run for pre-bound claims.
    Provisioning-failure events (checked earlier) still take precedence.
    """
    if not pvc.volume_name:
        return None
    return _findings_report(
        pvc_id,
        gaps,
        _finding(
            "pvc.awaiting_prebound_volume",
            "warning",
            "high",
            pvc_id,
            explanation=(
                f"spec.volumeName is set to '{pvc.volume_name}' but the PVC is not yet Bound. "
                "The PVC is waiting for the named PV to become available and satisfy the claim."
            ),
            evidence=(_evidence(pvc_id, "spec.volumeName", pvc.volume_name),),
            next_checks=(
                f"inspect PersistentVolume '{pvc.volume_name}'",
                f"verify PV '{pvc.volume_name}' exists, is Available, and matches PVC requirements",
            ),
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
        return _resolve_default_class(pvc, pvc_id, gaps, sorted_classes)

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
    return _resolve_binding_mode(pvc, pvc_id, gaps, named[0])


def _resolve_default_class(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    sorted_classes: list[StorageClassSnapshot],
) -> AnalysisReport:
    """Handle PVCs that require a default StorageClass."""
    defaults = [sc for sc in sorted_classes if sc.is_default]
    if len(defaults) > 1:
        # Kubernetes selects the newest default; on exact timestamp ties, the
        # lexicographically smallest name wins.
        effective = min(
            defaults,
            key=lambda sc: (-_sc_creation_instant(sc).timestamp(), sc.identity.name),
        )
        multi_finding = _finding(
            "pvc.multiple_default_storage_classes",
            "warning",
            "high",
            pvc_id,
            explanation=(
                "Multiple StorageClasses are marked as default; only one should be default. "
                f"Kubernetes will use '{effective.identity.name}' (most recently created)."
            ),
            evidence=tuple(
                _evidence(
                    sc.identity,
                    f"metadata.annotations.{sc.default_annotation_key}",
                    sc.default_annotation_value,
                )
                for sc in defaults
            ),
            related=tuple(sc.identity for sc in defaults),
            next_checks=("remove the extra default annotation",),
        )
        effective_finding = _resolve_binding_mode_finding(pvc, pvc_id, gaps, effective)
        if effective.volume_binding_mode != "WaitForFirstConsumer" and any(
            g.source == "events" for g in gaps
        ):
            return _findings_report(pvc_id, gaps, multi_finding)
        return _findings_report(pvc_id, gaps, multi_finding, effective_finding)
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
    return _resolve_binding_mode(pvc, pvc_id, gaps, defaults[0])


def _resolve_binding_mode(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    resolved_sc: StorageClassSnapshot,
) -> AnalysisReport:
    """Emit WaitForFirstConsumer or generic provisioning-pending finding.

    For Immediate-mode classes, the generic finding asserts that no specific
    failure was reported — but that claim is false when events are unavailable.
    Return incomplete instead so a false negative is never serialised.
    WaitForFirstConsumer is deterministic (no event evidence required) and is
    always emitted regardless of an events gap.
    """
    if resolved_sc.volume_binding_mode != "WaitForFirstConsumer" and any(
        g.source == "events" for g in gaps
    ):
        return _incomplete_report(pvc_id, gaps)
    return _findings_report(
        pvc_id, gaps, _resolve_binding_mode_finding(pvc, pvc_id, gaps, resolved_sc)
    )


def _resolve_binding_mode_finding(
    pvc: PVCBindingSnapshot,
    pvc_id: ResourceIdentity,
    gaps: tuple[EvidenceGap, ...],
    resolved_sc: StorageClassSnapshot,
) -> Finding:
    """Return the WaitForFirstConsumer or provisioning-pending Finding for `resolved_sc`."""
    if resolved_sc.volume_binding_mode == "WaitForFirstConsumer":
        return _finding(
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
                    "volumeBindingMode",
                    "WaitForFirstConsumer",
                ),
            ),
            next_checks=("schedule a Pod that uses this PVC",),
        )
    return _finding(
        "pvc.provisioning_pending",
        "info",
        "medium",
        pvc_id,
        explanation=(
            f"PVC is not yet Bound with StorageClass '{resolved_sc.identity.name}' "
            "(volumeBindingMode=Immediate); provisioning has not reported a "
            "specific failure."
        ),
        evidence=(
            _evidence(pvc_id, "status.phase", pvc.phase),
            _evidence(
                resolved_sc.identity,
                "volumeBindingMode",
                resolved_sc.volume_binding_mode,
            ),
        ),
        next_checks=("check provisioner pod logs", "describe PVC for events"),
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


def _event_instant_from_snapshot(ev: WarningEventSnapshot) -> datetime:
    """Parse a WarningEventSnapshot.last_seen string into a UTC datetime for chronological sort.

    Invalid or empty values fall back to epoch so they sort last.
    """
    raw = ev.last_seen
    if not raw:
        return _EPOCH
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)


def _sc_creation_instant(sc: StorageClassSnapshot) -> datetime:
    """Parse a StorageClassSnapshot.created string into a UTC datetime for chronological sort.

    Invalid or empty values fall back to epoch so they sort last (oldest).
    """
    raw = sc.created
    if not raw:
        return _EPOCH
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)


def _evidence(resource: ResourceIdentity, field: str, value: str) -> Evidence:
    return Evidence(resource=resource, field=field, value=value)


def _finding(
    rule_id: str,
    severity: Severity,
    confidence: Confidence,
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
        severity=severity,
        confidence=confidence,
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
