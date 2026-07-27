"""Projection functions for the diagnose_pod compound tool (issue #70).

Deterministic evidence gathering for the most common agent workflow
("why is this pod broken?"): these helpers turn raw manifests, event
lists, and log tails into compact report lines so the model interprets
projected evidence instead of reasoning over full YAML dumps. Pure
functions only — fetching lives in the tool executor.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

#: Warning events shown before the report elides the rest.
MAX_WARNING_EVENTS = 10

#: Lines matching this pattern anchor the targeted log excerpt.
_ERROR_PATTERN = re.compile(r"error|fatal|panic|exception|traceback", re.IGNORECASE)

#: Sort fallback for events that carry no parseable timestamp at all.
_EPOCH = datetime.min.replace(tzinfo=UTC)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _status(pod: dict[str, Any]) -> dict[str, Any]:
    return _dict(pod.get("status"))


def _spec(pod: dict[str, Any]) -> dict[str, Any]:
    return _dict(pod.get("spec"))


def identity_lines(pod: dict[str, Any]) -> list[str]:
    """Phase, node, and creation time — the report's orientation header."""
    status = _status(pod)
    metadata = _dict(pod.get("metadata"))
    phase = status.get("phase") or "?"
    node = _spec(pod).get("nodeName") or "?"
    created = metadata.get("creationTimestamp") or "?"
    return [f"phase={phase}  node={node}  created={created}"]


