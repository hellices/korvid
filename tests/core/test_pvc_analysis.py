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
    created: str = "",
) -> StorageClassSnapshot:
    return StorageClassSnapshot(
        identity=ResourceIdentity(kind="StorageClass", namespace="", name=name),
        provisioner=provisioner,
        volume_binding_mode=volume_binding_mode,
        is_default=is_default,
        created=created,
    )


def _event(
    reason: str, message: str, count: int = 1, last_seen: str = "2024-01-01T00:00:00Z"
) -> WarningEventSnapshot:
    return WarningEventSnapshot(reason=reason, message=message, count=count, last_seen=last_seen)


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
    evidence_by_field = {e.field: e.value for e in report.findings[0].evidence}
    assert evidence_by_field["event.message"] == "quota exhausted"


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


def test_immediate_class_preserves_empty_phase_evidence() -> None:
    report = analyze_pvc_binding(_pvc(phase="", storage_class_name="managed"), (_class("managed"),))
    finding = report.findings[0]
    evidence_fields = {e.field: e.value for e in finding.evidence}
    assert report.outcome == "findings"
    assert finding.rule_id == "pvc.provisioning_pending"
    assert evidence_fields["status.phase"] == ""


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


# ---------------------------------------------------------------------------
# Finding 1: Pre-bound PVC (spec.volumeName set, phase Pending)
# ---------------------------------------------------------------------------


def test_prebound_pending_pvc_emits_awaiting_prebound_volume() -> None:
    report = analyze_pvc_binding(
        _pvc(phase="Pending", volume_name="my-pv", storage_class_name="managed")
    )
    assert report.findings[0].rule_id == "pvc.awaiting_prebound_volume"
    assert report.findings[0].severity == "warning"
    assert report.findings[0].confidence == "high"


def test_prebound_pending_pvc_evidence_contains_volume_name() -> None:
    report = analyze_pvc_binding(
        _pvc(phase="Pending", volume_name="my-pv", storage_class_name="managed")
    )
    fields = [e.field for e in report.findings[0].evidence]
    assert "spec.volumeName" in fields
    vol_evidence = next(e for e in report.findings[0].evidence if e.field == "spec.volumeName")
    assert vol_evidence.value == "my-pv"


def test_prebound_pending_pvc_next_checks_mention_named_pv() -> None:
    report = analyze_pvc_binding(_pvc(phase="Pending", volume_name="my-pv"))
    assert any("my-pv" in chk for chk in report.findings[0].next_checks)


def test_prebound_pending_pvc_does_not_run_class_resolution() -> None:
    # Even with no storage classes, pre-bound PVC must not emit storage class findings
    report = analyze_pvc_binding(
        _pvc(phase="Pending", volume_name="my-pv", storage_class_name=None), ()
    )
    assert report.findings[0].rule_id == "pvc.awaiting_prebound_volume"


def test_prebound_provisioning_failure_takes_precedence_over_awaiting() -> None:
    # ProvisioningFailed / VolumeMismatch events outrank awaiting_prebound_volume
    report = analyze_pvc_binding(
        _pvc(phase="Pending", volume_name="my-pv"),
        (),
        (_event("VolumeMismatch", "wrong size"),),
    )
    assert report.findings[0].rule_id == "pvc.provisioning_failed"


def test_prebound_volume_mismatch_event_takes_precedence() -> None:
    report = analyze_pvc_binding(
        _pvc(phase="Pending", volume_name="my-pv"),
        (),
        (_event("FailedBinding", "no match"),),
    )
    assert report.findings[0].rule_id == "pvc.provisioning_failed"


# ---------------------------------------------------------------------------
# Finding 3: Failure event selection — newest last_seen first, then count desc
# ---------------------------------------------------------------------------


def test_newer_provisioning_failed_outranks_older_failed_binding() -> None:
    events = [
        _event("FailedBinding", "old error", count=100, last_seen="2024-01-01T00:00:00Z"),
        _event("ProvisioningFailed", "new error", count=1, last_seen="2024-06-01T00:00:00Z"),
    ]
    report = analyze_pvc_binding(_pvc(), (), events)
    assert report.findings[0].rule_id == "pvc.provisioning_failed"
    ev_messages = [e.value for e in report.findings[0].evidence if e.field == "event.message"]
    assert ev_messages == ["new error"]


