"""Scenario schema + YAML loader for the agent eval harness (issue #69).

A scenario is one diagnosable fault: simulated cluster state (manifests,
events, log tails as the k8s layer would return them), the user's
question, and deterministic grading assertions. Live models run against
these fixtures through the real ToolExecutor, so the model is free to
take any diagnostic path while the ground truth stays fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

#: The instant scenario fixture timestamps are authored against. Every
#: fixture timestamp must be at or before this instant — the fake cluster
#: rebases it to the wall clock at construction, so a later instant would
#: land in the run's future and distort ages and event ordering.
SCENARIO_NOW = datetime(2026, 7, 27, 8, 0, 0, tzinfo=UTC)

#: RFC 3339 timestamp string, as Kubernetes serializes them.
TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class ContainerLogs:
    """Log tails for one container instance pair (current and previous)."""

    current: tuple[str, ...] = ()
    previous: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """One ground-truth location: probing `tool` with `args` yields a result
    containing `contains` — the CI fixture-integrity test verifies that
    route is reachable. Grading is path-free: any successful read whose
    result contains the substring and whose arguments target the same
    object counts, whichever tool the model chose."""

    tool: str
    contains: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Scenario:
    """One graded diagnostic task: fixtures, question, and assertions."""

    id: str
    question: str
    screen: str
    #: Canonical root-cause label (e.g. ``oom_killed``); ``none`` for
    #: negative controls where the correct answer is "nothing is wrong".
    root_cause: str
    #: Alternative-groups: the answer must mention at least one keyword
    #: from every group (matched on normalized text).
    must_mention: tuple[tuple[str, ...], ...]
    #: Alternative-groups of misdiagnosis keywords: a **positive claim** of
    #: any keyword fails the run; a negated mention ("this is not an image
    #: pull problem") does not — ruling out the competing cause is part of
    #: a correct diagnosis, hedging both causes is not.
    must_not_mention: tuple[tuple[str, ...], ...] = ()
    #: Alternative-groups of ground-truth locations: each group is satisfied
    #: by fetching **any one** of its alternatives — detects "answered
    #: without fetching the evidence" without pinning the diagnostic path.
    expected_evidence: tuple[tuple[Evidence, ...], ...] = ()
    #: Full manifests, keyed by nothing — the fake kube indexes them.
    objects: tuple[dict[str, Any], ...] = ()
    #: Event manifests with ``involvedObject`` linking them to objects.
    events: tuple[dict[str, Any], ...] = ()
    #: ``namespace/pod/container`` → log tails.
    logs: dict[str, ContainerLogs] = field(default_factory=dict)
    #: Reads the fixture withholds the way an RBAC rule does. Each entry
    #: matches on ``kind`` plus optional ``namespace``, ``name`` and
    #: ``subresource`` (``log``); omitted keys are wildcards. A matching
    #: read fails 403 instead of returning data, which is what separates
    #: "evidence is unavailable" from "evidence says nothing is wrong".
    forbidden: tuple[dict[str, str], ...] = ()


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"scenario field {key!r} must be a non-empty string")
    return value


def _alt_groups(raw: Any, key: str) -> tuple[tuple[str, ...], ...]:
    """Normalize a keyword-assertion list: each entry is either one keyword
    or a list of alternatives, of which at least one must match."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"grading field {key!r} must be a list")
    groups: list[tuple[str, ...]] = []
    for entry in raw:
        alternatives = entry if isinstance(entry, list) else [entry]
        # These are the benchmark's pass/fail assertions: coercing YAML
        # booleans, numbers, or mappings would silently change the grade.
        for alt in alternatives:
            if not isinstance(alt, str) or not alt.strip():
                raise ValueError(f"grading field {key!r} keywords must be non-blank strings")
        group = tuple(alternatives)
        if not group:
            raise ValueError(f"grading field {key!r} has an empty entry")
        groups.append(group)
    return tuple(groups)


def _evidence_entry(entry: Any) -> Evidence:
    if not isinstance(entry, dict) or not isinstance(entry.get("args"), dict):
        raise ValueError("each expected_evidence entry needs 'tool', 'contains', and 'args' keys")
    tool = entry.get("tool")
    contains = entry.get("contains")
    # Coercing arbitrary YAML values (e.g. a list) would load fine but could
    # never match a real tool call, silently making the group unsatisfiable.
    if not isinstance(tool, str) or not tool.strip():
        raise ValueError("expected_evidence field 'tool' must be a non-blank string")
    if not isinstance(contains, str) or not contains.strip():
        raise ValueError("expected_evidence field 'contains' must be a non-blank string")
    return Evidence(tool=tool, contains=contains, args=dict(entry["args"]))


