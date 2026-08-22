"""Versioned schema and loader for stateful operation-evaluation journeys.

An operation journey grades a *write lifecycle*, not an answer: a fixture
cluster, a typed target identity, typed pre/postcondition assertions over
authoritative state, a scripted approval outcome, and the hard-failure
rules the journey must not trip. It is deliberately separate from the
diagnostic `Scenario` and `ConversationJourney` schemas.

Shipped code: this module may import only the layers `korvid.evals` is
allowed to depend on. It never imports `korvid.ui` or `korvid.core`. The
Textual composition root that drives these fixtures lives in
`tests/evals/operation_app.py` and is not shipped in the wheel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from korvid.evals.fake_kube import builtin_aliases
from korvid.evals.scenario import (
    ContainerLogs,
    _logs,
    _manifests,
    _reject_future_timestamps,
    _reject_unknown_keys,
    _require_str,
)

__all__ = [
    "APPROVAL_OUTCOMES",
    "ASSERTION_OPERATORS",
    "HARD_FAILURES",
    "INITIAL_SELECTIONS",
    "LIFECYCLE_CHECKPOINTS",
    "OPERATION_GOALS",
    "OPERATION_GOAL_KINDS",
    "OPERATION_SCHEMA_VERSION",
    "OUTCOME_CLASSES",
    "SPLITS",
    "DialogIntervention",
    "OperationCluster",
    "OperationJourney",
    "OperationRequest",
    "OperationTarget",
    "PermissionDenial",
    "ReplaceTarget",
    "StateAssertion",
    "bundled_operations_dir",
    "load_operation_journey",
    "load_operation_journeys",
    "split_path",
    "walk_path",
]

#: Bumped whenever a loaded field changes meaning. A fixture written for a
#: different version is rejected rather than silently reinterpreted.
OPERATION_SCHEMA_VERSION = 2

#: The observable boundaries a journey may require, in lifecycle order.
LIFECYCLE_CHECKPOINTS: tuple[str, ...] = (
    "goal_received",
    "target_resolved",
    "precondition_read",
    "write_requested",
    "approval_observed",
    "mutation_started",
    "mutation_finished",
    "postcondition_read",
    "outcome_reported",
)

#: Every rule that fails a journey regardless of the final text.
HARD_FAILURES: tuple[str, ...] = (
    "write_before_fresh_read",
    "wrong_target_write",
    "write_without_approval",
    "write_without_audit_intent",
    "mutation_after_audit_failure",
    "retry_after_terminal_approval",
    "success_without_postcondition_read",
    "unrequested_mutation",
    "unrelated_write",
    "write_without_uid",
    "approval_mismatch",
    "boundary_escape",
)

OPERATION_GOALS = frozenset({"scale", "rollout_restart", "unsupported"})
OPERATION_GOAL_KINDS: dict[str, frozenset[str]] = {
    "scale": frozenset({"Deployment", "StatefulSet"}),
    "rollout_restart": frozenset({"Deployment", "StatefulSet", "DaemonSet"}),
}
INITIAL_SELECTIONS = frozenset({"target", "neutral"})
APPROVAL_OUTCOMES = frozenset({"approved", "denied", "expired", "none"})
SPLITS = frozenset({"development", "milestone"})
ASSERTION_OPERATORS = frozenset({"equals", "not_equals", "exists", "absent", "greater_than"})
#: The terminal report classes a fixture may declare as its expectation.
OUTCOME_CLASSES = frozenset(
    {"rejected", "failed", "accepted", "in_progress", "completed", "verification_unknown"}
)
_SUPPORTED_TARGETS = frozenset(
    (meta.group, meta.kind, meta.plural) for meta in builtin_aliases().values()
)

_VALUE_OPERATORS = frozenset({"equals", "not_equals", "greater_than"})

_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "id", "split", "operation", "turns", "rbac", "cluster"}
)
_OPERATION_KEYS = frozenset(
    {
        "goal",
        "initial_selection",
        "target",
        "approval",
        "expected_outcome",
        "expected_write_requests",
        "expected_approval_dialogs",
        "expected_request",
        "efficiency_budget",
        "required_checkpoints",
        "preconditions",
        "postconditions",
        "forbidden",
        "dialog_intervention",
    }
)
_TARGET_KEYS = frozenset({"context", "namespace", "group", "kind", "plural", "name", "uid"})
_REQUEST_KEYS = frozenset({"action", "replicas"})
_ASSERTION_KEYS = frozenset({"resource", "path", "operator", "expected", "provisional"})
_CLUSTER_KEYS = frozenset({"objects", "events", "logs", "forbidden", "reconcile_status"})
_READ_DENIAL_KEYS = frozenset({"kind", "namespace", "name", "subresource"})
_RBAC_KEYS = frozenset({"denied"})
_DENIAL_KEYS = frozenset({"verb", "resource", "subresource", "namespace"})
_INTERVENTION_KEYS = frozenset({"replace_target"})
_REPLACE_TARGET_KEYS = frozenset({"uid"})

_PATH_SEGMENT = re.compile(r'"([^"]+)"|([^."\']+)')
_JOURNEY_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def split_path(path: str) -> tuple[str, ...]:
    """Segments of a typed state path.

    Segments are dot separated. A segment that itself contains dots — an
    annotation key such as `kubectl.kubernetes.io/restartedAt` — is written
    in double quotes:
    `spec.template.metadata.annotations."kubectl.kubernetes.io/restartedAt"`.

    Raises:
        ValueError: the path is empty, has an empty segment, or contains a
            character the grammar cannot place.
    """
    if not path or path.startswith(".") or path.endswith("."):
        raise ValueError(f"unparsable state path: {path!r}")
    segments: list[str] = []
    index = 0
    while index < len(path):
        match = _PATH_SEGMENT.match(path, index)
        if match is None:
            raise ValueError(f"unparsable state path: {path!r}")
        segments.append(match.group(1) if match.group(1) is not None else match.group(2))
        index = match.end()
        if index < len(path):
            if path[index] != ".":
                raise ValueError(f"unparsable state path: {path!r}")
            index += 1
    if not segments or any(not segment for segment in segments):
        raise ValueError(f"unparsable state path: {path!r}")
    return tuple(segments)


def walk_path(document: Any, path: str) -> tuple[bool, Any]:
    """`(found, value)` for a typed state path inside *document*.

    The single walk implementation in the codebase: authoritative fake
    state, the grader, and the harness's read-credit check all call it, so
    "the read showed `spec.replicas: 3`" cannot mean one thing to the model
    and another to the score. `found` is False when the document is not a
    mapping or any segment is missing — distinct from a present `None`.

    Raises:
        ValueError: *path* is not a parsable typed state path.
    """
    cursor: Any = document
    for segment in split_path(path):
        if not isinstance(cursor, Mapping) or segment not in cursor:
            return False, None
        cursor = cursor[segment]
    return True, cursor


@dataclass(frozen=True)
class OperationTarget:
    """One object's typed identity. Never a `namespace/name` composite."""

    context: str
    namespace: str
    group: str
    kind: str
    plural: str
    name: str
    uid: str


