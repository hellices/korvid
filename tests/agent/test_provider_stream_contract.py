"""The provider-neutral stream-safety contract (issue #336).

Three built-in adapters accumulate provider output before the engine's own
response budget can see any of it, and each speaks a different protocol.
The bounds and the failure vocabulary therefore live once, in the ABC's
own module, so a third adapter cannot quietly pick different numbers or
invent a message the runtime has not audited.
"""

from __future__ import annotations

import pytest

from korvid.agent import provider as contract
from korvid.agent.provider import (
    MAX_REASONING_CHARS,
    MAX_TOOL_ARGUMENT_CHARS,
    MAX_TOOL_CALLS_PER_RESPONSE,
    STREAM_LIMIT,
    STREAM_TRUNCATED,
    OperatorSafeProviderError,
    ProviderStreamLimitError,
    ProviderStreamTruncatedError,
    append_bounded,
    guard_tool_call_count,
)
from korvid.agent.provider_plugin import (
    _MAX_TOOL_ARGUMENTS_BYTES,
    _MAX_TOOL_CALL_FIELD_LENGTH,
)


def test_the_argument_bound_is_the_one_the_plugin_contract_already_audited() -> None:
    """A built-in must not be looser than a third-party adapter.

    `provider_plugin` already refuses a plugin's tool call whose arguments
    pass 64 KiB. A built-in that accumulated more before the engine looked
    would make korvid's own adapters the weakest ones it ships.
    """
    assert MAX_TOOL_ARGUMENT_CHARS == _MAX_TOOL_ARGUMENTS_BYTES == 65_536


def test_the_bounds_are_stated_as_numbers_a_reader_can_check() -> None:
    assert MAX_TOOL_CALLS_PER_RESPONSE == 64
    assert MAX_REASONING_CHARS == 65_536
    # The id/name bound stays where it already was; the shared contract
    # does not fork a second answer for the same question.
    assert _MAX_TOOL_CALL_FIELD_LENGTH == 256


def test_appending_exactly_up_to_the_bound_is_allowed() -> None:
    accumulated = "a" * (MAX_TOOL_ARGUMENT_CHARS - 5)

    assert (
        len(append_bounded(accumulated, "bbbbb", limit=MAX_TOOL_ARGUMENT_CHARS))
        == MAX_TOOL_ARGUMENT_CHARS
    )


def test_one_character_past_the_bound_is_refused_before_it_is_stored() -> None:
    accumulated = "a" * MAX_TOOL_ARGUMENT_CHARS

    with pytest.raises(ProviderStreamLimitError, match="limit") as raised:
        append_bounded(accumulated, "b", limit=MAX_TOOL_ARGUMENT_CHARS)

    assert str(raised.value) == STREAM_LIMIT


def test_the_bound_is_cumulative_rather_than_per_fragment() -> None:
    """The failure mode is a stream of small fragments, not one big one."""

    def fold(pieces: int) -> str:
        accumulated = ""
        for _ in range(pieces):
            accumulated = append_bounded(accumulated, "x" * 1024, limit=MAX_TOOL_ARGUMENT_CHARS)
        return accumulated

    assert len(fold(MAX_TOOL_ARGUMENT_CHARS // 1024)) == MAX_TOOL_ARGUMENT_CHARS
    with pytest.raises(ProviderStreamLimitError, match="limit"):
        fold((MAX_TOOL_ARGUMENT_CHARS // 1024) + 1)


def test_exactly_the_permitted_number_of_calls_is_allowed() -> None:
    guard_tool_call_count(MAX_TOOL_CALLS_PER_RESPONSE)


def test_one_call_too_many_is_refused() -> None:
    with pytest.raises(ProviderStreamLimitError, match="limit"):
        guard_tool_call_count(MAX_TOOL_CALLS_PER_RESPONSE + 1)


def test_a_truncated_stream_and_an_exhausted_limit_are_different_types() -> None:
    """The engine reports them the same way; an operator does not read
    them the same way, and a test must be able to tell them apart."""
    assert not issubclass(ProviderStreamTruncatedError, ProviderStreamLimitError)
    assert not issubclass(ProviderStreamLimitError, ProviderStreamTruncatedError)


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ProviderStreamTruncatedError, STREAM_TRUNCATED),
        (ProviderStreamLimitError, STREAM_LIMIT),
    ],
)
def test_each_failure_carries_a_message_the_runtime_may_show(
    error: type[OperatorSafeProviderError], message: str
) -> None:
    """The runtime withholds every undeclared exception text, so a
    translation that is not declared reaches the operator as a bare class
    name — which is exactly the failure this contract exists to avoid."""
    assert issubclass(error, OperatorSafeProviderError)
    assert error(message).operator_message() == message


@pytest.mark.parametrize("error", [ProviderStreamTruncatedError, ProviderStreamLimitError])
def test_a_message_built_from_a_provider_never_inherits_the_exemption(
    error: type[OperatorSafeProviderError],
) -> None:
    assert error("HTTP 401: bearer sk-secret").operator_message() is None


def test_every_written_message_is_evidence_free() -> None:
    """A written sentence is safe because it interpolates nothing. Pinning
    that here keeps a later `f"...{exc}"` from being added to the table."""
    for message in contract.STREAM_MESSAGES:
        assert "{" not in message
        assert "%" not in message
        assert message.strip() == message
        assert message.endswith(".")


def test_the_written_messages_are_exactly_what_the_types_declare() -> None:
    declared = set(ProviderStreamTruncatedError.safe_messages) | set(
        ProviderStreamLimitError.safe_messages
    )
    assert declared == set(contract.STREAM_MESSAGES)


def test_the_contract_module_stays_a_stdlib_leaf() -> None:
    """Three adapters and the engine import it; a transport dependency
    here would drag `httpx` into an install that has no `[agent]` extra."""
    import ast
    from pathlib import Path

    source = Path(str(contract.__file__)).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module.split(".")[0])

    assert imported <= {"abc", "collections", "typing", "korvid", "__future__"}
