"""Physical conflict identity is distinct from supported dispatch spellings."""

from __future__ import annotations

import pytest

from korvid.core.keybindings import canonical_key, plan_keybindings, shift_alias_keys
from korvid.core.keymap_edit import KeymapEdit, KeymapRules

_ACTIONS = {"logs": ("l",), "describe": ("d",)}
_NUL_KEYS = ("ctrl+space", "ctrl+@", "ctrl+at")


@pytest.mark.parametrize("key", _NUL_KEYS)
@pytest.mark.parametrize("alias", _NUL_KEYS)
def test_nul_spellings_share_explicit_default_and_reserved_conflicts(key: str, alias: str) -> None:
    assert canonical_key(key) == canonical_key(alias)
    duplicate = plan_keybindings({"logs": key, "describe": alias}, _ACTIONS)
    assert duplicate.overrides == {"logs": key}
    assert duplicate.warnings
    default = plan_keybindings({"logs": key}, {"logs": ("l",), "describe": (alias,)})
    assert not default.overrides
    assert default.warnings
    reserved = plan_keybindings({"logs": key}, _ACTIONS, reserved_keys={alias: "fixed"})
    assert not reserved.overrides
    assert reserved.warnings


@pytest.mark.parametrize("key", _NUL_KEYS)
def test_edit_session_reports_nul_alias_conflicts(key: str) -> None:
    edit = KeymapEdit(KeymapRules(_ACTIONS), {})
    edit.assign("logs", key)
    edit.assign("describe", "ctrl+@")
    assert len(edit.conflicts()) == 1
    assert edit.conflicts()[0].key == canonical_key("ctrl+space")


def test_unsupported_null_name_is_rejected_instead_of_installing_an_inert_key() -> None:
    plan = plan_keybindings({"logs": "null"}, _ACTIONS)
    assert not plan.overrides
    assert plan.warnings


@pytest.mark.parametrize(
    ("literal", "named"),
    [("alt+/", "alt+slash"), ("alt+<", "alt+less_than_sign"), ("ctrl+,", "ctrl+comma")],
)
def test_modified_punctuation_uses_the_same_identity(literal: str, named: str) -> None:
    assert canonical_key(literal) == canonical_key(named)
    plan = plan_keybindings({"logs": literal, "describe": named}, _ACTIONS)
    assert plan.overrides == {"logs": literal}
    assert plan.warnings
    assert shift_alias_keys(literal) == named


def test_one_literal_comma_is_a_key_not_a_key_list() -> None:
    plan = plan_keybindings({"logs": ","}, _ACTIONS)
    assert plan.overrides == {"logs": ","}
    assert not plan.warnings
    assert shift_alias_keys(",") == "comma"


@pytest.mark.parametrize(
    ("key", "spellings"),
    [
        ("ctrl+h", {"backspace", "ctrl+h"}),
        ("backspace", {"backspace", "ctrl+h"}),
        ("ctrl+space", set(_NUL_KEYS)),
        ("ctrl+@", set(_NUL_KEYS)),
        ("ctrl+at", set(_NUL_KEYS)),
        ("ctrl+m", {"enter", "ctrl+m"}),
        ("ctrl+i", {"tab", "ctrl+i"}),
    ],
)
def test_emission_keeps_the_requested_supported_spelling_first(
    key: str, spellings: set[str]
) -> None:
    emitted = shift_alias_keys(key).split(",")
    assert emitted[0] == key
    assert set(emitted) == spellings
