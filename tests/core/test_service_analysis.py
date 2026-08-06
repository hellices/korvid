"""Tests for deterministic Service-to-EndpointSlice analysis."""

from __future__ import annotations

from korvid.core.service_analysis import (
    EndpointSliceSnapshot,
    EvidenceGap,
    ResourceIdentity,
    ServiceSnapshot,
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
    owner_uids: tuple[str, ...] = (),
    address_type: str = "IPv4",
    endpoints: int = 1,
    ready_endpoints: int = 1,
) -> EndpointSliceSnapshot:
    return EndpointSliceSnapshot(
        identity=ResourceIdentity(kind=kind, namespace=namespace, name=name, uid=uid),
        service_name=service_name,
        owner_uids=owner_uids,
        address_type=address_type,
        endpoints=endpoints,
        ready_endpoints=ready_endpoints,
    )


def test_ready_current_slice_is_healthy() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1"),
        (_slice(owner_uids=("svc-1",), endpoints=2, ready_endpoints=1),),
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
        (_slice(owner_uids=("old-uid",), endpoints=1, ready_endpoints=1),),
    )
    assert report.outcome == "incomplete"
    assert report.findings == ()
    assert report.gaps[0].source == "endpointslices/stale-owner"


def test_current_slices_without_ready_endpoints_warn() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1"),
        (_slice(owner_uids=("svc-1",), endpoints=2, ready_endpoints=0),),
    )
    assert report.findings[0].rule_id == "service.no_ready_endpoints"


def test_selectorless_unowned_slice_is_valid_manual_evidence() -> None:
    report = analyze_service_endpoints(
        _service(uid="svc-1", selector=()),
        (_slice(owner_uids=(), endpoints=1, ready_endpoints=1),),
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
