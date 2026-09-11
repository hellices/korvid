"""Executable contracts for pull-request isolation and bounded CI jobs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CODEQL_WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
TRUSTED_LINUX_RUNNER = (
    "${{ github.event_name == 'pull_request' && 'ubuntu-latest' || 'korvid-runners' }}"
)
WINDOWS_SEED = "${{ github.run_id }}"


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


def test_ci_jobs_have_explicit_bounded_timeouts() -> None:
    jobs = _jobs(CI_WORKFLOW)
    expected = {
        "changes": 10,
        "test": 45,
        "windows-test": 45,
        "pre-commit": 20,
        "security": 15,
        "dependency-review": 10,
        "ty-experimental": 15,
    }

    assert {name: jobs[name].get("timeout-minutes") for name in expected} == expected
    assert _jobs(CODEQL_WORKFLOW)["analyze"].get("timeout-minutes") == 20


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


def test_windows_suite_prints_and_uses_one_deterministic_seed() -> None:
    windows_job = _jobs(CI_WORKFLOW)["windows-test"]
    runs = _run_steps(windows_job)
    seed_messages = [run for run in runs if "pytest-randomly seed:" in run]
    full_suite = next(
        run for run in runs if "--ignore=tests/windows/test_native_terminal.py" in run
    )

    assert seed_messages == [f'Write-Output "pytest-randomly seed: {WINDOWS_SEED}"']
    assert full_suite.endswith(f" --randomly-seed={WINDOWS_SEED}")
    assert sum(run.count("--randomly-seed=") for run in runs) == 1


def test_experimental_ty_job_is_honestly_advisory() -> None:
    job = _jobs(CI_WORKFLOW)["ty-experimental"]
    runs = _run_steps(job)

    assert job.get("continue-on-error") is True
    assert "uv sync --locked --dev --all-extras" in runs
    assert "uv run --with ty ty check src/" in runs
    assert runs.index("uv sync --locked --dev --all-extras") < runs.index(
        "uv run --with ty ty check src/"
    )
    assert all("|| true" not in run for run in runs)
