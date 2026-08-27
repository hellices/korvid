"""Scenario schema + YAML loader for the agent eval harness (issue #69).

A scenario is one diagnosable fault: simulated cluster state (manifests,
events, log tails as the k8s layer would return them), the user's
question, and deterministic grading assertions. Live models run against
these fixtures through the real ToolExecutor, so the model is free to
take any diagnostic path while the ground truth stays fixed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from korvid.agent.interaction import InteractionContext
from korvid.evals.interaction import interaction_payload, load_interaction

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
    #: The exact workspace the turn starts from — the typed replacement
    #: for the prose `screen` string this schema used to carry. The agent
    #: reads it through an `AgentUiBridge`, never from the question, so a
    #: fixture that does not author it cannot be reproduced.
    interaction: InteractionContext
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
    """Directory containing the scenario pack in the source checkout."""
    return Path(__file__).parent / "scenarios"


_TOP_LEVEL_KEYS = frozenset({"id", "question", "interaction", "root_cause", "grading", "cluster"})
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
    if "interaction" not in data:
        raise ValueError(f"{path.name}: scenario needs an 'interaction' mapping")
    return Scenario(
        id=_require_str(data, "id"),
        question=_require_str(data, "question"),
        interaction=load_interaction(data["interaction"], f"{path.name}: 'interaction'"),
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


def select_scenarios(scenarios: Sequence[Scenario], scenario_ids: Sequence[str]) -> list[Scenario]:
    """Select an exact, repeatable subset of already-loaded scenarios by id.

    This is the machine-protocol counterpart to an external optimizer that
    wants one named scenario, or a fixed named set, run every time — without
    resorting to copying fixture files into a scratch directory just to
    change which of them load. Selection operates purely in memory over
    `scenarios`, whatever directory produced them.

    Fail-closed, deliberately stricter than a filter: an empty selection, a
    duplicate id, or an id absent from `scenarios` all raise rather than
    silently running zero scenarios, running one twice, or dropping the
    unknown name. A caller that mistypes an id must see an error, not a
    quietly smaller (or larger) case pack than it asked for.

    Args:
        scenarios: Already-loaded scenarios, e.g. from `load_scenarios`.
        scenario_ids: The exact ids to run, in any order; the result is
            still sorted by id, matching `load_scenarios`' own order so a
            selection is never distinguishable from a differently-ordered
            request for the same ids.

    Returns:
        The matching scenarios, sorted by id.

    Raises:
        ValueError: the selection is empty, names an id `scenarios` does
            not contain, or repeats an id.
    """
    ids = list(scenario_ids)
    if not ids:
        raise ValueError("scenario selection must name at least one scenario id")
    blank = [raw for raw in ids if not isinstance(raw, str) or not raw.strip()]
    if blank:
        raise ValueError("scenario selection ids must be non-empty strings")
    seen: set[str] = set()
    duplicates: set[str] = set()
    for scenario_id in ids:
        if scenario_id in seen:
            duplicates.add(scenario_id)
        seen.add(scenario_id)
    if duplicates:
        raise ValueError(f"duplicate scenario id(s) in selection: {sorted(duplicates)}")
    by_id = {scenario.id: scenario for scenario in scenarios}
    unknown = sorted(set(ids) - by_id.keys())
    if unknown:
        known = sorted(by_id)
        raise ValueError(f"unknown scenario id(s): {unknown}; known ids: {known}")
    return sorted((by_id[scenario_id] for scenario_id in ids), key=lambda s: s.id)


def _evidence_content(evidence: Evidence) -> dict[str, Any]:
    return {"tool": evidence.tool, "contains": evidence.contains, "args": dict(evidence.args)}


def _scenario_content(scenario: Scenario) -> dict[str, Any]:
    """A deep, JSON-safe view of everything that defines a scenario's behavior.

    Used only to derive the case-pack content hash. Every field that a
    grader or the agent can observe is included — fixtures, grading, and
    the starting interaction — so the hash changes whenever any of them
    changes, not just when the id does. Nothing here is a file path or a
    filesystem timestamp: two scenarios with identical content hash
    identically no matter which directory or mtime produced them.
    """
    return {
        "id": scenario.id,
        "question": scenario.question,
        "interaction": interaction_payload(scenario.interaction),
        "root_cause": scenario.root_cause,
        "must_mention": [list(group) for group in scenario.must_mention],
        "must_not_mention": [list(group) for group in scenario.must_not_mention],
        "expected_evidence": [
            [_evidence_content(evidence) for evidence in group]
            for group in scenario.expected_evidence
        ],
        "objects": [dict(obj) for obj in scenario.objects],
        "events": [dict(event) for event in scenario.events],
        "logs": {
            key: {"current": list(logs.current), "previous": list(logs.previous)}
            for key, logs in scenario.logs.items()
        },
        "forbidden": [dict(entry) for entry in scenario.forbidden],
    }


def _canonical_scalar(value: Any) -> Any | None:
    """Encode `value` if it is a canonical-hash scalar type; `None` sentinel
    reserved for "not a scalar this function handles" is impossible to
    confuse with an encoded `None` fixture value, which is always the
    2-element list `["null", None]` — never a bare `None`."""
    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        # `bool` subclasses `int`; checked first so `True` is never folded
        # into the `int` branch below.
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", value]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, datetime):
        # `datetime` subclasses `date`; checked first for the same reason.
        # A naive fixture timestamp is UTC (matching `_as_instant`), so the
        # canonical form is always a single, unambiguous, timezone-aware
        # instant — never dependent on how the author happened to spell it.
        instant = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return ["datetime", instant.astimezone(UTC).isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    return None


def _canonical_value(value: Any) -> Any:
    """Recursively encode `value` into a canonical, type-preserving,
    JSON-safe structure for the case-pack content hash.

    A YAML fixture can legitimately hold an unquoted timestamp (`yaml.safe_load`
    turns `2026-07-20T10:00:00Z` into a `datetime` and a bare `2026-07-20`
    into a `date`), so the hash must accept both — but it must never treat
    one the same as a string that merely renders the same way. Every value
    is tagged with its own type name before it is nested (`_canonical_scalar`
    handles the leaf types), so `datetime`, `date`, and `str` values that
    would collide under a naive `str()` fallback instead hash differently
    by construction. A mapping key must be a string — coercing a non-string
    key would hide a fixture-authoring mistake, not hash it — and any value
    of a type this function does not know how to canonicalize (a `set`,
    for instance) is rejected outright rather than silently passed through
    `str()`, which would make the digest meaningless for exactly the
    content it could not represent.
    """
    scalar = _canonical_scalar(value)
    if scalar is not None:
        return scalar
    if isinstance(value, Mapping):
        pairs: list[tuple[str, Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    "case-pack content mapping keys must be strings, got"
                    f" {key!r} ({type(key).__name__})"
                )
            pairs.append((key, _canonical_value(item)))
        pairs.sort(key=lambda pair: pair[0])
        return ["dict", pairs]
    if isinstance(value, list | tuple):
        return ["list", [_canonical_value(item) for item in value]]
    raise ValueError(
        f"case-pack content has an unsupported value of type {type(value).__name__}: {value!r}"
    )


def case_pack_identity(scenarios: Sequence[Scenario]) -> dict[str, Any]:
    """Deterministic identity for an exact set of loaded scenario definitions.

    Published so an external optimizer can confirm which case pack a run
    actually measured against without trusting a directory path or file
    mtimes (which say nothing once fixtures are packaged, mirrored, or
    checked out fresh). `scenario_ids` is sorted so the identity does not
    depend on selection or filesystem enumeration order; `sha256` is a
    digest of the scenarios' own content (question, interaction, grading,
    and cluster fixtures), so it is identical for two runs that loaded the
    same definitions and changes whenever any of them do.

    Args:
        scenarios: The exact scenarios the run loaded (the full bundled
            pack, a custom directory, or a `select_scenarios` subset).

    Returns:
        A mapping with `scenario_ids` (sorted), `count`, and `sha256`.

    Raises:
        ValueError: a scenario's content holds a mapping with a non-string
            key, or a value of a type the canonical content encoding does
            not recognize (e.g. a `set`) — both are scenario-authoring
            defects, not values a content hash may silently paper over.
    """
    ordered = sorted(scenarios, key=lambda s: s.id)
    ids = [scenario.id for scenario in ordered]
    content = [_scenario_content(scenario) for scenario in ordered]
    canonical = _canonical_value(content)
    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"scenario_ids": ids, "count": len(ids), "sha256": digest}
