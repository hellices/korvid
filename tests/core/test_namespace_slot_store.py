"""Automatic namespace slots persist per cluster without touching config (issue #406)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from korvid.core import config as config_module
from korvid.core.namespace_slot_store import (
    ClusterIdentity,
    NamespaceSlotStore,
    SlotStateError,
    default_slot_state_path,
)
from korvid.core.namespace_slots import SlotEntry, SlotOrigin

DEV = ClusterIdentity("dev", "https://dev.example:6443")
PROD = ClusterIdentity("prod", "https://prod.example:6443")


def _auto(namespace: str, *, available: bool = True) -> SlotEntry:
    return SlotEntry(namespace, SlotOrigin.AUTO, available=available)


def test_saved_slots_round_trip_for_the_same_identity(tmp_path: Path) -> None:
    store = NamespaceSlotStore(tmp_path / "slots.json")

    store.save(DEV, {2: _auto("prod"), 4: _auto("gone", available=False)})

    assert store.load(DEV) == {2: _auto("prod"), 4: _auto("gone", available=False)}


def test_a_missing_file_loads_an_empty_map(tmp_path: Path) -> None:
    assert NamespaceSlotStore(tmp_path / "absent" / "slots.json").load(DEV) == {}


def test_each_cluster_keeps_its_own_record(tmp_path: Path) -> None:
    store = NamespaceSlotStore(tmp_path / "slots.json")

    store.save(DEV, {2: _auto("dev-a")})
    store.save(PROD, {3: _auto("prod-a")})
    store.save(DEV, {5: _auto("dev-b")})

    assert store.load(DEV) == {5: _auto("dev-b")}
    assert store.load(PROD) == {3: _auto("prod-a")}


def test_a_reused_context_name_on_another_server_does_not_inherit_the_map(
    tmp_path: Path,
) -> None:
    store = NamespaceSlotStore(tmp_path / "slots.json")
    store.save(DEV, {2: _auto("dev-a")})

    assert store.load(ClusterIdentity("dev", "https://elsewhere.example:6443")) == {}


def test_pinned_entries_are_never_written(tmp_path: Path) -> None:
    path = tmp_path / "slots.json"

    NamespaceSlotStore(path).save(DEV, {1: SlotEntry("pin", SlotOrigin.PINNED), 2: _auto("a")})

    record = json.loads(path.read_text(encoding="utf-8"))["clusters"][0]
    assert record["slots"] == {"2": {"namespace": "a", "available": True}}


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        '{"version": 2, "clusters": []}',
        '{"version": 1, "clusters": {}}',
        '{"version": 1, "clusters": [{"context": "dev"}]}',
        '{"version": 1, "clusters": [{"context": "dev", "server": "s", "slots": []}]}',
        '{"version": 1, "clusters": [{"context": "dev", "server": "s",'
        ' "slots": {"x": {"namespace": "a", "available": true}}}]}',
        '{"version": 1, "clusters": [{"context": "dev", "server": "s",'
        ' "slots": {"12": {"namespace": "a", "available": true}}}]}',
        '{"version": 1, "clusters": [{"context": "dev", "server": "s", "slots": {"'
        + "1" * 5000
        + '": {"namespace": "a", "available": true}}}]}',
        '{"version": 1, "clusters": [{"context": "dev", "server": "s",'
        ' "slots": {"2": {"namespace": "", "available": true}}}]}',
        '{"version": 1, "clusters": [{"context": "dev", "server": "s",'
        ' "slots": {"2": {"namespace": "a", "available": "yes"}}}]}',
    ],
    ids=[
        "invalid-json",
        "not-a-mapping",
        "unknown-version",
        "clusters-not-a-list",
        "record-without-server",
        "slots-not-a-mapping",
        "non-numeric-slot",
        "out-of-range-slot",
        "slot-beyond-the-int-digit-limit",
        "empty-namespace",
        "non-boolean-availability",
    ],
)
def test_a_malformed_document_is_reported_and_never_overwritten(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "slots.json"
    path.write_text(content, encoding="utf-8")
    store = NamespaceSlotStore(path)

    with pytest.raises(SlotStateError, match="namespace slot state"):
        store.load(DEV)
    with pytest.raises(SlotStateError, match="namespace slot state"):
        store.save(DEV, {2: _auto("a")})
    assert path.read_text(encoding="utf-8") == content


def test_a_state_file_that_is_not_utf8_is_malformed_and_never_overwritten(
    tmp_path: Path,
) -> None:
    path = tmp_path / "slots.json"
    content = b'{"version": 1, "clusters": []}\xff\xfe'
    path.write_bytes(content)
    store = NamespaceSlotStore(path)

    with pytest.raises(SlotStateError, match="namespace slot state"):
        store.load(DEV)
    with pytest.raises(SlotStateError, match="namespace slot state"):
        store.save(DEV, {2: _auto("a")})
    assert path.read_bytes() == content


def test_a_failed_write_keeps_the_last_saved_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = NamespaceSlotStore(tmp_path / "slots.json")
    store.save(DEV, {2: _auto("kept")})

    def fail_replace(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_module, "os_replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        store.save(DEV, {2: _auto("lost")})

    assert store.load(DEV) == {2: _auto("kept")}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["slots.json", "slots.json.lock"]


def test_default_path_follows_the_xdg_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert default_slot_state_path() == tmp_path / "korvid" / "namespace-slots.json"


def test_default_path_falls_back_to_the_home_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    expected = tmp_path / ".local" / "state" / "korvid" / "namespace-slots.json"
    assert default_slot_state_path() == expected
