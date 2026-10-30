"""When a ConfigMap or Secret is known to have changed, relative to a container's start (#410 P2).

korvid sees a change to an object only as two sightings of it with different
resource versions. A sighting is not an instant: the server answered at some
point between the request being sent and the response arriving. So a change
is bounded by the old sighting's send time and the new sighting's arrival,
and a delayed old-version response cannot push the bound past the change.

Resource versions are compared only for equality, never ordered, and
`creationTimestamp` is never used as a change time. A container's start
comes from the node's clock and the sightings from korvid's, so the two are
ordered only with a fixed allowance for skew between them.

Only the changes korvid saw are known: one between two sightings in this
session, of the same incarnation, under the same context and credentials.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

#: How far the node's clock and korvid's may disagree.
SKEW = timedelta(minutes=1)
#: How many objects a `ChangeLog` remembers.
DEFAULT_CAPACITY = 256


@dataclass(frozen=True, slots=True)
class Sighting:
    """One read of an object's resource version: sent at `sent_at`, back by `arrived_at`.

    Both times are korvid's. `sent_at` may be earlier than the request was
    really sent, and `arrived_at` later than the response really arrived: a
    wider sighting only widens the change it bounds.
    """

    resource_version: str
    sent_at: datetime
    arrived_at: datetime

    def __post_init__(self) -> None:
        if not self.resource_version:
            raise ValueError("a sighting has a resource version")
        if self.sent_at.tzinfo is None or self.arrived_at.tzinfo is None:
            raise ValueError("sighting times are timezone-aware")
        if self.arrived_at < self.sent_at:
            raise ValueError("a sighting arrives after it is sent")


@dataclass(frozen=True, slots=True)
class ObservedChange:
    """A change known to have happened after `after` and before `before`."""

    after: datetime
    before: datetime


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """One incarnation of an object, under one context epoch and session generation."""

    epoch: int
    session_generation: int
    kind: str
    namespace: str
    name: str
    uid: str


class Ordering(Enum):
    """Where a change falls relative to a container's start."""

    AFTER_START = "after"
    BEFORE_START = "before"
    NOT_ESTABLISHED = "not-established"


def order(change: ObservedChange, started_at: datetime | None) -> Ordering:
    """Whether `change` is known to follow or precede a start at `started_at`.

    The whole bound must clear the start by `SKEW`; a bound that straddles it,
    or no start at all, establishes neither.
    """
    if started_at is None:
        return Ordering.NOT_ESTABLISHED
    if change.after >= started_at + SKEW:
        return Ordering.AFTER_START
    if change.before <= started_at - SKEW:
        return Ordering.BEFORE_START
    return Ordering.NOT_ESTABLISHED


@dataclass(frozen=True, slots=True)
class _Entry:
    last: Sighting
    change: ObservedChange | None


class ChangeLog:
    """The last sighting and the latest change of each recently read object.

    It is bounded: past `capacity` objects, the least recently recorded is
    forgotten, and its next sighting starts afresh.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("a change log's capacity is at least one")
        self._capacity = capacity
        self._entries: OrderedDict[ObjectKey, _Entry] = OrderedDict()

    def record(self, key: ObjectKey, sighting: Sighting) -> ObservedChange | None:
        """Record `sighting` of `key`, and return the latest change known for it.

        A sighting sent before the last one recorded is ignored: it can only
        be older news.
        """
        entry = self._entries.pop(key, None)
        if entry is None:
            entry = _Entry(sighting, None)
        elif sighting.sent_at < entry.last.sent_at:
            pass
        elif sighting.resource_version == entry.last.resource_version:
            entry = _Entry(sighting, entry.change)
        else:
            entry = _Entry(sighting, ObservedChange(entry.last.sent_at, sighting.arrived_at))
        self._entries[key] = entry
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return entry.change
