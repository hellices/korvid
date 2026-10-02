"""`:slots` / `:ns-slots` share one typed command and palette route (issue #406)."""

from __future__ import annotations

import pytest

from korvid.ui.command import COMMANDS, command_help, command_words, parse_command
from korvid.ui.messages import BuiltinCommand, BuiltinOperation, UnknownCommand


@pytest.mark.parametrize("text", ["slots", "ns-slots"])
def test_both_spellings_are_one_zero_argument_operation(text: str) -> None:
    command = parse_command(text, lambda alias: None)

    assert isinstance(command, BuiltinCommand)
    assert command.operation is BuiltinOperation.NAMESPACE_SLOTS
    assert command.arguments == ()


def test_reallocation_takes_no_hidden_arguments() -> None:
    assert isinstance(parse_command("slots reset", lambda alias: None), UnknownCommand)


def test_reallocation_is_discoverable_in_help_completion_and_palette() -> None:
    descriptors = [descriptor for descriptor in COMMANDS if "slots" in descriptor.aliases]
    assert len(descriptors) == 1
    palette = descriptors[0].palette
    assert palette is not None
    assert palette.canonical_text == "slots"
    assert {"slots", "ns-slots"}.issubset(command_words(()))
    assert any(":slots" in syntax for syntax, _ in command_help())
