"""The source-checkout campaign entry point."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from korvid.evals.operation import bundled_operations_dir, load_operation_journeys

from . import operation_campaign
from .operation_app import MIN_APPROVAL_TIMEOUT
from .operation_campaign import _korvid_revision, _seeds, approval_timeout_for, main

_JOURNEYS = {journey.id: journey for journey in load_operation_journeys(bundled_operations_dir())}


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
