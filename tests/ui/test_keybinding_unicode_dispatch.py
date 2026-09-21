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
