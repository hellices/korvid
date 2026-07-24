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
            cpu_request=_effective_resource(spec, "requests", "cpu"),
            mem_request=_effective_resource(spec, "requests", "memory"),
            cpu_limit=_effective_resource(spec, "limits", "cpu"),
            mem_limit=_effective_resource(spec, "limits", "memory"),
        )
