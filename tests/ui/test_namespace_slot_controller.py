"""The slot controller keeps one effective 1-9 map per cluster (issue #406)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from korvid.core.namespace_slot_store import ClusterIdentity, NamespaceSlotStore
from korvid.core.namespace_slots import SlotEntry, SlotOrigin
from korvid.k8s.errors import ApiStatusError
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.namespace_slot_controller import NamespaceSlotController, SlotPersistence
from korvid.ui.ui_surface import Severity, UiSurface
from korvid.ui.widgets.namespace_slots_screen import NamespaceSlotsScreen

DEV_SERVER = "https://dev.example:6443"


class FakeUi(UiSurface):
    """Records notifications, modals and workers."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.screens: list[Any] = []
        self.callbacks: list[Any] = []
        self.workers: list[Any] = []

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notifications.append((message, severity))

    def push_screen(self, screen: Any, callback: Any = None) -> Any:
        self.screens.append(screen)
        self.callbacks.append(callback)
        return None

    def run_worker(
        self,
        work: Any,
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Any:
        self.workers.append(work)
        return None

    async def drain(self) -> None:
        pending, self.workers = self.workers, []
        for work in pending:
            await work

    async def cancel_workers(self, group: str) -> None:  # pragma: no cover
        return None

    def suspend(self) -> contextlib.AbstractContextManager[None]:  # pragma: no cover
        return contextlib.nullcontext()

    def refresh(self) -> None:  # pragma: no cover
        return None

    def call_from_thread(  # pragma: no cover
        self, callback: Callable[..., Any], *args: Any
    ) -> None:
        callback(*args)

    def call_later(  # pragma: no cover
        self, callback: Callable[..., None], *args: Any
    ) -> None:
        callback(*args)

    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:  # pragma: no cover
        return contextlib.nullcontext()

    def is_current_screen(self, screen: Any) -> bool:  # pragma: no cover
        return True

    def screen_depth(self) -> int:  # pragma: no cover
        return 1

    def inline_focus_release_hint(self) -> str | None:  # pragma: no cover
        return None


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        *,
        pinned: Sequence[str] = (),
        names: Sequence[str] = (),
        persist: bool = True,
    ) -> None:
        self.ui = FakeUi()
        self.pinned = list(pinned)
        self.context: str | None = "dev"
        self.server = DEV_SERVER
        self.names = list(names)
        self.listing_error: Exception | None = None
        self.listing_available = True
        self.blocked: UnavailableReason | None = None
        #: When set, identity resolution reports `resolving` and waits for `resolved`.
        self.resolving: threading.Event | None = None
        self.resolved = threading.Event()
        #: Runs while a listing is in flight, before it returns.
        self.during_listing: Callable[[], None] | None = None
        self.path = tmp_path / "slots.json"
        self.store = NamespaceSlotStore(self.path)
        persistence = SlotPersistence(self.store, self._identity) if persist else None
        self.controller = NamespaceSlotController(
            ui=self.ui,
            pinned=lambda: self.pinned,
            context=lambda: self.context,
            list_namespaces=self._lister,
            persistence=persistence,
            can_open=lambda: self.blocked,
        )

    def _identity(self, context: str | None) -> tuple[str, str] | None:
        if self.resolving is not None:
            self.resolving.set()
            self.resolved.wait(timeout=5)
        return None if context is None else (context, self.server)

    def _lister(self) -> Callable[[], Awaitable[list[str]]] | None:
        if not self.listing_available:
            return None

        async def _list() -> list[str]:
            if self.during_listing is not None:
                self.during_listing()
            if self.listing_error is not None:
                raise self.listing_error
            return list(self.names)

        return _list

    def layout(self) -> dict[int, tuple[str, str, bool]]:
        return {
            slot: (entry.namespace, entry.origin.value, entry.available)
            for slot, entry in self.controller.slots.items()
        }

    def saved(self, context: str = "dev") -> dict[int, SlotEntry]:
        return self.store.load(ClusterIdentity(context, self.server))

    async def discover(self) -> None:
        token = self.controller.token()
        self.controller.observe(token, self.names)
        await self.ui.drain()

    async def visit(self, *namespaces: str) -> None:
        for namespace in namespaces:
            self.controller.visit(namespace)
        await self.ui.drain()


