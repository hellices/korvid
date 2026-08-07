"""Tests for the deterministic PVC binding analyzer (pvc.binding)."""

from __future__ import annotations

from korvid.core.findings import EvidenceGap, ResourceIdentity
from korvid.core.pvc_analysis import (
    PVCBindingSnapshot,
    StorageClassSnapshot,
    WarningEventSnapshot,
    analyze_pvc_binding,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PVC_IDENTITY = ResourceIdentity(kind="PersistentVolumeClaim", namespace="default", name="my-pvc")
_SC_IDENTITY = ResourceIdentity(kind="StorageClass", namespace="", name="managed")


def _pvc(
    phase: str = "Pending",
    volume_name: str = "",
    storage_class_name: str | None = "managed",
    requested_storage: str = "1Gi",
    access_modes: tuple[str, ...] = ("ReadWriteOnce",),
) -> PVCBindingSnapshot:
    return PVCBindingSnapshot(
        identity=_PVC_IDENTITY,
        phase=phase,
        volume_name=volume_name,
        storage_class_name=storage_class_name,
        requested_storage=requested_storage,
        access_modes=access_modes,
    )


def _class(
    name: str,
    provisioner: str = "kubernetes.io/no-provisioner",
    volume_binding_mode: str = "Immediate",
    is_default: bool = False,
) -> StorageClassSnapshot:
    return StorageClassSnapshot(
        identity=ResourceIdentity(kind="StorageClass", namespace="", name=name),
        provisioner=provisioner,
        volume_binding_mode=volume_binding_mode,
        is_default=is_default,
    )


def _event(reason: str, message: str, count: int = 1) -> WarningEventSnapshot:
    return WarningEventSnapshot(reason=reason, message=message, count=count, last_seen="now")


# ---------------------------------------------------------------------------
# Core rules
# ---------------------------------------------------------------------------


def test_bound_claim_is_healthy() -> None:
    report = analyze_pvc_binding(_pvc(phase="Bound", volume_name="pvc-123"))
    assert report.outcome == "healthy"
    assert report.evidence[0].field == "spec.volumeName"


def test_bound_without_volume_is_inconsistent() -> None:
    report = analyze_pvc_binding(_pvc(phase="Bound", volume_name=""))
    assert report.findings[0].rule_id == "pvc.bound_without_volume"


def test_pending_without_default_class_warns() -> None:
    report = analyze_pvc_binding(_pvc(storage_class_name=None), ())
    assert report.findings[0].rule_id == "pvc.no_default_storage_class"


def test_wait_for_first_consumer_is_informational() -> None:
    report = analyze_pvc_binding(
        _pvc(storage_class_name="managed"),
        (_class("managed", volume_binding_mode="WaitForFirstConsumer"),),
    )
    assert report.findings[0].rule_id == "pvc.waiting_for_first_consumer"
    assert report.findings[0].severity == "info"


def test_provisioning_failure_event_takes_precedence() -> None:
    report = analyze_pvc_binding(
        _pvc(storage_class_name="managed"),
        (_class("managed"),),
        (_event("ProvisioningFailed", "quota exhausted"),),
    )
    assert report.findings[0].rule_id == "pvc.provisioning_failed"
    assert report.findings[0].confidence == "high"
    assert report.findings[0].evidence[-1].value == "quota exhausted"


# ---------------------------------------------------------------------------
# Edge contracts
# ---------------------------------------------------------------------------


def test_lost_claim_is_critical() -> None:
    finding = analyze_pvc_binding(_pvc(phase="Lost")).findings[0]
    assert finding.rule_id == "pvc.lost"
    assert finding.severity == "critical"


def test_explicit_empty_class_waits_for_static_volume() -> None:
    finding = analyze_pvc_binding(_pvc(storage_class_name="")).findings[0]
    assert finding.rule_id == "pvc.awaiting_static_volume"


def test_missing_named_class_warns() -> None:
    finding = analyze_pvc_binding(_pvc(storage_class_name="missing"), (_class("other"),)).findings[
        0
    ]
    assert finding.rule_id == "pvc.storage_class_not_found"


def test_multiple_defaults_are_reported() -> None:
    report = analyze_pvc_binding(
        _pvc(storage_class_name=None),
        (_class("a", is_default=True), _class("b", is_default=True)),
    )
    assert report.findings[0].rule_id == "pvc.multiple_default_storage_classes"
    assert {item.name for item in report.findings[0].related} == {"a", "b"}


def test_immediate_class_reports_generic_pending() -> None:
    finding = analyze_pvc_binding(
        _pvc(storage_class_name="managed"), (_class("managed"),)
    ).findings[0]
    assert finding.rule_id == "pvc.provisioning_pending"


def test_storage_class_gap_prevents_false_no_default_finding() -> None:
    gap = EvidenceGap("storageclasses", "forbidden (HTTP 403)")
    report = analyze_pvc_binding(_pvc(storage_class_name=None), gaps=(gap,))
    assert report.outcome == "incomplete"
    assert report.findings == ()
    assert report.gaps == (gap,)


def test_event_gap_does_not_hide_storage_class_finding() -> None:
    gap = EvidenceGap("events", "forbidden (HTTP 403)")
    report = analyze_pvc_binding(
        _pvc(storage_class_name="missing"),
        (_class("other"),),
        gaps=(gap,),
    )
    assert report.outcome == "findings"
    assert report.findings[0].rule_id == "pvc.storage_class_not_found"
    assert report.gaps == (gap,)
