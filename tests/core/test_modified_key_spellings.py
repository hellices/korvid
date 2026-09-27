"""Modified uppercase spellings must not silently save unusable shortcuts."""

from __future__ import annotations

import pytest

from korvid.core.keybindings import plan_keybindings
from korvid.core.keymap_edit import KeymapEdit, KeymapRules

_ACTIONS = {"logs": ("f1",), "describe": ("f2",)}
_SPELLINGS = [
    ("alt+A", "alt+shift+a"),
    ("alt+É", "alt+shift+é"),
    ("ctrl+A", "ctrl+shift+a"),
    ("ctrl+shift+A", "ctrl+shift+a"),
    ("alt+ctrl+A", "alt+ctrl+shift+a"),
    ("alt+ctrl+shift+Z", "alt+ctrl+shift+z"),
    ("super+Z", "shift+super+z"),
    ("meta+Ω", "meta+shift+ω"),
    ("shift+É", "É"),
]


@pytest.mark.parametrize(("unsupported", "supported"), _SPELLINGS)
def test_startup_rejects_modified_uppercase_without_shadowing_the_supported_spelling(
    unsupported: str, supported: str
) -> None:
    plan = plan_keybindings({"logs": unsupported, "describe": supported}, _ACTIONS)

    assert plan.overrides == {"describe": supported}
    assert len(plan.warnings) == 1
    assert "supported by the terminal" in plan.warnings[0]


@pytest.mark.parametrize(("unsupported", "supported"), _SPELLINGS)
def test_editor_rejects_modified_uppercase_but_accepts_the_terminal_spelling(
    unsupported: str, supported: str
) -> None:
    edit = KeymapEdit(KeymapRules(actions=_ACTIONS), {})

    with pytest.raises(ValueError, match="supported by the terminal"):
        edit.assign("logs", unsupported)

    assert edit.overrides == {}
    assert not edit.dirty
    edit.assign("logs", supported)
    assert edit.overrides == {"logs": supported}


@pytest.mark.parametrize("key", ["A", "shift+a", "shift+A", "É"])
def test_plain_uppercase_and_existing_ascii_shift_aliases_remain_supported(key: str) -> None:
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert plan.overrides == {"logs": key}
    assert not plan.warnings