def _auto(namespace: str, *, available: bool = True) -> SlotEntry:
    return SlotEntry(namespace, SlotOrigin.AUTO, available=available)


async def test_before_activation_keys_follow_the_configured_pins(tmp_path: Path) -> None:
    harness = Harness(tmp_path, pinned=["prod"])

    assert harness.layout() == {1: ("prod", "pinned", True)}
    assert harness.controller.target(1) == "prod"
    assert harness.controller.target(2) is None


async def test_activation_restores_saved_slots_and_visits_fill_free_ones(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, pinned=["prod"], names=["prod", "dev", "alpha", "qa"])
    harness.store.save(ClusterIdentity("dev", DEV_SERVER), {4: _auto("qa")})

    await harness.controller.activate()
    assert harness.layout() == {1: ("prod", "pinned", True), 4: ("qa", "auto", True)}

    await harness.discover()
    assert harness.layout() == {1: ("prod", "pinned", True), 4: ("qa", "auto", True)}, (
        "a listing alone never assigns a slot"
    )
    await harness.visit("dev", "prod", "qa", "alpha")

    assert harness.layout() == {
        1: ("prod", "pinned", True),
        2: ("dev", "auto", True),
        3: ("alpha", "auto", True),
        4: ("qa", "auto", True),
    }
    assert harness.saved() == {2: _auto("dev"), 3: _auto("alpha"), 4: _auto("qa")}


