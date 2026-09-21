"""Persistence succeeds before the controller changes any live state."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from korvid.core.config import ConfigError
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.app_bindings import APP_BINDINGS
from korvid.ui.keybinding_catalog import KeybindingCatalog
from korvid.ui.keybinding_controller import KeybindingController
from korvid.ui.keybinding_surface import KeybindingSurface

from .test_write_coordinator import FakeUi


class MemoryBindings(KeybindingSurface):
    def __init__(self) -> None:
        self.current: dict[str, str] = {}
        self.keymap: dict[str, str] = {}
        self.installs = 0

    def overrides(self) -> dict[str, str]:
        return dict(self.current)

    def install(self, overrides: Mapping[str, str], keymap: Mapping[str, str]) -> None:
        self.current = dict(overrides)
        self.keymap = dict(keymap)
        self.installs += 1


def test_load_uses_contextual_rules_and_warns_without_persisting() -> None:
    surface = MemoryBindings()
    ui = FakeUi()
    saved: list[Mapping[str, str]] = []
    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=surface,
        ui=ui,
        save=saved.append,
        can_open=lambda: None,
    )
    controller.load({"logs": "r", "help": "1"})
    assert surface.current == {"logs": "r"}
    assert surface.keymap["rollout_restart"] == "r"
    assert saved == []
    assert any("reserved" in message for message in ui.messages())


def test_apply_persists_before_synchronously_installing_a_complete_map() -> None:
    surface = MemoryBindings()
    observed: list[tuple[dict[str, str], dict[str, str]]] = []

    def save(proposal: Mapping[str, str]) -> None:
        observed.append((dict(proposal), surface.overrides()))

    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=surface,
        ui=FakeUi(),
        save=save,
        can_open=lambda: None,
    )
    controller.load({"help": "f1"})
    assert controller.apply({"logs": "r"}) is None
    assert observed == [({"logs": "r"}, {"help": "f1"})]
    assert surface.current == {"logs": "r"}
    assert surface.keymap["help"] == "question_mark"
    assert surface.keymap["helm_rollback"] == "r"
    assert controller.apply({}) is None
    assert surface.current == {}
    assert surface.keymap["logs"] == "l"


@pytest.mark.parametrize(
    "error", [OSError("read only"), ConfigError("bad YAML"), UnicodeError("bad encoding")]
)
def test_save_failure_keeps_the_working_overrides_and_keymap(error: Exception) -> None:
    surface = MemoryBindings()

    def save(proposal: Mapping[str, str]) -> None:
        raise error

    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=surface,
        ui=FakeUi(),
        save=save,
        can_open=lambda: None,
    )
    controller.load({"help": "f1"})
    previous_keymap = dict(surface.keymap)
    message = controller.apply({"logs": "r"})
    assert message is not None
    assert str(error) in message
    assert surface.current == {"help": "f1"}
    assert surface.keymap == previous_keymap
    assert surface.installs == 1


def test_invalid_final_proposal_never_reaches_persistence() -> None:
    surface = MemoryBindings()
    saved: list[Mapping[str, str]] = []
    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=surface,
        ui=FakeUi(),
        save=saved.append,
        can_open=lambda: None,
    )
    assert controller.apply({"help": "l"}) is not None
    assert saved == []
    assert surface.installs == 0


def test_direct_open_refuses_a_protected_surface() -> None:
    ui = FakeUi()
    reason = UnavailableReason(AvailabilityCode.PROTECTED_UI, "Close the open dialog first")
    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=MemoryBindings(),
        ui=ui,
        save=lambda proposal: None,
        can_open=lambda: reason,
    )
    controller.open_editor()
    assert controller.unavailable_reason() == reason
    assert ui.screens == []
    assert any("Close the open dialog" in message for message in ui.messages())


def test_missing_persistence_has_an_actionable_refusal() -> None:
    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=MemoryBindings(),
        ui=FakeUi(),
        save=None,
        can_open=lambda: None,
    )
    assert controller.unavailable_reason() is not None
    assert controller.apply({"help": "f1"}) is not None
