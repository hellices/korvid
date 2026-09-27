"""Shift-only text keys must use the terminal's emitted character spelling."""

from __future__ import annotations

import pytest

from korvid.core.keybindings import plan_keybindings
from korvid.core.keymap_edit import KeymapEdit, KeymapRules

_ACTIONS = {"logs": ("f1",), "describe": ("f2",), "interrupt_agent": ("ctrl+x",)}
_PRIORITY_ACTIONS = frozenset({"interrupt_agent"})
_UNSUPPORTED = [
    "shift+slash",
    "shift+/",
    "shift+question_mark",
    "shift+?",
    "shift+1",
    "shift+comma",
    "shift+less_than_sign",
    "shift+space",
    "shift+é",
    "shift+한",
    "shift+☃",
    "shift+snowman",
    "shift+grinning_face",
]


@pytest.mark.parametrize("key", _UNSUPPORTED)
@pytest.mark.parametrize("action", ["logs", "interrupt_agent"])
def test_startup_rejects_shift_only_text_without_shadowing_the_emitted_key(
    key: str, action: str
) -> None:
    plan = plan_keybindings({action: key, "describe": "?"}, _ACTIONS, _PRIORITY_ACTIONS)

    assert plan.overrides == {"describe": "?"}
    assert len(plan.warnings) == 1
    assert "supported by the terminal" in plan.warnings[0]


@pytest.mark.parametrize("key", _UNSUPPORTED)
def test_editor_rejects_shift_only_text_without_changing_the_proposal(key: str) -> None:
    edit = KeymapEdit(KeymapRules(actions=_ACTIONS), {})

    with pytest.raises(ValueError, match="supported by the terminal"):
        edit.assign("logs", key)

    assert edit.overrides == {}
    assert not edit.dirty
    edit.assign("logs", "?")
    assert edit.overrides == {"logs": "?"}


@pytest.mark.parametrize(
    "key",
    [
        "A",
        "shift+a",
        "shift+A",
        "É",
        "shift+tab",
        "backtab",
        "shift+f5",
        "shift+left",
        "ctrl+shift+slash",
        "alt+shift+slash",
        "ctrl+shift+snowman",
        "ctrl+shift+é",
        "alt+shift+é",
    ],
)
def test_shift_letter_aliases_nontext_keys_and_control_modifiers_remain_supported(
    key: str,
) -> None:
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert plan.overrides == {"logs": key}
    assert plan.warnings == ()
