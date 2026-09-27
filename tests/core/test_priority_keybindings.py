"""Global priority remaps cannot consume ordinary text-entry keystrokes."""

from __future__ import annotations

import pytest

from korvid.core.keybindings import plan_keybindings
from korvid.core.keymap_edit import KeymapEdit, KeymapRules

_ACTIONS = {"interrupt_agent": ("ctrl+x",), "help": ("f1",)}
_PRIORITY_ACTIONS = frozenset({"interrupt_agent"})


@pytest.mark.parametrize(
    "key",
    [
        "k",
        "K",
        "shift+k",
        "é",
        "한",
        "space",
        "shift+space",
        "at",
        "slash",
        "backslash",
        "plus",
        "minus",
        "underscore",
        "less_than_sign",
        "greater_than_sign",
        "exclamation_mark",
        "copyright_sign",
        "snowman",
        "?",
        "<",
        "[",
        "shift+slash",
    ],
)
def test_printable_priority_overrides_are_rejected_but_normal_actions_keep_them(key: str) -> None:
    normal = plan_keybindings({"help": key}, _ACTIONS, _PRIORITY_ACTIONS)
    priority = plan_keybindings({"interrupt_agent": key}, _ACTIONS, _PRIORITY_ACTIONS)

    assert normal.overrides == {"help": key}
    assert normal.warnings == ()
    assert priority.overrides == {}
    assert any("text input" in warning for warning in priority.warnings)


@pytest.mark.parametrize("key", ["ctrl+k", "alt+k", "alt+shift+k", "ctrl+plus", "f12", "shift+f12"])
def test_nontext_priority_overrides_remain_supported(key: str) -> None:
    plan = plan_keybindings({"interrupt_agent": key}, _ACTIONS, _PRIORITY_ACTIONS)

    assert plan.overrides == {"interrupt_agent": key}
    assert plan.warnings == ()


def test_editor_assignment_rejects_printable_priority_keys_without_changing_the_proposal() -> None:
    edit = KeymapEdit(KeymapRules(actions=_ACTIONS, priority_actions=_PRIORITY_ACTIONS), {})

    with pytest.raises(ValueError, match="text input"):
        edit.assign("interrupt_agent", "k")

    assert edit.overrides == {}
    assert not edit.dirty
