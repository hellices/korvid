"""Typed summaries of Kubernetes objects (parsing isolated from I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

# Full Kubernetes Quantity grammar: signed decimal number followed by an
# optional binarySI / decimalSI suffix or a decimal exponent (e.g. '12e3').
_QUANTITY_RE = re.compile(
    r"^(?P<number>[+-]?\d+(?:\.\d+)?)(?P<suffix>[eE][+-]?\d+|Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE])?$"
)
_SUFFIX_MULTIPLIERS: dict[str, Decimal] = {
    "n": Decimal("1e-9"),
    "u": Decimal("1e-6"),
    "m": Decimal("1e-3"),
    "k": Decimal(10) ** 3,
    "M": Decimal(10) ** 6,
    "G": Decimal(10) ** 9,
    "T": Decimal(10) ** 12,
    "P": Decimal(10) ** 15,
    "E": Decimal(10) ** 18,
    "Ki": Decimal(2) ** 10,
    "Mi": Decimal(2) ** 20,
    "Gi": Decimal(2) ** 30,
    "Ti": Decimal(2) ** 40,
    "Pi": Decimal(2) ** 50,
    "Ei": Decimal(2) ** 60,
}


def parse_quantity(quantity: str) -> Decimal:
    """Parse any Kubernetes Quantity (DecimalSI, BinarySI, or decimal exponent)."""
    text = str(quantity).strip()
    match = _QUANTITY_RE.match(text)
    if match is None:
        raise ValueError(f"invalid Kubernetes quantity: {quantity!r}")
    suffix = match["suffix"]
    if suffix is None:
        return Decimal(match["number"])
    multiplier = _SUFFIX_MULTIPLIERS.get(suffix)
    if multiplier is not None:
        return Decimal(match["number"]) * multiplier
    return Decimal(text)  # decimal exponent form ('12e3'); Decimal parses it natively


def parse_cpu(quantity: str) -> float:
    """Parse a Kubernetes CPU quantity into cores (e.g. '100m' -> 0.1)."""
    return float(parse_quantity(quantity))


def parse_memory(quantity: str) -> int:
    """Parse a Kubernetes memory quantity into bytes (e.g. '128Mi' -> 134217728)."""
    return int(parse_quantity(quantity))


def format_cpu(cores: float) -> str:
    """Render cores as millicores (k9s convention): 1.1 -> '1100m'."""
    return f"{round(cores * 1000)}m"


def format_memory(size: int) -> str:
    """Render bytes as whole Mi (k9s convention); fall back to Ki below 1Mi."""
    if 0 < size < 2**20:
        return f"{round(size / 2**10)}Ki"
    return f"{round(size / 2**20)}Mi"


def _quantities(containers: list[dict[str, Any]], bucket: str, key: str) -> list[str]:
    return [
        str(q)
        for c in containers
        if (q := ((c.get("resources") or {}).get(bucket) or {}).get(key)) is not None
    ]


def _effective_resource(spec: dict[str, Any], bucket: str, key: str) -> str:
    """Effective pod resource per the scheduler.

    max(max(classic initContainers), sum(containers) + sum(sidecars)) where
    sidecars are initContainers with restartPolicy: Always (K8s 1.28+): they
    run for the pod's lifetime so they add to the sum, not the init max.
    Returns '-' when nothing is declared.
    """
    init_containers = spec.get("initContainers") or []
    sidecars = [c for c in init_containers if c.get("restartPolicy") == "Always"]
    classic_init = [c for c in init_containers if c.get("restartPolicy") != "Always"]
    main = _quantities((spec.get("containers") or []) + sidecars, bucket, key)
    init = _quantities(classic_init, bucket, key)
    if not main and not init:
        return "-"
    if key == "cpu":
        total = max(
            sum(parse_cpu(v) for v in main),
            max((parse_cpu(v) for v in init), default=0.0),
        )
        return format_cpu(total)
    total_mem = max(
        sum(parse_memory(v) for v in main),
        max((parse_memory(v) for v in init), default=0),
    )
    return format_memory(total_mem)


@dataclass(frozen=True)
class GenericSummary:
    """Minimal summary for any Kubernetes object kind."""

    name: str
    namespace: str
    kind: str
    created: str  # ISO-8601 timestamp or "" when absent

    @classmethod
    def from_manifest(cls, kind: str, manifest: dict[str, Any]) -> GenericSummary:
        meta = manifest.get("metadata") or {}
        return cls(
            name=str(meta.get("name", "")),
            namespace=str(meta.get("namespace", "")),
            kind=kind,
            created=str(meta.get("creationTimestamp") or ""),
        )

    def age(self, now: datetime | None = None) -> str:
        """Return k9s-style age string ("5m", "3h", "2d"); "-" when created is empty."""
        if not self.created:
            return "-"
        if now is None:
            now = datetime.now(UTC)
        try:
            ts = self.created
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            created_dt = datetime.fromisoformat(ts)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=UTC)
        except ValueError:
            return "-"
        total_seconds = int((now - created_dt).total_seconds())
        if total_seconds < 0:
            return "-"
        days = total_seconds // 86400
        if days >= 1:
            return f"{days}d"
        hours = total_seconds // 3600
        if hours >= 1:
            return f"{hours}h"
        return f"{total_seconds // 60}m"


def _display_phase(
    meta: dict[str, Any], status: dict[str, Any], statuses: list[dict[str, Any]]
) -> str:
    """Displayed pod status mirroring kubectl's printer.

    Container waiting/terminated reasons (CrashLoopBackOff, OOMKilled, ...)
    override ``status.phase``; a deletionTimestamp always wins as Terminating.
    """
    if meta.get("deletionTimestamp"):
        return "Terminating"
    reason = str(status.get("reason") or status.get("phase") or "Unknown")
    for cs in reversed(statuses):
        state = cs.get("state") or {}
        waiting_reason = (state.get("waiting") or {}).get("reason")
        terminated_reason = (state.get("terminated") or {}).get("reason")
        if waiting_reason:
            reason = str(waiting_reason)
        elif terminated_reason:
            reason = str(terminated_reason)
    return reason


@dataclass(frozen=True)
class PodSummary:
    name: str
    namespace: str
    phase: str
    ready: str
    restarts: int
    node: str | None
    qos: str = "-"
    cpu_request: str = "-"
    mem_request: str = "-"
    cpu_limit: str = "-"
    mem_limit: str = "-"
    containers: tuple[str, ...] = ()

    @classmethod
    def from_manifest(cls, obj: dict[str, Any]) -> PodSummary:
        meta = obj.get("metadata") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}
        statuses: list[dict[str, Any]] = status.get("containerStatuses") or []
        ready_count = sum(1 for s in statuses if s.get("ready"))
        restarts = sum(int(s.get("restartCount", 0)) for s in statuses)
        return cls(
            name=str(meta.get("name", "")),
            namespace=str(meta.get("namespace", "")),
            phase=_display_phase(meta, status, statuses),
            ready=f"{ready_count}/{len(statuses)}",
            restarts=restarts,
            node=spec.get("nodeName"),
            qos=str(status.get("qosClass") or "-"),
            cpu_request=_effective_resource(spec, "requests", "cpu"),
            mem_request=_effective_resource(spec, "requests", "memory"),
            cpu_limit=_effective_resource(spec, "limits", "cpu"),
            mem_limit=_effective_resource(spec, "limits", "memory"),
            containers=tuple(
                str(c["name"]) for c in (spec.get("containers") or []) if c.get("name")
            ),
        )