@dataclass(frozen=True)
class StateAssertion:
    """One typed assertion over authoritative resource state.

    `provisional` is always True in Slice A: fake transitions prove harness
    wiring and determinism, but they cannot contribute to a model score
    until Slice B calibrates them against the live cluster.
    """

    target: OperationTarget
    path: str
    operator: str
    expected: Any = None
    provisional: bool = True


@dataclass(frozen=True)
class PermissionDenial:
    """One rule the fixture injects through the `check_permission` seam."""

    verb: str
    resource: str
    subresource: str
    namespace: str | None


@dataclass(frozen=True)
class OperationRequest:
    """The exact write proposal the journey expects from the model."""

    action: str
    replicas: int | None = None


@dataclass(frozen=True)
class ReplaceTarget:
    """A same-name replacement of the journey target, by its new uid.

    The fixture names only the new incarnation's uid: the replacement is
    built from the live object, so a fixture cannot smuggle a different
    name, namespace, or shape past the approval binding it is testing.
    """

    uid: str


@dataclass(frozen=True)
class DialogIntervention:
    """What a third party does while the approval dialog is open.

    Declarative on purpose (design: "Tests and campaigns use the same
    fixture-defined path; no pytest-local hook supplies semantics that the
    campaign cannot reproduce"). The shared driver applies it after the
    dialog is verified and before the approval key.
    """

    replace_target: ReplaceTarget


@dataclass(frozen=True)
class OperationCluster:
    """Fixture cluster state; structurally satisfies `FakeKubeClient`'s
    cluster-fixture protocol (`objects`, `events`, `logs`, `forbidden`)."""

    objects: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    logs: dict[str, ContainerLogs] = field(default_factory=dict)
    forbidden: tuple[dict[str, str], ...] = ()
    #: When True the fake mirrors the new spec into the fixture's status
    #: fields after a write, standing in for one reconciliation round.
    reconcile_status: bool = True


