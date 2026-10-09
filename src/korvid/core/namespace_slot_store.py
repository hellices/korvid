"""Per-cluster persistence for automatic namespace slots (issue #406).

The document lives in the XDG state directory, not in configuration: the
config loader rejects unknown top-level keys, and these numbers are learned
state rather than user-authored settings. Only automatic entries are saved.
Pins are always read back from `favorite_namespaces`.

A malformed document is never replaced. Loading it fails, and so does saving
over it, so a damaged file cannot turn into an empty map that destroys every
other cluster's record.
"""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from korvid.core.audit import interprocess_lock
from korvid.core.config import _atomic_write_text
from korvid.core.namespace_slots import SLOT_COUNT, SlotEntry, SlotOrigin

_VERSION = 1


class SlotStateError(Exception):
    """The saved namespace slot document is unreadable or malformed."""


@dataclasses.dataclass(frozen=True)
class ClusterIdentity:
    """Whose map this is: the resolved context name and its API server.

    The server is part of the identity so that a context name reused for a
    different cluster never inherits the old cluster's numbers.
    """

    context: str
    server: str


def default_slot_state_path() -> Path:
    """XDG state dir (falls back to ~/.local/state) / korvid/namespace-slots.json."""
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "korvid" / "namespace-slots.json"


def _malformed(detail: str) -> SlotStateError:
    return SlotStateError(f"Malformed namespace slot state: {detail}")


def _slot_number(key: object) -> int | None:
    if not isinstance(key, str) or not key.isdecimal():
        return None
    try:
        slot = int(key)
    except ValueError:  # more digits than int() converts
        return None
    return slot if 1 <= slot <= SLOT_COUNT else None


def _parse_slot(key: object, value: object) -> tuple[int, SlotEntry]:
    slot = _slot_number(key)
    if slot is None:
        raise _malformed(f"slot {str(key)[:20]!r} is not 1-{SLOT_COUNT}")
    if not isinstance(value, dict):
        raise _malformed(f"slot {key} is not a mapping")
    namespace, available = value.get("namespace"), value.get("available")
    if not isinstance(namespace, str) or not namespace:
        raise _malformed(f"slot {key} has no namespace")
    if not isinstance(available, bool):
        raise _malformed(f"slot {key} availability is not a boolean")
    return slot, SlotEntry(namespace, SlotOrigin.AUTO, available=available)


def _parse_record(record: object) -> tuple[ClusterIdentity, dict[int, SlotEntry]]:
    if not isinstance(record, dict):
        raise _malformed("a cluster record is not a mapping")
    context, server, slots = record.get("context"), record.get("server"), record.get("slots")
    if not isinstance(context, str) or not isinstance(server, str):
        raise _malformed("a cluster record has no context or server")
    if not isinstance(slots, dict):
        raise _malformed(f"slots for context {context!r} are not a mapping")
    return ClusterIdentity(context, server), dict(
        _parse_slot(key, value) for key, value in slots.items()
    )


def _encode_record(identity: ClusterIdentity, slots: Mapping[int, SlotEntry]) -> dict[str, Any]:
    return {
        "context": identity.context,
        "server": identity.server,
        "slots": {
            str(slot): {"namespace": entry.namespace, "available": entry.available}
            for slot, entry in sorted(slots.items())
            if entry.origin is SlotOrigin.AUTO
        },
    }


class NamespaceSlotStore:
    """Reads and atomically rewrites one cluster's record at a time."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        """The JSON state file this store reads and replaces."""
        return self._path

    def _read(self) -> list[tuple[ClusterIdentity, dict[int, SlotEntry]]]:
        try:
            content = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except UnicodeDecodeError as exc:
            raise _malformed("not valid UTF-8") from exc
        try:
            document = json.loads(content)
        except json.JSONDecodeError as exc:
            raise _malformed("not valid JSON") from exc
        version = document.get("version") if isinstance(document, dict) else None
        if type(version) is not int or version != _VERSION:  # True == 1.0 == 1
            raise _malformed(f"expected a version {_VERSION} document")
        clusters = document.get("clusters")
        if not isinstance(clusters, list):
            raise _malformed("clusters is not a list")
        return [_parse_record(record) for record in clusters]

    def load(self, identity: ClusterIdentity) -> dict[int, SlotEntry]:
        """The saved automatic entries for *identity*, empty when none exist.

        Raises:
            SlotStateError: The document is malformed.
            OSError: The document cannot be read.
        """
        for saved, slots in self._read():
            if saved == identity:
                return slots
        return {}

    def save(self, identity: ClusterIdentity, slots: Mapping[int, SlotEntry]) -> None:
        """Replace *identity*'s record, preserving every other cluster's.

        The latest document is re-read under the cross-process lock, so a
        concurrent korvid saving another cluster is never lost.

        Raises:
            SlotStateError: The current document is malformed; it is left as is.
            OSError: Reading or atomically replacing the document fails.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with interprocess_lock(self._path.with_name(f"{self._path.name}.lock")):
            records = [
                _encode_record(saved, entries)
                for saved, entries in self._read()
                if saved != identity
            ]
            records.append(_encode_record(identity, slots))
            document = {"version": _VERSION, "clusters": records}
            _atomic_write_text(self._path, json.dumps(document, indent=2) + "\n")
