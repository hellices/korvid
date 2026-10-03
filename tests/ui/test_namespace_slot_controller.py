"""The slot controller keeps one effective 1-9 map per cluster (issue #406)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable, Sequence
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
        return None if context is None else (context, self.server)

    def _lister(self) -> Callable[[], Awaitable[list[str]]] | None:
        if not self.listing_available:
            return None

        async def _list() -> list[str]:
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

    def discover(self) -> None:
        token = self.controller.token()
        self.controller.observe(token, self.names)

    def visit(self, *namespaces: str) -> None:
        for namespace in namespaces:
            self.controller.visit(namespace)


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

    harness.discover()
    assert harness.layout() == {1: ("prod", "pinned", True), 4: ("qa", "auto", True)}, (
        "a listing alone never assigns a slot"
    )
    harness.visit("dev", "prod", "qa", "alpha")

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
    harness.visit("unlisted")
    assert harness.layout() == {1: ("unlisted", "auto", True)}, "unknown inventory trusts it"

    harness.discover()
    harness.visit("misspelled", "dev", "*", "")

    assert harness.layout() == {1: ("unlisted", "auto", False), 2: ("dev", "auto", True)}


async def test_a_refresh_with_an_unchanged_map_does_not_rewrite_the_file(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    harness.visit("dev")
    harness.discover()
    harness.visit("dev")
    before = harness.path.stat().st_mtime_ns
    harness.path.write_text(harness.path.read_text(encoding="utf-8"), encoding="utf-8")
    marker = harness.path.stat().st_mtime_ns

    harness.discover()

    assert before <= marker == harness.path.stat().st_mtime_ns


async def test_a_confirmed_missing_namespace_is_kept_unavailable_and_not_dispatched(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev", "prod"])
    await harness.controller.activate()
    harness.visit("dev", "prod")
    harness.names = ["dev", "zeta"]

    harness.discover()

    assert harness.layout() == {1: ("dev", "auto", True), 2: ("prod", "auto", False)}
    assert harness.controller.target(2) is None
    message, severity = harness.ui.notifications[-1]
    assert "prod" in message
    assert ":slots" in message
    assert severity == "warning"


async def test_failed_discovery_keeps_the_map_and_marks_it_stale(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    harness.visit("dev")

    harness.controller.observe_failure(harness.controller.token())

    assert harness.layout() == {1: ("dev", "auto", True)}
    assert harness.controller.stale
    harness.discover()
    assert not harness.controller.stale


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
    harness.visit("dev")

    harness.controller.deactivate()
    harness.visit("between")
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
    harness.visit("dev-only")

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
    harness.visit("dev")

    assert harness.layout() == {1: ("dev", "auto", True)}
    assert not harness.path.exists()


async def test_a_malformed_state_file_warns_once_and_is_never_overwritten(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev"])
    harness.path.write_text("{broken", encoding="utf-8")

    await harness.controller.activate()
    harness.visit("dev")
    harness.visit("qa")

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
    harness.visit("dev")

    def fail_save(*_args: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(harness.store, "save", fail_save)
    harness.visit("qa")
    harness.visit("zeta")

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


async def test_confirmed_reallocation_reclaims_unavailable_slots_after_saving(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, pinned=["prod"], names=["prod", "old", "dev"])
    await harness.controller.activate()
    harness.visit("old", "dev")
    harness.names = ["prod", "dev", "new"]

    screen = await _open_reallocation(harness)

    assert [(c.slot, c.before, c.after) for c in screen.changes] == [
        (2, _auto("old"), _auto("dev")),
        (3, _auto("dev"), None),
    ]
    harness.ui.callbacks[-1](True)
    assert harness.layout() == {1: ("prod", "pinned", True), 2: ("dev", "auto", True)}
    assert harness.saved() == {2: _auto("dev")}


async def test_cancelled_reallocation_keeps_the_current_map(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    harness.visit("dev", "old")
    harness.names = ["dev", "new"]
    layout, saved = harness.layout(), harness.saved()

    await _open_reallocation(harness)
    assert harness.layout() == layout, "opening the preview must not touch the map"
    assert harness.saved() == saved, "opening the preview must not touch the file"
    harness.ui.callbacks[-1](False)

    assert harness.layout() == layout == {1: ("dev", "auto", True), 2: ("old", "auto", True)}
    assert harness.saved() == saved


async def test_confirmed_reallocation_clears_a_stale_map(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    harness.visit("gone", "dev")
    harness.controller.observe_failure(harness.controller.token())

    await _open_reallocation(harness)
    assert harness.controller.stale, "only a confirmed reallocation rewrites the map"
    harness.ui.callbacks[-1](True)

    assert not harness.controller.stale
    assert harness.layout() == {1: ("dev", "auto", True)}


async def test_reallocation_save_failure_keeps_the_current_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    harness.visit("dev", "old")
    harness.names = ["dev"]
    await _open_reallocation(harness)

    def fail_save(*_args: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(harness.store, "save", fail_save)
    harness.ui.callbacks[-1](True)

    assert harness.layout() == {1: ("dev", "auto", True), 2: ("old", "auto", True)}
    message, severity = harness.ui.notifications[-1]
    assert "disk full" in message
    assert severity == "error"


async def test_a_reallocation_confirmed_after_a_context_switch_is_refused(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path, names=["dev", "old"])
    await harness.controller.activate()
    harness.visit("dev", "old")
    harness.names = ["dev"]
    await _open_reallocation(harness)

    harness.controller.deactivate()
    harness.ui.callbacks[-1](True)

    assert harness.saved() == {1: _auto("dev"), 2: _auto("old")}
    assert "context changed" in harness.ui.notifications[-1][0]


async def test_an_unchanged_reallocation_opens_no_modal(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev", "unvisited"])
    await harness.controller.activate()
    harness.visit("dev")

    harness.controller.open_reallocation()
    await harness.ui.drain()

    assert harness.ui.screens == []
    assert "already" in harness.ui.notifications[-1][0]


async def test_in_memory_reallocation_does_not_claim_it_was_saved(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev", "old"], persist=False)
    await harness.controller.activate()
    harness.visit("dev", "old")
    harness.names = ["dev"]
    await _open_reallocation(harness)

    harness.ui.callbacks[-1](True)

    assert harness.layout() == {1: ("dev", "auto", True)}
    assert "not saved" in harness.ui.notifications[-1][0]


async def test_a_denied_listing_refuses_reallocation_without_probing(tmp_path: Path) -> None:
    harness = Harness(tmp_path, names=["dev"])
    await harness.controller.activate()
    harness.visit("dev")
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
