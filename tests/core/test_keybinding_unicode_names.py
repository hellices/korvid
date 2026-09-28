"""Unicode names are usable only when they match the emitted key identity."""

from __future__ import annotations

import string

import pytest

from korvid.core.keybindings import canonical_key, plan_keybindings, shift_alias_keys

_ACTIONS = {"logs": ("f1",), "describe": ("f2",)}


@pytest.mark.parametrize("modifier", ["", "alt+", "ctrl+", "shift+", "shift+ctrl+"])
@pytest.mark.parametrize(
    "name",
    [
        "latin_small_letter_a",
        "latin_capital_letter_a",
        "latin_small_letter_e_with_acute",
        "greek_small_letter_alpha",
        "digit_one",
        "solidus",
        "reverse_solidus",
        "commercial_at",
        "hyphen_minus",
        "plus_sign",
        "low_line",
        "SNOWMAN",
        "black heart suit",
    ],
)
def test_inert_unicode_names_are_rejected(name: str, modifier: str) -> None:
    key = f"{modifier}{name}"
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert not plan.overrides
    assert len(plan.warnings) == 1
    assert "exactly one key supported by the terminal" in plan.warnings[0]
    assert key in plan.warnings[0]


def test_inert_unicode_name_cannot_coexist_with_its_character_override() -> None:
    plan = plan_keybindings({"logs": "a", "describe": "latin_small_letter_a"}, _ACTIONS)

    assert plan.overrides == {"logs": "a"}
    assert len(plan.warnings) == 1
    assert "'describe' must map to exactly one key" in plan.warnings[0]


@pytest.mark.parametrize(
    "key", [*string.punctuation, "a", "A", "1", "é", "É", "\u03b1", "한", "☃", "😀"]
)
def test_literal_punctuation_and_unicode_characters_remain_usable(key: str) -> None:
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert plan.overrides == {"logs": key}
    assert not plan.warnings


@pytest.mark.parametrize("modifier", ["", "alt+", "ctrl+", "shift+ctrl+"])
@pytest.mark.parametrize(
    ("character", "name"),
    [
        (",", "comma"),
        (".", "full_stop"),
        ("<", "less_than_sign"),
        ("☃", "snowman"),
        ("€", "euro_sign"),
        ("♥", "black_heart_suit"),
        ("😀", "grinning_face"),
    ],
)
def test_round_tripping_unicode_names_keep_physical_conflicts_and_emission(
    character: str, name: str, modifier: str
) -> None:
    named = f"{modifier}{name}"
    literal = f"{modifier}{character}"
    accepted = plan_keybindings({"logs": named}, _ACTIONS)
    duplicate = plan_keybindings({"logs": named, "describe": literal}, _ACTIONS)

    assert canonical_key(named) == canonical_key(literal)
    assert accepted.overrides == {"logs": named}
    assert not accepted.warnings
    assert duplicate.overrides == {"logs": named}
    assert len(duplicate.warnings) == 1
    assert "duplicate key" in duplicate.warnings[0]
    assert shift_alias_keys(named) == shift_alias_keys(literal)