def test_count_tiebreaker_when_last_seen_equal() -> None:
    events = [
        _event("ProvisioningFailed", "low count", count=1, last_seen="2024-06-01T00:00:00Z"),
        _event("FailedBinding", "high count", count=50, last_seen="2024-06-01T00:00:00Z"),
    ]
    report = analyze_pvc_binding(_pvc(), (), events)
    ev_messages = [e.value for e in report.findings[0].evidence if e.field == "event.message"]
    assert ev_messages == ["high count"]


def test_failure_event_evidence_includes_count_and_last_seen() -> None:
    events = [
        _event("ProvisioningFailed", "quota exceeded", count=5, last_seen="2024-03-15T10:00:00Z")
    ]
    report = analyze_pvc_binding(_pvc(), (), events)
    evidence_fields = {e.field: e.value for e in report.findings[0].evidence}
    assert evidence_fields["event.count"] == "5"
    assert evidence_fields["event.last_seen"] == "2024-03-15T10:00:00Z"


def test_failure_event_last_seen_omitted_when_empty() -> None:
    ev = WarningEventSnapshot(reason="ProvisioningFailed", message="x", count=1, last_seen="")
    report = analyze_pvc_binding(_pvc(), (), [ev])
    evidence_fields = [e.field for e in report.findings[0].evidence]
    assert "event.last_seen" not in evidence_fields


# ---------------------------------------------------------------------------
# Regression: pre-bound + explicit-empty storageClassName (canonical order)
# ---------------------------------------------------------------------------


def test_prebound_with_empty_storage_class_emits_awaiting_prebound_volume() -> None:
    """volume_name='my-pv' + storage_class_name='' must emit pvc.awaiting_prebound_volume.

    Pre-bound check must precede explicit-empty-static binding so spec.volumeName wins.
    """
    report = analyze_pvc_binding(_pvc(phase="Pending", volume_name="my-pv", storage_class_name=""))
    assert report.findings[0].rule_id == "pvc.awaiting_prebound_volume"


def test_prebound_with_empty_storage_class_evidence_has_volume_name() -> None:
    """Evidence for the pre-bound+empty-class case must include spec.volumeName='my-pv'."""
    report = analyze_pvc_binding(_pvc(phase="Pending", volume_name="my-pv", storage_class_name=""))
    fields = {e.field: e.value for e in report.findings[0].evidence}
    assert fields.get("spec.volumeName") == "my-pv"


def test_prebound_with_empty_storage_class_next_checks_name_pv() -> None:
    """next_checks must reference the named PV for pre-bound+empty-class."""
    report = analyze_pvc_binding(_pvc(phase="Pending", volume_name="my-pv", storage_class_name=""))
    assert any("my-pv" in chk for chk in report.findings[0].next_checks)


# ---------------------------------------------------------------------------
# Event selection — chronological (not lexicographic) newest wins
# ---------------------------------------------------------------------------


def test_chronologically_newer_event_wins_over_lexicographically_larger_timestamp() -> None:
    """RFC3339 offset timestamps must be compared chronologically, not as strings.

    "2024-01-01T10:00:00+05:30" is lexicographically larger but chronologically
    earlier (04:30 UTC) than "2024-01-01T05:00:00Z" (05:00 UTC).
    """
    events = [
        # Lexicographically larger, but earlier in UTC (04:30 UTC)
        _event("FailedBinding", "earlier-utc", count=1, last_seen="2024-01-01T10:00:00+05:30"),
        # Lexicographically smaller, but chronologically newer (05:00 UTC)
        _event("ProvisioningFailed", "newer-utc", count=1, last_seen="2024-01-01T05:00:00Z"),
    ]
    report = analyze_pvc_binding(_pvc(), (), events)
    ev_messages = [e.value for e in report.findings[0].evidence if e.field == "event.message"]
    assert ev_messages == ["newer-utc"]


def test_fractional_seconds_are_handled_chronologically() -> None:
    """Fractional-second RFC3339 timestamps must not break chronological comparison."""
    events = [
        _event("FailedBinding", "low-frac", count=1, last_seen="2024-06-01T12:00:00.100Z"),
        _event("ProvisioningFailed", "high-frac", count=1, last_seen="2024-06-01T12:00:00.900Z"),
    ]
    report = analyze_pvc_binding(_pvc(), (), events)
    ev_messages = [e.value for e in report.findings[0].evidence if e.field == "event.message"]
    assert ev_messages == ["high-frac"]