def _evidence(raw: Any) -> tuple[tuple[Evidence, ...], ...]:
    """Normalize evidence assertions into alternative-groups: each entry is
    either one location or a list of alternative locations, of which any one
    satisfies the group — the model is free to choose its diagnostic path."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("grading field 'expected_evidence' must be a list")
    groups: list[tuple[Evidence, ...]] = []
    for entry in raw:
        alternatives = entry if isinstance(entry, list) else [entry]
        if not alternatives:
            raise ValueError("expected_evidence has an empty alternatives group")
        groups.append(tuple(_evidence_entry(alt) for alt in alternatives))
    return tuple(groups)


def _log_stream(entry: dict[str, Any], key: str, stream: str) -> tuple[str, ...]:
    raw = entry.get(stream)
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(line, str) for line in raw):
        raise ValueError(f"log entry {key!r} field {stream!r} must be a list of strings")
    return tuple(raw)


def _logs(raw: Any) -> dict[str, ContainerLogs]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("cluster field 'logs' must be a mapping")
    logs: dict[str, ContainerLogs] = {}
    for key, value in raw.items():
        if str(key).count("/") != 2:
            raise ValueError(f"log key {key!r} must be 'namespace/pod/container'")
        if not isinstance(value, dict):
            raise ValueError(f"log entry {key!r} must be a mapping of current/previous")
        unknown = set(value) - {"current", "previous"}
        if unknown:
            raise ValueError(f"log entry {key!r} has unknown keys: {sorted(unknown)}")
        logs[str(key)] = ContainerLogs(
            current=_log_stream(value, str(key), "current"),
            previous=_log_stream(value, str(key), "previous"),
        )
    return logs


def _as_instant(value: Any) -> datetime | None:
    """The instant `value` denotes, if it is a fixture timestamp — either an
    RFC 3339 string or the `datetime` `yaml.safe_load` produces for unquoted
    ones (naive values are read as UTC, the timezone fixtures use)."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and TIMESTAMP_PATTERN.match(value):
        return datetime.fromisoformat(value)
    return None


def _reject_future_timestamps(value: Any, label: str) -> None:
    """Recursively reject any fixture timestamp after `SCENARIO_NOW`."""
    instant = _as_instant(value)
    if instant is not None and instant > SCENARIO_NOW:
        raise ValueError(
            f"{label}: timestamp {value!r} is after the scenario anchor"
            f" {SCENARIO_NOW.isoformat().replace('+00:00', 'Z')}"
        )
    if isinstance(value, dict):
        for item in value.values():
            _reject_future_timestamps(item, label)
    elif isinstance(value, list | tuple):
        for item in value:
            _reject_future_timestamps(item, label)


def _manifests(raw: Any, key: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(m, dict) for m in raw):
        raise ValueError(f"cluster field {key!r} must be a list of mappings")
    return tuple(raw)


def bundled_scenarios_dir() -> Path:
    """Directory containing the scenario pack that ships with korvid."""
    return Path(__file__).parent / "scenarios"


_TOP_LEVEL_KEYS = frozenset({"id", "question", "screen", "root_cause", "grading", "cluster"})
_GRADING_KEYS = frozenset({"must_mention", "must_not_mention", "expected_evidence"})
_CLUSTER_KEYS = frozenset({"objects", "events", "logs"})


def _reject_unknown_keys(mapping: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(map(str, mapping)) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {unknown}")


def load_scenario(path: Path) -> Scenario:
    """Load and validate one scenario YAML file."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: scenario file must be a YAML mapping")
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, f"{path.name}: scenario")
    grading = data.get("grading")
    if not isinstance(grading, dict):
        raise ValueError(f"{path.name}: scenario needs a 'grading' mapping")
    _reject_unknown_keys(grading, _GRADING_KEYS, f"{path.name}: 'grading'")
    cluster = data.get("cluster")
    if not isinstance(cluster, dict):
        raise ValueError(f"{path.name}: scenario needs a 'cluster' mapping")
    _reject_unknown_keys(cluster, _CLUSTER_KEYS, f"{path.name}: 'cluster'")
    must_mention = _alt_groups(grading.get("must_mention"), "must_mention")
    if not must_mention:
        raise ValueError(f"{path.name}: grading needs at least one must_mention entry")
    root_cause = _require_str(data, "root_cause")
    must_not_mention = _alt_groups(grading.get("must_not_mention"), "must_not_mention")
    if root_cause == "none" and not must_not_mention:
        # Without forbidden groups a negative control cannot catch
        # over-diagnosis: "healthy and ready, but OOMKilled" would pass.
        raise ValueError(
            f"{path.name}: negative controls (root_cause 'none') need at"
            " least one must_not_mention entry"
        )
    expected_evidence = _evidence(grading.get("expected_evidence"))
    if not expected_evidence:
        # Issue #69: every scenario declares ground-truth evidence; without
        # it evidence_fetched would grade as a free pass.
        raise ValueError(f"{path.name}: grading needs at least one expected_evidence entry")
    objects = _manifests(cluster.get("objects"), "objects")
    events = _manifests(cluster.get("events"), "events")
    _reject_future_timestamps(objects, f"{path.name}: 'objects'")
    _reject_future_timestamps(events, f"{path.name}: 'events'")
    return Scenario(
        id=_require_str(data, "id"),
        question=_require_str(data, "question"),
        screen=_require_str(data, "screen"),
        root_cause=root_cause,
        must_mention=must_mention,
        must_not_mention=must_not_mention,
        expected_evidence=expected_evidence,
        objects=objects,
        events=events,
        logs=_logs(cluster.get("logs")),
    )


def load_scenarios(directory: Path) -> list[Scenario]:
    """Load every ``*.yaml`` scenario in a directory, sorted by scenario id."""
    scenarios = [load_scenario(path) for path in sorted(directory.glob("*.yaml"))]
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.id in seen:
            raise ValueError(f"duplicate scenario id {scenario.id!r}")
        seen.add(scenario.id)
    return sorted(scenarios, key=lambda s: s.id)