def _container_statuses(pod: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(prefix, status) pairs for init and regular containers, in pod order."""
    status = _status(pod)
    entries: list[tuple[str, dict[str, Any]]] = []
    for key, prefix in (("initContainerStatuses", "init "), ("containerStatuses", "")):
        raw = status.get(key)
        if not isinstance(raw, list):
            continue
        entries.extend((prefix, cs) for cs in raw if isinstance(cs, dict))
    return entries


def _state_phrase(state: dict[str, Any]) -> str:
    waiting = state.get("waiting")
    if isinstance(waiting, dict):
        phrase = f"waiting reason={waiting.get('reason') or '?'}"
        message = waiting.get("message")
        if message:
            phrase += f" — {message}"
        return phrase
    terminated = state.get("terminated")
    if isinstance(terminated, dict):
        phrase = f"terminated exit={terminated.get('exitCode', '?')}"
        reason = terminated.get("reason")
        if reason:
            phrase += f" ({reason})"
        return phrase
    running = state.get("running")
    if isinstance(running, dict):
        return f"running since {running.get('startedAt') or '?'}"
    return "state unknown"


def container_state_lines(pod: dict[str, Any]) -> list[str]:
    """One line per container: state, readiness, restarts, last exit."""
    lines: list[str] = []
    for prefix, cs in _container_statuses(pod):
        state = _dict(cs.get("state"))
        parts = [
            f"{prefix}{cs.get('name') or '?'}:",
            _state_phrase(state),
            "ready" if cs.get("ready") else "not-ready",
            f"restarts={cs.get('restartCount', 0)}",
        ]
        last = cs.get("lastState")
        if isinstance(last, dict):
            terminated = last.get("terminated")
            if isinstance(terminated, dict):
                exit_part = f"last-exit={terminated.get('exitCode', '?')}"
                reason = terminated.get("reason")
                if reason:
                    exit_part += f" ({reason})"
                parts.append(exit_part)
        lines.append("  ".join(parts))
    return lines


def troubled_containers(pod: dict[str, Any]) -> list[str]:
    """Container names whose logs carry diagnostic evidence.

    Waiting states, non-zero terminations, and any restarts qualify — a
    container that crashed and recovered still logged why it crashed.
    Completed init containers (exit 0) are healthy: earlier restarts are
    part of normal retry-until-success, unlike a regular container's.
    """
    names: list[str] = []
    for prefix, cs in _container_statuses(pod):
        name = cs.get("name")
        if not isinstance(name, str) or not name:
            continue
        state = _dict(cs.get("state"))
        restarts = cs.get("restartCount") or 0
        waiting = isinstance(state.get("waiting"), dict)
        terminated = state.get("terminated")
        completed = isinstance(terminated, dict) and terminated.get("exitCode") == 0
        if prefix == "init " and completed:
            continue  # a Completed init container succeeded — retries were routine
        failed_exit = isinstance(terminated, dict) and not completed
        if waiting or failed_exit or restarts > 0:
            names.append(name)
    return names


def restarted_containers(pod: dict[str, Any]) -> set[str]:
    """Container names with restarts — their crash evidence is in the
    *previous* instance's logs, not the freshly restarted current one."""
    return {
        name
        for _prefix, cs in _container_statuses(pod)
        if isinstance(name := cs.get("name"), str) and name and (cs.get("restartCount") or 0) > 0
    }


def condition_lines(pod: dict[str, Any]) -> list[str]:
    """Pod conditions, failing ones first — those carry the reasons."""
    raw = _status(pod).get("conditions")
    if not isinstance(raw, list):
        return []
    failing: list[str] = []
    healthy: list[str] = []
    for cond in raw:
        if not isinstance(cond, dict):
            continue
        line = f"{cond.get('type') or '?'}={cond.get('status') or '?'}"
        reason = cond.get("reason")
        if reason:
            line += f" ({reason})"
        message = cond.get("message")
        if message:
            line += f": {message}"
        (healthy if cond.get("status") == "True" else failing).append(line)
    return failing + healthy


def _event_count(ev: dict[str, Any]) -> int:
    """Recurrence count: `series.count` (events.k8s.io repeating events)
    first, then the core v1 `count`, defaulting to a single occurrence."""
    raw = _dict(ev.get("series")).get("count") or ev.get("count") or 1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _event_last_seen(ev: dict[str, Any]) -> str:
    """Raw timestamp of the latest occurrence: `series.lastObservedTime`
    for repeating events, then the non-series fallbacks."""
    raw = (
        _dict(ev.get("series")).get("lastObservedTime")
        or ev.get("lastTimestamp")
        or ev.get("eventTime")
        or ev.get("firstTimestamp")
        or ""
    )
    return str(raw)


def _event_instant(ev: dict[str, Any]) -> datetime:
    """Parsed instant for sorting — RFC 3339 strings do not sort
    chronologically once offsets or fractional seconds differ."""
    raw = _event_last_seen(ev)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def warning_event_lines(events: list[dict[str, Any]]) -> list[str]:
    """Warning events, deduplicated by (reason, message), newest first.

    The newest duplicate wins so its count reflects the latest tally.
    Capped at `MAX_WARNING_EVENTS` with an explicit elision marker.
    """
    warnings = [
        ev for ev in events if isinstance(ev, dict) and str(ev.get("type") or "") == "Warning"
    ]
    warnings.sort(key=_event_instant, reverse=True)
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    deduped = 0
    for ev in warnings:
        key = (str(ev.get("reason") or ""), str(ev.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped += 1
        if deduped > MAX_WARNING_EVENTS:
            continue
        ts = _event_last_seen(ev)
        line = f"{key[0]} ({_event_count(ev)}x"
        if ts:
            line += f", last {ts}"
        line += f"): {key[1]}"
        lines.append(line)
    if deduped > MAX_WARNING_EVENTS:
        lines.append(f"…and {deduped - MAX_WARNING_EVENTS} more warning kinds")
    return lines


def log_excerpt(lines: list[str], *, context: int = 5, final: int = 10) -> str:
    """Targeted excerpt: context around the last error-ish line plus the tail.

    Small models lose evidence buried mid-context, so the excerpt keeps
    only the last match window and the final lines, with a visible "…"
    where untargeted lines were dropped. A short log is returned whole.
    """
    if len(lines) <= context * 2 + final + 1:
        return "\n".join(lines)
    last_match = next(
        (i for i in range(len(lines) - 1, -1, -1) if _ERROR_PATTERN.search(lines[i])), None
    )
    tail_start = len(lines) - final
    if last_match is None:
        return "\n".join(["…", *lines[tail_start:]])
    window_start = max(0, last_match - context)
    window_end = min(len(lines), last_match + context + 1)
    if window_end >= tail_start:  # the match window runs into the tail — merge
        merged_start = min(window_start, tail_start)
        prefix = ["…"] if merged_start > 0 else []
        return "\n".join([*prefix, *lines[merged_start:]])
    parts: list[str] = []
    if window_start > 0:
        parts.append("…")
    parts.extend(lines[window_start:window_end])
    parts.append("…")
    parts.extend(lines[tail_start:])
    return "\n".join(parts)


def pvc_names(pod: dict[str, Any]) -> list[str]:
    """Names of PersistentVolumeClaims the pod mounts, in spec order."""
    volumes = _spec(pod).get("volumes")
    if not isinstance(volumes, list):
        return []
    names: list[str] = []
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        claim = volume.get("persistentVolumeClaim")
        if isinstance(claim, dict):
            name = claim.get("claimName")
            if isinstance(name, str) and name:
                names.append(name)
    return names


def node_condition_line(node: dict[str, Any]) -> str:
    """Ready plus any abnormal condition of the pod's node, one line.

    Pressure conditions are only mentioned when they fire — a healthy
    node contributes a single short line, not a condition dump.
    """
    metadata = _dict(node.get("metadata"))
    name = metadata.get("name") or "?"
    status = _dict(node.get("status"))
    conditions = status.get("conditions")
    if not isinstance(conditions, list) or not conditions:
        return f"node {name}: no conditions reported"
    parts: list[str] = []
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        ctype = str(cond.get("type") or "")
        cstatus = str(cond.get("status") or "?")
        # Ready is healthy when True; every other condition is healthy only
        # when False — Unknown means the kubelet cannot report, so it counts
        # as abnormal for both.
        abnormal = cstatus != "True" if ctype == "Ready" else cstatus != "False"
        if ctype == "Ready" or abnormal:
            parts.append(f"{ctype}={cstatus}")
    return f"node {name}: " + ", ".join(parts)
