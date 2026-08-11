"""Strict schema for multi-turn conversational agent evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from korvid.evals.scenario import (
    ContainerLogs,
    Evidence,
    _alt_groups,
    _evidence,
    _logs,
    _manifests,
    _reject_future_timestamps,
    _reject_unknown_keys,
    _require_str,
)

_TOP_LEVEL_KEYS = frozenset({"id", "root_cause", "turns", "cluster"})
_TURN_KEYS = frozenset({"user", "screen", "grading", "forbidden_targets"})
_GRADING_KEYS = frozenset(
    {
        "must_mention",
        "must_not_mention",
        "expected_evidence",
        "max_tool_calls",
    }
)
_CLUSTER_KEYS = frozenset({"objects", "events", "logs", "forbidden"})


@dataclass(frozen=True)
class JourneyTurn:
    """One scripted user turn and its deterministic acceptance assertions."""

    user: str
    screen: str
    must_mention: tuple[tuple[str, ...], ...]
    must_not_mention: tuple[tuple[str, ...], ...]
    expected_evidence: tuple[tuple[Evidence, ...], ...]
    forbidden_targets: tuple[dict[str, Any], ...] = ()
    max_tool_calls: int | None = None


@dataclass(frozen=True)
class ConversationJourney:
    """A shared cluster state plus ordered turns on one persistent runtime."""

    id: str
    root_cause: str
    turns: tuple[JourneyTurn, ...]
    objects: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    logs: dict[str, ContainerLogs]
    #: Reads the fixture withholds, as `Scenario.forbidden`.
    forbidden: tuple[dict[str, str], ...] = ()


def _positive_int_or_none(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


_FORBIDDEN_KEYS = frozenset({"kind", "namespace", "name", "subresource"})


def _deniable_kinds() -> frozenset[str]:
    """Plural resource names the fake cluster can actually withhold.

    `_deny` compares the plural, so `kind: pod` would load and match
    nothing - the rule reads as a denial and behaves as an allowance.
    """
    from korvid.evals.fake_kube import builtin_aliases

    return frozenset(meta.plural for meta in builtin_aliases().values())


#: The kind each subresource can be paired with. `log` only ever reaches
#: the matcher from a pod log read, so any other pairing is a rule that
#: cannot fire.
_SUBRESOURCE_KINDS = {"log": "pods"}
#: The only subresource the fixture's matcher understands. A rule naming
#: anything else would load cleanly and deny nothing, so the journey would
#: publish a score for an evidence gap it never created.
_FORBIDDEN_SUBRESOURCES = frozenset({"log"})


def _check_forbidden_rule(item: dict[str, Any], label: str) -> None:
    """Reject a rule the matcher could not honour.

    A rule that loads but matches nothing is the worst outcome available
    here: the journey reports a score for an evidence gap it never created,
    and the run is indistinguishable from a model that handled the gap well.
    """
    _reject_unknown_keys(item, _FORBIDDEN_KEYS, label)
    if not isinstance(item.get("kind"), str) or not item["kind"]:
        raise ValueError(f"{label} entries need a 'kind'")
    if not all(isinstance(value, str) for value in item.values()):
        raise ValueError(f"{label} values must be strings")
    if any(not value.strip() for value in item.values()):
        raise ValueError(f"{label}: blank selector values match no read")
    if (kind := item["kind"]) not in _deniable_kinds():
        raise ValueError(
            f"{label}: kind {kind!r} is not a resource the fixture serves "
            f"(use the plural name, e.g. 'pods')"
        )
    subresource = item.get("subresource")
    if subresource is None:
        return
    if subresource not in _FORBIDDEN_SUBRESOURCES:
        raise ValueError(
            f"{label}: unsupported subresource {subresource!r} "
            f"(known: {sorted(_FORBIDDEN_SUBRESOURCES)})"
        )
    if (owner := _SUBRESOURCE_KINDS[subresource]) != kind:
        raise ValueError(
            f"{label}: subresource {subresource!r} only applies to {owner!r}, not {kind!r}"
        )


def _forbidden(raw: Any, label: str) -> tuple[dict[str, str], ...]:
    """Parse withheld-read rules, rejecting anything the matcher would
    silently ignore - a typo'd key would otherwise widen the denial to a
    whole kind and quietly change what the journey measures."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValueError(f"{label} must be a list of rule mappings")
    rules: list[dict[str, str]] = []
    for item in raw:
        _check_forbidden_rule(item, label)
        rules.append({str(key): str(value) for key, value in item.items()})
    return tuple(rules)


