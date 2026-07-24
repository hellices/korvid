"""Typed summaries of Kubernetes objects (parsing isolated from I/O)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Kubernetes quantity suffixes → multiplier relative to the base unit.
_CPU_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3}
_MEM_SUFFIXES = {
    "": 1,
    "k": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
}


def parse_cpu(quantity: str) -> float:
    """Parse a Kubernetes CPU quantity into cores (e.g. '100m' -> 0.1)."""
    for suffix, mult in sorted(_CPU_SUFFIXES.items(), key=lambda kv: -len(kv[0])):
        if suffix and quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * mult
    return float(quantity)


def parse_memory(quantity: str) -> int:
    """Parse a Kubernetes memory quantity into bytes (e.g. '128Mi' -> 134217728)."""
    for suffix, mult in sorted(_MEM_SUFFIXES.items(), key=lambda kv: -len(kv[0])):
        if suffix and quantity.endswith(suffix):
            return int(float(quantity[: -len(suffix)]) * mult)
    return int(float(quantity))


def format_cpu(cores: float) -> str:
    """Render cores as millicores (k9s convention): 1.1 -> '1100m'."""
    return f"{round(cores * 1000)}m"


def format_memory(size: int) -> str:
    """Render bytes as whole Mi (k9s convention): 134217728 -> '128Mi'."""
    return f"{round(size / 2**20)}Mi"


def _sum_resources(containers: list[dict[str, Any]], bucket: str, key: str) -> str:
    """Sum a resource quantity across containers; '-' when nothing is declared."""
    values = [
        q
        for c in containers
        if (q := ((c.get("resources") or {}).get(bucket) or {}).get(key)) is not None
    ]
    if not values:
        return "-"
    if key == "cpu":
        return format_cpu(sum(parse_cpu(str(v)) for v in values))
    return format_memory(sum(parse_memory(str(v)) for v in values))


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

    @classmethod
    def from_manifest(cls, obj: dict[str, Any]) -> PodSummary:
        meta = obj.get("metadata") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}
        statuses: list[dict[str, Any]] = status.get("containerStatuses") or []
        containers: list[dict[str, Any]] = spec.get("containers") or []
        ready_count = sum(1 for s in statuses if s.get("ready"))
        restarts = sum(int(s.get("restartCount", 0)) for s in statuses)
        return cls(
            name=str(meta.get("name", "")),
            namespace=str(meta.get("namespace", "")),
            phase=str(status.get("phase", "Unknown")),
            ready=f"{ready_count}/{len(statuses)}",
            restarts=restarts,
            node=spec.get("nodeName"),
            qos=str(status.get("qosClass") or "-"),
            cpu_request=_sum_resources(containers, "requests", "cpu"),
            mem_request=_sum_resources(containers, "requests", "memory"),
            cpu_limit=_sum_resources(containers, "limits", "cpu"),
            mem_limit=_sum_resources(containers, "limits", "memory"),
        )
