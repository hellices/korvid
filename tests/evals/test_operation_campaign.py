"""The source-checkout campaign entry point."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from korvid.evals.operation_grader import OperationGrade, StateAssertionResult

from . import operation_campaign
from .operation_app import OperationRun, run_operation_journey
from .operation_campaign import _korvid_revision, _record, main


@pytest.mark.parametrize(
    ("field", "provisional"),
    [
        pytest.param("provisional_assertions", True, id="provisional"),
        pytest.param("scored_assertions", False, id="scored"),
    ],
)
def test_record_omits_observed_state_from_assertion_artifacts(
    tmp_path: Path, field: str, provisional: bool
) -> None:
    sentinel = "LEAK-SENTINEL"
    result = StateAssertionResult(
        path="data",
        operator="exists",
        expected=None,
        observed={"password": sentinel},
        found=True,
        satisfied=True,
        provisional=provisional,
    )
    grade = OperationGrade(
        journey_id="secret-check",
        safe=True,
        hard_failures=(),
        checkpoints=(),
        missing_checkpoints=(),
        outcome="completed",
        truthful=True,
        completion=True,
        verification=True,
        request_match=True,
        efficiency=1.0,
        quality=1.0,
        scored_assertions=() if provisional else (result,),
        provisional_assertions=(result,) if provisional else (),
        tool_calls=1,
        iterations=1,
    )
    run = OperationRun(
        journey_id="secret-check",
        answer="done",
        grade=grade,
        journal=(),
        audit=(),
        wall_time_s=0.1,
    )

    record = _record(
        run,
        "secret-check",
        None,
        1,
        audit_path=tmp_path / "audit.jsonl",
        run_id="run-test",
    )

    assertion = record[field][0]
    assert "observed" not in assertion
    assert sentinel not in json.dumps(record)


def test_a_scripted_campaign_writes_a_provenance_stamped_artifact(tmp_path: Path) -> None:
    payload_path = tmp_path / "operations.json"
    markdown_path = tmp_path / "operations.md"
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "1",
            "--json",
            str(payload_path),
            "--out",
            str(markdown_path),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    assert code == 0
    payload = json.loads(payload_path.read_text())
    meta = payload["meta"]
    assert meta["schema_version"] == 2
    assert meta["model_tier"] == "low"
    assert meta["mode"] == "scripted"
    assert meta["repetitions"] == 1
    assert set(meta["prompts"]) == {"pack", "overlays", "source", "sha256"}
    assert meta["korvid_revision"]
    run = payload["runs"][0]
    assert run["template_id"] == "scale-deployment-up"
    assert run["instance_id"] == "scale-deployment-up"
    assert run["seed"] is None
    assert run["safe"] is True
    assert run["quality"] == 1.0
    assert run["journal"]
    assert run["audit"]
    assert markdown_path.read_text().startswith("| journey |")


def test_untracked_eval_inputs_mark_the_campaign_revision_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KORVID_EVAL_REVISION", raising=False)

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[1:3] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(stdout="a" * 40 + "\n")
        stdout = "" if "--untracked-files=no" in command else "?? tests/evals/new_fixture.yaml\n"
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _korvid_revision() == f"{'a' * 40}+dirty"


def test_campaign_preserves_an_error_record_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = operation_campaign._parse_args(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "2",
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    _seeds_value, pairs = operation_campaign._validated_inputs(args)
    original = run_operation_journey
    attempts = 0

    async def flaky_run(*run_args: Any, **run_kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("provider disconnected")
        return await original(*run_args, **run_kwargs)

    monkeypatch.setattr(operation_campaign, "run_operation_journey", flaky_run)
    records = asyncio.run(
        operation_campaign._run(
            args,
            pairs,
            run_id="run-test",
            run_dir=tmp_path,
        )
    )
    assert len(records) == 2
    assert records[0]["error"] == "RuntimeError: provider disconnected"
    assert records[0]["safe"] is False
    assert records[1]["error"] is None


def test_reusing_an_artifact_base_creates_a_new_run_directory_each_time(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    first_payload_path = tmp_path / "first.json"
    second_payload_path = tmp_path / "second.json"

    first = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "1",
            "--json",
            str(first_payload_path),
            "--artifacts",
            str(artifacts),
        ]
    )
    second = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "1",
            "--json",
            str(second_payload_path),
            "--artifacts",
            str(artifacts),
        ]
    )

    assert first == 0
    assert second == 0

    first_payload = json.loads(first_payload_path.read_text())
    second_payload = json.loads(second_payload_path.read_text())
    first_meta = first_payload["meta"]
    second_meta = second_payload["meta"]
    assert first_meta["run_id"] != second_meta["run_id"]

    first_dir = Path(first_meta["artifact_dir"])
    second_dir = Path(second_meta["artifact_dir"])
    assert first_dir == artifacts / first_meta["run_id"]
    assert second_dir == artifacts / second_meta["run_id"]

    first_audit = next(first_dir.glob("*-audit.jsonl"))
    second_audit = next(second_dir.glob("*-audit.jsonl"))
    assert len(first_audit.read_text().splitlines()) == 2
    assert len(second_audit.read_text().splitlines()) == 2

    first_counts = [
        entry["detail"]
        for entry in first_payload["runs"][0]["journal"]
        if entry["event"] == "audit_intent_observed"
    ]
    second_counts = [
        entry["detail"]
        for entry in second_payload["runs"][0]["journal"]
        if entry["event"] == "audit_intent_observed"
    ]
    assert first_counts == ["action=scale context=eval count=1"]
    assert second_counts == ["action=scale context=eval count=1"]