def _targets(raw: Any, label: str) -> tuple[dict[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValueError(f"{label} must be a list of argument mappings")
    if any(not item for item in raw):
        raise ValueError(f"{label} entries must be non-empty mappings")
    return tuple(dict(item) for item in raw)


def _turn(raw: Any, path: Path, index: int) -> JourneyTurn:
    label = f"{path.name}: turn {index}"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    _reject_unknown_keys(raw, _TURN_KEYS, label)
    grading = raw.get("grading")
    if not isinstance(grading, dict):
        raise ValueError(f"{label} needs a 'grading' mapping")
    _reject_unknown_keys(grading, _GRADING_KEYS, f"{label} grading")
    must_mention = _alt_groups(grading.get("must_mention"), "must_mention")
    expected_evidence = _evidence(grading.get("expected_evidence"))
    if not must_mention:
        raise ValueError(f"{label} needs at least one must_mention entry")
    if not expected_evidence:
        raise ValueError(f"{label} needs at least one expected_evidence entry")
    return JourneyTurn(
        user=_require_str(raw, "user"),
        screen=_require_str(raw, "screen"),
        must_mention=must_mention,
        must_not_mention=_alt_groups(grading.get("must_not_mention"), "must_not_mention"),
        expected_evidence=expected_evidence,
        forbidden_targets=_targets(raw.get("forbidden_targets"), f"{label} forbidden_targets"),
        max_tool_calls=_positive_int_or_none(
            grading.get("max_tool_calls"), f"{label} max_tool_calls"
        ),
    )


def load_journey(path: Path) -> ConversationJourney:
    """Load and strictly validate one journey YAML file."""
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: journey must be a mapping")
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, f"{path.name}: journey")
    raw_turns = data.get("turns")
    if not isinstance(raw_turns, list) or len(raw_turns) < 2:
        raise ValueError(f"{path.name}: journey needs at least two turns")
    cluster = data.get("cluster")
    if not isinstance(cluster, dict):
        raise ValueError(f"{path.name}: journey needs a 'cluster' mapping")
    _reject_unknown_keys(cluster, _CLUSTER_KEYS, f"{path.name}: cluster")
    objects = _manifests(cluster.get("objects"), "objects")
    events = _manifests(cluster.get("events"), "events")
    _reject_future_timestamps(objects, f"{path.name}: objects")
    _reject_future_timestamps(events, f"{path.name}: events")
    return ConversationJourney(
        id=_require_str(data, "id"),
        root_cause=_require_str(data, "root_cause"),
        turns=tuple(_turn(raw, path, index) for index, raw in enumerate(raw_turns, 1)),
        objects=objects,
        events=events,
        logs=_logs(cluster.get("logs")),
        forbidden=_forbidden(cluster.get("forbidden"), f"{path.name}: forbidden"),
    )


def bundled_journeys_dir() -> Path:
    """Directory containing the conversational journey pack."""
    return Path(__file__).parent / "journeys"


def load_journeys(directory: Path) -> list[ConversationJourney]:
    """Load every journey in stable id order and reject duplicate ids."""
    journeys = [load_journey(path) for path in sorted(directory.glob("*.yaml"))]
    seen: set[str] = set()
    for journey in journeys:
        if journey.id in seen:
            raise ValueError(f"duplicate journey id {journey.id!r}")
        seen.add(journey.id)
    return sorted(journeys, key=lambda journey: journey.id)
