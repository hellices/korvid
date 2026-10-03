"""Stable numeric namespace slots behind keys 1-9 (issue #406).

Pinned `favorite_namespaces` take the leading slots in configured order.
A namespace the user switches to takes the lowest free slot the first time,
and saved automatic slots keep their numbers. A complete, authorized listing
only judges availability: it never assigns a slot, and a namespace it
confirms missing keeps its slot as unavailable until an explicit
reallocation.

Everything here is pure data. Nothing selects a namespace, so a refresh can
never switch the active one.
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Iterator, Mapping, Sequence

#: Keys 1-9; `0` is the separate all-namespaces action.
SLOT_COUNT = 9
_SLOTS = range(1, SLOT_COUNT + 1)


class SlotOrigin(enum.StrEnum):
    """Who chose a slot's namespace."""

    PINNED = "pinned"
    AUTO = "auto"


@dataclasses.dataclass(frozen=True)
class SlotEntry:
    """One numbered slot's namespace and whether discovery still lists it."""

    namespace: str
    origin: SlotOrigin
    available: bool = True

    def describe(self) -> str:
        """`dev (pinned)` / `qa (auto, unavailable)` - one label for every surface."""
        state = "" if self.available else ", unavailable"
        return f"{self.namespace} ({self.origin.value}{state})"


@dataclasses.dataclass(frozen=True)
class SlotChange:
    """A slot whose entry a reallocation would change."""

    slot: int
    before: SlotEntry | None
    after: SlotEntry | None


@dataclasses.dataclass(frozen=True)
class SlotMap:
    """The effective 1-9 map that dispatch, help and the picker all read."""

    entries: Mapping[int, SlotEntry] = dataclasses.field(default_factory=dict)

    def get(self, slot: int) -> SlotEntry | None:
        """The entry behind key *slot*, or None for an empty or invalid slot."""
        return self.entries.get(slot)

    def items(self) -> Iterator[tuple[int, SlotEntry]]:
        """Occupied slots in ascending key order."""
        for slot in _SLOTS:
            entry = self.entries.get(slot)
            if entry is not None:
                yield slot, entry

    def automatic(self) -> dict[int, SlotEntry]:
        """The automatic entries - the only part that is ever persisted."""
        return {slot: entry for slot, entry in self.items() if entry.origin is SlotOrigin.AUTO}


def _pins(pinned: Sequence[str]) -> dict[int, SlotEntry]:
    return {
        slot: SlotEntry(namespace, SlotOrigin.PINNED)
        for slot, namespace in zip(_SLOTS, pinned, strict=False)
    }


def _pack(entries: dict[int, SlotEntry], namespaces: Sequence[str]) -> None:
    """Place *namespaces*, in order, into the free slots not yet named."""
    taken = {entry.namespace for entry in entries.values()}
    fresh = [name for name in dict.fromkeys(namespaces) if name not in taken]
    free = (slot for slot in _SLOTS if slot not in entries)
    for slot, namespace in zip(free, fresh, strict=False):
        entries[slot] = SlotEntry(namespace, SlotOrigin.AUTO)


def build(
    pinned: Sequence[str],
    saved: Mapping[int, SlotEntry],
    inventory: frozenset[str] | None,
) -> SlotMap:
    """Lay out the effective map from pins, saved slots and a listing.

    Args:
        pinned: Configured `favorite_namespaces`, in order.
        saved: Previously saved automatic entries by slot number.
        inventory: Names from a complete listing, or None when the listing
            failed, was denied or never ran. None never infers a deletion.

    Returns:
        The map: pins first, then saved slots in place. A listing marks
        entries available or not; it never adds one.
    """
    entries = _pins(pinned)
    taken = {entry.namespace for entry in entries.values()}
    for slot in sorted(saved):
        entry = saved[slot]
        if slot not in _SLOTS or slot in entries or entry.namespace in taken:
            continue
        available = entry.available if inventory is None else entry.namespace in inventory
        entries[slot] = SlotEntry(entry.namespace, SlotOrigin.AUTO, available=available)
        taken.add(entry.namespace)
    return SlotMap(entries)


def place(slots: SlotMap, namespace: str) -> SlotMap:
    """*slots* with a visited *namespace* in the lowest free slot.

    A namespace already in the map, pinned, automatic or unavailable, keeps
    its slot, and with no free slot the map is unchanged. Unavailable slots
    are never free: only `reallocate` reclaims them.
    """
    entries = dict(slots.items())
    _pack(entries, [namespace])
    return SlotMap(entries)


def reallocate(
    pinned: Sequence[str], saved: Mapping[int, SlotEntry], inventory: frozenset[str]
) -> SlotMap:
    """Repack the automatic slots against a complete listing, keeping pins.

    Saved namespaces that are still listed keep their relative order but move
    down into the lowest free slots; the rest are dropped, so unavailable
    slots are reclaimed. Only an explicit, confirmed user action may commit
    this.
    """
    entries = _pins(pinned)
    listed = [saved[slot].namespace for slot in sorted(saved) if slot in _SLOTS]
    _pack(entries, [name for name in listed if name in inventory])
    return SlotMap(entries)


def preview(current: SlotMap, proposed: SlotMap) -> list[SlotChange]:
    """The slots whose entry differs between *current* and *proposed*."""
    return [
        SlotChange(slot, current.get(slot), proposed.get(slot))
        for slot in _SLOTS
        if current.get(slot) != proposed.get(slot)
    ]
