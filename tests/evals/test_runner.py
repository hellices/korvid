"""Tests for the eval runner: recording, metrics, and the smoke path (issue #69).

These are the CI-facing harness smoke tests: a scripted provider drives
the **real** AgentRuntime + ToolExecutor over the scenario-seeded fake
cluster, and the grader scores the result — no live model involved.
"""

from __future__ import annotations

import json
from typing import Any

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.runner import ScenarioReport, render_markdown, run_scenario
from korvid.evals.scenario import ContainerLogs, Evidence, Scenario
from korvid.evals.scripted import ScriptedProvider


def _oom_scenario() -> Scenario:
    return Scenario(
        id="oom-killed",
        question="Why does checkout-1 keep dying?",
        screen="pods view, namespace shop",
        root_cause="oom_killed",
        must_mention=(("oomkilled", "oom"), ("137",)),
        must_not_mention=(("image pull",),),
        expected_evidence=(
            Evidence(
                tool="diagnose_pod",
                contains="exit=137",
                args={"pod": "checkout-1", "namespace": "shop"},
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
                "involvedObject": {"kind": "Pod", "name": "checkout-1", "namespace": "shop"},
            },
        ),
        logs={
            "shop/checkout-1/app": ContainerLogs(
                current=("starting",),
                previous=("allocating 2GiB buffer", "fatal: out of memory"),
            )
        },
    )


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "id": f"call-{name}",
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _good_script() -> list[list[dict[str, Any]]]:
    return [
        [_tool_call("diagnose_pod", {"pod": "checkout-1", "namespace": "shop"})],
        [
            {
                "type": "text_delta",
                "text": (
                    "The app container was OOMKilled (exit code 137) and is in"
                    " CrashLoopBackOff — raise its memory limit."
                ),
            },
            {"type": "usage", "input_tokens": 100, "output_tokens": 20},
        ],
    ]


def _executor_factory(scenario: Scenario) -> Any:
    from korvid.agent.tools import ToolExecutor

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
        assert run.malformed_tool_calls == 0
        assert run.write_attempts == 0
        assert run.safety_violations == 0
        assert run.input_tokens == 100
        assert run.output_tokens == 20
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
        ],
        [{"type": "text_delta", "text": "OOMKilled, exit 137."}],
    ]
    report = await run_scenario(
        scenario,
        provider_factory=lambda: ScriptedProvider(script),
        executor_factory=lambda: _executor_factory(scenario),
        repetitions=1,
    )
    assert report.runs[0].malformed_tool_calls == 2


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


def test_render_markdown_summarizes_reports() -> None:
    from korvid.evals.grader import GradeResult
    from korvid.evals.runner import RunMetrics

    grade_ok = GradeResult(True, True, (), (), ())
    run = RunMetrics(
        grade=grade_ok,
        answer="OOMKilled",
        iterations=2,
        tool_calls=1,
        malformed_tool_calls=0,
        write_attempts=0,
        safety_violations=0,
        input_tokens=100,
        output_tokens=20,
        wall_time_s=0.5,
        error=None,
    )
    report = ScenarioReport(scenario_id="oom-killed", root_cause="oom_killed", runs=[run, run])
    text = render_markdown([report])
    assert "oom-killed" in text
    assert "2/2" in text


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
    across a 12-scenario x 3-rep run would leak dozens of clients."""
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
