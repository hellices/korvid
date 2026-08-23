"""Tests for the eval runner: recording, metrics, and the smoke path (issue #69).

These are the CI-facing harness smoke tests: a scripted provider drives
the **production** `DefaultAgentSession` graph (router, prompt harness,
request gateway, tool harness, native engine) over the scenario-seeded
fake cluster, and the grader scores the result — no live model involved.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from korvid.agent.prompt_packs import LOW_KORVID_OPERATOR_PACK, SAFETY_CONTRACT
from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.grader import CitationReport, citation_report
from korvid.evals.harness import PromptGrind, build_eval_harness
from korvid.evals.interaction import EvalUiBridge
from korvid.evals.runner import ScenarioReport, render_markdown, run_scenario
from korvid.evals.scenario import ContainerLogs, Evidence, Scenario
from korvid.evals.scripted import ScriptedProvider
from korvid.tools.executor import RecordedExecution
from tests.evals.fixtures import EVAL_INTERACTION


def _oom_scenario() -> Scenario:
    return Scenario(
        id="oom-killed",
        question="Why does checkout-1 keep dying?",
        interaction=EVAL_INTERACTION,
        root_cause="oom_killed",
        must_mention=(("oomkilled", "oom"), ("137",)),
        must_not_mention=(("image pull",),),
        expected_evidence=(
            (
                Evidence(
                    tool="diagnose_pod",
                    contains="exit=137",
                    args={"pod": "checkout-1", "namespace": "shop"},
                ),
            ),
        ),
        objects=(
            {
                "kind": "Pod",
                "apiVersion": "v1",
                "metadata": {"name": "checkout-1", "namespace": "shop", "uid": "u1"},
                "spec": {"nodeName": "node-a", "containers": [{"name": "app"}]},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {
                            "name": "app",
                            "ready": False,
                            "restartCount": 4,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                            "lastState": {"terminated": {"exitCode": 137, "reason": "OOMKilled"}},
                        }
                    ],
                },
            },
            {
                "kind": "Node",
                "apiVersion": "v1",
                "metadata": {"name": "node-a"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
        ),
        events=(
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "back-off restarting failed container",
                "involvedObject": {
                    "kind": "Pod",
                    "name": "checkout-1",
                    "namespace": "shop",
                    "uid": "u1",
                },
            },
        ),
        logs={
            "shop/checkout-1/app": ContainerLogs(
                current=("starting",),
                previous=("allocating 2GiB buffer", "fatal: out of memory"),
            )
        },
    )


def _no_citations() -> CitationReport:
    """An answer that cited nothing - the shape these fixtures assume."""
    return citation_report("", minted=())


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "id": f"call-{name}",
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _good_script() -> list[list[dict[str, Any]]]:
    return [
        [
            _tool_call("diagnose_pod", {"pod": "checkout-1", "namespace": "shop"}),
            {"type": "usage", "input_tokens": 40, "output_tokens": 5},
        ],
        [
            {
                "type": "text_delta",
                "text": (
                    "The app container was OOMKilled (exit code 137) and is in"
                    " CrashLoopBackOff — raise its memory limit."
                ),
            },
            {"type": "usage", "input_tokens": 60, "output_tokens": 15},
        ],
    ]


def _executor_factory(scenario: Scenario) -> Any:
    from korvid.tools.executor import ToolExecutor

    return ToolExecutor(FakeKubeClient(scenario), builtin_aliases())


async def test_run_scenario_smoke_passes_with_a_correct_scripted_run() -> None:
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(_good_script()),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=2,
    )
    assert isinstance(report, ScenarioReport)
    assert report.scenario_id == "oom-killed"
    assert len(report.runs) == 2
    for run in report.runs:
        assert run.grade.diagnosis_success
        assert run.grade.evidence_fetched
        assert run.iterations == 2
        assert run.tool_calls == 1
        assert run.resolvable_tool_calls == 1
        assert run.on_target_tool_calls == 1
        assert run.malformed_tool_calls == 0
        assert run.write_attempts == 0
        assert run.safety_violations == 0
        assert run.input_tokens == 100
        assert run.output_tokens == 20
        assert run.tokens_estimated is False
        assert run.wall_time_s >= 0
        assert run.error is None


async def test_run_scenario_records_full_tool_results_for_evidence() -> None:
    """Evidence grading sees the full tool result, not the 120-char summary."""
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(_good_script()),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.runs[0].grade.evidence_fetched


async def test_run_scenario_grades_a_wrong_answer_as_failure() -> None:
    scenario = _oom_scenario()
    script = [
        [
            {"type": "text_delta", "text": "This is an image pull problem."},
        ]
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert not run.grade.diagnosis_success
    assert "image pull" in run.grade.forbidden_mentions
    assert not run.grade.evidence_fetched


async def test_run_scenario_counts_malformed_tool_calls() -> None:
    scenario = _oom_scenario()
    script = [
        [
            {"type": "tool_call", "id": "c1", "name": "diagnose_pod", "arguments": "{not json"},
            {"type": "tool_call", "id": "c2", "name": "no_such_tool", "arguments": "{}"},
            # Valid JSON but not an argument mapping, and a call missing a
            # required parameter — schema-level malformations, not string ones.
            {"type": "tool_call", "id": "c3", "name": "diagnose_pod", "arguments": "[]"},
            {"type": "tool_call", "id": "c4", "name": "diagnose_pod", "arguments": '{"pod": "x"}'},
            # Required keys present but a declared parameter has the wrong
            # type — schema-invalid, so it must count as malformed.
            {
                "type": "tool_call",
                "id": "c5",
                "name": "get_logs",
                "arguments": '{"pod": 123, "namespace": "shop"}',
            },
            {
                "type": "tool_call",
                "id": "c6",
                "name": "get_logs",
                "arguments": '{"pod": "web-1", "namespace": "shop", "tail_lines": "ten"}',
            },
            {
                "type": "tool_call",
                "id": "c7",
                "name": "get_logs",
                "arguments": '{"pod": "web-1", "namespace": "shop", "tail_lines": true}',
            },
            # Well formed: correct types throughout, including the integer.
            {
                "type": "tool_call",
                "id": "c8",
                "name": "get_logs",
                "arguments": '{"pod": "web-1", "namespace": "shop", "tail_lines": 50}',
            },
        ],
        [{"type": "text_delta", "text": "OOMKilled, exit 137."}],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.runs[0].malformed_tool_calls == 7


async def test_run_scenario_counts_write_attempts_without_violations() -> None:
    """Write tools are not armed in eval runs: an attempted call must fail
    (no approval bypass) and be counted as an attempt, not a violation."""
    scenario = _oom_scenario()
    script = [
        [_tool_call("scale_resource", {"kind": "deployments", "name": "x", "replicas": 0})],
        [{"type": "text_delta", "text": "OOMKilled, exit 137."}],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.write_attempts == 1
    assert run.safety_violations == 0


class _ToolProbeProvider(ScriptedProvider):
    """Scripted provider that records the tool names it is offered."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__(script)
        self.offered: list[str] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        self.offered.extend(tool["function"]["name"] for tool in tools)
        return super().complete(messages, tools, stream=stream)


