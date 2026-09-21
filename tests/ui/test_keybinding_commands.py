"""Both editor spellings share the existing typed command and palette routes."""

from __future__ import annotations

import pytest

from korvid.ui.command import COMMANDS, command_help, command_words, parse_command
from korvid.ui.messages import BuiltinCommand, UnknownCommand


@pytest.mark.parametrize("text", ["keys", "keybindings"])
def test_editor_alias_is_one_zero_argument_operation(text: str) -> None:
    command = parse_command(text, lambda alias: None)
    assert isinstance(command, BuiltinCommand)
    assert command.operation.value == "keys"
    assert command.arguments == ()


@pytest.mark.parametrize("text", ["keys help", "keybindings reset"])
def test_editor_does_not_accept_a_hidden_mutation_argument(text: str) -> None:
    assert isinstance(parse_command(text, lambda alias: None), UnknownCommand)


def test_editor_is_discoverable_in_existing_help_completion_and_palette() -> None:
    descriptors = [descriptor for descriptor in COMMANDS if "keys" in descriptor.aliases]
    assert len(descriptors) == 1
    descriptor = descriptors[0]
    assert descriptor.palette is not None
    assert descriptor.palette.canonical_text == "keys"
    assert "keybindings" in descriptor.aliases
    assert {"keys", "keybindings"}.issubset(command_words(()))
    assert any(":keys" in syntax for syntax, _ in command_help())