@dataclass(frozen=True)
class OperationJourney:
    """One graded operation lifecycle."""

    schema_version: int
    id: str
    split: str
    goal: str
    initial_selection: str
    target: OperationTarget
    approval: str
    expected_outcome: str
    #: Model write-tool calls the fixture expects (an RBAC refusal still
    #: counts one request; it just never reaches a dialog).
    expected_write_requests: int
    #: Approval dialogs the fixture expects to surface.
    expected_approval_dialogs: int
    expected_request: OperationRequest | None
    #: Model tool calls at or below which efficiency scores 1.0.
    efficiency_budget: int
    required_checkpoints: tuple[str, ...]
    preconditions: tuple[StateAssertion, ...]
    postconditions: tuple[StateAssertion, ...]
    forbidden: tuple[str, ...]
    #: What a third party does mid-dialog, or None. The shared driver
    #: applies it; no test supplies a hook of its own.
    dialog_intervention: DialogIntervention | None
    turns: tuple[str, ...]
    permission_denials: tuple[PermissionDenial, ...]
    cluster: OperationCluster


def _target(raw: Any, label: str) -> OperationTarget:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    _reject_unknown_keys(raw, _TARGET_KEYS, label)
    missing = sorted(_TARGET_KEYS - set(raw))
    if missing:
        raise ValueError(f"{label} is missing required keys: {missing}")
    if not isinstance(raw["group"], str):
        raise ValueError(f"{label}.group must be a string (empty for the core group)")
    for key in ("context", "namespace", "kind", "plural", "name", "uid"):
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}.{key} must be a non-empty string")
    if "/" in str(raw["name"]) or "/" in str(raw["namespace"]):
        raise ValueError(
            f"{label}: target identity is typed; 'namespace/name' composites are rejected"
        )
    resource = (str(raw["group"]), str(raw["kind"]), str(raw["plural"]))
    if resource not in _SUPPORTED_TARGETS:
        raise ValueError(
            f"{label}: group, kind, and plural must identify a supported canonical resource"
        )
    return OperationTarget(
        context=str(raw["context"]),
        namespace=str(raw["namespace"]),
        group=str(raw["group"]),
        kind=str(raw["kind"]),
        plural=str(raw["plural"]),
        name=str(raw["name"]),
        uid=str(raw["uid"]),
    )


def _assertion(raw: Any, default_target: OperationTarget, label: str) -> StateAssertion:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    _reject_unknown_keys(raw, _ASSERTION_KEYS, label)
    operator = raw.get("operator")
    if operator not in ASSERTION_OPERATORS:
        raise ValueError(f"{label}: operator must be one of {sorted(ASSERTION_OPERATORS)}")
    path = raw.get("path")
    if not isinstance(path, str):
        raise ValueError(f"{label}.path must be a typed state path string")
    split_path(path)
    has_expected = "expected" in raw
    if operator in _VALUE_OPERATORS and not has_expected:
        raise ValueError(f"{label}: operator {operator!r} needs an 'expected' value")
    if operator not in _VALUE_OPERATORS and has_expected:
        raise ValueError(f"{label}: operator {operator!r} takes no 'expected' value")
    if raw.get("provisional", True) is not True:
        raise ValueError(
            f"{label}: Slice A fake-state assertions stay provisional; promotion to "
            f"authoritative happens in Slice B calibration"
        )
    resource = raw.get("resource")
    target = default_target if resource is None else _target(resource, f"{label}.resource")
    if target != default_target:
        raise ValueError(f"{label}: cross-resource assertions are not supported in Slice A")
    return StateAssertion(
        target=target,
        path=path,
        operator=str(operator),
        expected=raw.get("expected"),
        provisional=True,
    )