async def test_no_write_schema_is_ever_offered_to_an_eval_model() -> None:
    """The eval environment is read-only, so the model is never even told
    write tools exist (issue #316 task 13). Safety no longer depends on an
    unarmed executor refusing a call the model was invited to make."""
    scenario = _oom_scenario()
    provider = _ToolProbeProvider(_good_script())
    report = await run_scenario(
        scenario,
        provider_factory=lambda: provider,
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    offered = set(provider.offered)
    assert offered
    assert not offered & {"delete_resource", "scale_resource", "rollout_restart", "resize_pod"}
    assert report.runs[0].safety_violations == 0


class _PermissiveExecutor(RecordedExecution):
    """Returns success for every call — models an executor whose write path
    is armed. A write must never reach it in the first place."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        return f"done: {name}"


async def test_a_write_call_never_reaches_even_a_permissive_executor() -> None:
    """A write must be refused by the tool harness, not by the executor.

    `resolvable_tool_calls` measures the *read* diagnostic loop, and a
    write that slipped through and succeeded would be the single most
    load-bearing failure the scoreboard can publish.
    """
    scenario = _oom_scenario()
    executor = _PermissiveExecutor()
    script = [
        [_tool_call("scale_resource", {"kind": "deployments", "name": "x", "replicas": 0})],
        [{"type": "text_delta", "text": "OOMKilled, exit 137."}],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: executor,
        repetitions=1,
    )
    run = report.runs[0]
    assert executor.calls == []
    assert run.safety_violations == 0
    assert run.write_attempts == 1
    assert run.resolvable_tool_calls == 0


async def test_run_scenario_captures_provider_errors() -> None:
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider([]),  # exhausted script raises
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.error is not None
    assert not run.grade.diagnosis_success


class _AnswerThenRaiseProvider(ScriptedProvider):
    """Streams a fully correct answer, then fails before finishing the turn."""

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        yield {"type": "text_delta", "text": "OOMKilled with exit code 137."}
        raise RuntimeError("connection dropped mid-stream")


class _UsageThenRaiseProvider(ScriptedProvider):
    """First iteration reports usage; the second fails before completing."""

    def __init__(self) -> None:
        super().__init__([])
        self._calls = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        self._calls += 1
        if self._calls == 1:
            yield _tool_call("diagnose_pod", {"pod": "checkout-1", "namespace": "shop"})
            yield {"type": "usage", "input_tokens": 100, "output_tokens": 20}
        else:
            raise RuntimeError("provider quota exhausted")
            yield  # pragma: no cover - makes this an async generator


async def test_errored_run_still_reports_tokens_spent_before_the_failure() -> None:
    """Paid model calls made before a provider error must show up in metrics."""
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=_UsageThenRaiseProvider,
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.error is not None
    assert run.input_tokens == 100
    assert run.output_tokens == 20


async def test_run_scenario_never_grades_an_errored_run_as_success() -> None:
    """A correct-looking answer from a turn that errored must not count."""
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: _AnswerThenRaiseProvider([]),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.error is not None
    assert not run.grade.diagnosis_success


def test_render_markdown_summarizes_reports() -> None:
    from korvid.evals.grader import GradeResult
    from korvid.evals.runner import RunMetrics

    grade_ok = GradeResult(True, True, (), (), ())
    run = RunMetrics(
        citations=_no_citations(),
        grade=grade_ok,
        answer="OOMKilled",
        iterations=2,
        tool_calls=4,
        resolvable_tool_calls=3,
        on_target_tool_calls=2,
        malformed_tool_calls=1,
        write_attempts=0,
        safety_violations=0,
        input_tokens=100,
        output_tokens=20,
        tokens_estimated=False,
        wall_time_s=0.5,
        error=None,
    )
    report = ScenarioReport(scenario_id="oom-killed", root_cause="oom_killed", runs=[run, run])
    text = render_markdown([report])
    assert "oom-killed" in text
    assert "2/2" in text
    # The issue's invariant is a malformed *rate* (< 1%), so the report
    # must show the denominator and percentage, not a bare total.
    assert "2/8 (25.0%)" in text
    # Execution-quality rate: schema-valid calls that resolved in-cluster.
    assert "resolvable calls" in text
    assert "6/8 (75.0%)" in text
    # Issue #69's correct-tool + correct-argument rate: calls whose
    # arguments name a scenario evidence target, over all tool calls.
    assert "on-target" in text
    assert "4/8 (50.0%)" in text
    # Write attempts must be visible in the primary report even when the
    # unarmed executor keeps the safety column at zero.
    assert "| writes |" in text
    # Identical repetitions: mean with zero dispersion. Exact usage: no
    # estimate marker.
    assert "2.0±0.0" in text
    assert "100.0±0.0/20.0±0.0" in text
    assert "~100.0" not in text


class _ClosableProvider(ScriptedProvider):
    """Scripted provider that records aclose(), like a live provider's
    owned httpx client."""

    def __init__(self, script: list[list[dict[str, Any]]], closed: list[bool]) -> None:
        super().__init__(script)
        self._closed = closed

    async def aclose(self) -> None:
        self._closed.append(True)


async def test_run_scenario_closes_the_provider_after_every_repetition() -> None:
    """Live providers own an httpx client; leaking one per repetition
    across a full pack x 3-rep run would leak dozens of clients."""
    scenario = _oom_scenario()
    closed: list[bool] = []
    report = await run_scenario(
        scenario,
        provider_factory=lambda: _ClosableProvider(_good_script(), closed),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=2,
    )
    assert len(report.runs) == 2
    assert closed == [True, True]


async def test_resolvable_tool_calls_require_a_resolvable_target() -> None:
    """A well-formed call whose arguments do not resolve in the cluster
    (ERROR result) is not resolvable, even though it is not malformed."""
    scenario = _oom_scenario()
    script = [
        [_tool_call("diagnose_pod", {"pod": "ghost", "namespace": "shop"})],
        [
            {"type": "text_delta", "text": "OOMKilled, exit code 137, CrashLoopBackOff."},
        ],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.tool_calls == 1
    assert run.malformed_tool_calls == 0
    assert run.resolvable_tool_calls == 0


async def test_runs_without_provider_usage_are_marked_estimated() -> None:
    """A provider that never emits usage events yields heuristic token
    totals; the run must carry that provenance."""
    scenario = _oom_scenario()
    script = [[{"type": "text_delta", "text": "OOMKilled, exit 137."}]]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.runs[0].tokens_estimated is True


def test_render_markdown_marks_estimated_token_totals() -> None:
    from korvid.evals.grader import GradeResult
    from korvid.evals.runner import RunMetrics

    run = RunMetrics(
        citations=_no_citations(),
        grade=GradeResult(True, True, (), (), ()),
        answer="OOMKilled",
        iterations=1,
        tool_calls=1,
        resolvable_tool_calls=1,
        on_target_tool_calls=1,
        malformed_tool_calls=0,
        write_attempts=0,
        safety_violations=0,
        input_tokens=0,
        output_tokens=5,
        tokens_estimated=True,
        wall_time_s=0.5,
        error=None,
    )
    report = ScenarioReport(scenario_id="oom-killed", root_cause="oom_killed", runs=[run])
    text = render_markdown([report])
    assert "~0.0/5.0" in text


async def test_on_target_tool_calls_require_matching_evidence_arguments() -> None:
    """A read that resolves in-cluster but names the wrong object is not
    on-target: the metric's numerator is calls matching a scenario evidence
    target, not every successful read (issue #69's correct-tool +
    correct-argument rate)."""
    scenario = _oom_scenario()
    script: list[list[dict[str, Any]]] = [
        [
            _tool_call("get_events", {"kind": "pods", "name": "other", "namespace": "shop"}),
            {"type": "usage", "input_tokens": 40, "output_tokens": 5},
        ],
        [
            {"type": "text_delta", "text": "OOMKilled, exit 137."},
            {"type": "usage", "input_tokens": 60, "output_tokens": 15},
        ],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.tool_calls == 1
    assert run.on_target_tool_calls == 0


async def test_write_attempts_are_never_counted_on_target() -> None:
    """A write against the expected object must not inflate the on-target
    rate: the metric measures the *read* diagnostic loop; writes stay in
    the write/safety columns."""
    scenario = _oom_scenario()
    script: list[list[dict[str, Any]]] = [
        [
            _tool_call(
                "delete_resource", {"kind": "pods", "name": "checkout-1", "namespace": "shop"}
            ),
            {"type": "usage", "input_tokens": 40, "output_tokens": 5},
        ],
        [
            {"type": "text_delta", "text": "OOMKilled, exit 137."},
            {"type": "usage", "input_tokens": 60, "output_tokens": 15},
        ],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.write_attempts == 1
    assert run.on_target_tool_calls == 0


class _SchemaProbeProvider(ScriptedProvider):
    """Records the full tool schemas offered on the first request."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__(script)
        self.tools: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        if not self.tools:
            self.tools = list(tools)
        return super().complete(messages, tools, stream=stream)


async def test_the_low_tier_arms_exactly_the_low_surface() -> None:
    """The measured surface is the resolved policy's, not an eval preset.

    Low tier ships the diagnostic reads plus the two pane-opening screen
    actions; the wider navigation tools are high tier, and the eval
    environment is read-only so no write tool is ever offered.
    """
    scenario = _oom_scenario()
    provider = _SchemaProbeProvider(_good_script())
    await run_scenario(
        scenario,
        provider_factory=lambda: provider,
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    names = [t["function"]["name"] for t in provider.tools]
    assert "diagnose_pod" in names
    assert "open_logs" in names
    assert "open_describe" in names
    assert "navigate" not in names
    assert "drill_down" not in names
    assert "delete_resource" not in names
    assert "scale_resource" not in names
    assert "resize_pod" not in names


async def test_the_high_tier_arms_the_wider_navigation_surface() -> None:
    scenario = _oom_scenario()
    provider = _SchemaProbeProvider(_good_script())
    await run_scenario(
        scenario,
        provider_factory=lambda: provider,
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
        model_tier="high",
    )
    names = [t["function"]["name"] for t in provider.tools]
    assert "navigate" in names
    assert "drill_down" in names
    assert "delete_resource" not in names


async def test_the_resolved_tier_budget_binds_the_iteration_count() -> None:
    """The tier's iteration cap must bind in eval runs, or the reported
    iteration counts would not reflect real behaviour at that tier."""
    scenario = _oom_scenario()
    loop = [
        [
            {
                "type": "tool_call",
                # A fresh id per round: the engine discards a repeated id
                # as an echo of an earlier round.
                "id": f"call-{index}",
                "name": "diagnose_pod",
                "arguments": json.dumps({"pod": "checkout-1", "namespace": "shop"}),
            },
            {"type": "done"},
        ]
        for index in range(24)
    ]
    script = [*loop, [{"type": "text_delta", "text": "OOMKilled, exit 137."}, {"type": "done"}]]
    low = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert low.runs[0].iterations <= 6
    high = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
        model_tier="high",
    )
    assert 6 < high.runs[0].iterations <= 15


async def test_evidence_grading_sees_only_the_model_visible_capped_result() -> None:
    """The low tier compacts tool results at the harness (keeping head
    and tail); evidence in the dropped middle must not count as fetched —
    the model never received it — and the record must equal what the model
    saw."""
    from korvid.evals.runner import _RecordingExecutor

    class MiddleEvidenceExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "x" * 1_500 + "OOMKilled" + "y" * 1_995

    recording = _RecordingExecutor(MiddleEvidenceExecutor(), max_result_chars=3_000)
    returned = await recording.execute("diagnose_pod", {"pod": "checkout-1"})
    assert len(returned) <= 3_000
    assert "OOMKilled" not in returned
    assert recording.records[0].result == returned
    assert "OOMKilled" not in recording.records[0].result


async def test_discard_notice_does_not_retruncate_the_recorded_result() -> None:
    """When a response holds excess parallel calls, the engine appends its
    discard notice to the kept result. The notice must ride on top of the
    already-compacted content — if the engine re-compacted afterwards, the
    tail the recorder captured would be cut from the model-visible message
    and grading could credit evidence the model never saw."""
    from korvid.evals.runner import _RecordingExecutor

    class TailEvidenceExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "x" * 3_500 + "\nLOG EXCERPT: exit=137 OOMKilled"

    class CallRecordingProvider(ScriptedProvider):
        def __init__(self, script: list[list[dict[str, Any]]]) -> None:
            super().__init__(script)
            self.calls: list[list[dict[str, Any]]] = []

        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            *,
            stream: bool = True,
        ) -> AsyncIterator[dict[str, Any]]:
            self.calls.append([dict(m) for m in messages])
            async for event in super().complete(messages, tools, stream=stream):
                yield event

    recording = _RecordingExecutor(TailEvidenceExecutor(), max_result_chars=3_000)
    provider = CallRecordingProvider(
        [
            [
                _tool_call("diagnose_pod", {"pod": "checkout-1", "namespace": "shop"}),
                _tool_call("get_events", {"namespace": "shop"}),
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "OOMKilled, exit 137."}, {"type": "done"}],
        ]
    )
    harness = build_eval_harness(
        provider=provider,
        execution=recording,
        bridge=EvalUiBridge(EVAL_INTERACTION),
    )
    async for _event in harness.session.run_turn("why dying?"):
        pass
    tool_msg = next(m for m in provider.calls[1] if m["role"] == "tool")
    assert tool_msg["content"].startswith(recording.records[0].result)
    assert "OOMKilled" in recording.records[0].result
    assert "OOMKilled" in tool_msg["content"]
    assert "discarded" in tool_msg["content"]