def test_invalid_timestamp_sorts_last() -> None:
    """Events with unparsable last_seen must sort after valid timestamps."""
    events = [
        WarningEventSnapshot(
            reason="ProvisioningFailed", message="invalid-ts", count=99, last_seen="not-a-date"
        ),
        _event("FailedBinding", "valid-ts", count=1, last_seen="2024-01-01T00:00:00Z"),
    ]
    report = analyze_pvc_binding(_pvc(), (), events)
    ev_messages = [e.value for e in report.findings[0].evidence if e.field == "event.message"]
    assert ev_messages == ["valid-ts"]


# ============================================================
# PR #216 review findings — RED tests
# ============================================================

# --- Item 3: exact StorageClass evidence paths (top-level volumeBindingMode) ---


def test_wait_for_first_consumer_evidence_uses_top_level_path() -> None:
    """WaitForFirstConsumer evidence path must be 'volumeBindingMode', not 'spec.volumeBindingMode'."""
    report = analyze_pvc_binding(
        _pvc(storage_class_name="managed"),
        (_class("managed", volume_binding_mode="WaitForFirstConsumer"),),
    )
    assert report.findings[0].rule_id == "pvc.waiting_for_first_consumer"
    paths = [e.field for e in report.findings[0].evidence]
    assert "volumeBindingMode" in paths, f"Expected 'volumeBindingMode' in {paths}"
    assert "spec.volumeBindingMode" not in paths, (
        f"'spec.volumeBindingMode' must not appear in {paths}"
    )


def test_provisioning_pending_evidence_uses_top_level_path() -> None:
    """Generic pending evidence path must be 'volumeBindingMode', not 'spec.volumeBindingMode'."""
    report = analyze_pvc_binding(
        _pvc(storage_class_name="managed"),
        (_class("managed", volume_binding_mode="Immediate"),),
    )
    assert report.findings[0].rule_id == "pvc.provisioning_pending"
    paths = [e.field for e in report.findings[0].evidence]
    assert "volumeBindingMode" in paths, f"Expected 'volumeBindingMode' in {paths}"
    assert "spec.volumeBindingMode" not in paths, (
        f"'spec.volumeBindingMode' must not appear in {paths}"
    )


# --- Item 4: exact default annotation identity ---


def test_multiple_default_evidence_uses_actual_annotation_key_stable() -> None:
    """Multiple-default evidence must use the actual annotation key, not a synthetic one."""
    sc_a = StorageClassSnapshot(
        identity=ResourceIdentity(kind="StorageClass", namespace="", name="a"),
        provisioner="p",
        volume_binding_mode="Immediate",
        is_default=True,
        default_annotation_key="storageclass.kubernetes.io/is-default-class",
        default_annotation_value="true",
    )
    sc_b = StorageClassSnapshot(
        identity=ResourceIdentity(kind="StorageClass", namespace="", name="b"),
        provisioner="p",
        volume_binding_mode="Immediate",
        is_default=True,
        default_annotation_key="storageclass.kubernetes.io/is-default-class",
        default_annotation_value="true",
    )
    report = analyze_pvc_binding(_pvc(storage_class_name=None), (sc_a, sc_b))
    assert report.findings[0].rule_id == "pvc.multiple_default_storage_classes"
    for ev in report.findings[0].evidence:
        assert ev.field.startswith("metadata.annotations."), (
            f"Evidence field must start with 'metadata.annotations.' but got: {ev.field!r}"
        )
        assert ev.value == "true", f"Evidence value must be 'true' but got: {ev.value!r}"


def test_multiple_default_evidence_uses_actual_annotation_key_beta() -> None:
    """Multiple-default evidence must preserve beta annotation key when that was set."""
    sc_a = StorageClassSnapshot(
        identity=ResourceIdentity(kind="StorageClass", namespace="", name="a"),
        provisioner="p",
        volume_binding_mode="Immediate",
        is_default=True,
        default_annotation_key="storageclass.beta.kubernetes.io/is-default-class",
        default_annotation_value="true",
    )
    sc_b = StorageClassSnapshot(
        identity=ResourceIdentity(kind="StorageClass", namespace="", name="b"),
        provisioner="p",
        volume_binding_mode="Immediate",
        is_default=True,
        default_annotation_key="storageclass.beta.kubernetes.io/is-default-class",
        default_annotation_value="true",
    )
    report = analyze_pvc_binding(_pvc(storage_class_name=None), (sc_a, sc_b))
    for ev in report.findings[0].evidence:
        assert "beta" in ev.field, (
            f"Beta annotation key must be reflected in evidence field, got: {ev.field!r}"
        )