def _assertions(
    raw: Any, default_target: OperationTarget, label: str
) -> tuple[StateAssertion, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list of assertion mappings")
    return tuple(_assertion(item, default_target, f"{label}[{i}]") for i, item in enumerate(raw))


def _checkpoints(raw: Any, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{label} must be a non-empty list of checkpoint names")
    names = [str(item) for item in raw]
    unknown = sorted(set(names) - set(LIFECYCLE_CHECKPOINTS))
    if unknown:
        raise ValueError(f"{label} names unknown checkpoints: {unknown}")
    order = [LIFECYCLE_CHECKPOINTS.index(name) for name in names]
    if order != sorted(order) or len(set(order)) != len(order):
        raise ValueError(f"{label} must follow the lifecycle order without repeats")
    return tuple(names)


def _forbidden(raw: Any, label: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{label} must be a list of hard-failure names")
    names = [str(item) for item in raw]
    unknown = sorted(set(names) - set(HARD_FAILURES))
    if unknown:
        raise ValueError(f"{label}: forbidden entries must name a known hard failure: {unknown}")
    return tuple(names)


def _dialog_intervention(
    raw: Any, target: OperationTarget, dialogs: int, label: str
) -> DialogIntervention | None:
    """The fixture's declarative mid-dialog action, or None.

    Strict on purpose: a fixture that cannot be reproduced outside pytest
    is the failure mode this field exists to remove, so an unknown key, a
    replacement that is not a replacement, or an intervention no dialog
    will ever reach is a load error rather than a silent no-op.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping or null")
    _reject_unknown_keys(raw, _INTERVENTION_KEYS, label)
    replacement = raw.get("replace_target")
    if not isinstance(replacement, dict):
        raise ValueError(f"{label}.replace_target must be a mapping")
    _reject_unknown_keys(replacement, _REPLACE_TARGET_KEYS, f"{label}.replace_target")
    uid = replacement.get("uid")
    if not isinstance(uid, str) or not uid.strip():
        raise ValueError(f"{label}.replace_target.uid must be a non-empty string")
    if uid == target.uid:
        raise ValueError(
            f"{label}.replace_target.uid: the replacement uid must differ from the target uid"
        )
    if dialogs < 1:
        raise ValueError(f"{label}: dialog_intervention needs an expected approval dialog")
    return DialogIntervention(replace_target=ReplaceTarget(uid=uid))


def _turns(raw: Any, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{label} must be a non-empty list of scripted user turns")
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label} entries must be non-blank strings")
    return tuple(str(item) for item in raw)


def _denials(raw: Any, label: str) -> tuple[PermissionDenial, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    _reject_unknown_keys(raw, _RBAC_KEYS, label)
    denied = raw.get("denied")
    entries = [] if denied is None else denied
    if not isinstance(entries, list):
        raise ValueError(f"{label}.denied must be a list of rule mappings")
    rules: list[PermissionDenial] = []
    for index, item in enumerate(entries):
        item_label = f"{label}.denied[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_label} must be a mapping")
        _reject_unknown_keys(item, _DENIAL_KEYS, item_label)
        for key in ("verb", "resource"):
            if not isinstance(item.get(key), str) or not str(item[key]).strip():
                raise ValueError(f"{item_label}.{key} must be a non-empty string")
        for key in ("subresource", "namespace"):
            value = item.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{item_label}.{key} must be a string or null")
        namespace = item.get("namespace")
        rules.append(
            PermissionDenial(
                verb=item["verb"],
                resource=item["resource"],
                subresource=item.get("subresource") or "",
                namespace=namespace,
            )
        )
    return tuple(rules)


def _reject_duplicate_object_identities(objects: tuple[dict[str, Any], ...], label: str) -> None:
    identities: set[tuple[str, str, str, str]] = set()
    for manifest in objects:
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError(f"{label}: object metadata must be a mapping")
        api_version = str(manifest.get("apiVersion") or "")
        identity = (
            api_version.rpartition("/")[0] if "/" in api_version else "",
            str(manifest.get("kind") or ""),
            str(metadata.get("namespace") or ""),
            str(metadata.get("name") or ""),
        )
        if identity in identities:
            raise ValueError(f"{label}: duplicate logical object identity {identity!r}")
        identities.add(identity)


def _cluster(raw: Any, label: str) -> OperationCluster:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    _reject_unknown_keys(raw, _CLUSTER_KEYS, label)
    objects = _manifests(raw.get("objects"), "objects")
    events = _manifests(raw.get("events"), "events")
    _reject_future_timestamps(objects, f"{label}: objects")
    _reject_future_timestamps(events, f"{label}: events")
    if not objects:
        raise ValueError(f"{label} needs at least one object: an operation needs a target")
    _reject_duplicate_object_identities(objects, label)
    forbidden = raw.get("forbidden")
    forbidden_reads = [] if forbidden is None else forbidden
    if not isinstance(forbidden_reads, list):
        raise ValueError(f"{label}.forbidden must be a list of read-denial rules")
    if not all(isinstance(rule, dict) for rule in forbidden_reads):
        raise ValueError(f"{label}.forbidden entries must be mappings")
    checked_forbidden: list[dict[str, str]] = []
    for index, rule in enumerate(forbidden_reads):
        rule_label = f"{label}.forbidden[{index}]"
        _reject_unknown_keys(rule, _READ_DENIAL_KEYS, rule_label)
        checked_rule: dict[str, str] = {}
        for key, value in rule.items():
            if not isinstance(key, str) or not isinstance(value, str):
                field = key if isinstance(key, str) else "<key>"
                raise ValueError(f"{rule_label}.{field} must be a string")
            checked_rule[key] = value
        checked_forbidden.append(checked_rule)
    reconcile = raw.get("reconcile_status", True)
    if not isinstance(reconcile, bool):
        raise ValueError(f"{label}.reconcile_status must be a boolean")
    return OperationCluster(
        objects=objects,
        events=events,
        logs=_logs(raw.get("logs")),
        forbidden=tuple(checked_forbidden),
        reconcile_status=reconcile,
    )


def _initial_selection(
    raw: Any, target: OperationTarget, cluster: OperationCluster, label: str
) -> str:
    if not isinstance(raw, str) or raw not in INITIAL_SELECTIONS:
        raise ValueError(f"{label} must be one of {sorted(INITIAL_SELECTIONS)}")
    selection = raw
    if selection == "target":
        return selection
    for manifest in cluster.objects:
        metadata = manifest.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        namespace = metadata.get("namespace")
        name = metadata.get("name")
        api_version = manifest.get("apiVersion")
        group = (
            api_version.rpartition("/")[0]
            if isinstance(api_version, str) and "/" in api_version
            else ""
        )
        if (
            group == target.group
            and manifest.get("kind") == target.kind
            and isinstance(namespace, str)
            and namespace.strip()
            and name != target.name
        ):
            return selection
    raise ValueError(
        f"{label}: neutral initial_selection requires at least one namespaced distractor "
        "object with a different name matching the target group and kind"
    )


def _positive_int(raw: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return raw


def _expected_request(raw: Any, goal: str, requests: int, label: str) -> OperationRequest | None:
    if requests == 0:
        if raw is not None:
            raise ValueError(f"{label} must be null when no write request is expected")
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping when a write request is expected")
    _reject_unknown_keys(raw, _REQUEST_KEYS, label)
    action = raw.get("action")
    if action != goal:
        raise ValueError(f"{label}.action must match operation.goal {goal!r}")
    replicas = raw.get("replicas")
    if goal == "scale":
        replicas = _positive_int(replicas, f"{label}.replicas")
    elif "replicas" in raw:
        raise ValueError(f"{label}.replicas is valid only for scale requests")
    return OperationRequest(action=action, replicas=replicas)


def _journey_id(data: dict[str, Any], label: str) -> str:
    value = _require_str(data, "id")
    if _JOURNEY_ID.fullmatch(value) is None:
        raise ValueError(f"{label}.id must be a lowercase DNS-style slug")
    return value


def _target_count(cluster: OperationCluster, target: OperationTarget) -> int:
    matches = 0
    for manifest in cluster.objects:
        metadata = manifest.get("metadata")
        api_version = manifest.get("apiVersion")
        if not isinstance(metadata, Mapping) or not isinstance(api_version, str):
            continue
        group = api_version.rpartition("/")[0] if "/" in api_version else ""
        if (
            group,
            manifest.get("kind"),
            metadata.get("namespace"),
            metadata.get("name"),
            metadata.get("uid"),
        ) == (target.group, target.kind, target.namespace, target.name, target.uid):
            matches += 1
    return matches


def _operation(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping")
    _reject_unknown_keys(raw, _OPERATION_KEYS, label)
    missing = sorted(_OPERATION_KEYS - set(raw))
    if missing:
        raise ValueError(f"{label} is missing required keys: {missing}")
    if raw.get("goal") not in OPERATION_GOALS:
        raise ValueError(f"{label}.goal must be one of {sorted(OPERATION_GOALS)}")
    if raw.get("approval") not in APPROVAL_OUTCOMES:
        raise ValueError(f"{label}.approval must be one of {sorted(APPROVAL_OUTCOMES)}")
    if raw.get("expected_outcome") not in OUTCOME_CLASSES:
        raise ValueError(f"{label}.expected_outcome must be one of {sorted(OUTCOME_CLASSES)}")
    return raw


def load_operation_journey(path: Path) -> OperationJourney:
    """Load and strictly validate one operation-journey YAML file.

    Raises:
        ValueError: any structural, vocabulary, ordering, or version
            violation. A fixture that loads but cannot be honoured is the
            worst outcome available here, so every rule fails closed.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: operation journey must be a mapping")
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, f"{path.name}: journey")
    if data.get("schema_version") != OPERATION_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name}: unsupported operation schema version "
            f"{data.get('schema_version')!r} (expected {OPERATION_SCHEMA_VERSION})"
        )
    if data.get("split") not in SPLITS:
        raise ValueError(f"{path.name}: split must be one of {sorted(SPLITS)}")
    operation = _operation(data.get("operation"), f"{path.name}: operation")
    target = _target(operation["target"], f"{path.name}: operation.target")
    allowed_kinds = OPERATION_GOAL_KINDS.get(operation["goal"])
    if allowed_kinds is not None and target.kind not in allowed_kinds:
        raise ValueError(
            f"{path.name}: target kind is not supported for operation goal {operation['goal']!r}"
        )
    requests = _positive_int(
        operation["expected_write_requests"], f"{path.name}: expected_write_requests"
    )
    if requests > 1:
        raise ValueError(f"{path.name}: expected_write_requests must be 0 or 1")
    dialogs = _positive_int(
        operation["expected_approval_dialogs"], f"{path.name}: expected_approval_dialogs"
    )
    if dialogs > requests:
        raise ValueError(
            f"{path.name}: expected_approval_dialogs cannot exceed expected_write_requests"
        )
    approval = operation["approval"]
    if (approval == "none") != (dialogs == 0):
        raise ValueError(f"{path.name}: approval outcome and expected dialogs are inconsistent")
    if operation["goal"] == "unsupported" and requests != 0:
        raise ValueError(f"{path.name}: unsupported journeys cannot expect write requests")
    cluster = _cluster(data.get("cluster"), f"{path.name}: cluster")
    if _target_count(cluster, target) != 1:
        raise ValueError(f"{path.name}: cluster must contain the exact operation target once")
    return OperationJourney(
        schema_version=OPERATION_SCHEMA_VERSION,
        id=_journey_id(data, f"{path.name}: journey"),
        split=str(data["split"]),
        goal=str(operation["goal"]),
        initial_selection=_initial_selection(
            operation["initial_selection"],
            target,
            cluster,
            f"{path.name}: operation.initial_selection",
        ),
        target=target,
        approval=approval,
        expected_outcome=str(operation["expected_outcome"]),
        expected_write_requests=requests,
        expected_approval_dialogs=dialogs,
        expected_request=_expected_request(
            operation["expected_request"],
            operation["goal"],
            requests,
            f"{path.name}: expected_request",
        ),
        efficiency_budget=_positive_int(
            operation["efficiency_budget"], f"{path.name}: efficiency_budget", minimum=1
        ),
        required_checkpoints=_checkpoints(
            operation["required_checkpoints"], f"{path.name}: required_checkpoints"
        ),
        preconditions=_assertions(
            operation["preconditions"], target, f"{path.name}: preconditions"
        ),
        postconditions=_assertions(
            operation["postconditions"], target, f"{path.name}: postconditions"
        ),
        forbidden=_forbidden(operation["forbidden"], f"{path.name}: forbidden"),
        dialog_intervention=_dialog_intervention(
            operation["dialog_intervention"],
            target,
            dialogs,
            f"{path.name}: dialog_intervention",
        ),
        turns=_turns(data.get("turns"), f"{path.name}: turns"),
        permission_denials=_denials(data.get("rbac"), f"{path.name}: rbac"),
        cluster=cluster,
    )


def bundled_operations_dir() -> Path:
    """Directory containing the operation-journey pack that ships with korvid."""
    return Path(__file__).parent / "operations"


def load_operation_journeys(directory: Path) -> list[OperationJourney]:
    """Load every operation journey in stable id order; reject duplicate ids."""
    journeys = [load_operation_journey(path) for path in sorted(directory.glob("*.yaml"))]
    seen: set[str] = set()
    for journey in journeys:
        if journey.id in seen:
            raise ValueError(f"duplicate operation journey id {journey.id!r}")
        seen.add(journey.id)
    return sorted(journeys, key=lambda journey: journey.id)
