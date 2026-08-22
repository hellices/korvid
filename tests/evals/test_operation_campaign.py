"""The source-checkout campaign entry point."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from korvid.evals.operation import bundled_operations_dir, load_operation_journeys
from korvid.evals.operation_grader import OperationGrade, StateAssertionResult

from . import operation_campaign
from .operation_app import MIN_APPROVAL_TIMEOUT, OperationRun, run_operation_journey
from .operation_campaign import _korvid_revision, _record, _seeds, approval_timeout_for, main

_JOURNEYS = {journey.id: journey for journey in load_operation_journeys(bundled_operations_dir())}


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
    assert meta["profile"] == "small"
    assert meta["mode"] == "scripted"
    assert meta["repetitions"] == 1
    assert set(meta["prompts"]) == {"source", "sha256"}
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


def test_campaign_artifacts_are_written_as_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_write_text = Path.write_text
    encodings: dict[str, str | None] = {}

    def checked_write_text(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.suffix in {".json", ".md"}:
            encodings[path.suffix] = encoding
        return original_write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", checked_write_text)
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "1",
            "--json",
            str(tmp_path / "operations.json"),
            "--out",
            str(tmp_path / "operations.md"),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    assert code == 0
    assert encodings == {".md": "utf-8", ".json": "utf-8"}


@pytest.mark.parametrize(
    ("flag", "filename"),
    [
        pytest.param("--out", "operations.md", id="markdown"),
        pytest.param("--json", "operations.json", id="json"),
    ],
)
def test_campaign_reports_result_artifact_write_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    filename: str,
) -> None:
    target = tmp_path / filename
    original_write_text = Path.write_text

    def fail_target_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path == target:
            raise PermissionError("read-only filesystem")
        return original_write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", fail_target_write)

    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "1",
            flag,
            str(target),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out.startswith("| journey |")
    assert f"error: could not write {target}: read-only filesystem" in captured.err
    assert "Traceback" not in captured.err


def test_revision_is_captured_before_artifact_directory_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifacts = tmp_path / "artifacts"
    observed: list[bool] = []

    def fake_revision() -> str:
        observed.append(artifacts.exists())
        return "revision"

    monkeypatch.setattr(operation_campaign, "_korvid_revision", fake_revision)
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "1",
            "--artifacts",
            str(artifacts),
        ]
    )
    assert code == 0
    assert observed == [False]


def test_a_live_campaign_records_the_serving_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serving = {"model": "operator-model", "unavailable": []}
    calls: list[str] = []

    async def fake_capture_serving(
        base_url: str,
        model: str,
        **_kwargs: object,
    ) -> dict[str, Any]:
        calls.append("capture")
        assert (base_url, model) == ("https://models.example/v1", "operator-model")
        return serving

    async def fake_run(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        calls.append("run")
        return []

    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "operator-model")
    monkeypatch.setattr(
        operation_campaign,
        "capture_serving",
        fake_capture_serving,
        raising=False,
    )
    monkeypatch.setattr(operation_campaign, "_run", fake_run)
    payload_path = tmp_path / "operations.json"

    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--reps",
            "1",
            "--json",
            str(payload_path),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )

    assert code == 0
    assert calls == ["capture", "run"]
    assert json.loads(payload_path.read_text())["meta"]["serving"] == serving


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        pytest.param({}, "needs a live model endpoint", id="missing-endpoint"),
        pytest.param(
            {
                "KORVID_EVAL_BASE_URL": "https://models.example/v1",
                "KORVID_EVAL_MODEL": "operator-model",
                "KORVID_EVAL_TIMEOUT_SECONDS": "invalid",
            },
            "KORVID_EVAL_TIMEOUT_SECONDS must be a positive number",
            id="invalid-timeout",
        ),
    ],
)
def test_live_provider_configuration_is_rejected_before_artifact_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
    message: str,
) -> None:
    captures = 0

    async def fake_capture(*_args: object, **_kwargs: object) -> dict[str, Any]:
        nonlocal captures
        captures += 1
        return {"model": "operator-model", "unavailable": []}

    for name in (
        "KORVID_EVAL_BASE_URL",
        "KORVID_EVAL_MODEL",
        "KORVID_EVAL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(operation_campaign, "capture_serving", fake_capture)
    artifacts = tmp_path / "artifacts"

    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--reps",
            "1",
            "--artifacts",
            str(artifacts),
        ]
    )

    assert code == 2
    assert captures == 0
    assert not artifacts.exists()
    stderr = capsys.readouterr().err
    assert stderr.startswith("error: ")
    assert message in stderr
    assert "Traceback" not in stderr


def test_seeded_generation_is_rejected_in_scripted_mode(tmp_path: Path) -> None:
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--seeds",
            "1,2",
            "--json",
            str(tmp_path / "out.json"),
        ]
    )
    assert code == 2


def test_an_empty_operation_pack_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    operations = tmp_path / "operations"
    operations.mkdir()
    code = main(["--operations", str(operations), "--scripted"])
    assert code == 2
    assert "operation pack must contain at least one journey" in capsys.readouterr().err


def test_duplicate_generation_seeds_are_rejected() -> None:
    with pytest.raises(ValueError, match="--seeds must not contain duplicates"):
        _seeds("7,7")


def test_source_campaign_revision_identifies_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KORVID_EVAL_REVISION", raising=False)
    assert re.fullmatch(r"[0-9a-f]{40}(?:\+dirty)?", _korvid_revision())


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


def test_live_campaign_returns_nonzero_for_infrastructure_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_run(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        return [{"error": "RuntimeError: provider disconnected"}]

    async def fake_capture(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"model": "operator-model", "unavailable": []}

    monkeypatch.setenv("KORVID_EVAL_BASE_URL", "https://models.example/v1")
    monkeypatch.setenv("KORVID_EVAL_MODEL", "operator-model")
    monkeypatch.setattr(operation_campaign, "_run", fake_run)
    monkeypatch.setattr(operation_campaign, "capture_serving", fake_capture)
    monkeypatch.setattr(operation_campaign, "render_markdown", lambda _records: "")
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--reps",
            "1",
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    assert code == 1


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


def test_an_unknown_journey_id_is_reported(tmp_path: Path) -> None:
    code = main(["--only", "no-such-journey", "--scripted", "--json", str(tmp_path / "out.json")])
    assert code == 2


def test_a_custom_fixture_pack_without_a_script_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    operations_dir = tmp_path / "operations"
    operations_dir.mkdir()
    custom_journey_id = "custom-scale-deployment-up"
    source = bundled_operations_dir() / "scale-deployment-up.yaml"
    (operations_dir / f"{custom_journey_id}.yaml").write_text(
        source.read_text().replace("id: scale-deployment-up", f"id: {custom_journey_id}", 1)
    )
    payload_path = tmp_path / "out.json"
    artifacts_dir = tmp_path / "artifacts"

    code = main(
        [
            "--operations",
            str(operations_dir),
            "--only",
            custom_journey_id,
            "--scripted",
            "--json",
            str(payload_path),
            "--artifacts",
            str(artifacts_dir),
        ]
    )

    assert code == 2
    stderr = capsys.readouterr().err
    assert "error: scripted mode requires OPERATION_SCRIPTS entries for:" in stderr
    assert custom_journey_id in stderr
    assert "Traceback" not in stderr
    assert not payload_path.exists()
    assert not artifacts_dir.exists()


def test_a_non_positive_repetition_count_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "0",
            "--json",
            str(tmp_path / "out.json"),
        ]
    )
    assert code == 2
    stderr = capsys.readouterr().err
    assert "error: --reps must be >= 1" in stderr
    assert "Traceback" not in stderr


def test_a_malformed_seed_list_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--seeds",
            "1,two",
            "--json",
            str(tmp_path / "out.json"),
        ]
    )
    assert code == 2
    stderr = capsys.readouterr().err
    assert "error: --seeds must be a comma-separated list of integers" in stderr
    assert "Traceback" not in stderr


def test_a_sub_second_approval_timeout_is_a_usage_error(tmp_path: Path) -> None:
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--approval-timeout",
            "0.5",
            "--json",
            str(tmp_path / "out.json"),
        ]
    )
    assert code == 2


@pytest.mark.parametrize("timeout", ["nan", "inf"])
def test_a_non_finite_approval_timeout_is_a_usage_error(tmp_path: Path, timeout: str) -> None:
    artifacts = tmp_path / "artifacts"
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--approval-timeout",
            timeout,
            "--artifacts",
            str(artifacts),
        ]
    )
    assert code == 2
    assert not artifacts.exists()


def test_an_expiry_journey_uses_the_shortest_supported_window() -> None:
    """The expiry fixture waits out its whole window, so the default would
    burn `--approval-timeout` seconds per repetition to prove nothing extra."""

    assert approval_timeout_for(_JOURNEYS["restart-approval-expired"], 5.0) == pytest.approx(
        MIN_APPROVAL_TIMEOUT
    )
    assert approval_timeout_for(_JOURNEYS["scale-deployment-up"], 5.0) == pytest.approx(5.0)
    assert approval_timeout_for(
        _JOURNEYS["restart-approval-expired"], MIN_APPROVAL_TIMEOUT
    ) == pytest.approx(MIN_APPROVAL_TIMEOUT)


def test_the_campaign_runs_the_replacement_journey_it_could_not_run_before(
    tmp_path: Path,
) -> None:
    """The journey whose mid-dialog swap used to live in a pytest hook now
    runs unchanged from the campaign entry point: same fixture, same
    driver, same conflict, and nothing mutated. (Step 9 runs all twelve
    from the command line; this pins the one that motivated the field.)"""

    payload_path = tmp_path / "replacement.json"
    code = main(
        [
            "--only",
            "scale-same-name-replacement",
            "--scripted",
            "--reps",
            "1",
            "--json",
            str(payload_path),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    assert code == 0
    run = json.loads(payload_path.read_text())["runs"][0]
    assert run["safe"] is True
    assert run["outcome"] == "failed"
    journal = run["journal"]
    assert [entry for entry in journal if entry["event"] == "target_replaced"] != []
    assert [entry for entry in journal if entry["event"] == "uid_conflict"] != []
    assert [entry for entry in journal if entry["event"] == "mutation_started"] == []
