"""Pure, bounded state for current problems and recent Warning events."""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from korvid.core.findings import Evidence, Finding, ResourceIdentity
from korvid.core.redaction import redact_text
from korvid.k8s.events import select_event_timestamp

_TEXT_LIMIT = 1024
_REASON_LIMIT = 128
_TARGET_LIMITS = {"group": 253, "kind": 128, "namespace": 63, "name": 253, "uid": 128}
_FRESH_STATES = frozenset({"complete", "partial", "capped"})
_STALE_AFTER = timedelta(seconds=30)
_MAX_COUNT = 2**63 - 1


def _normalized_text(value: object) -> str:
    if not isinstance(value, (str, int, float, bool)):
        return ""
    return " ".join(redact_text(str(value), "pulse", []).split())


def _text(value: object, limit: int = _TEXT_LIMIT) -> str:
    sanitized = _normalized_text(value)
    return sanitized if len(sanitized) <= limit else sanitized[: limit - 1] + "…"


def _aware(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Pulse timestamps must be timezone-aware")
    return timestamp.astimezone(UTC)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _group(api_version: object) -> str:
    return (
        api_version.split("/", 1)[0] if isinstance(api_version, str) and "/" in api_version else ""
    )


def _identity(resource: ResourceIdentity) -> ResourceIdentity:
    target = PulseTarget("", resource.kind, resource.namespace, resource.name, resource.uid)
    return ResourceIdentity(
        target.kind,
        target.namespace,
        target.name,
        target.uid or "",
    )


def _finding(finding: Finding) -> Finding:
    return Finding(
        _text(finding.rule_id, 128),
        _text(finding.rule_version, 32),
        finding.severity,
        finding.confidence,
        _identity(finding.primary),
        tuple(_identity(resource) for resource in finding.related),
        tuple(
            Evidence(
                _identity(evidence.resource), _text(evidence.field, 256), _text(evidence.value)
            )
            for evidence in finding.evidence
        ),
        _text(finding.explanation),
        tuple(_text(check) for check in finding.next_checks),
    )


@dataclass(frozen=True, slots=True)
class PulseTarget:
    """Resource reference; an absent UID cannot be navigated safely."""

    group: str
    kind: str
    namespace: str
    name: str
    uid: str | None

    def __post_init__(self) -> None:
        navigable = True
        for field, limit in _TARGET_LIMITS.items():
            raw = getattr(self, field)
            value = _text(raw, limit)
            if raw is not None and raw != value:
                navigable = False
            object.__setattr__(self, field, value if field != "uid" else value or None)
        if not navigable or not self.kind or not self.name:
            object.__setattr__(self, "uid", None)


@dataclass(frozen=True, slots=True)
class PulseItem:
    """One current finding or recent Warning observation."""

    key: str
    category: Literal["current", "recent"]
    target: PulseTarget
    reason: str
    message: str
    observed_at: datetime
    count: int = 1
    finding: Finding | None = None
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _text(self.key, 256))
        object.__setattr__(self, "target", replace(self.target))
        object.__setattr__(self, "reason", _text(self.reason, _REASON_LIMIT))
        object.__setattr__(self, "message", _text(self.message))
        object.__setattr__(self, "source", _text(self.source, 128))
        object.__setattr__(self, "observed_at", _aware(self.observed_at))
        object.__setattr__(self, "count", _count(self.count))
        if self.finding is not None:
            object.__setattr__(self, "finding", _finding(self.finding))


PulseCoverageState = Literal[
    "loading", "complete", "partial", "forbidden", "unavailable", "failed", "capped", "stale"
]


@dataclass(frozen=True, slots=True)
class PulseCoverage:
    """Observation status for a source, independent of its findings."""

    source: str
    state: PulseCoverageState
    observed_at: datetime | None
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _text(self.source, 128))
        object.__setattr__(self, "detail", _text(self.detail))
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", _aware(self.observed_at))


@dataclass(frozen=True, slots=True)
class PulseSnapshot:
    """Detached, immutable view of one context and namespace epoch."""

    epoch: int
    scope: str | None
    current: tuple[PulseItem, ...]
    recent: tuple[PulseItem, ...]
    coverage: tuple[PulseCoverage, ...]
    dropped: int = 0
    current_dropped: int = 0