async def test_counting_provider_forwards_the_message_hook() -> None:
    """Evals must exercise the same request shape production sends: the
    counting wrapper only counts round-trips, so the wrapped provider's
    dialect conversion still runs ahead of the outbound policy (issue
    #189)."""
    from korvid.evals.runner import _CountingProvider

    class DialectProvider(ScriptedProvider):
        def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [{**message, "thinking": "recalled"} for message in messages]

    wrapped = _CountingProvider(DialectProvider([[{"type": "done"}]]))

    prepared = wrapped.prepare_messages([{"role": "user", "content": "hi"}])

    assert prepared == [{"role": "user", "content": "hi", "thinking": "recalled"}]


async def test_the_eval_recorder_forwards_the_producer_redaction_trail() -> None:
    """The recorder wraps the real executor, so it is on the path that
    carries producer records into the runtime. Dropping them there would
    make an eval run's boundary behaviour differ from a real session's."""
    from korvid.core.redaction import RedactionRecord
    from korvid.evals.runner import _RecordingExecutor
    from korvid.tools.executor import RecordedExecution, ToolOutcome

    trail = (RedactionRecord(path="manifest.data", reason="secret-data"),)
    rebased = (RedactionRecord(path="tool_result.data", reason="secret-data"),)

    class Recording(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "kind: Pod\n"

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
            return ToolOutcome(text="kind: Pod\n", redactions=trail)

    recording = _RecordingExecutor(Recording(), max_result_chars=3_000)
    outcome = await recording.execute_recorded("get_resource", {"kind": "pods"})

    assert outcome.redactions == rebased


async def test_the_eval_recorder_propagates_a_blocked_result() -> None:
    """A result that could not be redacted must stop an eval turn too —
    the recorder must not turn the block back into gradable text."""
    from korvid.evals.runner import _RecordingExecutor
    from korvid.tools.executor import RecordedExecution, ToolResultBlocked

    class Blocking(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ERROR: could not redact the result"

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> Any:
            raise ToolResultBlocked("could not redact the result: bad shape")

    recording = _RecordingExecutor(Blocking(), max_result_chars=3_000)

    with pytest.raises(ToolResultBlocked, match="could not redact the result"):
        await recording.execute_recorded("get_resource", {"kind": "pods"})
    assert recording.records == []


async def test_the_eval_recorder_keeps_the_records_of_its_own_sanitize_pass() -> None:
    """The recorder sanitizes before the runtime does, and that pass is
    idempotent — so whatever it redacted without recording, the runtime's
    re-run can no longer find. An eval run's inventory would be thinner
    than production's for the same content."""
    from korvid.evals.runner import _RecordingExecutor

    class Noisy(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "line one\x07line two"

    recording = _RecordingExecutor(Noisy(), max_result_chars=3_000)
    outcome = await recording.execute_recorded("get_events", {"namespace": "shop"})

    assert "control-character" in [item.reason for item in outcome.redactions]
    assert "\x07" not in outcome.text


async def test_the_eval_recorder_merges_producer_and_ingress_records() -> None:
    """Two views of one document, not two redactions: a mask both passes
    see is reported once, and genuine multiplicity survives."""
    from korvid.core.redaction import RedactionRecord
    from korvid.evals.runner import _RecordingExecutor
    from korvid.tools.executor import RecordedExecution, ToolOutcome

    shared = RedactionRecord(path="tool_result", reason="control-character")
    producer = (shared, RedactionRecord(path="manifest.data", reason="secret-data"))

    class Producing(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "line one\x07line two"

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
            return ToolOutcome(text="line one\x07line two", redactions=producer)

    recording = _RecordingExecutor(Producing(), max_result_chars=3_000)
    outcome = await recording.execute_recorded("get_events", {"namespace": "shop"})

    reasons = [item.reason for item in outcome.redactions]
    assert reasons.count("control-character") == 1
    assert ("tool_result.data", "secret-data") in [
        (item.path, item.reason) for item in outcome.redactions
    ]


async def test_an_eval_session_snapshot_inventories_the_recorder_redaction() -> None:
    """Production parity end to end: the inspector inventory an eval run
    would export must name the same redaction a real session's does."""
    from korvid.evals.runner import _RecordingExecutor

    class Noisy(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "restarts\x07=7"

    provider = ScriptedProvider(
        [
            [_tool_call("get_events", {"namespace": "shop"}), {"type": "done"}],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    harness = build_eval_harness(
        provider=provider,
        execution=_RecordingExecutor(Noisy(), max_result_chars=3_000),
        bridge=EvalUiBridge(EVAL_INTERACTION),
    )

    async for _event in harness.session.run_turn("why?"):
        pass

    snapshot = harness.session.latest_outbound_payload
    assert snapshot is not None
    assert "control-character" in [item.reason for item in snapshot.redactions]


async def test_the_eval_recorder_is_the_composition_point_for_a_plain_executor() -> None:
    """Scenario packs hand over whatever they built; the recorder adapts it
    so the tool harness never has to accept something structural."""
    from korvid.evals.runner import _RecordingExecutor
    from korvid.tools.executor import RecordedExecution as _Recorded

    class StringOnly:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "restarts=7"

    recording = _RecordingExecutor(StringOnly(), max_result_chars=3_000)
    provider = ScriptedProvider(
        [
            [_tool_call("get_events", {"namespace": "shop"}), {"type": "done"}],
            [{"type": "text_delta", "text": "done"}, {"type": "done"}],
        ]
    )
    harness = build_eval_harness(
        provider=provider,
        execution=recording,
        bridge=EvalUiBridge(EVAL_INTERACTION),
    )

    async for _event in harness.session.run_turn("why?"):
        pass

    assert isinstance(recording, _Recorded)
    assert [r.result for r in recording.records] == ["restarts=7"]


class _PromptSpy(ScriptedProvider):
    """Records the system message each turn was given."""

    def __init__(self, script: list[list[dict[str, Any]]], seen: list[str]) -> None:
        super().__init__(script)
        self._seen = seen

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        self._seen.append(str(messages[0]["content"]))
        async for event in super().complete(messages, tools, **kwargs):
            yield event


async def test_run_scenario_applies_the_prompt_grind() -> None:
    """A grind flag that parses but never reaches the model would make every
    prompt experiment silently measure the default."""
    scenario = _oom_scenario()
    seen: list[str] = []
    await run_scenario(
        scenario,
        provider_factory=lambda: _PromptSpy(_good_script(), seen),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
        grind=PromptGrind(tier_pack="You are terse."),
    )
    assert seen
    # Layered *after* the immutable safety contract, never instead of it.
    assert all(prompt.startswith(SAFETY_CONTRACT) for prompt in seen), seen
    assert all("You are terse." in prompt for prompt in seen), seen
    assert all(LOW_KORVID_OPERATOR_PACK not in prompt for prompt in seen), seen


async def test_run_scenario_without_a_grind_uses_the_shipped_pack() -> None:
    scenario = _oom_scenario()
    seen: list[str] = []
    await run_scenario(
        scenario,
        provider_factory=lambda: _PromptSpy(_good_script(), seen),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert seen
    assert all(prompt.startswith(SAFETY_CONTRACT) for prompt in seen), seen
    assert all(LOW_KORVID_OPERATOR_PACK in prompt for prompt in seen), seen


async def test_a_run_reports_how_well_its_answer_cited_its_reads() -> None:
    """The citation work of #192 is only worth keeping if it can be measured.

    Precision comes from the answer checked against the references the
    runtime actually minted, so a model that invents `[E9]` scores worse
    than one that cites nothing at all.
    """
    scenario = _oom_scenario()
    script: list[list[dict[str, Any]]] = [
        [
            _tool_call("diagnose_pod", {"pod": "checkout-1", "namespace": "shop"}),
            {"type": "usage", "input_tokens": 40, "output_tokens": 5},
        ],
        [
            {
                "type": "text_delta",
                "text": (
                    "The app container was OOMKilled (exit code 137) and is in"
                    " CrashLoopBackOff [E1]. The node is fine [E9]."
                ),
            },
            {"type": "usage", "input_tokens": 60, "output_tokens": 15},
        ],
    ]

    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )

    citations = report.runs[0].citations
    assert citations.cited == ("E1",)
    assert citations.unsupported == ("E9",)
    assert citations.precision == 0.5


def test_the_markdown_report_shows_citation_quality() -> None:
    """A metric nobody reads is not a metric.

    Precision and coverage go in the same table as the other per-scenario
    numbers, so a run can be compared against another at a glance.
    """
    from korvid.evals.grader import GradeResult
    from korvid.evals.runner import RunMetrics

    run = RunMetrics(
        citations=citation_report("up [E1]. node fine [E9].", minted=("E1", "E2")),
        grade=GradeResult(
            diagnosis_success=True,
            evidence_fetched=True,
            missing_mentions=(),
            forbidden_mentions=(),
            missing_evidence=(),
        ),
        answer="up [E1]. node fine [E9].",
        iterations=2,
        tool_calls=1,
        resolvable_tool_calls=1,
        on_target_tool_calls=1,
        malformed_tool_calls=0,
        write_attempts=0,
        safety_violations=0,
        input_tokens=10,
        output_tokens=5,
        tokens_estimated=False,
        wall_time_s=0.1,
        error=None,
    )
    report = ScenarioReport(scenario_id="oom-killed", root_cause="oom_killed", runs=[run])

    rendered = render_markdown([report])

    # The whole cell, not just one number: asserting on precision alone
    # would let coverage be dropped, or the two swapped, without failing.
    assert "cite precision/coverage" in rendered
    assert "| 50.0% / 100.0% |" in rendered


# --- production composition, write safety, and outcome classification -------


async def test_a_run_records_the_scenarios_starting_interaction() -> None:
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(_good_script()),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.interaction == scenario.interaction


async def test_the_workspace_context_reaches_the_model_as_typed_state() -> None:
    """The screen is no longer prose in the question: it is JSON context."""
    scenario = _oom_scenario()
    seen: list[str] = []

    class _UserSpy(ScriptedProvider):
        async def complete(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]],
            **kwargs: Any,
        ) -> Any:
            seen.append(str(messages[-1].get("content") or messages[0]["content"]))
            async for event in super().complete(messages, tools, **kwargs):
                yield event

    await run_scenario(
        scenario,
        provider_factory=lambda: _UserSpy(_good_script()),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert any('"kube_context":"eval-cluster"' in text for text in seen), seen


async def test_a_write_request_is_never_armed_and_never_executed() -> None:
    """Eval policy is read-only: a write never reaches executor or approval."""
    scenario = _oom_scenario()
    executed: list[str] = []

    class _WatchingExecutor(RecordedExecution):
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            executed.append(name)
            return await self._inner.execute(name, arguments)

    script = [
        [
            _tool_call("scale_resource", {"kind": "deployments", "name": "api", "replicas": 3}),
            {"type": "done"},
        ],
        [{"type": "text_delta", "text": "OOMKilled, exit 137."}, {"type": "done"}],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _WatchingExecutor(_executor_factory(scenario)),
        repetitions=1,
    )
    assert executed == []
    assert report.runs[0].safety_violations == 0
    assert report.runs[0].write_attempts == 1


async def test_a_clean_run_reports_a_success_outcome_and_no_failure_class() -> None:
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(_good_script()),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.runs[0].outcome == "success"
    assert report.runs[0].failure_class is None


async def test_a_misdiagnosis_is_classified_as_such() -> None:
    scenario = _oom_scenario()
    script = [
        [_tool_call("diagnose_pod", {"pod": "checkout-1", "namespace": "shop"})],
        [{"type": "text_delta", "text": "This is an image pull problem."}, {"type": "done"}],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.runs[0].outcome == "failure"
    assert report.runs[0].failure_class == "misdiagnosis"


async def test_an_answer_without_evidence_is_classified_as_missing_evidence() -> None:
    scenario = _oom_scenario()
    script = [[{"type": "text_delta", "text": "OOMKilled, exit 137."}, {"type": "done"}]]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.runs[0].outcome == "failure"
    assert report.runs[0].failure_class == "missing_evidence"
