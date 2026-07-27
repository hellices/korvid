"""Scenario schema + YAML loader for the agent eval harness (issue #69).

A scenario is one diagnosable fault: simulated cluster state (manifests,
events, log tails as the k8s layer would return them), the user's
question, and deterministic grading assertions. Live models run against
these fixtures through the real ToolExecutor, so the model is free to
take any diagnostic path while the ground truth stays fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ContainerLogs:
    """Log tails for one container instance pair (current and previous)."""

    current: tuple[str, ...] = ()
    previous: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    """One ground-truth location: probing `tool` with `args` must yield a
    result containing `contains`. Grading checks the substring against
    whatever arguments the model actually used; `args` also lets the CI
    fixture-integrity test verify the evidence really is reachable."""

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
    #: Alternative-groups of misdiagnosis keywords: **any mention** from any
    #: group fails the run, even a negated one ("this is not an image pull
    #: problem"). Grading is deterministic keyword matching, not assertion
    #: parsing, so pick keywords a correct answer would never bring up.
    must_not_mention: tuple[tuple[str, ...], ...] = ()
    #: Which tool results contain the ground truth — detects "answered
    #: without fetching the evidence".
    expected_evidence: tuple[Evidence, ...] = ()
    #: Full manifests, keyed by nothing — the fake kube indexes them.
    objects: tuple[dict[str, Any], ...] = ()
    #: Event manifests with ``involvedObject`` linking them to objects.
    events: tuple[dict[str, Any], ...] = ()
    #: ``namespace/pod/container`` → log tails.
    logs: dict[str, ContainerLogs] = field(default_factory=dict)


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
        group = tuple(str(alt) for alt in alternatives if str(alt).strip())
        if not group:
            raise ValueError(f"grading field {key!r} has an empty entry")
        groups.append(group)
    return tuple(groups)


def _evidence(raw: Any) -> tuple[Evidence, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("grading field 'expected_evidence' must be a list")
    entries: list[Evidence] = []
    for entry in raw:
        if (
            not isinstance(entry, dict)
            or not entry.get("tool")
            or not entry.get("contains")
            or not isinstance(entry.get("args"), dict)
        ):
            raise ValueError(
                "each expected_evidence entry needs 'tool', 'contains', and 'args' keys"
            )
        entries.append(
            Evidence(
                tool=str(entry["tool"]),
                contains=str(entry["contains"]),
                args=dict(entry["args"]),
            )
        )
    return tuple(entries)


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


def _manifests(raw: Any, key: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(m, dict) for m in raw):
        raise ValueError(f"cluster field {key!r} must be a list of mappings")
    return tuple(raw)


def bundled_scenarios_dir() -> Path:
    """Directory containing the scenario pack that ships with korvid."""
    return Path(__file__).parent / "scenarios"


def load_scenario(path: Path) -> Scenario:
    """Load and validate one scenario YAML file."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: scenario file must be a YAML mapping")
    grading = data.get("grading")
    if not isinstance(grading, dict):
        raise ValueError(f"{path.name}: scenario needs a 'grading' mapping")
    cluster = data.get("cluster")
    if not isinstance(cluster, dict):
        raise ValueError(f"{path.name}: scenario needs a 'cluster' mapping")
    must_mention = _alt_groups(grading.get("must_mention"), "must_mention")
    if not must_mention:
        raise ValueError(f"{path.name}: grading needs at least one must_mention entry")
    return Scenario(
        id=_require_str(data, "id"),
        question=_require_str(data, "question"),
        screen=_require_str(data, "screen"),
        root_cause=_require_str(data, "root_cause"),
        must_mention=must_mention,
        must_not_mention=_alt_groups(grading.get("must_not_mention"), "must_not_mention"),
        expected_evidence=_evidence(grading.get("expected_evidence")),
        objects=_manifests(cluster.get("objects"), "objects"),
        events=_manifests(cluster.get("events"), "events"),
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
