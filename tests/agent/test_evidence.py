"""korvid mints the evidence references an answer may cite (issue #192).

A diagnostic answer is only checkable if its claims point at the cluster
reads that produced them. The references therefore have to come from
korvid: a provider that invents `[E3]` must not be able to make an
unsupported claim look sourced.
"""

from __future__ import annotations

from typing import Any

import pytest

from korvid.agent.evidence import EvidenceLedger
from korvid.agent.runtime import AgentRuntime
from korvid.tools.executor import RecordedExecution, ToolOutcome

from .test_runtime import ScriptedProvider


def test_a_successful_read_is_given_a_reference() -> None:
    """Reads are citable; the reference is korvid's, not the model's."""
    ledger = EvidenceLedger()

    ref = ledger.record("get_pod", {"name": "api-1", "namespace": "default"}, "phase: Running")

    assert ref == "E1"
    assert ledger.resolve("E1") is not None


def test_references_are_stable_and_do_not_repeat_within_a_turn() -> None:
    """A citation must identify exactly one read."""
    ledger = EvidenceLedger()

    first = ledger.record("get_pod", {"name": "api-1"}, "phase: Running")
    second = ledger.record("get_events", {"name": "api-1"}, "BackOff")

    assert [first, second] == ["E1", "E2"]
    assert first is not None
    assert second is not None
    assert ledger.resolve(first) is not ledger.resolve(second)


def test_a_failed_read_is_not_citable() -> None:
    """An error is not evidence; citing it would launder a gap as support."""
    ledger = EvidenceLedger()

    ref = ledger.record("get_pod", {"name": "gone"}, "ERROR: not found", error=True)

    assert ref is None
    assert ledger.references() == ()


def test_an_unknown_reference_does_not_resolve() -> None:
    """A provider-invented reference resolves to nothing, visibly."""
    ledger = EvidenceLedger()
    ledger.record("get_pod", {"name": "api-1"}, "phase: Running")

    assert ledger.resolve("E7") is None
    assert ledger.resolve("nonsense") is None


def test_evidence_carries_what_makes_it_checkable() -> None:
    """Source, target and a bounded excerpt - enough to go and look."""
    ledger = EvidenceLedger()

    ref = ledger.record("get_pod", {"namespace": "prod", "name": "api-1"}, "phase: Running")
    assert ref is not None
    item = ledger.resolve(ref)

    assert item is not None
    assert item.tool == "get_pod"
    assert item.namespace == "prod"
    assert item.name == "api-1"
    assert "Running" in item.excerpt


def test_the_excerpt_is_bounded() -> None:
    """Evidence rides in the prompt; an unbounded excerpt would blow the
    small-profile budget the issue requires to stay intact."""
    ledger = EvidenceLedger(excerpt_limit=40)

    ref = ledger.record("get_logs", {"name": "api-1"}, "x" * 500)
    assert ref is not None
    item = ledger.resolve(ref)

    assert item is not None
    assert len(item.excerpt) <= 40


def test_citations_are_split_into_supported_and_unknown() -> None:
    """Unsupported citations are surfaced, never silently dropped."""
    ledger = EvidenceLedger()
    ledger.record("get_pod", {"name": "api-1"}, "phase: Running")

    supported, unknown = ledger.check_citations("The pod is up [E1], and the node is fine [E9].")

    assert supported == ("E1",)
    assert unknown == ("E9",)


def test_malformed_citation_syntax_is_not_treated_as_a_citation() -> None:
    """Degrade safely: junk is not a reference and is not reported as one."""
    ledger = EvidenceLedger()
    ledger.record("get_pod", {"name": "api-1"}, "phase: Running")

    supported, unknown = ledger.check_citations("see [E], [E1x], [] and [E01]")

    assert supported == ()
    assert unknown == ()


def test_a_repeated_citation_is_reported_once() -> None:
    """Duplicates are a formatting artefact, not extra support."""
    ledger = EvidenceLedger()
    ledger.record("get_pod", {"name": "api-1"}, "phase: Running")

    supported, unknown = ledger.check_citations("[E1] and again [E1]")

    assert supported == ("E1",)
    assert unknown == ()


def test_the_ledger_is_scoped_to_one_turn() -> None:
    """A citation resolves only to evidence read in the current turn."""
    ledger = EvidenceLedger()
    ledger.record("get_pod", {"name": "api-1"}, "phase: Running")

    ledger.start_turn()

    assert ledger.resolve("E1") is None
    assert ledger.references() == ()


def test_recording_requires_a_tool_name() -> None:
    """A reference with no source could not be navigated to."""
    ledger = EvidenceLedger()

    with pytest.raises(ValueError, match="tool name"):
        ledger.record("", {"name": "api-1"}, "phase: Running")


class _ReadExecutor(RecordedExecution):
    """Succeeds for every tool except one that reports a failure.

    `get_resource` is declared `structured_yaml`, so its result has to
    parse as a document or the outbound policy blocks the turn.
    """

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return "kind: Pod\nstatus:\n  phase: Running\n"

    async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        if name == "list_resources":
            return ToolOutcome(text="ERROR: not found", error=True)
        return ToolOutcome(text=await self.execute(name, arguments))


def _tool_call(call_id: str, name: str, args: str) -> list[dict[str, Any]]:
    return [
        {"type": "tool_call", "id": call_id, "name": name, "arguments": args},
        {"type": "done"},
    ]


async def test_the_runtime_mints_a_reference_for_each_successful_read() -> None:
    """A turn's reads are citable by the answer that follows them."""
    provider = ScriptedProvider(
        [
            _tool_call("c1", "get_resource", '{"namespace": "prod", "name": "api-1"}'),
            [{"type": "text_delta", "text": "api-1 is running [E1]"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(provider, _ReadExecutor())

    async for _ in runtime.run_turn("what is wrong?", "view=pods"):
        pass

    item = runtime.evidence.resolve("E1")
    assert item is not None
    assert item.tool == "get_resource"
    assert item.name == "api-1"


async def test_a_failed_read_gets_no_reference_from_the_runtime() -> None:
    """Gaps stay gaps: an error must not become citable support."""
    provider = ScriptedProvider(
        [
            _tool_call("c1", "list_resources", '{"name": "gone"}'),
            [{"type": "text_delta", "text": "no such pod"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(provider, _ReadExecutor())

    async for _ in runtime.run_turn("what is wrong?", "view=pods"):
        pass

    assert runtime.evidence.references() == ()


async def test_each_turn_starts_from_an_empty_ledger() -> None:
    """A citation cannot resolve to a read from an earlier question."""
    provider = ScriptedProvider(
        [
            _tool_call("c1", "get_resource", '{"name": "api-1"}'),
            [{"type": "text_delta", "text": "ok [E1]"}, {"type": "done"}],
            [{"type": "text_delta", "text": "still ok"}, {"type": "done"}],
        ]
    )
    runtime = AgentRuntime(provider, _ReadExecutor())

    async for _ in runtime.run_turn("first", "view=pods"):
        pass
    assert runtime.evidence.references() == ("E1",)

    async for _ in runtime.run_turn("second", "view=pods"):
        pass

    assert runtime.evidence.references() == ()