class PulseRule(ABC):
    """A pure current-state rule for one independently observed source."""

    @property
    @abstractmethod
    def source(self) -> str:
        """Return the collection source key."""

    @abstractmethod
    def evaluate(
        self, objects: Sequence[dict[str, Any]], observed_at: datetime
    ) -> tuple[PulseItem, ...]:
        """Derive current findings without retaining input objects."""

    def can_clear(self, resource: dict[str, Any]) -> bool:
        """Allow reconciliation only when this resource's evidence is assessable.

        Evaluate-only rules retain their existing behavior by default. Rules
        accepting incomplete status should override this pure assessment.
        """
        return True


def _count(value: object) -> int:
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        return max(1, min(value, _MAX_COUNT))
    if isinstance(value, str) and value.isdecimal() and len(value) <= 20:
        return max(1, min(int(value), _MAX_COUNT))
    return 1


def _event_time(event: dict[str, Any]) -> datetime | None:
    raw = select_event_timestamp(event)
    if not raw or len(raw) > 64:
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except (ValueError, OverflowError):
        return None


def _event_reference(event: dict[str, Any]) -> dict[str, Any]:
    return _mapping(event.get("regarding")) or _mapping(event.get("involvedObject"))


def _event_target(event: dict[str, Any]) -> PulseTarget:
    reference = _event_reference(event)
    return PulseTarget(
        _group(reference.get("apiVersion")),
        reference.get("kind", ""),
        reference.get("namespace", ""),
        reference.get("name", ""),
        reference.get("uid"),
    )


def _event_namespace(event: dict[str, Any]) -> str:
    metadata = _mapping(event.get("metadata"))
    return _text(metadata.get("namespace", _event_reference(event).get("namespace", "")), 63)


def _event_key(event: dict[str, Any], target: PulseTarget) -> str:
    metadata = _mapping(event.get("metadata"))
    identity: tuple[str, ...]
    if metadata.get("uid"):
        identity = ("uid", str(metadata["uid"]))
    elif metadata.get("name"):
        identity = ("name", str(metadata.get("namespace", "")), str(metadata["name"]))
    else:
        identity = ("reference", repr(target), _normalized_text(event.get("reason")))
    encoded = json.dumps(identity, ensure_ascii=False).encode("utf-8")
    return "event:" + hashlib.sha256(encoded).hexdigest()


def _warning_item(event: dict[str, Any], occurred_at: datetime) -> PulseItem:
    target = _event_target(event)
    series = _mapping(event.get("series"))
    return PulseItem(
        _event_key(event, target),
        "recent",
        target,
        event.get("reason", "Warning"),
        event.get("note") or event.get("message", ""),
        occurred_at,
        _count(series.get("count") or event.get("count") or event.get("deprecatedCount")),
        source="events",
    )


def _warning_clipped(event: dict[str, Any]) -> bool:
    reference = _event_reference(event)
    fields = [(reference.get(field), limit) for field, limit in _TARGET_LIMITS.items()]
    fields.extend(
        [
            (_group(reference.get("apiVersion")), 253),
            (event.get("reason"), _REASON_LIMIT),
            (event.get("note") or event.get("message"), _TEXT_LIMIT),
        ]
    )
    return any(len(_normalized_text(value)) > limit for value, limit in fields)


def _location(resource: dict[str, Any]) -> tuple[str | None, str | None, str, str]:
    metadata = _mapping(resource.get("metadata"))
    return (
        _text(_group(resource["apiVersion"]), 253) if resource.get("apiVersion") else None,
        _text(resource["kind"], 128) if resource.get("kind") else None,
        _text(metadata.get("namespace"), 63),
        _text(metadata.get("name"), 253),
    )


def _was_observed(
    target: PulseTarget,
    locations: set[tuple[str | None, str | None, str, str]],
) -> bool:
    return any(
        (group, kind, target.namespace, target.name) in locations
        for group in (target.group, None)
        for kind in (target.kind, None)
    )


