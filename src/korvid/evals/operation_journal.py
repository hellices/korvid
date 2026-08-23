"""Append-only, actor-attributed journal of operation boundaries.

The journal records boundaries, not only model tool calls: the app's own
target resolution, the approval driver's verified keystroke, the audit
records, the injected `WriteOps` execution, fixture-actor interference,
and the grader's final read.

It is also a published artifact (`run.journal` is serialized into campaign
output), so it stores summaries rather than payloads: `result` is a token
from a closed status vocabulary, `detail` is a key=value summary over an
allowlist, and state mappings reject Secret paths and non-scalars.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from types import MappingProxyType
from typing import Any

from korvid.evals.operation import LIFECYCLE_CHECKPOINTS, OperationTarget

__all__ = [
    "JOURNAL_ACTORS",
    "JOURNAL_DETAIL_KEYS",
    "JOURNAL_RESULTS",
    "ActionJournal",
    "JournalEvent",
    "JournalTarget",
    "summarize",
    "summarize_action",
    "summarize_arguments",
    "summarize_untrusted",
]

JOURNAL_ACTORS: tuple[str, ...] = (
    "model_tool",
    "app_internal",
    "approval_driver",
    "fixture_actor",
    "audit",
    "write_ops",
    "grader",
)

JOURNAL_RESULTS: tuple[str, ...] = (
    "",
    "absent",
    "approved",
    "bare_name",
    "blocked",
    "captured",
    "conflict",
    "credited",
    "declined",
    "denied",
    "durable",
    "empty",
    "error",
    "expired",
    "found",
    "hard_failure",
    "intent",
    "keystroke",
    "matched",
    "mismatched",
    "missing",
    "no_credit",
    "no_keystroke",
    "no_uid",
    "present",
    "refused",
    "replaced",
    "reported",
    "requested",
    "resolved",
    "row_key",
    "skipped",
    "started",
    "success",
)

JOURNAL_DETAIL_KEYS: tuple[str, ...] = (
    "action",
    "chars",
    "checkpoint",
    "context",
    "count",
    "dropped",
    "generation",
    "group",
    "kind",
    "name",
    "namespace",
    "path",
    "plural",
    "reason",
    "replicas",
    "resource",
    "row_key",
    "status",
    "tool",
    "uid",
)

_SECRET_SEGMENTS = frozenset({"data", "stringdata"})
_SCALARS = (str, int, float, bool)
_DETAIL_KEYS = frozenset(JOURNAL_DETAIL_KEYS)
_RESULTS = frozenset(JOURNAL_RESULTS)
_SUMMARY_VALUE = re.compile(r"[A-Za-z0-9._:/@=+-]{1,120}")
_UNKNOWN_TOOL = "unknown_tool"
_REDACTED_VALUE = "redacted"


def _summary_text(key: str, value: Any, *, strip_quotes: bool) -> str:
    """Normalized summary token for one field value."""

    if isinstance(value, bool) or not isinstance(value, _SCALARS):
        raise ValueError(f"journal detail values must be scalars: {key!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"journal detail value is not a bounded summary token: {key!r}")
    text = str(value)
    if strip_quotes:
        text = text.replace('"', "")
    if not _SUMMARY_VALUE.fullmatch(text):
        raise ValueError(f"journal detail value is not a bounded summary token: {key!r}")
    return text


def summarize(**fields: Any) -> str:
    """Build a journal `detail` from allowlisted fields."""

    parts: list[str] = []
    for key, value in fields.items():
        if key not in _DETAIL_KEYS:
            raise ValueError(f"journal detail key is not allowlisted: {key!r}")
        if value is None:
            continue
        parts.append(f"{key}={_summary_text(key, value, strip_quotes=True)}")
    return " ".join(parts)


def _project_untrusted_fields(
    fields: Iterable[tuple[str, object]],
    *,
    reserved_keys: frozenset[str],
    redact_strings: bool = False,
) -> tuple[list[str], int]:
    parts: list[str] = []
    dropped = 0
    for key, value in fields:
        if key not in _DETAIL_KEYS or key in reserved_keys:
            dropped += 1
            continue
        try:
            text = _summary_text(key, value, strip_quotes=False)
        except ValueError:
            dropped += 1
            continue
        if redact_strings and isinstance(value, str):
            text = _REDACTED_VALUE
        parts.append(f"{key}={text}")
    return parts, dropped


def summarize_untrusted(**fields: object) -> str:
    """Best-effort journal detail for untrusted inputs; never raises."""

    parts, dropped = _project_untrusted_fields(fields.items(), reserved_keys=frozenset({"dropped"}))
    parts.append(f"dropped={dropped}")
    return " ".join(parts)


def summarize_action(value: str) -> str:
    """Return a bounded tool/action token or a safe fallback."""

    if isinstance(value, str) and _SUMMARY_VALUE.fullmatch(value):
        return value
    return _UNKNOWN_TOOL


def summarize_arguments(tool: str, arguments: Mapping[str, Any]) -> str:
    """Project raw tool arguments without publishing untrusted string values."""

    parts: list[str] = []
    dropped = 0
    try:
        parts.append(f"tool={_summary_text('tool', tool, strip_quotes=False)}")
    except ValueError:
        dropped += 1
    arg_parts, arg_dropped = _project_untrusted_fields(
        sorted(arguments.items()),
        reserved_keys=frozenset({"tool", "dropped"}),
        redact_strings=True,
    )
    parts.extend(arg_parts)
    parts.append(f"dropped={dropped + arg_dropped}")
    return " ".join(parts)


@dataclass(frozen=True)
class JournalTarget:
    """The object an event is about, as typed identity."""

    context: str
    namespace: str | None
    group: str
    kind: str
    plural: str
    name: str
    uid: str | None

    @classmethod
    def of(cls, target: OperationTarget, *, uid: str | None = None) -> JournalTarget:
        """Build a journal target from a loaded operation target."""

        return cls(
            context=target.context,
            namespace=target.namespace,
            group=target.group,
            kind=target.kind,
            plural=target.plural,
            name=target.name,
            uid=target.uid if uid is None else uid,
        )


@dataclass(frozen=True)
class JournalEvent:
    """One recorded boundary."""

    sequence: int
    event: str
    actor: str
    action: str = ""
    target: JournalTarget | None = None
    approval: str | None = None
    pre_state: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    post_state: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    result: str = ""
    detail: str = ""
    credit: bool = False


def _checked_state(
    state: Mapping[str, Any] | None, target: JournalTarget | None
) -> Mapping[str, Any]:
    if not state:
        return MappingProxyType({})
    if target is not None and target.kind == "Secret":
        raise ValueError("Secret state is never journaled")
    checked: dict[str, Any] = {}
    for path, value in state.items():
        segments = [segment.strip('"').lower() for segment in str(path).split(".")]
        if any(segment in _SECRET_SEGMENTS for segment in segments):
            raise ValueError(f"journal state must not carry secret payloads: {path!r}")
        if value is not None and not isinstance(value, _SCALARS):
            raise ValueError(f"journal state values must be scalars: {path!r}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"journal state values must be finite: {path!r}")
        checked[str(path)] = value
    return MappingProxyType(checked)


def _checked_detail(detail: str) -> str:
    if not detail:
        return ""
    for part in detail.split(" "):
        key, separator, value = part.partition("=")
        if not separator or key not in _DETAIL_KEYS or not _SUMMARY_VALUE.fullmatch(value):
            raise ValueError(f"journal detail must be an allowlisted key=value summary: {detail!r}")
    return detail


def _checked_action(action: str) -> str:
    if not action:
        return ""
    if not _SUMMARY_VALUE.fullmatch(action):
        raise ValueError(f"journal action must be a bounded summary token: {action!r}")
    return action


class ActionJournal:
    """Append-only event log. Nothing removes or rewrites an entry."""

    def __init__(self) -> None:
        self._events: list[JournalEvent] = []

    @property
    def events(self) -> tuple[JournalEvent, ...]:
        """Every event so far, in append order."""

        return tuple(self._events)

    def append(
        self,
        *,
        event: str,
        actor: str,
        action: str = "",
        target: JournalTarget | None = None,
        approval: str | None = None,
        pre_state: Mapping[str, Any] | None = None,
        post_state: Mapping[str, Any] | None = None,
        result: str = "",
        detail: str = "",
        credit: bool = False,
    ) -> JournalEvent:
        """Record one boundary and return it."""

        if actor not in JOURNAL_ACTORS:
            raise ValueError(f"unknown journal actor: {actor!r}")
        if credit and actor != "model_tool":
            raise ValueError("only model_tool events may earn read credit")
        if result not in _RESULTS:
            raise ValueError(f"journal result must be an allowlisted status summary: {result!r}")
        entry = JournalEvent(
            sequence=len(self._events) + 1,
            event=event,
            actor=actor,
            action=_checked_action(action),
            target=target,
            approval=approval,
            pre_state=_checked_state(pre_state, target),
            post_state=_checked_state(post_state, target),
            result=result,
            detail=_checked_detail(detail),
            credit=credit,
        )
        self._events.append(entry)
        return entry

    def checkpoints(self) -> tuple[str, ...]:
        """Recorded lifecycle checkpoints in append order."""

        return tuple(e.event for e in self._events if e.event in LIFECYCLE_CHECKPOINTS)

    def has(self, event: str) -> bool:
        """Whether `event` was recorded at least once."""

        return any(e.event == event for e in self._events)

    def count(self, event: str) -> int:
        """How many times `event` was recorded."""

        return sum(1 for e in self._events if e.event == event)

    def payload(self) -> list[dict[str, Any]]:
        """JSON-ready records for the campaign artifact."""

        return [
            {
                "sequence": entry.sequence,
                "event": entry.event,
                "actor": entry.actor,
                "action": entry.action,
                "target": None if entry.target is None else asdict(entry.target),
                "approval": entry.approval,
                "pre_state": dict(entry.pre_state),
                "post_state": dict(entry.post_state),
                "result": entry.result,
                "detail": entry.detail,
                "credit": entry.credit,
            }
            for entry in self._events
        ]
