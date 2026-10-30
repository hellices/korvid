"""Recorded workload creation failures, and what their messages say (#411).

A failure is a `FailedCreate` event about the workload or one of its
children, matched by UID, or a `ReplicaFailure` condition whose reason is
`FailedCreate`. Its message is the API server's text, not an API field: it is
classified only by the fixed quota and LimitRanger phrasings below, and kept
verbatim, bounded, whatever it says. A quota is named only as the message
names it; a LimitRange is never named, because its messages do not say which.
A denial an admission webhook or a ValidatingAdmissionPolicy wrote is never
classified further: its text is the policy author's own, and can say
anything.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from korvid.k8s.events import select_event_timestamp

#: How many characters of a message are kept.
MESSAGE_LIMIT = 500
_FAILED_CREATE = "FailedCreate"
_PER = r"per (?:Container|Pod|PersistentVolumeClaim) is"


class RejectionKind(StrEnum):
    """What a rejection message says, by its fixed phrasing."""

    QUOTA_EXCEEDED = "quota exceeded"
    QUOTA_REQUIRES_FIELD = "quota requires a field"
    QUOTA_STATUS_UNKNOWN = "quota not yet computed"
    LIMIT_MINIMUM = "below a LimitRange minimum"
    LIMIT_MAXIMUM = "above a LimitRange maximum"
    LIMIT_RATIO = "above a LimitRange ratio"
    OTHER = "other rejection"


#: Each phrase, in the order a message is searched; quota phrases name a quota.
_PHRASES: tuple[tuple[RejectionKind, re.Pattern[str]], ...] = (
    (RejectionKind.QUOTA_EXCEEDED, re.compile(r"exceeded quota: ([^,\s]+),")),
    (RejectionKind.QUOTA_REQUIRES_FIELD, re.compile(r"failed quota: ([^:\s]+): must specify")),
    (RejectionKind.QUOTA_STATUS_UNKNOWN, re.compile(r"status unknown for quota: ([^,\s]+)")),
    (RejectionKind.LIMIT_MINIMUM, re.compile(rf"minimum \S+ usage {_PER}")),
    (RejectionKind.LIMIT_MAXIMUM, re.compile(rf"maximum \S+ usage {_PER}")),
    # Anchored at a word's start, so a long unbroken message is scanned in linear time.
    (RejectionKind.LIMIT_RATIO, re.compile(rf"(?<!\S)\S+ max limit to request ratio {_PER}")),
)
_AUTHORED_DENIAL = re.compile(
    r'admission webhook "[^"]*" denied the request'
    r"|ValidatingAdmissionPolicy '[^']*' with binding '[^']*' denied request"
)


class FailureSource(StrEnum):
    """Where a failure was recorded."""

    EVENT = "event"
    CONDITION = "condition"


@dataclass(frozen=True, slots=True)
class Subject:
    """An object a failure can be recorded on: the workload or one of its children."""

    kind: str
    name: str
    uid: str


@dataclass(frozen=True, slots=True)
class Rejection:
    """A message's classification, the quotas it names, and the message, bounded."""

    kinds: tuple[RejectionKind, ...]
    quotas: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class Failure:
    """One recorded creation failure: about what, from where, how often, and when last."""

    subject: Subject
    source: FailureSource
    rejection: Rejection
    count: int
    at: datetime | None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _bounded(message: str) -> str:
    if len(message) <= MESSAGE_LIMIT:
        return message
    return message[: MESSAGE_LIMIT - 1] + "…"


def classify(message: str) -> Rejection:
    """Classify `message` by its fixed phrasings, in the order it states them.

    A message that states none of them, or that an admission webhook or a
    ValidatingAdmissionPolicy wrote, is `OTHER`.
    """
    if _AUTHORED_DENIAL.search(message):
        return Rejection((RejectionKind.OTHER,), (), _bounded(message))
    matches = sorted(
        (match.start(), kind, match.group(1) if match.groups() else None)
        for kind, pattern in _PHRASES
        for match in pattern.finditer(message)
    )
    kinds = dict.fromkeys(kind for _, kind, _ in matches)
    quotas = dict.fromkeys(quota for _, _, quota in matches if quota is not None)
    return Rejection(tuple(kinds) or (RejectionKind.OTHER,), tuple(quotas), _bounded(message))


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _event_count(event: Mapping[str, Any]) -> int:
    for value in (
        _mapping(event.get("series")).get("count"),
        event.get("deprecatedCount"),
        event.get("count"),
    ):
        count = _count(value)
        if count is not None:
            return count
    return 1


def _event_subject(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(event.get("regarding")) or _mapping(event.get("involvedObject"))


def _newest_first(failure: Failure) -> tuple[bool, float]:
    return (failure.at is None, -failure.at.timestamp() if failure.at is not None else 0.0)


def event_failures(
    events: Iterable[object], subjects: Sequence[Subject]
) -> tuple[tuple[Failure, ...], int]:
    """The `FailedCreate` events about `subjects`, newest first, and how many are not.

    An event is about a subject only when its UID matches. One whose kind and
    name match a subject but whose UID is another is about an earlier
    incarnation: it is counted, not returned. One without a UID establishes
    neither, and is ignored.
    """
    by_uid = {subject.uid: subject for subject in subjects}
    names = {(subject.kind, subject.name) for subject in subjects}
    failures: list[Failure] = []
    earlier = 0
    for event in events:
        if not isinstance(event, Mapping) or event.get("reason") != _FAILED_CREATE:
            continue
        about = _event_subject(event)
        uid = _text(about.get("uid"))
        subject = by_uid.get(uid)
        if subject is None:
            earlier += bool(uid) and (_text(about.get("kind")), _text(about.get("name"))) in names
            continue
        message = _text(event.get("note")) or _text(event.get("message"))
        at = _timestamp(select_event_timestamp(dict(event)))
        failures.append(
            Failure(subject, FailureSource.EVENT, classify(message), _event_count(event), at)
        )
    return tuple(sorted(failures, key=_newest_first)), earlier


def condition_failure(subject: Subject, status: Mapping[str, Any]) -> Failure | None:
    """The subject's `ReplicaFailure` condition, if it records a `FailedCreate`.

    The ReplicaSet controller also sets it with `FailedDelete`, which is not
    a creation failure.
    """
    conditions = status.get("conditions")
    for raw in conditions if isinstance(conditions, list) else []:
        condition = _mapping(raw)
        if (
            condition.get("type") == "ReplicaFailure"
            and condition.get("status") == "True"
            and condition.get("reason") == _FAILED_CREATE
        ):
            at = _timestamp(condition.get("lastUpdateTime")) or _timestamp(
                condition.get("lastTransitionTime")
            )
            rejection = classify(_text(condition.get("message")))
            return Failure(subject, FailureSource.CONDITION, rejection, 1, at)
    return None
