"""Pure bounded session timeline primitives (issue #282 Task 1)."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from korvid.core.redaction import RedactionRecord, redact_text, strip_control_characters

_MAX_WARNING_REASON_CHARS = 128
_MAX_WARNING_NOTE_CHARS = 240
_MAX_EVENT_TIMESTAMP_CHARS = 64
_MAX_RESOURCE_KIND_CHARS = 128
_MAX_RESOURCE_NAMESPACE_CHARS = 63
_MAX_RESOURCE_NAME_CHARS = 253
_MAX_RESOURCE_UID_CHARS = 128


class TimelineSource(StrEnum):
    WATCH = "watch"
    EVENT = "event"
    CONTEXT = "context"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class TimelineResourceRef:
    kind_alias: str | None
    display_kind: str
    namespace: str
    name: str
    uid: str | None = None

    def matches(self, other: TimelineResourceRef) -> bool:
        """Return whether two resource references identify the same object."""
        if (self.kind_alias, self.namespace, self.name) != (
            other.kind_alias,
            other.namespace,
            other.name,
        ):
            return False
        if self.uid and other.uid:
            return self.uid == other.uid
        return True


@dataclass(frozen=True, slots=True)
class WatchDeltaPayload:
    verb: Literal["ADDED", "MODIFIED", "DELETED"]


@dataclass(frozen=True, slots=True)
class WarningEventPayload:
    reason: str
    note: str
    count: int


@dataclass(frozen=True, slots=True)
class ContextSwitchPayload:
    phase: Literal["started", "completed", "failed"]
    from_context: str | None
    to_context: str | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class WriteAuditPayload:
    action: str
    outcome: str


TimelinePayload = WatchDeltaPayload | WarningEventPayload | ContextSwitchPayload | WriteAuditPayload


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    sequence: int
    occurred_at: str
    epoch: int
    source: TimelineSource
    resource: TimelineResourceRef | None
    payload: TimelinePayload


@dataclass(frozen=True, slots=True)
class TimelineStats:
    entry_count: int
    encoded_bytes: int
    evicted: int
    refused: int


@dataclass(frozen=True, slots=True)
class TimelineSnapshot:
    entries: tuple[TimelineEntry, ...]
    stats: TimelineStats


@dataclass(frozen=True, slots=True)
class AppendResult:
    accepted: bool
    diagnostic: str | None
    evicted: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _normalize_text(raw: object, path: str) -> str:
    records: list[RedactionRecord] = []
    redacted = redact_text(str(raw or ""), path, records)
    return " ".join(redacted.split())


def _strip_controls(raw: object, path: str) -> str:
    records: list[RedactionRecord] = []
    return strip_control_characters(str(raw or ""), path, records)


def _resource_ref(
    *,
    kind_alias: str | None,
    display_kind: str,
    namespace: str | None,
    name: str | None,
    uid: str | None,
) -> TimelineResourceRef | None:
    normalized_name = str(name or "")
    normalized_namespace = str(namespace or "")
    if not normalized_name:
        return None
    return TimelineResourceRef(
        kind_alias=kind_alias,
        display_kind=display_kind,
        namespace=normalized_namespace,
        name=normalized_name,
        uid=uid,
    )


class SessionTimeline:
    def __init__(self, max_entries: int, max_bytes: int) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: deque[tuple[TimelineEntry, int]] = deque()
        self._next_sequence = 1
        self._encoded_bytes = 0
        self._evicted = 0
        self._refused = 0

    def append_watch(
        self,
        *,
        epoch: int,
        kind_alias: str,
        display_kind: str,
        namespace: str,
        name: str,
        uid: str | None,
        verb: Literal["ADDED", "MODIFIED", "DELETED"],
    ) -> AppendResult:
        resource = TimelineResourceRef(
            kind_alias=kind_alias,
            display_kind=display_kind,
            namespace=namespace,
            name=name,
            uid=uid,
        )
        return self._append(
            epoch=epoch,
            source=TimelineSource.WATCH,
            resource=resource,
            payload=WatchDeltaPayload(verb=verb),
        )

    def append_warning_event(
        self, *, epoch: int, event: dict[str, Any], kind_alias: str | None
    ) -> AppendResult:
        involved_value = event.get("involvedObject")
        involved = involved_value if isinstance(involved_value, dict) else {}
        metadata_value = event.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        count_value = event.get("count")
        count = (
            count_value
            if isinstance(count_value, int)
            and not isinstance(count_value, bool)
            and count_value > 0
            else 1
        )
        resource = _resource_ref(
            kind_alias=kind_alias,
            display_kind=_strip_controls(
                involved.get("kind") or "Event", "timeline.event.resource.kind"
            )[:_MAX_RESOURCE_KIND_CHARS],
            namespace=_strip_controls(
                involved.get("namespace"), "timeline.event.resource.namespace"
            )[:_MAX_RESOURCE_NAMESPACE_CHARS],
            name=_strip_controls(involved.get("name"), "timeline.event.resource.name")[
                :_MAX_RESOURCE_NAME_CHARS
            ],
            uid=_strip_controls(involved.get("uid"), "timeline.event.resource.uid")[
                :_MAX_RESOURCE_UID_CHARS
            ]
            or None,
        )
        payload = WarningEventPayload(
            reason=_normalize_text(event.get("reason") or "Warning", "timeline.event.reason")[
                :_MAX_WARNING_REASON_CHARS
            ],
            note=_normalize_text(event.get("message") or "", "timeline.event.message")[
                :_MAX_WARNING_NOTE_CHARS
            ],
            count=count,
        )
        occurred_at = _strip_controls(
            event.get("lastTimestamp")
            or event.get("eventTime")
            or event.get("firstTimestamp")
            or metadata.get("creationTimestamp")
            or _utc_now(),
            "timeline.event.occurred_at",
        )[:_MAX_EVENT_TIMESTAMP_CHARS]
        return self._append(
            epoch=epoch,
            source=TimelineSource.EVENT,
            resource=resource,
            payload=payload,
            occurred_at=occurred_at,
        )

    def append_context_switch(
        self,
        *,
        epoch: int,
        phase: Literal["started", "completed", "failed"],
        from_context: str | None,
        to_context: str | None,
        note: str = "",
    ) -> AppendResult:
        return self._append(
            epoch=epoch,
            source=TimelineSource.CONTEXT,
            resource=None,
            payload=ContextSwitchPayload(
                phase=phase,
                from_context=from_context,
                to_context=to_context,
                note=" ".join(note.split())[:160],
            ),
        )

    def append_write(
        self,
        *,
        epoch: int,
        action: str,
        kind_alias: str,
        display_kind: str,
        namespace: str | None,
        name: str,
        uid: str | None,
        outcome: str,
    ) -> AppendResult:
        resource = TimelineResourceRef(
            kind_alias=kind_alias,
            display_kind=display_kind,
            namespace=namespace or "",
            name=name,
            uid=uid,
        )
        return self._append(
            epoch=epoch,
            source=TimelineSource.WRITE,
            resource=resource,
            payload=WriteAuditPayload(
                action=" ".join(action.split()),
                outcome=" ".join(outcome.split())[:160],
            ),
        )

    def snapshot(
        self,
        *,
        epoch: int | None,
        source: TimelineSource | None,
        resource: TimelineResourceRef | None,
    ) -> TimelineSnapshot:
        entries = tuple(
            entry
            for entry, _size in self._entries
            if (epoch is None or entry.epoch == epoch)
            and (source is None or entry.source is source)
            and (
                resource is None
                or (entry.resource is not None and entry.resource.matches(resource))
            )
        )
        return TimelineSnapshot(
            entries=entries,
            stats=TimelineStats(
                entry_count=len(self._entries),
                encoded_bytes=self._encoded_bytes,
                evicted=self._evicted,
                refused=self._refused,
            ),
        )

    def _append(
        self,
        *,
        epoch: int,
        source: TimelineSource,
        resource: TimelineResourceRef | None,
        payload: TimelinePayload,
        occurred_at: str | None = None,
    ) -> AppendResult:
        entry = TimelineEntry(
            sequence=self._next_sequence,
            occurred_at=occurred_at or _utc_now(),
            epoch=epoch,
            source=source,
            resource=resource,
            payload=payload,
        )
        encoded = json.dumps(
            self._entry_dict(entry),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded_size = len(encoded)
        if encoded_size > self._max_bytes:
            self._refused += 1
            return AppendResult(
                accepted=False,
                diagnostic=(f"timeline entry too large ({encoded_size} > {self._max_bytes} bytes)"),
                evicted=0,
            )
        self._entries.append((entry, encoded_size))
        self._next_sequence += 1
        self._encoded_bytes += encoded_size
        evicted = 0
        while len(self._entries) > self._max_entries or self._encoded_bytes > self._max_bytes:
            _oldest, oldest_size = self._entries.popleft()
            self._encoded_bytes -= oldest_size
            self._evicted += 1
            evicted += 1
        return AppendResult(accepted=True, diagnostic=None, evicted=evicted)

    @staticmethod
    def _entry_dict(entry: TimelineEntry) -> dict[str, Any]:
        return {
            "sequence": entry.sequence,
            "occurred_at": entry.occurred_at,
            "epoch": entry.epoch,
            "source": entry.source.value,
            "resource": None if entry.resource is None else asdict(entry.resource),
            "payload": asdict(entry.payload),
        }
