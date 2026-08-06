"""Deterministic Service-to-EndpointSlice analysis contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Severity = Literal["warning"]
Confidence = Literal["high", "medium"]
Outcome = Literal["healthy", "findings", "incomplete", "not_applicable"]

_ANALYZER = "service.endpoints"
_VERSION = "1"
_RULE_VERSION = "1"


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    """Stable identity for a Kubernetes resource."""

    kind: str
    namespace: str
    name: str
    uid: str = ""


@dataclass(frozen=True, slots=True)
class Evidence:
    """One deterministic evidence item for a report."""

    resource: ResourceIdentity
    field: str
    value: str


@dataclass(frozen=True, slots=True)
class EvidenceGap:
    """A missing or untrusted evidence source."""

    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class Finding:
    """A versioned rule result for a single diagnostic finding."""

    rule_id: str
    rule_version: str
    severity: Severity
    confidence: Confidence
    primary: ResourceIdentity
    related: tuple[ResourceIdentity, ...]
    evidence: tuple[Evidence, ...]
    explanation: str
    next_checks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    """Immutable Service input for the analyzer."""

    identity: ResourceIdentity
    service_type: str
    selector: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class EndpointSliceSnapshot:
    """Immutable EndpointSlice input for the analyzer."""

    identity: ResourceIdentity
    service_name: str
    owner_uids: tuple[str, ...]
    address_type: str
    endpoints: int
    ready_endpoints: int


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """Deterministic diagnostic output for one Service."""

    analyzer: str
    version: str
    outcome: Outcome
    primary: ResourceIdentity
    findings: tuple[Finding, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    gaps: tuple[EvidenceGap, ...] = ()

    def as_document(self) -> dict[str, object]:
        """Return a stable structured document view."""

        return {
            "analyzer": self.analyzer,
            "version": self.version,
            "outcome": self.outcome,
            "primary": _resource_document(self.primary),
            "findings": [_finding_document(finding) for finding in self.findings],
            "evidence": [_evidence_document(item) for item in self.evidence],
            "gaps": [_gap_document(item) for item in self.gaps],
        }


def analyze_service_endpoints(
    service: ServiceSnapshot,
    slices: Sequence[EndpointSliceSnapshot],
    gap: EvidenceGap | None = None,
) -> AnalysisReport:
    """Analyze a Service against EndpointSlice snapshots."""

    if service.service_type == "ExternalName":
        return _not_applicable_report(service.identity)
    if gap is not None:
        return _incomplete_report(service.identity, (gap,))

    matching = tuple(
        sorted(
            (
                item
                for item in slices
                if item.service_name == service.identity.name
                and item.identity.namespace == service.identity.namespace
            ),
            key=_slice_sort_key,
        )
    )
    current, stale = _partition_current(service.identity.uid, matching)
    if stale:
        return _incomplete_report(
            service.identity,
            (EvidenceGap("endpointslices/stale-owner", _stale_reason(service, stale)),),
        )
    if not current:
        return _no_slices_report(service)

    evidence = _current_evidence(current)
    if sum(item.ready_endpoints for item in current) == 0:
        return _no_ready_report(service, current, evidence)
    return AnalysisReport(
        analyzer=_ANALYZER,
        version=_VERSION,
        outcome="healthy",
        primary=service.identity,
        evidence=evidence,
    )


def _partition_current(
    service_uid: str,
    slices: Sequence[EndpointSliceSnapshot],
) -> tuple[tuple[EndpointSliceSnapshot, ...], tuple[EndpointSliceSnapshot, ...]]:
    current: list[EndpointSliceSnapshot] = []
    stale: list[EndpointSliceSnapshot] = []
    for item in slices:
        if item.owner_uids and (not service_uid or service_uid not in item.owner_uids):
            stale.append(item)
        else:
            current.append(item)
    return tuple(current), tuple(stale)


def _slice_sort_key(item: EndpointSliceSnapshot) -> tuple[str, str, str, str]:
    return (
        item.identity.namespace,
        item.identity.name,
        item.identity.uid,
        item.address_type,
    )


def _current_evidence(slices: Sequence[EndpointSliceSnapshot]) -> tuple[Evidence, ...]:
    items: list[Evidence] = []
    for item in slices:
        items.append(Evidence(item.identity, "endpoints.ready", str(item.ready_endpoints)))
        items.append(Evidence(item.identity, "endpoints.total", str(item.endpoints)))
        items.append(Evidence(item.identity, "endpoints.address_type", item.address_type))
        if item.owner_uids:
            items.append(Evidence(item.identity, "endpoints.owner_uids", ",".join(item.owner_uids)))
    return tuple(items)


def _confidence_for_healthy(
    service: ServiceSnapshot, slices: Sequence[EndpointSliceSnapshot]
) -> Confidence:
    if not service.identity.uid:
        return "medium"
    if any(service.identity.uid in item.owner_uids for item in slices if item.owner_uids):
        return "high"
    return "medium"


def _no_slices_report(service: ServiceSnapshot) -> AnalysisReport:
    finding = Finding(
        rule_id="service.no_endpoint_slices",
        rule_version=_RULE_VERSION,
        severity="warning",
        confidence="medium",
        primary=service.identity,
        related=(),
        evidence=(),
        explanation="No EndpointSlices matched the Service name.",
        next_checks=(
            "Confirm EndpointSlice discovery is available in the namespace.",
            "Verify the Service selector or manual slice labels match the Service name.",
        ),
    )
    return AnalysisReport(
        analyzer=_ANALYZER,
        version=_VERSION,
        outcome="findings",
        primary=service.identity,
        findings=(finding,),
    )


def _no_ready_report(
    service: ServiceSnapshot,
    slices: Sequence[EndpointSliceSnapshot],
    evidence: tuple[Evidence, ...],
) -> AnalysisReport:
    finding = Finding(
        rule_id="service.no_ready_endpoints",
        rule_version=_RULE_VERSION,
        severity="warning",
        confidence=_confidence_for_healthy(service, slices),
        primary=service.identity,
        related=tuple(item.identity for item in slices),
        evidence=evidence,
        explanation="Matching EndpointSlices exist, but none report ready endpoints.",
        next_checks=(
            "Inspect the matching EndpointSlices for readiness and address counts.",
            "Check the backing Pods or manual slice payload for readiness issues.",
        ),
    )
    return AnalysisReport(
        analyzer=_ANALYZER,
        version=_VERSION,
        outcome="findings",
        primary=service.identity,
        findings=(finding,),
        evidence=evidence,
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
        gaps=gaps,
    )


def _not_applicable_report(primary: ResourceIdentity) -> AnalysisReport:
    return AnalysisReport(
        analyzer=_ANALYZER,
        version=_VERSION,
        outcome="not_applicable",
        primary=primary,
    )


def _stale_reason(service: ServiceSnapshot, stale: Sequence[EndpointSliceSnapshot]) -> str:
    count = len(stale)
    verb = "is" if count == 1 else "are"
    if service.identity.uid:
        return (
            f"{count} EndpointSlice{'' if count == 1 else 's'} {verb} owned by a "
            f"different Service UID than {service.identity.uid!r}."
        )
    return f"{count} EndpointSlice{'' if count == 1 else 's'} {verb} owned by a Service, but the Service UID is absent."


def _resource_document(resource: ResourceIdentity) -> dict[str, str]:
    return {
        "kind": resource.kind,
        "namespace": resource.namespace,
        "name": resource.name,
        "uid": resource.uid,
    }


def _evidence_document(item: Evidence) -> dict[str, object]:
    return {
        "resource": _resource_document(item.resource),
        "field": item.field,
        "value": item.value,
    }


def _gap_document(item: EvidenceGap) -> dict[str, str]:
    return {"source": item.source, "reason": item.reason}


def _finding_document(item: Finding) -> dict[str, object]:
    return {
        "rule_id": item.rule_id,
        "rule_version": item.rule_version,
        "severity": item.severity,
        "confidence": item.confidence,
        "primary": _resource_document(item.primary),
        "related": [_resource_document(resource) for resource in item.related],
        "evidence": [_evidence_document(evidence) for evidence in item.evidence],
        "explanation": item.explanation,
        "next_checks": list(item.next_checks),
    }
