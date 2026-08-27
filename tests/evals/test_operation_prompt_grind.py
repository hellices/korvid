"""Typed prompt-grind override for the stateful operation harness.

`run_operation_journey` composes through `build_eval_harness` exactly like
the read-only scenario/journey harnesses — this module proves the grind
travels through unchanged: the immutable safety contract still composes
first, the default (no grind) run is unaffected, and two grinds run
concurrently never leak into each other.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from korvid.agent.prompt_packs import SAFETY_CONTRACT
from korvid.evals.harness import NO_GRIND, PromptGrind
from korvid.evals.operation import bundled_operations_dir, load_operation_journeys
from korvid.evals.scripted import ScriptedProvider

from .operation_app import OperationRun, run_operation_journey
from .operation_scripts import OPERATION_SCRIPTS

_JOURNEYS = {journey.id: journey for journey in load_operation_journeys(bundled_operations_dir())}
_JOURNEY_ID = "scale-deployment-up"


class _PromptSpy(ScriptedProvider):
    """Records every outbound message list, so a test can inspect the
    exact system message the model was sent."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        self.calls.append([dict(message) for message in messages])
        async for event in super().complete(messages, tools, stream=stream):
            yield event


async def _run(tmp_path: Path, *, grind: PromptGrind = NO_GRIND) -> OperationRun:
    return await run_operation_journey(
        _JOURNEYS[_JOURNEY_ID],
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS[_JOURNEY_ID]),
        grind=grind,
    )


async def _run_with_spy(tmp_path: Path, *, grind: PromptGrind, spy: _PromptSpy) -> OperationRun:
    return await run_operation_journey(
        _JOURNEYS[_JOURNEY_ID],
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: spy,
        grind=grind,
    )


async def test_omitting_the_grind_publishes_the_default_prompt_identity(tmp_path: Path) -> None:
    run = await _run(tmp_path)
    assert run.prompt["source"] == "default"
    assert sorted(run.prompt) == ["overlays", "pack", "sha256", "source"]


async def test_a_tier_pack_grind_changes_the_published_identity(tmp_path: Path) -> None:
    baseline = await _run(tmp_path / "baseline")
    ground = await _run(
        tmp_path / "ground", grind=PromptGrind(tier_pack="Answer in one short sentence.")
    )
    assert ground.prompt["source"] == "override"
    assert ground.prompt["sha256"] != baseline.prompt["sha256"]


async def test_the_safety_contract_still_composes_first_with_an_active_grind(
    tmp_path: Path,
) -> None:
    spy = _PromptSpy(OPERATION_SCRIPTS[_JOURNEY_ID])
    grind = PromptGrind(tier_pack="GRIND-TIER-PACK-MARKER", overlay="GRIND-OVERLAY-MARKER")
    await _run_with_spy(tmp_path, grind=grind, spy=spy)
    assert spy.calls, "the scripted provider must have been called at least once"
    system_message = spy.calls[0][0]
    assert system_message["role"] == "system"
    content = system_message["content"]
    assert content.startswith(SAFETY_CONTRACT)
    safety_end = len(SAFETY_CONTRACT)
    assert "GRIND-TIER-PACK-MARKER" in content[safety_end:]
    assert "GRIND-OVERLAY-MARKER" in content[safety_end:]
    assert "GRIND-TIER-PACK-MARKER" not in content[:safety_end]
    assert "GRIND-OVERLAY-MARKER" not in content[:safety_end]


async def test_simultaneous_runs_with_different_grinds_do_not_leak(tmp_path: Path) -> None:
    grind_a = PromptGrind(tier_pack="Tier pack A.")
    grind_b = PromptGrind(tier_pack="Tier pack B.")
    concurrent_a, concurrent_b = await asyncio.gather(
        _run(tmp_path / "concurrent-a", grind=grind_a),
        _run(tmp_path / "concurrent-b", grind=grind_b),
    )
    solo_a = await _run(tmp_path / "solo-a", grind=grind_a)
    solo_b = await _run(tmp_path / "solo-b", grind=grind_b)
    assert concurrent_a.prompt["sha256"] == solo_a.prompt["sha256"]
    assert concurrent_b.prompt["sha256"] == solo_b.prompt["sha256"]
    assert concurrent_a.prompt["sha256"] != concurrent_b.prompt["sha256"]
