"""Persistence succeeds before the controller changes any live state."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

from korvid.core.config import ConfigError, load_config
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.app_bindings import APP_BINDINGS
from korvid.ui.keybinding_catalog import KeybindingCatalog
from korvid.ui.keybinding_controller import KeybindingController
from korvid.ui.keybinding_surface import KeybindingSurface
from korvid.ui.widgets.keybinding_editor import KeybindingEditorScreen

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


def _open_editor(controller: KeybindingController, ui: FakeUi) -> KeybindingEditorScreen:
    controller.open_editor()
    screen, _callback = ui.screens[-1]
    assert isinstance(screen, KeybindingEditorScreen)
    return screen


@pytest.mark.parametrize(
    ("section", "expected_raw", "expected_safe"),
    [
        ("{help: 1}", {"help": 1}, {}),
        ("{help: null}", {"help": None}, {}),
        ("{help: true}", {"help": True}, {}),
        ("{help: [f1]}", {"help": ["f1"]}, {}),
        ("{unknown_action: f1}", {"unknown_action": "f1"}, {}),
        ("{help: '1'}", {"help": "1"}, {}),
        ("{help: l}", {"help": "l"}, {}),
        ("[[help, 1]]", {"help": 1}, {}),
        ("{help: 1, logs: r}", {"help": 1, "logs": "r"}, {"logs": "r"}),
    ],
    ids=[
        "integer",
        "null",
        "boolean",
        "list-value",
        "unknown",
        "reserved",
        "collision",
        "pairs",
        "mixed",
    ],
)
async def test_rejected_persisted_entries_remain_resettable_after_load(
    tmp_path: Path,
    section: str,
    expected_raw: dict[str, object],
    expected_safe: dict[str, str],
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(f"keybindings: {section}\n", encoding="utf-8")
    original = path.read_bytes()
    raw = load_config(path).keybindings
    assert raw == expected_raw
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
    controller.load(raw)
    assert surface.current == expected_safe
    assert ui.messages()
    screen = _open_editor(controller, ui)
    assert screen.edit.overrides == expected_safe
    assert not screen.edit.dirty

    screen.edit.reset_all()

    assert screen.edit.dirty
    assert screen.edit.overrides == {}
    assert surface.current == expected_safe
    assert saved == []
    assert path.read_bytes() == original
    assert screen.edit.undo()
    assert not screen.edit.dirty
    assert screen.edit.overrides == expected_safe


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


@pytest.mark.parametrize("proposal", [{}, {"help": "f1"}], ids=["reset", "assignment"])
async def test_successful_save_clears_cleanup_for_reopened_sessions(
    proposal: dict[str, str],
) -> None:
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
    controller.load({"help": 1})
    screen = _open_editor(controller, ui)
    screen.edit.reset_all()
    assert screen.edit.dirty

    assert controller.apply(proposal) is None

    assert saved == [proposal]
    assert surface.current == proposal
    reopened = _open_editor(controller, ui)
    reopened.edit.reset_all()
    assert not reopened.edit.cleanup_pending
    assert reopened.edit.dirty == bool(proposal)


@pytest.mark.parametrize(
    "error",
    [
        OSError("read only"),
        ConfigError("bad YAML"),
        UnicodeError("bad encoding"),
        yaml.YAMLError("bad YAML"),
    ],
)
async def test_failed_save_retains_cleanup_for_reopened_sessions(error: Exception) -> None:
    surface = MemoryBindings()
    ui = FakeUi()

    def save(proposal: Mapping[str, str]) -> None:
        raise error

    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=surface,
        ui=ui,
        save=save,
        can_open=lambda: None,
    )
    controller.load({"help": 1})
    previous_keymap = dict(surface.keymap)
    screen = _open_editor(controller, ui)
    screen.edit.reset_all()
    assert screen.edit.dirty

    message = controller.apply(screen.edit.overrides)

    assert message is not None
    assert str(error) in message
    assert surface.current == {}
    assert surface.keymap == previous_keymap
    assert surface.installs == 1
    reopened = _open_editor(controller, ui)
    assert not reopened.edit.dirty
    reopened.edit.reset_all()
    assert reopened.edit.dirty
    assert reopened.edit.cleanup_pending


@pytest.mark.parametrize("raw", [{}, {"help": "f1"}, {"help": " f1 "}])
async def test_accepted_loads_do_not_require_cleanup(raw: dict[str, str]) -> None:
    ui = FakeUi()
    controller = KeybindingController(
        catalog=KeybindingCatalog(APP_BINDINGS),
        surface=MemoryBindings(),
        ui=ui,
        save=lambda proposal: None,
        can_open=lambda: None,
    )
    controller.load({"help": 1})
    controller.load(raw)

    screen = _open_editor(controller, ui)
    screen.edit.reset_all()

    assert not screen.edit.cleanup_pending
    assert screen.edit.dirty == bool(raw)


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
