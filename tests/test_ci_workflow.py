"""Executable contracts for pull-request isolation and bounded CI jobs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CODEQL_WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
TRUSTED_LINUX_RUNNER = (
    "${{ github.event_name == 'pull_request' && 'ubuntu-latest' || 'korvid-runners' }}"
)
WINDOWS_SEED = "${{ github.run_id }}"
CI_JOB_TIMEOUTS = {
    "changes": 10,
    "test": 45,
    "windows-test": 45,
    "pre-commit": 20,
    "security": 15,
    "dependency-review": 10,
    "ty-experimental": 15,
}
CODEQL_JOB_TIMEOUTS = {"analyze": 20}


def _jobs(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = document.get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _run_steps(job: Mapping[str, Any]) -> list[str]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    return [str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step]


def _assert_job_timeouts(
    label: str,
    jobs: Mapping[str, Any],
    expected: Mapping[str, int],
) -> None:
    assert set(jobs) == set(expected), f"unreviewed {label} job set: {sorted(jobs)}"
    assert {name: jobs[name].get("timeout-minutes") for name in expected} == expected


def test_ci_jobs_have_explicit_bounded_timeouts() -> None:
    _assert_job_timeouts("CI", _jobs(CI_WORKFLOW), CI_JOB_TIMEOUTS)
    _assert_job_timeouts("CodeQL", _jobs(CODEQL_WORKFLOW), CODEQL_JOB_TIMEOUTS)


@pytest.mark.parametrize(
    ("label", "workflow", "expected"),
    [
        ("CI", CI_WORKFLOW, CI_JOB_TIMEOUTS),
        ("CodeQL", CODEQL_WORKFLOW, CODEQL_JOB_TIMEOUTS),
    ],
)
def test_timeout_contract_rejects_an_unreviewed_unsafe_job(
    label: str,
    workflow: Path,
    expected: Mapping[str, int],
) -> None:
    jobs = _jobs(workflow)
    jobs["unsafe-extra-job"] = {
        "runs-on": "korvid-runners",
        "steps": [{"run": "python -m untrusted_source"}],
    }

    with pytest.raises(AssertionError, match=r"unreviewed .* job"):
        _assert_job_timeouts(label, jobs, expected)


def test_pull_request_jobs_never_use_self_hosted_runners() -> None:
    ci_jobs = _jobs(CI_WORKFLOW)
    event_sensitive_jobs = ("test", "pre-commit", "security", "ty-experimental")

    assert ci_jobs["changes"]["runs-on"] == "ubuntu-latest"
    assert ci_jobs["windows-test"]["runs-on"] == "windows-latest"
    assert ci_jobs["dependency-review"]["runs-on"] == "ubuntu-latest"
    assert {name: ci_jobs[name]["runs-on"] for name in event_sensitive_jobs} == dict.fromkeys(
        event_sensitive_jobs, TRUSTED_LINUX_RUNNER
    )
    assert _jobs(CODEQL_WORKFLOW)["analyze"]["runs-on"] == TRUSTED_LINUX_RUNNER


def test_windows_pytest_processes_print_and_share_one_deterministic_seed() -> None:
    windows_job = _jobs(CI_WORKFLOW)["windows-test"]
    runs = _run_steps(windows_job)
    seed_messages = [run for run in runs if "pytest-randomly seed:" in run]
    seeded_runs = [run for run in runs if "--randomly-seed=" in run]

    assert seed_messages == [f'Write-Output "pytest-randomly seed: {WINDOWS_SEED}"']
    assert len(seeded_runs) == 2
    assert all(run.endswith(f" --randomly-seed={WINDOWS_SEED}") for run in seeded_runs)
    assert sum(run.count("--randomly-seed=") for run in runs) == 2


def test_experimental_ty_job_is_honestly_advisory() -> None:
    job = _jobs(CI_WORKFLOW)["ty-experimental"]
    runs = _run_steps(job)
    steps = job.get("steps")
    assert isinstance(steps, list)
    run_steps = [step for step in steps if isinstance(step, dict) and "run" in step]

    assert "continue-on-error" not in job
    assert "uv sync --locked --dev --all-extras" in runs
    assert "uv run --with ty ty check src/" in runs
    assert runs.index("uv sync --locked --dev --all-extras") < runs.index(
        "uv run --with ty ty check src/"
    )
    assert [step.get("continue-on-error") for step in run_steps] == [None, True]
    assert all("|| true" not in run for run in runs)
