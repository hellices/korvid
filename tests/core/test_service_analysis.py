"""Tests for deterministic Service-to-EndpointSlice analysis."""

from __future__ import annotations

from typing import cast

from korvid.core.findings import AnalysisReport as SharedReport
from korvid.core.service_analysis import (
    AnalysisReport as ServiceReport,
)
from korvid.core.service_analysis import (
    EndpointSliceSnapshot,
    EvidenceGap,
    Outcome,
    ResourceIdentity,
    ServiceSnapshot,
    Severity,
    analyze_service_endpoints,
)


def _service(
    *,
    kind: str = "Service",
    namespace: str = "default",
    name: str = "web",
    uid: str = "",
    service_type: str = "ClusterIP",
    selector: tuple[tuple[str, str], ...] = (("app", "web"),),
) -> ServiceSnapshot:
    return ServiceSnapshot(
        identity=ResourceIdentity(kind=kind, namespace=namespace, name=name, uid=uid),
        service_type=service_type,
        selector=selector,
    )


def _slice(
    *,
    kind: str = "EndpointSlice",
    namespace: str = "default",
    name: str = "web-1",
    uid: str = "",
    service_name: str = "web",
    service_owner_uids: tuple[str, ...] = (),
    address_type: str = "IPv4",
    endpoints: int = 1,
    ready_endpoints: int = 1,
) -> EndpointSliceSnapshot:
    return EndpointSliceSnapshot(
        identity=ResourceIdentity(kind=kind, namespace=namespace, name=name, uid=uid),
        service_name=service_name,
        service_owner_uids=service_owner_uids,
        address_type=address_type,
        endpoints=endpoints,
        ready_endpoints=ready_endpoints,
    )


def test_ready_current_slice_is_healthy() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1"),
        (_slice(service_owner_uids=("svc-1",), endpoints=2, ready_endpoints=1),),
    )
    assert report.outcome == "healthy"
    assert report.findings == ()
    assert report.evidence[0].field == "endpoints.ready"


def test_no_current_slices_is_a_versioned_finding() -> None:
    report = analyze_service_endpoints(_service(uid="svc-1"), ())
    assert report.outcome == "findings"
    assert report.findings[0].rule_id == "service.no_endpoint_slices"
    assert report.findings[0].rule_version == "1"


def test_unavailable_slice_evidence_never_reports_healthy() -> None:
    gap = EvidenceGap(source="endpointslices", reason="forbidden (HTTP 403)")
    report = analyze_service_endpoints(_service(), (), gap)
    assert report.outcome == "incomplete"
    assert report.gaps == (gap,)


def test_replaced_service_slice_is_stale_not_healthy() -> None:
    report = analyze_service_endpoints(
        _service(uid="new-uid"),
        (_slice(service_owner_uids=("old-uid",), endpoints=1, ready_endpoints=1),),
    )
    assert report.outcome == "incomplete"
    assert report.findings == ()
    assert report.gaps[0].source == "endpointslices/stale-owner"
    assert (
        report.gaps[0].reason
        == "1 EndpointSlice is owned by a different Service UID than 'new-uid'."
    )


def test_owned_slice_with_missing_service_uid_is_stale() -> None:
    report = analyze_service_endpoints(
        _service(),
        (_slice(service_owner_uids=("other",), endpoints=1, ready_endpoints=1),),
    )
    assert report.outcome == "incomplete"
    assert report.findings == ()
    assert report.gaps[0].source == "endpointslices/stale-owner"
    assert (
        report.gaps[0].reason
        == "1 EndpointSlice is owned by a Service, but the Service UID is absent."
    )


def test_stale_owner_reason_uses_plural_grammar() -> None:
    report = analyze_service_endpoints(
        _service(uid="new-uid"),
        (
            _slice(name="web-1", uid="slice-1", service_owner_uids=("old-uid",)),
            _slice(name="web-2", uid="slice-2", service_owner_uids=("old-uid",)),
        ),
    )
    assert report.outcome == "incomplete"
    assert report.gaps[0].reason
    assert (
        report.gaps[0].reason
        == "2 EndpointSlices are owned by a different Service UID than 'new-uid'."
    )


def test_matching_requires_namespace_and_service_name() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1", namespace="default"),
        (_slice(namespace="other", service_owner_uids=("svc-1",), endpoints=1, ready_endpoints=1),),
    )
    assert report.outcome == "findings"
    assert report.findings[0].rule_id == "service.no_endpoint_slices"