def _clearable_location(resource: dict[str, Any]) -> bool:
    metadata = _mapping(resource.get("metadata"))
    if not isinstance(metadata.get("name"), str) or not metadata["name"]:
        return False
    if not isinstance(metadata.get("uid"), str) or not metadata["uid"]:
        return False
    fields = [
        (metadata[field], _TARGET_LIMITS[field])
        for field in ("name", "namespace", "uid")
        if field in metadata
    ]
    fields.extend(
        (resource[field], _TEXT_LIMIT) for field in ("apiVersion", "kind") if field in resource
    )
    return all(isinstance(value, str) and _text(value, limit) == value for value, limit in fields)


def _item_order(item: PulseItem) -> tuple[str, ...]:
    return (
        item.target.group,
        item.target.kind,
        item.target.namespace,
        item.target.name,
        item.target.uid or "",
        item.reason,
        item.key,
    )


def _item_bytes(item: PulseItem) -> int:
    return len(json.dumps(asdict(item), ensure_ascii=False, default=str).encode("utf-8"))


class PulseModel:
    """Keep current findings separate from bounded recent event evidence."""

    def __init__(
        self,
        rules: Sequence[PulseRule] = (),
        *,
        max_events: int = 100,
        max_bytes: int = 131072,
        retention: timedelta = timedelta(minutes=15),
        max_current_findings: int = 200,
        max_current_bytes: int = 262144,
    ) -> None:
        for name, value in (
            ("max_events", max_events),
            ("max_bytes", max_bytes),
            ("max_current_findings", max_current_findings),
            ("max_current_bytes", max_current_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        self._rules = tuple(rules)
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._retention = retention
        self._max_current_findings = max_current_findings
        self._max_current_bytes = max_current_bytes
        self.reset(epoch=0, scope=None)

    def reset(self, epoch: int, scope: str | None) -> None:
        """Start an empty epoch, with all registered sources loading."""
        self._epoch = epoch
        self._scope = _text(scope, 63) if scope is not None else None
        self._current: dict[str, tuple[PulseItem, ...]] = {}
        self._current_dropped: dict[str, int] = {}
        self._recent: dict[str, PulseItem] = {}
        self._recent_sizes: dict[str, int] = {}
        self._recent_versions: dict[str, tuple[datetime, int]] = {}
        self._event_bytes = 0
        self._coverage = {
            source: PulseCoverage(source, "loading", None)
            for source in {"events", *(_text(rule.source, 128) for rule in self._rules)}
        }
        self._successful_at: dict[str, datetime] = {}
        self._started_at: datetime | None = None
        self._latest_time: datetime | None = None
        self._dropped = 0
        self._clipped = False

    def replace_source(
        self,
        source: str,
        objects: Sequence[dict[str, Any]],
        coverage: PulseCoverage,
    ) -> None:
        """Reconcile observed identities; incomplete reads cannot imply absence."""
        source = _text(source, 128)
        if source != coverage.source:
            raise ValueError("coverage source must match the replaced source")
        previous_time = self._successful_at.get(source)
        if previous_time and coverage.observed_at and coverage.observed_at < previous_time:
            return
        observed_at = coverage.observed_at
        if coverage.state not in _FRESH_STATES or observed_at is None:
            self.set_coverage(coverage)
            return
        if source == "events":
            self.set_coverage(coverage)
            for event in objects:
                self.record_warning(event, self._epoch, observed_at)
            return
        rules = tuple(rule for rule in self._rules if _text(rule.source, 128) == source)
        scoped, clearable, incomplete = self._assess_current(objects, rules)
        if incomplete:
            coverage = replace(
                coverage,
                state="capped" if coverage.state == "capped" else "partial",
                detail=f"Assessment incomplete for {incomplete} resource observations; "
                f"unverified previous findings retained. {coverage.detail}",
            )
        fresh = tuple(
            replace(item, source=source)
            for rule in rules
            for item in rule.evaluate(scoped, observed_at)
            if self._in_scope(item.target.namespace)
        )
        self._replace_current(source, clearable, fresh, coverage)
        self.set_coverage(coverage)

    def _assess_current(
        self, objects: Sequence[dict[str, Any]], rules: tuple[PulseRule, ...]
    ) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], int]:
        scoped = []
        clearable = []
        incomplete = 0
        for resource in objects:
            metadata = _mapping(resource.get("metadata"))
            namespace = metadata.get("namespace")
            if (
                isinstance(namespace, str)
                and namespace == _text(namespace, 63)
                and not self._in_scope(namespace)
            ):
                continue
            if (
                not isinstance(metadata.get("name"), str)
                or not metadata["name"]
                or not self._in_scope(_text(namespace, 63))
            ):
                incomplete += 1
                continue
            scoped.append(resource)
            if _clearable_location(resource) and all(rule.can_clear(resource) for rule in rules):
                clearable.append(resource)
            else:
                incomplete += 1
        return tuple(scoped), tuple(clearable), incomplete

    def _replace_current(
        self,
        source: str,
        objects: Sequence[dict[str, Any]],
        fresh: tuple[PulseItem, ...],
        coverage: PulseCoverage,
    ) -> None:
        observed_at = coverage.observed_at
        can_reconcile = (
            observed_at is not None
            and self._advance_time(observed_at) - observed_at <= _STALE_AFTER
        )
        retained: dict[str, PulseItem] = {}
        if coverage.state != "complete" or not can_reconcile:
            locations = {_location(resource) for resource in objects} if can_reconcile else set()
            retained = {
                item.key: item
                for item in self._current.get(source, ())
                if not _was_observed(item.target, locations)
            }
        retained.update((item.key, item) for item in fresh)
        self._current[source] = self._retain_current(source, tuple(retained.values()), coverage)

    def _retain_current(
        self, source: str, items: tuple[PulseItem, ...], coverage: PulseCoverage
    ) -> tuple[PulseItem, ...]:
        newest = sorted(
            sorted(items, key=_item_order), key=lambda item: item.observed_at, reverse=True
        )
        retained: list[PulseItem] = []
        retained_bytes = 0
        for item in newest:
            encoded_bytes = _item_bytes(item)
            if (
                len(retained) < self._max_current_findings
                and retained_bytes + encoded_bytes <= self._max_current_bytes
            ):
                retained.append(item)
                retained_bytes += encoded_bytes
        discarded = len(items) - len(retained)
        if discarded:
            self._current_dropped[source] = self._current_dropped.get(source, 0) + discarded
            self.set_coverage(
                PulseCoverage(
                    f"current-buffer:{source}",
                    "capped",
                    coverage.observed_at,
                    f"Discarded {self._current_dropped[source]} current findings from {source} "
                    f"to enforce per-source retention limits ({self._max_current_findings} findings / "
                    f"{self._max_current_bytes} UTF-8 bytes). This is retention loss, not recovery.",
                )
            )
        return tuple(sorted(retained, key=_item_order))

    def record_warning(
        self,
        event: dict[str, Any],
        epoch: int,
        observed_at: datetime,
    ) -> None:
        """Merge a Warning by Event UID and monotonic event-time/count evidence."""
        if epoch != self._epoch or not self._in_scope(_event_namespace(event)):
            return
        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            self._mark_event_gap(
                self._advance_time(observed_at),
                "Warning omitted because its event type is missing or invalid.",
            )
            return
        if event_type != "Warning":
            return
        now = self._advance_time(observed_at)
        occurred_at = _event_time(event)
        if occurred_at is None:
            self._mark_event_gap(
                now,
                "Warning omitted because its event timestamp is missing, invalid, or timezone-naive.",
            )
            return
        if occurred_at > now:
            self._mark_event_gap(
                now, "Warning omitted because its event timestamp is in the future."
            )
            return
        if occurred_at < now - self._retention:
            return
        item = _warning_item(event, occurred_at)
        prior = self._recent_versions.get(item.key)
        if prior is not None and (
            item.observed_at < prior[0]
            or item.count < prior[1]
            or (item.observed_at, item.count) == prior
        ):
            return
        self._retain_warning(item, now, clipped=_warning_clipped(event))

    def _retain_warning(self, item: PulseItem, now: datetime, *, clipped: bool) -> None:
        encoded_bytes = _item_bytes(item)
        self._expire(now)
        if encoded_bytes > self._max_bytes:
            if item.key in self._recent:
                self._recent_versions[item.key] = (item.observed_at, item.count)
            self._dropped += 1
            self._buffer_coverage(now)
            return
        self._forget_warning(item.key)
        self._recent[item.key] = item
        self._recent_sizes[item.key] = encoded_bytes
        self._recent_versions[item.key] = (item.observed_at, item.count)
        self._event_bytes += encoded_bytes
        while len(self._recent) > self._max_events or self._event_bytes > self._max_bytes:
            oldest = min(
                self._recent.values(), key=lambda retained: (retained.observed_at, retained.key)
            )
            self._forget_warning(oldest.key)
            self._dropped += 1
        self._clipped = self._clipped or clipped
        if self._dropped or self._clipped:
            self._buffer_coverage(now)

    def _forget_warning(self, key: str) -> None:
        self._recent.pop(key, None)
        self._event_bytes -= self._recent_sizes.pop(key, 0)
        self._recent_versions.pop(key, None)

    def _expire(self, now: datetime) -> None:
        expired = [
            item.key for item in self._recent.values() if item.observed_at < now - self._retention
        ]
        for key in expired:
            self._forget_warning(key)

    def _buffer_coverage(self, now: datetime) -> None:
        detail = f"Dropped {self._dropped} Warning events to enforce buffer limits."
        if self._clipped:
            detail += " Warning text exceeded field limits and was truncated."
        self.set_coverage(PulseCoverage("event-buffer", "capped", now, detail))

    def _mark_event_gap(self, now: datetime, omission: str) -> None:
        previous = self._coverage["events"]
        state: PulseCoverageState = "partial"
        if previous.state in {"failed", "forbidden", "unavailable", "capped"}:
            state = previous.state
        detail = previous.detail
        if omission not in detail:
            detail = f"{detail} {omission}".strip()
        self.set_coverage(
            PulseCoverage(
                "events",
                state,
                previous.observed_at or now,
                detail,
            )
        )

    def set_coverage(self, coverage: PulseCoverage) -> None:
        """Record source coverage without modifying current findings or events."""
        if coverage.observed_at is not None:
            self._advance_time(coverage.observed_at)
        if coverage.state in _FRESH_STATES and coverage.observed_at is None:
            coverage = replace(
                coverage,
                state="partial",
                detail="Missing observation timestamp; previous findings remain unverified.",
            )
        self._coverage[coverage.source] = coverage
        if coverage.state in _FRESH_STATES and coverage.observed_at is not None:
            previous = self._successful_at.get(coverage.source)
            if previous is None or coverage.observed_at >= previous:
                self._successful_at[coverage.source] = coverage.observed_at

    def _in_scope(self, namespace: str) -> bool:
        return self._scope is None or namespace == self._scope

    def _advance_time(self, now: datetime) -> datetime:
        now = _aware(now)
        if self._started_at is None:
            self._started_at = now
        if self._latest_time is None or now > self._latest_time:
            self._latest_time = now
        return self._latest_time

    def _snapshot_coverage(self, source: str, now: datetime) -> PulseCoverage:
        coverage = self._coverage[source]
        last_success = self._successful_at.get(source) or self._started_at
        if (
            coverage.state != "stale"
            and last_success is not None
            and now - last_success > _STALE_AFTER
        ):
            return replace(
                coverage,
                state="stale",
                detail=f"Last observation: {coverage.state}. No fresh observation for 30 seconds. {coverage.detail}",
            )
        return coverage

    def snapshot(self, now: datetime) -> PulseSnapshot:
        """Return a deterministic view without exposing mutable containers."""
        now = self._advance_time(now)
        self._expire(now)
        return PulseSnapshot(
            self._epoch,
            self._scope,
            tuple(item for source in sorted(self._current) for item in self._current[source]),
            tuple(
                sorted(
                    self._recent.values(),
                    key=lambda item: (item.observed_at, item.key),
                    reverse=True,
                )
            ),
            tuple(self._snapshot_coverage(source, now) for source in sorted(self._coverage)),
            self._dropped,
            sum(self._current_dropped.values()),
        )
