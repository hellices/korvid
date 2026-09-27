"""Validate key spellings against Textual's actual terminal events and bindings."""

from __future__ import annotations

import pytest
from textual._xterm_parser import XTermParser
from textual.binding import BindingsMap
from textual.events import Key

from korvid.core.keybindings import canonical_key, plan_keybindings, shift_alias_keys

_ACTIONS = {"logs": ("f1",), "describe": ("f2",)}


@pytest.mark.parametrize(
    ("character", "name"),
    [
        ("a", "latin_small_letter_a"),
        ("A", "latin_capital_letter_a"),
        ("é", "latin_small_letter_e_with_acute"),
        ("\u03b1", "greek_small_letter_alpha"),
        ("1", "digit_one"),
        ("/", "solidus"),
        ("@", "commercial_at"),
        ("_", "low_line"),
    ],
)
def test_unicode_names_without_terminal_events_are_rejected(character: str, name: str) -> None:
    events = [event for event in XTermParser().feed(character) if isinstance(event, Key)]
    bindings = BindingsMap([(shift_alias_keys(name), "describe")])

    assert len(events) == 1
    assert events[0].key == canonical_key(character)
    assert events[0].key not in bindings.key_to_bindings

    plan = plan_keybindings({"logs": character, "describe": name}, _ACTIONS)

    assert plan.overrides == {"logs": character}
    assert len(plan.warnings) == 1


@pytest.mark.parametrize(
    ("key", "sequence"),
    [
        ("a", "a"),
        ("A", "A"),
        ("shift+a", "A"),
        ("shift+A", "A"),
        ("É", "É"),
        ("é", "é"),
        ("\u03b1", "\u03b1"),
        ("한", "한"),
        ("/", "/"),
        ("slash", "/"),
        (",", ","),
        ("comma", ","),
        ("<", "<"),
        ("less_than_sign", "<"),
        ("☃", "☃"),
        ("snowman", "☃"),
        ("😀", "😀"),
        ("grinning_face", "😀"),
        ("ctrl+a", "\x01"),
        ("ctrl+space", "\x00"),
        ("ctrl+@", "\x00"),
        ("ctrl+at", "\x00"),
        ("backspace", "\x08"),
        ("ctrl+h", "\x08"),
        ("enter", "\r"),
        ("ctrl+m", "\r"),
        ("tab", "\t"),
        ("ctrl+i", "\t"),
        ("newline", "\n"),
        ("backtab", "\x1b[Z"),
        ("shift+tab", "\x1b[Z"),
        ("alt+shift+a", "\x1b[97;4u"),
        ("alt+shift+é", "\x1b[233;4u"),
        ("ctrl+shift+a", "\x1b[97;6u"),
        ("alt+ctrl+shift+a", "\x1b[97;8u"),
        ("shift+super+z", "\x1b[122;10u"),
        ("meta+shift+ω", "\x1b[969;34u"),
    ],
)
def test_supported_keys_match_actual_terminal_dispatch(key: str, sequence: str) -> None:
    plan = plan_keybindings({"logs": key}, _ACTIONS)

    assert plan.overrides == {"logs": key}
    assert not plan.warnings

    bindings = BindingsMap([(shift_alias_keys(plan.overrides["logs"]), "logs")])
    events = [event for event in XTermParser().feed(sequence) if isinstance(event, Key)]

    assert len(events) == 1
    assert canonical_key(events[0].key) == canonical_key(key)
    assert [binding.action for binding in bindings.key_to_bindings[events[0].key]] == ["logs"]


@pytest.mark.parametrize(("uppercase", "emitted"), [("A", "alt+shift+a"), ("É", "alt+shift+é")])
def test_legacy_alt_uppercase_uses_the_explicit_terminal_spelling(
    uppercase: str, emitted: str
) -> None:
    parser = XTermParser()
    messages = [*parser.feed(f"\x1b{uppercase}x"), *parser.feed("")]
    events = [event for event in messages if isinstance(event, Key)]

    assert [event.key for event in events] == [emitted, "x"]
    rejected = plan_keybindings({"logs": f"alt+{uppercase}"}, _ACTIONS)
    assert rejected.overrides == {}
    assert len(rejected.warnings) == 1

    accepted = plan_keybindings({"logs": emitted}, _ACTIONS)
    assert accepted.overrides == {"logs": emitted}
    assert not accepted.warnings
    bindings = BindingsMap([(shift_alias_keys(accepted.overrides["logs"]), "logs")])
    assert [binding.action for binding in bindings.key_to_bindings[events[0].key]] == ["logs"]