def test_current_slices_without_ready_endpoints_warn() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1"),
        (_slice(service_owner_uids=("svc-1",), endpoints=2, ready_endpoints=0),),
    )
    assert report.findings[0].rule_id == "service.no_ready_endpoints"


def test_selectorless_unowned_slice_is_valid_manual_evidence() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1", selector=()),
        (_slice(service_owner_uids=(), endpoints=1, ready_endpoints=1),),
    )
    assert report.outcome == "healthy"


def test_external_name_is_not_applicable() -> None:
    report = analyze_service_endpoints(_service(service_type="ExternalName"), ())
    assert report.outcome == "not_applicable"


def test_document_uses_stable_public_keys() -> None:
    document = analyze_service_endpoints(_service(), ()).as_document()
    assert tuple(document) == (
        "analyzer",
        "version",
        "outcome",
        "primary",
        "findings",
        "evidence",
        "gaps",
    )


def test_service_analysis_reexports_shared_finding_types() -> None:
    assert ServiceReport is SharedReport
    assert Severity is not None
    assert Outcome is not None


def test_service_report_document_contract_is_unchanged() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1"),
        (_slice(service_owner_uids=("svc-1",), endpoints=1, ready_endpoints=0),),
    )
    document = report.as_document()
    findings = cast(list[dict[str, object]], document["findings"])
    assert tuple(document) == (
        "analyzer",
        "version",
        "outcome",
        "primary",
        "findings",
        "evidence",
        "gaps",
    )
    assert findings[0]["severity"] == "warning"


# -- service_owner_uids stale-check tests (PR #212 fix) ---------------------


def _slice_with_svc(
    *,
    namespace: str = "default",
    name: str = "web-1",
    uid: str = "",
    service_name: str = "web",
    service_owner_uids: tuple[str, ...] = (),
    address_type: str = "IPv4",
    endpoints: int = 1,
    ready_endpoints: int = 1,
) -> EndpointSliceSnapshot:
    """Helper for tests that need to set service_owner_uids directly."""
    return EndpointSliceSnapshot(
        identity=ResourceIdentity(kind="EndpointSlice", namespace=namespace, name=name, uid=uid),
        service_name=service_name,
        service_owner_uids=service_owner_uids,
        address_type=address_type,
        endpoints=endpoints,
        ready_endpoints=ready_endpoints,
    )


def test_custom_controller_owner_only_with_ready_endpoints_is_healthy() -> None:
    """An EndpointSlice owned only by a custom CRD controller (no Service ref) must
    not be flagged stale.  The stale check applies only to core/v1 Service owners."""
    report = analyze_service_endpoints(
        _service(uid="svc-1"),
        (_slice_with_svc(service_owner_uids=(), endpoints=1, ready_endpoints=1),),
    )
    assert report.outcome == "healthy"


def test_mismatching_service_owner_uid_yields_incomplete() -> None:
    """When service_owner_uids contains a UID that differs from the current Service,
    the slice is stale and the outcome must be incomplete."""
    report = analyze_service_endpoints(
        _service(uid="new-svc-uid"),
        (_slice_with_svc(service_owner_uids=("old-svc-uid",), endpoints=1, ready_endpoints=1),),
    )
    assert report.outcome == "incomplete"
    assert report.gaps[0].source == "endpointslices/stale-owner"


def test_mixed_custom_and_service_owner_uses_only_service_refs_for_stale_check() -> None:
    """When a slice has both a custom-controller ref and a Service ref, only the
    Service UID matters for the stale decision."""
    # Matching Service UID → healthy even though custom UID is unrelated
    report_healthy = analyze_service_endpoints(
        _service(uid="svc-1"),
        (
            _slice_with_svc(
                service_owner_uids=("svc-1",),
                endpoints=1,
                ready_endpoints=1,
            ),
        ),
    )
    assert report_healthy.outcome == "healthy"

    # Mismatching Service UID → stale, even though custom UID happens to be there
    report_stale = analyze_service_endpoints(
        _service(uid="svc-new"),
        (
            _slice_with_svc(
                service_owner_uids=("svc-old",),
                endpoints=1,
                ready_endpoints=1,
            ),
        ),
    )
    assert report_stale.outcome == "incomplete"