# ---------------------------------------------------------------------------
# Item 1: Multiple defaults — effective class selection and dual findings
# ---------------------------------------------------------------------------


def test_multiple_defaults_newer_wffc_gives_both_findings() -> None:
    """Newer WaitForFirstConsumer default: report both rule IDs in the same AnalysisReport."""
    older_immediate = _class(
        "immediate-sc",
        volume_binding_mode="Immediate",
        is_default=True,
        created="2024-01-01T00:00:00Z",
    )
    newer_wffc = _class(
        "wffc-sc",
        volume_binding_mode="WaitForFirstConsumer",
        is_default=True,
        created="2024-06-01T00:00:00Z",
    )
    report = analyze_pvc_binding(_pvc(storage_class_name=None), (older_immediate, newer_wffc))
    assert report.outcome == "findings"
    rule_ids = [f.rule_id for f in report.findings]
    assert "pvc.multiple_default_storage_classes" in rule_ids
    assert "pvc.waiting_for_first_consumer" in rule_ids


def test_multiple_defaults_effective_class_is_newest_by_timestamp() -> None:
    """Kubernetes selects the most recently created default; effective class is newest."""
    older = _class(
        "old-sc", volume_binding_mode="Immediate", is_default=True, created="2023-01-01T00:00:00Z"
    )
    newer = _class(
        "new-sc",
        volume_binding_mode="WaitForFirstConsumer",
        is_default=True,
        created="2024-01-01T00:00:00Z",
    )
    report = analyze_pvc_binding(_pvc(storage_class_name=None), (older, newer))
    wffc_finding = next(f for f in report.findings if f.rule_id == "pvc.waiting_for_first_consumer")
    assert any(ev.value == "WaitForFirstConsumer" for ev in wffc_finding.evidence)


def test_multiple_defaults_older_wffc_gives_immediate_effective_finding() -> None:
    """Older WaitForFirstConsumer default: newer Immediate is effective."""
    older_wffc = _class(
        "wffc-sc",
        volume_binding_mode="WaitForFirstConsumer",
        is_default=True,
        created="2023-01-01T00:00:00Z",
    )
    newer_immediate = _class(
        "imm-sc", volume_binding_mode="Immediate", is_default=True, created="2024-06-01T00:00:00Z"
    )
    report = analyze_pvc_binding(_pvc(storage_class_name=None), (older_wffc, newer_immediate))
    rule_ids = [f.rule_id for f in report.findings]
    assert "pvc.multiple_default_storage_classes" in rule_ids
    assert "pvc.provisioning_pending" in rule_ids
    assert "pvc.waiting_for_first_consumer" not in rule_ids


def test_multiple_defaults_name_tiebreak_is_deterministic() -> None:
    """Equal timestamps: name tie-break selects the lexicographically smallest class."""
    sc_a = _class(
        "alpha-sc", volume_binding_mode="Immediate", is_default=True, created="2024-01-01T00:00:00Z"
    )
    sc_z = _class(
        "zeta-sc",
        volume_binding_mode="WaitForFirstConsumer",
        is_default=True,
        created="2024-01-01T00:00:00Z",
    )
    report1 = analyze_pvc_binding(_pvc(storage_class_name=None), (sc_a, sc_z))
    report2 = analyze_pvc_binding(_pvc(storage_class_name=None), (sc_z, sc_a))
    rule_ids_1 = [f.rule_id for f in report1.findings]
    rule_ids_2 = [f.rule_id for f in report2.findings]
    assert rule_ids_1 == rule_ids_2
    assert "pvc.provisioning_pending" in rule_ids_1
    assert "pvc.waiting_for_first_consumer" not in rule_ids_1
    effective = next(f for f in report1.findings if f.rule_id == "pvc.provisioning_pending")
    evidence_fields = {e.field: e.value for e in effective.evidence}
    assert evidence_fields["volumeBindingMode"] == "Immediate"


