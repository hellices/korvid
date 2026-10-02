"""Executable contracts for pull-request isolation and bounded CI jobs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.platforms import bash_executable

ROOT = Path(__file__).parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
CODEQL_WORKFLOW = ROOT / ".github" / "workflows" / "codeql.yml"
TRUSTED_LINUX_RUNNER = (
    "${{ github.event_name == 'pull_request' && 'ubuntu-latest' || 'korvid-runners' }}"
)
SELECTED_LINUX_RUNNER = (
    "${{ needs.changes.outputs.runner == 'korvid-runners' && 'korvid-runners' || 'ubuntu-latest' }}"
)
RUNNER_SELECTED_JOBS = ("test", "pre-commit", "security", "ty-experimental")
RUNNER_STEP_ENV = {
    "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
    "EVENT_NAME": "${{ github.event_name }}",
    "EVENT_ACTION": "${{ github.event.action }}",
    "PR": "${{ github.event.pull_request.number }}",
    "REPO": "${{ github.repository }}",
    "OWNER": "${{ github.repository_owner }}",
    "HEAD_REPO": "${{ github.event.pull_request.head.repo.full_name }}",
    "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
    "ACTOR": "${{ github.actor }}",
    "TRIGGERING_ACTOR": "${{ github.triggering_actor }}",
    "HEAD_SHA": "${{ github.event.pull_request.head.sha }}",
}
OWNER_PULL_REQUEST = {
    "EVENT_NAME": "pull_request",
    "EVENT_ACTION": "synchronize",
    "PR": "7",
    "REPO": "hellices/korvid",
    "OWNER": "hellices",
    "HEAD_REPO": "hellices/korvid",
    "PR_AUTHOR": "hellices",
    "ACTOR": "hellices",
    "TRIGGERING_ACTOR": "hellices",
    "HEAD_SHA": "a" * 40,
}
FILE_LIST_CALL = "api --paginate repos/hellices/korvid/pulls/7/files --jq " + (
    ".[] | .filename, (.previous_filename // empty)"
)
PULL_REQUEST_CALL = "api repos/hellices/korvid/pulls/7 --jq " + (
    r'"\(.head.sha) \(.changed_files)"'
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


def _runner_step(jobs: Mapping[str, Any]) -> Mapping[str, Any]:
    steps = jobs["changes"]["steps"]
    matches = [step for step in steps if isinstance(step, dict) and step.get("id") == "runner"]
    assert len(matches) == 1, "changes must have exactly one step with id 'runner'"
    step = matches[0]
    assert isinstance(step, dict)
    return step


def _select_runner(
    tmp_path: Path,
    context: Mapping[str, str],
    *,
    files: tuple[str, ...] = ("src/korvid/ui/app.py",),
    api_fails: bool = False,
    current_head: str | None = None,
    changed_files: str | None = None,
) -> str:
    """Run the workflow's own runner-selection step against a fake `gh`."""
    script = str(_runner_step(_jobs(CI_WORKFLOW))["run"])
    listing = tmp_path / "files.txt"
    listing.write_text("".join(f"{name}\n" for name in files), encoding="utf-8")
    calls = tmp_path / "gh-calls.txt"
    output = tmp_path / "github-output.txt"
    output.write_text("", encoding="utf-8")
    head = context.get("HEAD_SHA", "") if current_head is None else current_head
    count = str(len(files)) if changed_files is None else changed_files
    # Both answers arrive already reduced by `--jq`: the file list one path
    # per line, the pull request as "<head sha> <changed_files>". The query
    # tests below run the real queries through jq.
    fake_gh = (
        'gh() { printf "%s\\n" "$*" >> "$FAKE_GH_CALLS"; '
        '[ "$FAKE_GH_FAILS" = 1 ] && return 1; '
        'case "$2" in --paginate) cat "$FAKE_GH_FILES" ;; *) echo "$FAKE_GH_PULL_REQUEST" ;; esac; }\n'
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "GITHUB_OUTPUT": output.as_posix(),
        "FAKE_GH_FILES": listing.as_posix(),
        "FAKE_GH_CALLS": calls.as_posix(),
        "FAKE_GH_FAILS": "1" if api_fails else "0",
        "FAKE_GH_PULL_REQUEST": f"{head} {count}",
        **context,
    }

    result = subprocess.run(
        [bash_executable(), "--noprofile", "--norc", "-c", fake_gh + script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    selections = [
        line.removeprefix("runner=")
        for line in output.read_text(encoding="utf-8").splitlines()
        if line.startswith("runner=")
    ]
    assert len(selections) == 1, f"expected one runner selection, got {selections}"
    if context["EVENT_NAME"] == "pull_request" and calls.exists():
        made = calls.read_text(encoding="utf-8").splitlines()
        assert made in ([FILE_LIST_CALL], [FILE_LIST_CALL, PULL_REQUEST_CALL]), made
    return selections[0]


def test_linux_ci_jobs_take_the_runner_selected_by_changes() -> None:
    ci_jobs = _jobs(CI_WORKFLOW)

    assert ci_jobs["changes"]["runs-on"] == "ubuntu-latest"
    assert ci_jobs["changes"]["outputs"]["runner"] == "${{ steps.runner.outputs.runner }}"
    assert ci_jobs["windows-test"]["runs-on"] == "windows-latest"
    assert ci_jobs["dependency-review"]["runs-on"] == "ubuntu-latest"
    assert {name: ci_jobs[name]["runs-on"] for name in RUNNER_SELECTED_JOBS} == dict.fromkeys(
        RUNNER_SELECTED_JOBS, SELECTED_LINUX_RUNNER
    )
    assert all(ci_jobs[name]["needs"] == "changes" for name in RUNNER_SELECTED_JOBS)
    assert _jobs(CODEQL_WORKFLOW)["analyze"]["runs-on"] == TRUSTED_LINUX_RUNNER


def test_required_jobs_that_newly_wait_on_changes_still_run_when_it_fails() -> None:
    # A job skipped because `needs` failed reports as passing to a required
    # check; these must run on the hosted fallback instead.
    ci_jobs = _jobs(CI_WORKFLOW)

    for name in ("pre-commit", "security", "ty-experimental"):
        assert ci_jobs[name]["if"] == "${{ !cancelled() }}", name


def test_runner_step_reads_its_inputs_only_from_the_environment() -> None:
    step = _runner_step(_jobs(CI_WORKFLOW))

    assert step["env"] == RUNNER_STEP_ENV
    assert "${{" not in str(step["run"])


def test_trusted_push_runs_on_korvid_runners(tmp_path: Path) -> None:
    context = {**OWNER_PULL_REQUEST, "EVENT_NAME": "push", "PR": ""}

    assert _select_runner(tmp_path, context) == "korvid-runners"


def test_owner_pull_request_without_dependency_changes_runs_on_korvid_runners(
    tmp_path: Path,
) -> None:
    files = ("src/korvid/ui/app.py", "tests/test_ci_workflow.py", "docs/index.md")

    assert _select_runner(tmp_path, OWNER_PULL_REQUEST, files=files) == "korvid-runners"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("HEAD_REPO", "someone/korvid"),
        ("PR_AUTHOR", "dependabot[bot]"),
        ("PR_AUTHOR", "someone"),
        ("ACTOR", "someone"),
        ("TRIGGERING_ACTOR", "someone"),
        ("OWNER", ""),
        # Only a push names who supplied the head. Opening or reopening a PR
        # makes the owner the actor without saying who pushed the branch -
        # a collaborator's push followed by the owner's reopen would pass.
        ("EVENT_ACTION", "opened"),
        ("EVENT_ACTION", "reopened"),
        ("EVENT_ACTION", ""),
        ("HEAD_SHA", ""),
    ],
)
def test_pull_request_not_wholly_from_the_owner_runs_hosted(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    context = {**OWNER_PULL_REQUEST, field: value}

    assert _select_runner(tmp_path, context) == "ubuntu-latest"


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        ".pre-commit-config.yaml",
        ".github/workflows/ci.yml",
        ".github/dependabot.yml",
    ],
)
def test_owner_pull_request_that_changes_third_party_code_runs_hosted(
    tmp_path: Path,
    path: str,
) -> None:
    files = ("src/korvid/ui/app.py", path)

    assert _select_runner(tmp_path, OWNER_PULL_REQUEST, files=files) == "ubuntu-latest"


