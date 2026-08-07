"""Shared deterministic finding/report contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Severity = Literal["info", "warning", "critical"]
Confidence = Literal["high", "medium"]
Outcome = Literal["healthy", "findings", "incomplete", "not_applicable"]


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
class AnalysisReport:
    """Deterministic diagnostic output for one resource."""

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
