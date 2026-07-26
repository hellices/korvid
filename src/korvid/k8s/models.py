"""Typed summaries of Kubernetes objects (parsing isolated from I/O)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

# Full Kubernetes Quantity grammar: signed decimal number (including the
# bare-point forms '<digits>.' and '.<digits>') followed by an optional
# binarySI / decimalSI suffix or a decimal exponent (e.g. '12e3').
_QUANTITY_RE = re.compile(
    r"^(?P<number>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
    r"(?P<suffix>[eE][+-]?\d+|Ki|Mi|Gi|Ti|Pi|Ei|[numkMGTPE])?$"
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
    """Render cores as millicores: 1.1 -> '1100m'."""
    return f"{round(cores * 1000)}m"


def format_memory(size: int) -> str:
    """Render bytes as whole Mi; fall back to Ki below 1Mi."""
    if 0 < size < 2**20:
        return f"{round(size / 2**10)}Ki"
    return f"{round(size / 2**20)}Mi"


def format_age(created: str, now: datetime | None = None) -> str:
    """Compact age string ("5m", "3h", "2d") from an RFC 3339 timestamp.

    Returns "-" when `created` is empty, unparsable, or in the future.
    """
    if not created:
        return "-"
    if now is None:
        now = datetime.now(UTC)
    try:
        ts = created
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


def _quantities(containers: list[dict[str, Any]], bucket: str, key: str) -> list[str]:
    return [
        str(q)
        for c in containers
        if (q := ((c.get("resources") or {}).get(bucket) or {}).get(key)) is not None
    ]


def _init_peak_and_sidecars(
    init_containers: list[dict[str, Any]], bucket: str, key: str
) -> tuple[float, float, bool]:
    """Walk initContainers in declaration order, per the scheduler.

    Sidecars (restartPolicy: Always) started before a classic init keep
    running while it executes, so each init's peak is its own request plus
    the cumulative sidecar requests declared before it. Returns
    (init_peak, sidecar_total, declared).
    """
    peak = 0.0
    running = 0.0
    declared = False
    for c in init_containers:
        q = ((c.get("resources") or {}).get(bucket) or {}).get(key)
        value = 0.0
        if q is not None:
            value = float(parse_cpu(q) if key == "cpu" else parse_memory(q))
            declared = True
        if c.get("restartPolicy") == "Always":
            running += value
        else:
            peak = max(peak, running + value)
    return peak, running, declared


def _effective_value(spec: dict[str, Any], bucket: str, key: str) -> float | int | None:
    """Effective pod resource per the scheduler, as an exact numeric value.

    max(init phase peak, sum(containers) + sum(sidecars)) where the init
    phase peak accounts for sidecars already running while later classic
    inits execute (see _init_peak_and_sidecars). Returns None when nothing
    is declared. CPU is cores (float), memory is bytes (int). Pod-level
    resources (spec.resources, K8s 1.34+) take precedence over the
    container-derived calculation, per resource.
    """
    pod_level = ((spec.get("resources") or {}).get(bucket) or {}).get(key)
    if pod_level is not None:
        return parse_cpu(pod_level) if key == "cpu" else parse_memory(pod_level)
    main = _quantities(spec.get("containers") or [], bucket, key)
    init_peak, sidecar_total, init_declared = _init_peak_and_sidecars(
        spec.get("initContainers") or [], bucket, key
    )
    if not main and not init_declared:
        return None
    if key == "cpu":
        return max(sum(parse_cpu(v) for v in main) + sidecar_total, init_peak)
    return max(sum(parse_memory(v) for v in main) + int(sidecar_total), int(init_peak))


@dataclass(frozen=True)
class ContainerLimits:
    """One container's declared limits - the kubelet enforces each limit
    independently, so severity coloring needs the per-container breakdown,
    not a pod-aggregate sum (PR #51 review)."""

    name: str
    cpu_cores: float | None = None
    mem_bytes: int | None = None


def _container_limits(spec: dict[str, Any]) -> tuple[ContainerLimits, ...]:
    """Limits for every container that may contribute to pod metrics: main
    containers, sidecars, and classic inits (which appear while running)."""
    out: list[ContainerLimits] = []
    for c in list(spec.get("containers") or []) + list(spec.get("initContainers") or []):
        limits = (c.get("resources") or {}).get("limits") or {}
        out.append(
            ContainerLimits(
                name=str(c.get("name") or ""),
                cpu_cores=None if "cpu" not in limits else parse_cpu(limits["cpu"]),
                mem_bytes=None if "memory" not in limits else parse_memory(limits["memory"]),
            )
        )
    return tuple(out)


def _pod_level_limit(spec: dict[str, Any], key: str) -> float | int | None:
    """spec.resources.limits (K8s 1.34+): the only true whole-pod ceiling.
    Summed container limits are never one - each is enforced independently."""
    value = ((spec.get("resources") or {}).get("limits") or {}).get(key)
    if value is None:
        return None
    return parse_cpu(value) if key == "cpu" else parse_memory(value)


def _format_effective(value: float | int | None, key: str) -> str:
    """Display string for an effective resource value; '-' when undeclared."""
    if value is None:
        return "-"
    if key == "cpu":
        return format_cpu(float(value))
    return format_memory(int(value))


def _effective_resource(spec: dict[str, Any], bucket: str, key: str) -> str:
    return _format_effective(_effective_value(spec, bucket, key), key)


def _owner_uids(meta: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(ref["uid"]) for ref in (meta.get("ownerReferences") or []) if ref.get("uid"))


def _labels(meta: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """`metadata.labels` as a hashable tuple for frozen summaries (issue #44)."""
    labels = meta.get("labels")
    if not isinstance(labels, dict):
        return ()
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


@dataclass(frozen=True)
class GenericSummary:
    """Minimal summary for any Kubernetes object kind."""

    name: str
    namespace: str
    kind: str
    created: str  # ISO-8601 timestamp or "" when absent
    uid: str = ""
    owner_uids: tuple[str, ...] = ()
    #: metadata.labels as sorted pairs; feeds the client-side `-l` filter (#44).
    labels: tuple[tuple[str, str], ...] = ()
    #: spec.replicas when the kind carries one (Deployment/StatefulSet/...);
    #: None otherwise - 0 must stay distinguishable from "not scalable".
    desired: int | None = None

    @classmethod
    def from_manifest(cls, kind: str, manifest: dict[str, Any]) -> GenericSummary:
        meta = manifest.get("metadata") or {}
        spec = manifest.get("spec")
        # CRDs may define spec as an array or scalar; only mappings can carry replicas.
        replicas = spec.get("replicas") if isinstance(spec, dict) else None
        if isinstance(replicas, bool) or not isinstance(replicas, int):
            replicas = None  # bools and non-integers are never a replica count
        return cls(
            name=str(meta.get("name", "")),
            namespace=str(meta.get("namespace", "")),
            kind=kind,
            created=str(meta.get("creationTimestamp") or ""),
            uid=str(meta.get("uid") or ""),
            owner_uids=_owner_uids(meta),
            labels=_labels(meta),
            desired=replicas,
        )

    def age(self, now: datetime | None = None) -> str:
        """Return compact age string ("5m", "3h", "2d"); "-" when created is empty."""
        return format_age(self.created, now=now)


@dataclass(frozen=True)
class ReplicaSetSummary(GenericSummary):
    """ReplicaSet summary with rollout-history fields for drill-down views.

    ``desired`` is inherited from GenericSummary (always set from
    spec.replicas here).
    """

    revision: str = "-"  # deployment.kubernetes.io/revision annotation
    current: int = 0
    ready: str = "0/0"

    @classmethod
    def from_manifest(cls, kind: str, manifest: dict[str, Any]) -> ReplicaSetSummary:
        meta = manifest.get("metadata") or {}
        spec = manifest.get("spec") or {}
        status = manifest.get("status") or {}
        desired = int(spec.get("replicas") or 0)
        return cls(
            name=str(meta.get("name", "")),
            namespace=str(meta.get("namespace", "")),
            kind=kind,
            created=str(meta.get("creationTimestamp") or ""),
            uid=str(meta.get("uid") or ""),
            owner_uids=_owner_uids(meta),
            labels=_labels(meta),
            revision=str(
                (meta.get("annotations") or {}).get("deployment.kubernetes.io/revision") or "-"
            ),
            desired=desired,
            current=int(status.get("replicas") or 0),
            ready=f"{int(status.get('readyReplicas') or 0)}/{desired}",
        )


def summary_for(kind: str, manifest: dict[str, Any]) -> GenericSummary:
    """Build the richest summary available for *kind* (ReplicaSet gets history fields)."""
    if kind == "ReplicaSet":
        return ReplicaSetSummary.from_manifest(kind, manifest)
    return GenericSummary.from_manifest(kind, manifest)


def _terminated_reason(terminated: dict[str, Any]) -> str | None:
    """kubectl-style reason for a terminated state; None when nothing to show.

    Falls back to ``Signal:<n>`` / ``ExitCode:<n>`` when ``reason`` is empty;
    a clean zero exit without a reason yields None.
    """
    if not terminated:
        return None
    reason = terminated.get("reason")
    if reason:
        return str(reason)
    signal = terminated.get("signal")
    if signal:
        return f"Signal:{signal}"
    exit_code = terminated.get("exitCode")
    if exit_code:
        return f"ExitCode:{exit_code}"
    return None


def _init_phase(spec: dict[str, Any], status: dict[str, Any]) -> str | None:
    """Init-container display status ('Init:<reason>' / 'Init:i/n'), or None when done.

    Restartable (sidecar) init containers that have started are skipped, like
    kubectl; a zero exit code means the init container finished successfully.
    """
    init_statuses: list[dict[str, Any]] = status.get("initContainerStatuses") or []
    declared = spec.get("initContainers") or []
    # Status can lag the spec during initialization; kubectl uses the spec count.
    total = max(len(declared), len(init_statuses))
    sidecar_names = {str(c.get("name")) for c in declared if c.get("restartPolicy") == "Always"}
    for i, cs in enumerate(init_statuses):
        state = cs.get("state") or {}
        terminated = state.get("terminated") or {}
        if terminated and terminated.get("exitCode") == 0:
            continue
        if str(cs.get("name")) in sidecar_names and cs.get("started"):
            continue
        if terminated:
            return f"Init:{_terminated_reason(terminated) or 'Error'}"
        waiting_reason = (state.get("waiting") or {}).get("reason")
        if waiting_reason and waiting_reason != "PodInitializing":
            return f"Init:{waiting_reason}"
        return f"Init:{i}/{total}"
    return None


def _is_initialized(status: dict[str, Any]) -> bool:
    """True when the pod's Initialized condition is True."""
    return any(
        c.get("type") == "Initialized" and c.get("status") == "True"
        for c in (status.get("conditions") or [])
    )


#: Routine startup waiting reasons that must not read as trouble.
_BENIGN_WAITING_REASONS = frozenset({"ContainerCreating", "PodInitializing"})


@dataclass(frozen=True)
class ContainerTrouble:
    """Why one container is unhealthy, captured verbatim from its statuses.

    Everything here comes from `containerStatuses` / `initContainerStatuses`
    (waiting reason and message, last termination) so the hint strip renders
    API data only — no synthesized diagnoses.
    """

    container: str
    reason: str
    message: str = ""
    exit_code: int | None = None
    exit_reason: str | None = None
    finished_at: str | None = None
    restarts: int = 0


def _running_not_ready_reason(cs: dict[str, Any], state: dict[str, Any]) -> str | None:
    """`NotReady` when a running-but-unready container previously died abnormally.

    The useful failure data (exit 137 OOMKilled) then lives only in
    `lastState.terminated`; without this the row would get an event-only hint.
    """
    if state.get("running") is None or cs.get("ready") is not False:
        # `running` may serialize as an empty object (startedAt is optional),
        # so presence is checked rather than truthiness.
        return None
    last = (cs.get("lastState") or {}).get("terminated") or {}
    if not last:
        return None
    last_reason = _terminated_reason(last)
    if last_reason is None or last_reason == "Completed":
        return None
    return "NotReady"


def _container_trouble(cs: dict[str, Any], *, name_prefix: str = "") -> ContainerTrouble | None:
    """Trouble entry for one container status, or None when it is healthy.

    Captures a non-benign waiting reason, a current abnormal termination
    (non-zero exit / signal), or a running-but-unready container whose last
    termination was abnormal. The most recent termination rides along either
    way so "why" (exit 137 OOMKilled) shows next to "what" (CrashLoopBackOff).
    """
    state = cs.get("state") or {}
    waiting = state.get("waiting") or {}
    waiting_reason = str(waiting.get("reason") or "")
    reason: str | None = None
    if waiting_reason and waiting_reason not in _BENIGN_WAITING_REASONS:
        reason = waiting_reason
    terminated = state.get("terminated") or {}
    if reason is None and terminated:
        reason = _terminated_reason(terminated)
        if reason == "Completed" or (reason is None):
            return None
    if reason is None:
        reason = _running_not_ready_reason(cs, state)
    if reason is None:
        return None
    last = terminated or ((cs.get("lastState") or {}).get("terminated") or {})
    exit_code = last.get("exitCode")
    return ContainerTrouble(
        container=f"{name_prefix}{cs.get('name', '')}",
        reason=reason,
        message=str(waiting.get("message") or ""),
        exit_code=None if exit_code is None else int(exit_code),
        exit_reason=str(last["reason"]) if last.get("reason") else None,
        finished_at=str(last["finishedAt"]) if last.get("finishedAt") else None,
        restarts=int(cs.get("restartCount", 0)),
    )


def _pod_level_trouble(status: dict[str, Any]) -> ContainerTrouble | None:
    """Pod-scoped failure with no container status: Evicted, Unschedulable, ...

    Rendered with the pseudo-container name `pod` so the strip reads
    `pod Evicted: The node was low on resource: memory.`
    """
    if str(status.get("phase") or "") == "Succeeded":
        return None
    reason = str(status.get("reason") or "")
    if reason:
        return ContainerTrouble(
            container="pod", reason=reason, message=str(status.get("message") or "")
        )
    for cond in status.get("conditions") or []:
        if (
            cond.get("type") == "PodScheduled"
            and cond.get("status") == "False"
            and cond.get("reason")
        ):
            return ContainerTrouble(
                container="pod",
                reason=str(cond["reason"]),
                message=str(cond.get("message") or ""),
            )
    return None


_RESIZE_CONDITIONS = ("PodResizePending", "PodResizeInProgress")


def _resize_trouble(status: dict[str, Any]) -> list[ContainerTrouble]:
    """In-place resize outcome conditions (1.35 GA, issue #27) as hint
    entries: a pending resize (Infeasible/Deferred) or one in progress is
    exactly what an operator parked on the row wants explained."""
    entries: list[ContainerTrouble] = []
    for cond in status.get("conditions") or []:
        if cond.get("type") in _RESIZE_CONDITIONS and cond.get("status") == "True":
            reason = str(cond.get("reason") or "")
            message = str(cond.get("message") or "")
            detail = f"{reason}: {message}" if reason and message else reason or message
            entries.append(
                ContainerTrouble(container="pod", reason=str(cond["type"]), message=detail)
            )
    return entries


def _pod_trouble(status: dict[str, Any]) -> tuple[ContainerTrouble, ...]:
    """Trouble entries: pod-level failure first, then resize outcomes, then
    unhealthy containers (init containers before app containers)."""
    entries: list[ContainerTrouble] = []
    pod_level = _pod_level_trouble(status)
    if pod_level is not None:
        entries.append(pod_level)
    entries.extend(_resize_trouble(status))
    for cs in status.get("initContainerStatuses") or []:
        entry = _container_trouble(cs, name_prefix="init:")
        if entry is not None:
            entries.append(entry)
    for cs in status.get("containerStatuses") or []:
        entry = _container_trouble(cs)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def _deletion_status(meta: dict[str, Any], status: dict[str, Any], phase: str) -> str | None:
    """kubectl's deletion overrides: NodeLost is Unknown regardless of phase;
    the generic Terminating override applies only to non-terminal phases."""
    if not meta.get("deletionTimestamp"):
        return None
    if status.get("reason") == "NodeLost":
        return "Unknown"
    if phase not in ("Succeeded", "Failed"):
        return "Terminating"
    return None


def _is_scheduling_gated(status: dict[str, Any]) -> bool:
    return any(
        c.get("type") == "PodScheduled"
        and c.get("status") == "False"
        and c.get("reason") == "SchedulingGated"
        for c in (status.get("conditions") or [])
    )


def _is_pod_ready(status: dict[str, Any]) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in (status.get("conditions") or [])
    )


def _display_phase(
    meta: dict[str, Any],
    spec: dict[str, Any],
    status: dict[str, Any],
    statuses: list[dict[str, Any]],
) -> str:
    """Displayed pod status mirroring kubectl's printer.

    Init-container failures render as ``Init:<reason>``; container
    waiting/terminated reasons (CrashLoopBackOff, OOMKilled, ...) override
    ``status.phase``. A deletionTimestamp renders Terminating only while the
    phase is non-terminal (kubectl keeps Completed/Failed reasons for
    deleting terminal pods, and shows Unknown for NodeLost deletions).
    """
    phase = str(status.get("phase") or "")
    deletion = _deletion_status(meta, status, phase)
    if deletion is not None:
        return deletion
    # kubectl scans regular containers once the pod is initialized; a stale
    # Init:* status must not hide a current CrashLoopBackOff.
    if not _is_initialized(status):
        init_reason = _init_phase(spec, status)
        if init_reason is not None:
            return init_reason
    reason = str(status.get("reason") or status.get("phase") or "Unknown")
    # kubectl promotes a gated PodScheduled condition before scanning containers.
    if _is_scheduling_gated(status):
        reason = "SchedulingGated"
    has_running = False
    for cs in reversed(statuses):
        state = cs.get("state") or {}
        waiting_reason = (state.get("waiting") or {}).get("reason")
        terminated_reason = _terminated_reason(state.get("terminated") or {})
        if waiting_reason:
            reason = str(waiting_reason)
        elif terminated_reason:
            reason = terminated_reason
        elif state.get("running") and cs.get("ready"):
            has_running = True
    # A completed sidecar next to a running main container is a running pod
    # only once the pod reports Ready; otherwise kubectl shows NotReady.
    if reason == "Completed" and has_running:
        return "Running" if _is_pod_ready(status) else "NotReady"
    return reason


def _ready_transition_at(status: dict[str, Any]) -> str | None:
    """When the pod's Ready condition last changed, or None if unrecorded."""
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Ready" and cond.get("lastTransitionTime"):
            return str(cond["lastTransitionTime"])
    return None


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
    #: Exact effective requests for ratio math; the display strings above
    #: are rounded (1500Ki renders as 1Mi) and must never feed a percentage.
    cpu_request_cores: float | None = None
    mem_request_bytes: int | None = None
    #: Pod-level spec.resources.limits (K8s 1.34+) only - the sole true
    #: whole-pod ceiling. Per-container limits live in `container_limits`;
    #: they are enforced independently and must never be summed (PR #51).
    cpu_limit_cores: float | None = None
    mem_limit_bytes: int | None = None
    #: Declared limits per container (main + init) for severity coloring
    #: (issue #50): proximity to a container's own limit is OOMKill/throttle
    #: territory, over-request is normal burst.
    container_limits: tuple[ContainerLimits, ...] = ()
    containers: tuple[str, ...] = ()
    uid: str = ""
    owner_uids: tuple[str, ...] = ()
    #: metadata.labels as sorted pairs; feeds the client-side `-l` filter (#44).
    labels: tuple[tuple[str, str], ...] = ()
    #: Per-container failure details for the ops hint strip (#26); empty when healthy.
    trouble: tuple[ContainerTrouble, ...] = ()
    #: RFC 3339 time the Ready condition last flipped; freshness cutoff for
    #: event-only hints (a Warning older than it explains a previous failure).
    ready_transition_at: str | None = None
    #: metadata.creationTimestamp (RFC 3339 UTC) or "" when absent; feeds
    #: the AGE column and age sorting (issue #37).
    created: str = ""

    def age(self, now: datetime | None = None) -> str:
        """Return compact age string ("5m", "3h", "2d"); "-" when created is empty."""
        return format_age(self.created, now=now)

    @classmethod
    def from_manifest(cls, obj: dict[str, Any]) -> PodSummary:
        meta = obj.get("metadata") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}
        statuses: list[dict[str, Any]] = status.get("containerStatuses") or []
        ready_count = sum(1 for s in statuses if s.get("ready"))
        restarts = sum(int(s.get("restartCount", 0)) for s in statuses)
        cpu_request = _effective_value(spec, "requests", "cpu")
        mem_request = _effective_value(spec, "requests", "memory")
        cpu_limit = _pod_level_limit(spec, "cpu")
        mem_limit = _pod_level_limit(spec, "memory")
        return cls(
            name=str(meta.get("name", "")),
            namespace=str(meta.get("namespace", "")),
            phase=_display_phase(meta, spec, status, statuses),
            ready=f"{ready_count}/{len(statuses)}",
            restarts=restarts,
            node=spec.get("nodeName"),
            qos=str(status.get("qosClass") or "-"),
            cpu_request=_format_effective(cpu_request, "cpu"),
            mem_request=_format_effective(mem_request, "memory"),
            cpu_limit=_effective_resource(spec, "limits", "cpu"),
            mem_limit=_effective_resource(spec, "limits", "memory"),
            cpu_request_cores=None if cpu_request is None else float(cpu_request),
            mem_request_bytes=None if mem_request is None else int(mem_request),
            cpu_limit_cores=None if cpu_limit is None else float(cpu_limit),
            mem_limit_bytes=None if mem_limit is None else int(mem_limit),
            container_limits=_container_limits(spec),
            containers=tuple(
                str(c["name"]) for c in (spec.get("containers") or []) if c.get("name")
            ),
            uid=str(meta.get("uid") or ""),
            owner_uids=_owner_uids(meta),
            labels=_labels(meta),
            trouble=_pod_trouble(status),
            ready_transition_at=_ready_transition_at(status),
            created=str(meta.get("creationTimestamp") or ""),
        )
