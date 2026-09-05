"""Tests for the eval runner: recording, metrics, and the smoke path."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.grader import CitationReport, citation_report
from korvid.evals.harness import build_eval_harness
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


def _executor_factory(scenario: Scenario) -> RecordedExecution:
    from korvid.tools.executor import ToolExecutor

    return ToolExecutor(FakeKubeClient(scenario), builtin_aliases())


class _ToolProbeProvider(ScriptedProvider):
    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__(script)
        self.offered: list[str] = []
        self.tools: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        self.offered.extend(tool["function"]["name"] for tool in tools)
        if not self.tools:
            self.tools = list(tools)
        return super().complete(messages, tools, stream=stream)


class _PermissiveExecutor(RecordedExecution):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(name)
        return f"done: {name}"


class _AnswerThenRaiseProvider(ScriptedProvider):
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
            yield  # pragma: no cover


class _ClosableProvider(ScriptedProvider):
    def __init__(self, script: list[list[dict[str, Any]]], closed: list[bool]) -> None:
        super().__init__(script)
        self._closed = closed

    async def aclose(self) -> None:
        self._closed.append(True)


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
    assert report.interaction == scenario.interaction
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
        assert run.outcome == "success"
        assert run.failure_class is None


async def test_run_scenario_closes_the_provider_after_every_repetition() -> None:
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


async def test_run_scenario_grades_a_wrong_answer_as_failure() -> None:
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(
            [[{"type": "text_delta", "text": "This is an image pull problem."}]]
        ),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )

    run = report.runs[0]
    assert not run.grade.diagnosis_success
    assert "image pull" in run.grade.forbidden_mentions
    assert not run.grade.evidence_fetched
    assert run.outcome == "failure"


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


async def test_run_scenario_counts_malformed_tool_calls() -> None:
    scenario = _oom_scenario()
    script = [
        [
            {"type": "tool_call", "id": "c1", "name": "diagnose_pod", "arguments": "{not json"},
            {"type": "tool_call", "id": "c2", "name": "no_such_tool", "arguments": "{}"},
            {"type": "tool_call", "id": "c3", "name": "diagnose_pod", "arguments": "[]"},
            {"type": "tool_call", "id": "c4", "name": "diagnose_pod", "arguments": '{"pod": "x"}'},
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


async def test_no_write_schema_is_ever_offered_to_an_eval_model() -> None:
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


async def test_a_write_call_never_reaches_even_a_permissive_executor() -> None:
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
    assert run.write_attempts == 1
    assert run.safety_violations == 0
    assert run.resolvable_tool_calls == 0
    assert run.on_target_tool_calls == 0


async def test_errored_run_still_reports_tokens_spent_before_the_failure() -> None:
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

    run = RunMetrics(
        citations=_no_citations(),
        grade=GradeResult(True, True, (), (), ()),
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
    assert "2/8 (25.0%)" in text
    assert "resolvable calls" in text
    assert "6/8 (75.0%)" in text
    assert "on-target" in text
    assert "4/8 (50.0%)" in text
    assert "| writes |" in text
    assert "2.0±0.0" in text
    assert "100.0±0.0/20.0±0.0" in text
    assert "~100.0" not in text


async def test_resolvable_tool_calls_require_a_resolvable_target() -> None:
    scenario = _oom_scenario()
    script = [
        [_tool_call("diagnose_pod", {"pod": "ghost", "namespace": "shop"})],
        [{"type": "text_delta", "text": "OOMKilled, exit code 137, CrashLoopBackOff."}],
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
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(
            [[{"type": "text_delta", "text": "OOMKilled, exit 137."}]]
        ),
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
    assert "~0.0/5.0" in render_markdown([report])


async def test_on_target_tool_calls_credit_any_alternative_in_an_evidence_group() -> None:
    """`on_target` is an any-of over the group, like the grader's own
    evidence check but a second, independent implementation.

    Sixteen shipped groups list two or more routes to the same fact. An
    all-of here would report a correct read as off-target and publish a
    tool-use regression the model never had.
    """
    base = _oom_scenario()
    scenario = replace(
        base,
        expected_evidence=(
            (
                # Two different objects, either of which proves the fact —
                # the shape sixteen shipped groups use (endpoints vs
                # endpointslices, pod vs owning workload).
                Evidence(
                    tool="get_resource",
                    contains="Ready",
                    args={"kind": "nodes", "name": "node-a"},
                ),
                Evidence(
                    tool="diagnose_pod",
                    contains="exit=137",
                    args={"pod": "checkout-1", "namespace": "shop"},
                ),
            ),
        ),
    )
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(_good_script()),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    run = report.runs[0]
    assert run.tool_calls == 1
    assert run.on_target_tool_calls == 1
    assert run.grade.evidence_fetched


async def test_on_target_tool_calls_require_matching_evidence_arguments() -> None:
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


async def test_the_low_tier_arms_exactly_the_low_surface() -> None:
    scenario = _oom_scenario()
    provider = _ToolProbeProvider(_good_script())
    await run_scenario(
        scenario,
        provider_factory=lambda: provider,
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )

    names = [tool["function"]["name"] for tool in provider.tools]
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
    provider = _ToolProbeProvider(_good_script())
    await run_scenario(
        scenario,
        provider_factory=lambda: provider,
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
        model_tier="high",
    )

    names = [tool["function"]["name"] for tool in provider.tools]
    assert "navigate" in names
    assert "drill_down" in names
    assert "delete_resource" not in names


async def test_the_resolved_tier_budget_binds_the_iteration_count() -> None:
    scenario = _oom_scenario()
    loop = [
        [
            {
                "type": "tool_call",
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
    high = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
        model_tier="high",
    )

    assert low.runs[0].iterations <= 6
    assert 6 < high.runs[0].iterations <= 15


async def test_evidence_grading_sees_only_the_model_visible_capped_result() -> None:
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


async def test_discard_notice_keeps_tail_evidence_within_the_result_budget() -> None:
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
            self.calls.append([dict(message) for message in messages])
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

    tool_msg = next(message for message in provider.calls[1] if message["role"] == "tool")
    assert len(tool_msg["content"]) <= 3_000
    assert "OOMKilled" in recording.records[0].result
    assert "OOMKilled" in tool_msg["content"]
    assert "discarded" in tool_msg["content"]


async def test_the_eval_recorder_forwards_the_producer_redaction_trail() -> None:
    from korvid.core.redaction import RedactionRecord
    from korvid.evals.runner import _RecordingExecutor
    from korvid.tools.executor import ToolOutcome

    trail = (RedactionRecord(path="manifest.data", reason="secret-data"),)

    class Recording(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "kind: Pod\n"

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
            return ToolOutcome(text="kind: Pod\n", redactions=trail)

    recording = _RecordingExecutor(Recording(), max_result_chars=3_000)
    outcome = await recording.execute_recorded("get_resource", {"kind": "pods"})

    assert outcome.redactions == (RedactionRecord(path="tool_result.data", reason="secret-data"),)


async def test_the_eval_recorder_propagates_a_blocked_result() -> None:
    from korvid.evals.runner import _RecordingExecutor
    from korvid.tools.executor import ToolResultBlocked

    class Blocking(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ERROR: could not redact the result"

        async def execute_recorded(self, name: str, arguments: dict[str, Any]) -> Any:
            raise ToolResultBlocked("could not redact the result: bad shape")

    recording = _RecordingExecutor(Blocking(), max_result_chars=3_000)

    with pytest.raises(ToolResultBlocked, match="could not redact the result"):
        await recording.execute_recorded("get_resource", {"kind": "pods"})
    assert recording.records == []


async def test_the_eval_recorder_merges_producer_and_ingress_records() -> None:
    from korvid.core.redaction import RedactionRecord
    from korvid.evals.runner import _RecordingExecutor
    from korvid.tools.executor import ToolOutcome

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


async def test_a_run_reports_how_well_its_answer_cited_its_reads() -> None:
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

    assert "cite precision/coverage" in rendered
    assert "| 50.0% / 100.0% |" in rendered


async def test_an_answer_without_evidence_is_classified_as_missing_evidence() -> None:
    scenario = _oom_scenario()
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(
            [[{"type": "text_delta", "text": "OOMKilled, exit 137."}, {"type": "done"}]]
        ),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )

    run = report.runs[0]
    assert run.grade.diagnosis_success is True
    assert run.outcome == "failure"
    assert run.failure_class == "missing_evidence"


def test_the_scenario_markdown_column_says_which_success_it_counts() -> None:
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
        input_tokens=1,
        output_tokens=1,
        tokens_estimated=False,
        wall_time_s=0.1,
        error=None,
    )
    report = ScenarioReport(scenario_id="oom-killed", root_cause="oom_killed", runs=[run])
    header = render_markdown([report]).splitlines()[0]
    cells = [cell.strip() for cell in header.strip("|").split("|")]

    assert "correct diagnosis" in cells
    assert "success" not in cells
