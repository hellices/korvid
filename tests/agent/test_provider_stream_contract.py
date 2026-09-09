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


def test_the_bounds_are_stated_as_numbers_a_reader_can_check() -> None:
    assert MAX_TOOL_ARGUMENT_CHARS == 65_536
    assert MAX_TOOL_CALLS_PER_RESPONSE == 64
    assert MAX_REASONING_CHARS == 65_536


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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, contract.CREDENTIAL_REFUSED),
        (403, contract.NOT_PERMITTED),
        (404, contract.MODEL_UNKNOWN),
        (429, contract.RATE_LIMITED),
        (400, contract.REQUEST_REJECTED),
        (422, contract.REQUEST_REJECTED),
        (500, contract.SERVER_ERROR),
        (502, contract.SERVER_ERROR),
        (503, contract.UNAVAILABLE),
    ],
)
def test_a_refusing_status_becomes_the_written_message_for_its_class(
    status: int, expected: str
) -> None:
    """The status class is what an operator can act on, and it is knowable
    without reading a byte of the body."""
    error = contract.status_error(status)

    assert isinstance(error, contract.ProviderStatusError)
    assert error.operator_message() == expected


def test_every_typed_failure_shares_one_catchable_base() -> None:
    """A caller that only needs "korvid would not accept this answer"
    must not have to enumerate five classes."""
    for error in (
        ProviderStreamTruncatedError,
        ProviderStreamLimitError,
        contract.ProviderProtocolError,
        contract.ProviderStatusError,
        contract.ProviderTransportError,
    ):
        assert issubclass(error, contract.ProviderStreamError)
        assert issubclass(error, OperatorSafeProviderError)


def test_every_written_message_is_evidence_free() -> None:
    """A written sentence is safe because it interpolates nothing. Pinning
    that here keeps a later `f"...{exc}"` from being added to the table."""
    for message in contract.STREAM_MESSAGES:
        assert "{" not in message
        assert "%" not in message
        assert message.strip() == message
        assert message.endswith(".")


def test_the_written_messages_are_exactly_what_the_types_declare() -> None:
    declared: set[str] = set()
    for error in (
        ProviderStreamTruncatedError,
        ProviderStreamLimitError,
        contract.ProviderProtocolError,
        contract.ProviderStatusError,
        contract.ProviderTransportError,
    ):
        declared |= set(error.safe_messages)

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
