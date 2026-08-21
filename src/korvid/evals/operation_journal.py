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

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
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
    "summarize_arguments",
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


def _summary_text(key: str, value: Any) -> str:
    """Normalized summary token for one trusted field value."""

    if isinstance(value, bool) or not isinstance(value, _SCALARS):
        raise ValueError(f"journal detail values must be scalars: {key!r}")
    text = str(value).replace('"', "")
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
        parts.append(f"{key}={_summary_text(key, value)}")
    return " ".join(parts)


def summarize_arguments(tool: str, arguments: Mapping[str, Any]) -> str:
    """Project raw tool arguments onto the detail allowlist best-effort."""

    kept: dict[str, Any] = {}
    dropped = 0
    for key, value in sorted(arguments.items()):
        if key not in _DETAIL_KEYS or key == "tool":
            dropped += 1
            continue
        if isinstance(value, bool) or not isinstance(value, _SCALARS):
            dropped += 1
            continue
        if isinstance(value, str) and not value:
            dropped += 1
            continue
        try:
            _summary_text(key, value)
        except ValueError:
            dropped += 1
            continue
        kept[key] = value
    fields: dict[str, Any] = {"tool": tool}
    fields.update(kept)
    fields["dropped"] = dropped
    return summarize(**fields)


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
    pre_state: dict[str, Any] = field(default_factory=dict)
    post_state: dict[str, Any] = field(default_factory=dict)
    result: str = ""
    detail: str = ""
    credit: bool = False


def _checked_state(state: Mapping[str, Any] | None, target: JournalTarget | None) -> dict[str, Any]:
    if not state:
        return {}
    if target is not None and target.kind == "Secret":
        raise ValueError("Secret state is never journaled")
    checked: dict[str, Any] = {}
    for path, value in state.items():
        segments = [segment.strip('"').lower() for segment in str(path).split(".")]
        if any(segment in _SECRET_SEGMENTS for segment in segments):
            raise ValueError(f"journal state must not carry secret payloads: {path!r}")
        if value is not None and not isinstance(value, _SCALARS):
            raise ValueError(f"journal state values must be scalars: {path!r}")
        checked[str(path)] = value
    return checked


def _checked_detail(detail: str) -> str:
    if not detail:
        return ""
    for part in detail.split(" "):
        key, separator, value = part.partition("=")
        if not separator or key not in _DETAIL_KEYS or not _SUMMARY_VALUE.fullmatch(value):
            raise ValueError(f"journal detail must be an allowlisted key=value summary: {detail!r}")
    return detail


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
            action=action,
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

        return [asdict(entry) for entry in self._events]
