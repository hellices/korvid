"""The source-checkout campaign entry point."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from korvid.evals.operation import bundled_operations_dir, load_operation_journeys

from .operation_app import MIN_APPROVAL_TIMEOUT
from .operation_campaign import approval_timeout_for, main

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
    assert meta["schema_version"] == 1
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


def test_an_unknown_journey_id_is_reported(tmp_path: Path) -> None:
    code = main(["--only", "no-such-journey", "--scripted", "--json", str(tmp_path / "out.json")])
    assert code == 2


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