def test_owner_pull_request_that_renames_third_party_code_away_runs_hosted(
    tmp_path: Path,
) -> None:
    # The file list reports a rename as its new path plus `previous_filename`.
    files = ("uv.lock.old", "uv.lock")

    assert _select_runner(tmp_path, OWNER_PULL_REQUEST, files=files) == "ubuntu-latest"


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq to evaluate the query")
def test_file_list_query_reports_both_sides_of_a_rename() -> None:
    script = str(_runner_step(_jobs(CI_WORKFLOW))["run"])
    query = FILE_LIST_CALL.split(" --jq ", 1)[1]
    page = [
        {"filename": "src/korvid/ui/app.py", "status": "modified"},
        {"filename": "uv.lock.old", "previous_filename": "uv.lock", "status": "renamed"},
    ]

    result = subprocess.run(
        ["jq", "-r", query],
        input=json.dumps(page),
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"--jq '{query}'" in script
    assert result.stdout.splitlines() == ["src/korvid/ui/app.py", "uv.lock.old", "uv.lock"]


def test_pull_request_whose_head_moved_during_selection_runs_hosted(tmp_path: Path) -> None:
    # The file list describes the PR's current head, not necessarily the
    # commit this run checks out.
    selected = _select_runner(tmp_path, OWNER_PULL_REQUEST, current_head="b" * 40)

    assert selected == "ubuntu-latest"


@pytest.mark.parametrize(
    ("changed_files", "expected"),
    [("3000", "korvid-runners"), ("3001", "ubuntu-latest")],
)
def test_pull_request_larger_than_the_file_list_runs_hosted(
    tmp_path: Path,
    changed_files: str,
    expected: str,
) -> None:
    # The files endpoint returns at most 3000 entries; past that, an excluded
    # path could be missing from the list the step scans.
    selected = _select_runner(tmp_path, OWNER_PULL_REQUEST, changed_files=changed_files)

    assert selected == expected


@pytest.mark.parametrize("changed_files", ["null", ""])
def test_pull_request_without_a_changed_file_count_runs_hosted(
    tmp_path: Path,
    changed_files: str,
) -> None:
    # Without the count, nothing shows the file list is complete.
    selected = _select_runner(tmp_path, OWNER_PULL_REQUEST, changed_files=changed_files)

    assert selected == "ubuntu-latest"


@pytest.mark.skipif(shutil.which("jq") is None, reason="needs jq to evaluate the query")
def test_pull_request_query_reports_the_head_and_the_changed_file_count() -> None:
    script = str(_runner_step(_jobs(CI_WORKFLOW))["run"])
    query = PULL_REQUEST_CALL.split(" --jq ", 1)[1]
    pulls = [{"head": {"sha": "a" * 40}, "changed_files": 3001}, {"head": {"sha": "a" * 40}}]

    result = subprocess.run(
        ["jq", "-r", f".[] | {query}"],
        input=json.dumps(pulls),
        capture_output=True,
        text=True,
        check=True,
    )

    assert f"--jq '{query}'" in script
    assert result.stdout.splitlines() == [f"{'a' * 40} 3001", f"{'a' * 40} null"]


def test_missing_identity_context_runs_hosted(tmp_path: Path) -> None:
    blank = dict.fromkeys(
        (
            "REPO",
            "OWNER",
            "HEAD_REPO",
            "PR_AUTHOR",
            "ACTOR",
            "TRIGGERING_ACTOR",
            "EVENT_ACTION",
            "HEAD_SHA",
        ),
        "",
    )
    context = {**OWNER_PULL_REQUEST, **blank}

    assert _select_runner(tmp_path, context) == "ubuntu-latest"


def test_unreadable_file_list_runs_hosted(tmp_path: Path) -> None:
    assert _select_runner(tmp_path, OWNER_PULL_REQUEST, api_fails=True) == "ubuntu-latest"


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