def test_multiple_defaults_retain_evidence_and_related() -> None:
    """Multiple-default finding retains evidence for all defaults and related identities."""
    sc_a = _class("a", is_default=True, created="2024-01-01T00:00:00Z")
    sc_b = _class("b", is_default=True, created="2024-06-01T00:00:00Z")
    report = analyze_pvc_binding(_pvc(storage_class_name=None), (sc_a, sc_b))
    multi = next(f for f in report.findings if f.rule_id == "pvc.multiple_default_storage_classes")
    assert {item.name for item in multi.related} == {"a", "b"}
    assert len(multi.evidence) == 2


# ============================================================
# PR #216 round-3 findings
# ============================================================

# --- Issue 3: events gap + Immediate → incomplete ---


def test_events_gap_with_immediate_class_returns_incomplete() -> None:
    """Immediate-class generic pending must not claim 'no specific failure' when events are unavailable."""
    gap = EvidenceGap("events", "forbidden (HTTP 403)")
    report = analyze_pvc_binding(
        _pvc(storage_class_name="managed"),
        (_class("managed", volume_binding_mode="Immediate"),),
        gaps=(gap,),
    )
    assert report.outcome == "incomplete"
    assert report.findings == ()
    assert gap in report.gaps


def test_events_gap_with_wffc_class_emits_finding_with_gap() -> None:
    """WaitForFirstConsumer is deterministic; events gap must not suppress it."""
    gap = EvidenceGap("events", "forbidden (HTTP 403)")
    report = analyze_pvc_binding(
        _pvc(storage_class_name="managed"),
        (_class("managed", volume_binding_mode="WaitForFirstConsumer"),),
        gaps=(gap,),
    )
    assert report.outcome == "findings"
    assert report.findings[0].rule_id == "pvc.waiting_for_first_consumer"
    assert gap in report.gaps


def test_events_gap_does_not_suppress_missing_class_finding() -> None:
    """pvc.storage_class_not_found is class-independent and must coexist with events gap."""
    gap = EvidenceGap("events", "forbidden (HTTP 403)")
    report = analyze_pvc_binding(
        _pvc(storage_class_name="missing"),
        (_class("other"),),
        gaps=(gap,),
    )
    assert report.outcome == "findings"
    assert report.findings[0].rule_id == "pvc.storage_class_not_found"
    assert gap in report.gaps


def test_events_gap_does_not_suppress_no_default_class_finding() -> None:
    """pvc.no_default_storage_class is class-independent and must coexist with events gap."""
    gap = EvidenceGap("events", "forbidden (HTTP 403)")
    report = analyze_pvc_binding(
        _pvc(storage_class_name=None),
        (),
        gaps=(gap,),
    )
    assert report.outcome == "findings"
    assert report.findings[0].rule_id == "pvc.no_default_storage_class"
    assert gap in report.gaps


def test_events_gap_with_multiple_defaults_immediate_keeps_multi_default_finding() -> None:
    """Duplicate-defaults finding is deterministic; events gap must not suppress it."""
    gap = EvidenceGap("events", "forbidden (HTTP 403)")
    sc_a = _class("a", is_default=True, created="2024-01-01T00:00:00Z")
    sc_b = _class("b", is_default=True, created="2024-06-01T00:00:00Z")
    report = analyze_pvc_binding(
        _pvc(storage_class_name=None),
        (sc_a, sc_b),
        gaps=(gap,),
    )
    rule_ids = [f.rule_id for f in report.findings]
    assert "pvc.multiple_default_storage_classes" in rule_ids
    assert "pvc.provisioning_pending" not in rule_ids
    assert gap in report.gaps


def test_events_gap_with_multiple_defaults_wffc_keeps_both_findings() -> None:
    """WFFC effective class is deterministic; both findings survive events gap."""
    gap = EvidenceGap("events", "forbidden (HTTP 403)")
    sc_old = _class(
        "old", volume_binding_mode="Immediate", is_default=True, created="2023-01-01T00:00:00Z"
    )
    sc_new = _class(
        "new",
        volume_binding_mode="WaitForFirstConsumer",
        is_default=True,
        created="2024-01-01T00:00:00Z",
    )
    report = analyze_pvc_binding(
        _pvc(storage_class_name=None),
        (sc_old, sc_new),
        gaps=(gap,),
    )
    rule_ids = [f.rule_id for f in report.findings]
    assert "pvc.multiple_default_storage_classes" in rule_ids
    assert "pvc.waiting_for_first_consumer" in rule_ids
    assert gap in report.gaps