async def test_a_visit_absent_from_the_last_complete_listing_is_not_assigned(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.visit("unlisted")
    assert harness.layout() == {1: ("unlisted", "auto", True)}, "unknown inventory trusts it"

    await harness.discover()
    await harness.visit("misspelled", "dev", "*", "")

    assert harness.layout() == {1: ("unlisted", "auto", False), 2: ("dev", "auto", True)}


async def test_a_visit_while_the_saved_map_loads_is_kept(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev", "qa"])
    harness.store.save(ClusterIdentity("dev", DEV_SERVER), {1: _auto("qa")})

    harness.resolving = threading.Event()

    activation = asyncio.create_task(harness.controller.activate())
    await asyncio.to_thread(harness.resolving.wait, 5)
    await harness.visit("dev")
    harness.resolved.set()
    await activation
    await harness.ui.drain()

    assert harness.layout() == {1: ("qa", "auto", True), 2: ("dev", "auto", True)}
    assert harness.saved() == {1: _auto("qa"), 2: _auto("dev")}


async def test_saves_run_off_the_event_loop_and_the_latest_map_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    await harness.controller.activate()
    writers: list[int] = []
    save = harness.store.save

    def recording(identity: ClusterIdentity, slots: Mapping[int, SlotEntry]) -> None:
        writers.append(threading.get_ident())
        save(identity, slots)

    monkeypatch.setattr(harness.store, "save", recording)
    harness.controller.visit("dev")
    harness.controller.visit("qa")

    assert writers == [], "a visit never writes the state file on the event loop"
    assert harness.controller.saving
    await harness.ui.drain()
    assert not harness.controller.saving
    assert threading.get_ident() not in writers
    assert harness.saved() == {1: _auto("dev"), 2: _auto("qa")}


async def test_a_refresh_with_an_unchanged_map_does_not_rewrite_the_file(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.visit("dev")
    await harness.discover()
    await harness.visit("dev")
    before = harness.path.stat().st_mtime_ns
    harness.path.write_text(harness.path.read_text(encoding="utf-8"), encoding="utf-8")
    marker = harness.path.stat().st_mtime_ns

    await harness.discover()

    assert before <= marker == harness.path.stat().st_mtime_ns


async def test_a_confirmed_missing_namespace_is_kept_unavailable_and_not_dispatched(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev", "prod"])
    await harness.controller.activate()
    await harness.visit("dev", "prod")
    harness.names = ["dev", "zeta"]

    await harness.discover()

    assert harness.layout() == {1: ("dev", "auto", True), 2: ("prod", "auto", False)}
    assert harness.controller.target(2) is None
    message, severity = harness.ui.notifications[-1]
    assert "prod" in message
    assert ":slots" in message
    assert severity == "warning"


async def test_failed_discovery_keeps_the_map_and_marks_it_stale(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.visit("dev")

    harness.controller.observe_failure(harness.controller.token())

    assert harness.layout() == {1: ("dev", "auto", True)}
    assert harness.controller.stale
    await harness.discover()
    assert not harness.controller.stale


async def test_a_failed_listing_forgets_the_last_inventory(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.discover()

    harness.controller.observe_failure(harness.controller.token())
    await harness.visit("created-since")

    assert harness.layout() == {1: ("created-since", "auto", True)}, (
        "without a current listing a visit is trusted again"
    )


class GatedSave:
    """Holds the first state-file write in its thread until `gate` is set;
    the writes numbered in *failing* (from 0) raise instead of writing."""

    def __init__(
        self,
        harness: Harness,
        monkeypatch: pytest.MonkeyPatch,
        *,
        failing: frozenset[int] = frozenset(),
    ) -> None:
        self.entered = threading.Event()
        self.gate = threading.Event()
        self._failing = failing
        self._calls = 0
        self._save = harness.store.save
        monkeypatch.setattr(harness.store, "save", self)

    def __call__(self, identity: ClusterIdentity, slots: Mapping[int, SlotEntry]) -> None:
        call, self._calls = self._calls, self._calls + 1
        if call == 0:
            self.entered.set()
            self.gate.wait(timeout=5)
        if call in self._failing:
            raise OSError("disk full")
        self._save(identity, slots)

    async def held(self) -> None:
        await asyncio.to_thread(self.entered.wait, 5)


async def test_switching_back_waits_for_this_clusters_save_and_its_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, names=["qa", "beta"])
    await harness.controller.activate()
    gated = GatedSave(harness, monkeypatch)
    harness.controller.visit("qa")
    flush = asyncio.create_task(harness.ui.drain())
    await gated.held()
    harness.controller.visit("beta")  # queued behind the write in flight

    harness.controller.deactivate()  # `:ctx` away and straight back
    activation = asyncio.create_task(harness.controller.activate())
    await asyncio.sleep(0)
    gated.gate.set()
    await activation
    await flush
    await harness.ui.drain()

    assert harness.layout() == {1: ("qa", "auto", True), 2: ("beta", "auto", True)}
    assert harness.saved() == {1: _auto("qa"), 2: _auto("beta")}


async def test_a_listing_during_activation_judges_the_restored_map(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    harness.store.save(ClusterIdentity("dev", DEV_SERVER), {1: _auto("gone")})
    harness.resolving = threading.Event()
    activation = asyncio.create_task(harness.controller.activate())
    await asyncio.to_thread(harness.resolving.wait, 5)
    await harness.visit("typo")  # before any listing, so it is trusted
    await harness.discover()  # the picker's listing lands mid-activation

    harness.resolved.set()
    await activation
    await harness.ui.drain()

    assert harness.layout() == {1: ("gone", "auto", False), 2: ("typo", "auto", False)}
    assert harness.controller.target(1) is None
    assert harness.saved() == {1: _auto("gone", available=False), 2: _auto("typo", available=False)}


async def test_a_failed_save_for_the_cluster_left_behind_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    await harness.controller.activate()
    gated = GatedSave(harness, monkeypatch, failing=frozenset({0}))
    harness.controller.visit("qa")
    flush = asyncio.create_task(harness.ui.drain())
    await gated.held()

    harness.controller.deactivate()  # `:ctx prod` while dev's map is written
    harness.context = "prod"
    gated.gate.set()
    await flush
    await harness.controller.activate()
    await harness.visit("api")

    assert [(m, s) for m, s in harness.ui.notifications if "'dev'" in m] == [
        ("Could not save namespace slots for context 'dev': disk full", "error")
    ]
    assert harness.saved("prod") == {1: _auto("api")}, "prod's saves are not suppressed"


async def test_shutdown_finishes_the_write_in_flight_then_writes_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path)
    await harness.controller.activate()
    gated = GatedSave(harness, monkeypatch)
    harness.controller.visit("dev")
    flush = asyncio.create_task(harness.ui.drain())
    await gated.held()
    harness.controller.visit("qa")

    flush.cancel()  # Textual cancels app workers before `on_unmount` runs
    with contextlib.suppress(asyncio.CancelledError):
        await flush
    gated.gate.set()
    await harness.controller.shutdown()

    assert harness.saved() == {1: _auto("dev"), 2: _auto("qa")}


async def test_a_result_from_before_a_context_switch_is_discarded(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["old-a", "old-b"])
    await harness.controller.activate()
    token = harness.controller.token()

    harness.controller.deactivate()
    harness.context = "prod"
    await harness.controller.activate()
    harness.controller.observe(token, harness.names)
    harness.controller.observe_failure(token)

    assert harness.layout() == {}
    assert not harness.controller.stale
    assert harness.saved("prod") == {}


async def test_a_visit_during_a_context_switch_is_not_saved_to_either_cluster(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.visit("dev")

    harness.controller.deactivate()
    await harness.visit("between")
    harness.context = "prod"
    await harness.controller.activate()

    assert harness.layout() == {}
    assert harness.saved("prod") == {}
    assert harness.saved("dev") == {1: _auto("dev")}


async def test_a_switch_shows_only_pins_until_the_new_cluster_is_restored(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, pinned=["shared"], names=["dev-only"])
    await harness.controller.activate()
    await harness.visit("dev-only")

    harness.controller.deactivate()

    assert harness.layout() == {1: ("shared", "pinned", True)}
    harness.context = "prod"
    await harness.controller.activate()
    assert harness.layout() == {1: ("shared", "pinned", True)}
    harness.context = "dev"
    harness.controller.deactivate()
    await harness.controller.activate()
    assert harness.layout() == {1: ("shared", "pinned", True), 2: ("dev-only", "auto", True)}


async def test_an_unresolved_identity_keeps_slots_in_memory_only(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    harness.context = None

    await harness.controller.activate()
    await harness.visit("dev")

    assert harness.layout() == {1: ("dev", "auto", True)}
    assert not harness.path.exists()


async def test_a_malformed_state_file_warns_once_and_is_never_overwritten(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    harness.path.write_text("{broken", encoding="utf-8")

    await harness.controller.activate()
    await harness.visit("dev")
    await harness.visit("qa")

    assert harness.layout() == {1: ("dev", "auto", True), 2: ("qa", "auto", True)}
    assert harness.path.read_text(encoding="utf-8") == "{broken"
    warnings = [
        message for message, severity in harness.ui.notifications if severity != "information"
    ]
    assert len(warnings) == 1
    assert "namespace slot state" in warnings[0]


async def test_a_failed_save_is_reported_once_and_keeps_the_last_saved_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.visit("dev")

    def fail_save(*_args: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(harness.store, "save", fail_save)
    await harness.visit("qa")
    await harness.visit("zeta")

    assert harness.layout()[3] == ("zeta", "auto", True)
    errors = [message for message, severity in harness.ui.notifications if severity == "error"]
    assert len(errors) == 1
    assert "read-only file system" in errors[0]
    assert json.loads(harness.path.read_text(encoding="utf-8"))["clusters"][0]["slots"] == {
        "1": {"namespace": "dev", "available": True}
    }


async def _open_reallocation(harness: Harness) -> NamespaceSlotsScreen:
    harness.controller.open_reallocation()
    await harness.ui.drain()
    screen = harness.ui.screens[-1]
    assert isinstance(screen, NamespaceSlotsScreen)
    return screen


async def _decide(harness: Harness, confirmed: bool) -> None:
    harness.ui.callbacks[-1](confirmed)
    await harness.ui.drain()


async def test_confirmed_reallocation_reclaims_unavailable_slots_after_saving(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, pinned=["prod"], names=["prod", "old", "dev"])
    await harness.controller.activate()
    await harness.visit("old", "dev")
    harness.names = ["prod", "dev", "new"]

    screen = await _open_reallocation(harness)

    assert [(c.slot, c.before, c.after) for c in screen.changes] == [
        (2, _auto("old"), _auto("dev")),
        (3, _auto("dev"), None),
    ]
    await _decide(harness, True)
    assert harness.layout() == {1: ("prod", "pinned", True), 2: ("dev", "auto", True)}
    assert harness.saved() == {2: _auto("dev")}


async def test_cancelled_reallocation_keeps_the_current_map(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev", "new"]
    layout, saved = harness.layout(), harness.saved()

    await _open_reallocation(harness)
    assert harness.layout() == layout, "opening the preview must not touch the map"
    assert harness.saved() == saved, "opening the preview must not touch the file"
    await _decide(harness, False)

    assert harness.layout() == layout == {1: ("dev", "auto", True), 2: ("old", "auto", True)}
    assert harness.saved() == saved


async def test_confirmed_reallocation_clears_a_stale_map(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.visit("gone", "dev")
    harness.controller.observe_failure(harness.controller.token())

    await _open_reallocation(harness)
    assert harness.controller.stale, "only a confirmed reallocation rewrites the map"
    await _decide(harness, True)

    assert not harness.controller.stale
    assert harness.layout() == {1: ("dev", "auto", True)}


async def test_a_visit_during_a_confirmed_save_takes_a_slot_of_the_new_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev", "new"]
    await _open_reallocation(harness)
    writing, release = threading.Event(), threading.Event()
    save = harness.store.save

    def held(identity: ClusterIdentity, slots: Mapping[int, SlotEntry]) -> None:
        writing.set()  # another korvid holds the file lock
        release.wait(timeout=5)
        save(identity, slots)

    monkeypatch.setattr(harness.store, "save", held)
    harness.ui.callbacks[-1](True)
    committing = asyncio.create_task(harness.ui.drain())
    await asyncio.to_thread(writing.wait, 5)
    harness.controller.visit("new")
    release.set()
    await committing
    await harness.ui.drain()

    assert harness.layout() == {1: ("dev", "auto", True), 2: ("new", "auto", True)}
    assert harness.saved() == {1: _auto("dev"), 2: _auto("new")}


async def _run_workers(harness: Harness) -> list[asyncio.Task[Any]]:
    """Start the queued workers side by side, as the app runs them."""
    pending, harness.ui.workers = harness.ui.workers, []
    return [asyncio.create_task(work) for work in pending]


async def test_visits_queued_behind_a_failed_reallocation_save_are_still_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev"]
    await _open_reallocation(harness)
    gated = GatedSave(harness, monkeypatch, failing=frozenset({1}))
    harness.controller.visit("qa")  # its save holds the lock...
    harness.ui.callbacks[-1](True)  # ...so the confirmed save waits for it
    tasks = await _run_workers(harness)
    await gated.held()
    harness.controller.visit("beta")  # queued while both wait

    gated.gate.set()
    await asyncio.gather(*tasks)
    await harness.ui.drain()

    assert any("Could not save" in m for m, _ in harness.ui.notifications)
    expected = {1: _auto("dev"), 2: _auto("old"), 3: _auto("qa"), 4: _auto("beta")}
    assert dict(harness.controller.slots.items()) == expected
    assert harness.saved() == expected


async def test_a_reallocation_confirmed_while_the_saved_map_loads_is_refused(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    harness.store.save(ClusterIdentity("dev", DEV_SERVER), {1: _auto("dev"), 3: _auto("qa")})
    harness.resolving = threading.Event()
    activation = asyncio.create_task(harness.controller.activate())
    await asyncio.to_thread(harness.resolving.wait, 5)
    await harness.visit("dev", "old")
    await _open_reallocation(harness)

    await _decide(harness, True)
    harness.resolved.set()
    await activation
    await harness.ui.drain()

    messages = [message for message, _ in harness.ui.notifications]
    assert not any("reallocated" in message for message in messages)
    assert any("still loading" in message for message in messages)
    assert harness.layout() == {
        1: ("dev", "auto", True),
        2: ("old", "auto", True),
        3: ("qa", "auto", True),
    }


async def test_a_switch_during_a_confirmed_save_never_lets_an_older_map_overwrite_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev", "qa"]
    await _open_reallocation(harness)
    gated = GatedSave(harness, monkeypatch)
    harness.ui.callbacks[-1](True)
    tasks = await _run_workers(harness)
    await gated.held()
    harness.controller.visit("qa")  # queued on the old map during the write

    harness.controller.deactivate()  # `:ctx` away before the write returns
    gated.gate.set()
    await asyncio.gather(*tasks)
    await harness.ui.drain()

    assert harness.saved() == {1: _auto("dev"), 2: _auto("qa")}


async def test_a_confirmed_reallocation_installs_its_listing_for_later_visits(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    await harness.discover()
    harness.names = ["dev", "new"]
    await _open_reallocation(harness)
    await _decide(harness, True)

    await harness.visit("old", "new")

    assert harness.layout() == {1: ("dev", "auto", True), 2: ("new", "auto", True)}


async def test_a_dialog_opened_during_the_listing_suppresses_the_preview(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev"]
    reason = UnavailableReason(AvailabilityCode.PROTECTED_UI, "Close the dialog first")
    harness.during_listing = lambda: setattr(harness, "blocked", reason)

    harness.controller.open_reallocation()
    await harness.ui.drain()

    assert harness.ui.screens == []
    assert harness.ui.notifications[-1][0] == "Close the dialog first"
    assert harness.layout() == {1: ("dev", "auto", True), 2: ("old", "auto", True)}


async def test_reallocation_save_failure_keeps_the_current_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev"]
    await _open_reallocation(harness)

    def fail_save(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.store, "save", fail_save)
    await _decide(harness, True)

    assert harness.layout() == {1: ("dev", "auto", True), 2: ("old", "auto", True)}
    message, severity = harness.ui.notifications[-1]
    assert "disk full" in message
    assert severity == "error"


async def test_a_reallocation_confirmed_after_a_context_switch_is_refused(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev"]
    await _open_reallocation(harness)

    harness.controller.deactivate()
    await _decide(harness, True)

    assert harness.saved() == {1: _auto("dev"), 2: _auto("old")}
    assert "context changed" in harness.ui.notifications[-1][0]


async def test_an_unchanged_reallocation_opens_no_modal(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev", "unvisited"])
    await harness.controller.activate()
    await harness.visit("dev")

    harness.controller.open_reallocation()
    await harness.ui.drain()

    assert harness.ui.screens == []
    assert "already" in harness.ui.notifications[-1][0]


async def test_in_memory_reallocation_does_not_claim_it_was_saved(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev", "old"], persist=False)
    await harness.controller.activate()
    await harness.visit("dev", "old")
    harness.names = ["dev"]
    await _open_reallocation(harness)

    await _decide(harness, True)

    assert harness.layout() == {1: ("dev", "auto", True)}
    assert "not saved" in harness.ui.notifications[-1][0]


async def test_a_denied_listing_refuses_reallocation_without_probing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    await harness.visit("dev")
    harness.listing_error = ApiStatusError(403, "Forbidden", "namespaces is forbidden")

    harness.controller.open_reallocation()
    await harness.ui.drain()

    assert harness.ui.screens == []
    assert harness.layout() == {1: ("dev", "auto", True)}
    assert harness.controller.stale
    message, severity = harness.ui.notifications[-1]
    assert ":ns <name>" in message
    assert severity == "error"


async def test_reallocation_is_refused_without_a_listing_or_over_protected_ui(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    harness.listing_available = False
    harness.controller.open_reallocation()
    harness.blocked = UnavailableReason(AvailabilityCode.PROTECTED_UI, "Close the dialog first")
    harness.controller.open_reallocation()

    assert harness.ui.workers == []
    assert [message for message, _ in harness.ui.notifications] == [
        "Namespace listing unavailable",
        "Close the dialog first",
    ]


def test_the_palette_probe_answers_like_the_command_without_listing(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    assert harness.controller.unavailable_reason() is None

    harness.listing_available = False
    unlisted = harness.controller.unavailable_reason()
    harness.blocked = UnavailableReason(AvailabilityCode.PROTECTED_UI, "Close the dialog first")

    assert unlisted is not None
    assert unlisted.message == "Namespace listing unavailable"
    assert harness.controller.unavailable_reason() == harness.blocked
    assert harness.ui.workers == []
    assert harness.ui.notifications == []
