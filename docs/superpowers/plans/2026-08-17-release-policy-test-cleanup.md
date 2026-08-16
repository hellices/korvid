# Release Policy Test Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace prose-coupled release tests with structural workflow and command assertions while preserving every release and maintainer-control invariant.

**Architecture:** Executable release-script tests remain in `test_release_scripts.py`. Documentation and agent-policy contracts move to a focused module that parses markdown sections and checks commands, links, and prohibitions instead of preferred sentences.

**Tech Stack:** Python 3.13, pytest, PyYAML, tomllib, pathlib.

## Global Constraints

- Preserve release ordering, credential isolation, artifact integrity, immutable-tag guidance, attestation gating, dry-run isolation, and maintainer-only merge policy.
- Do not modify workflows or documentation merely to make refactored tests pass.
- Assert commands, URLs, headings, YAML fields, and job dependencies rather than explanatory prose.
- Keep the release workflow itself unchanged in this plan.

---

### Task 1: Add Reusable Release-Contract Parsers

**Files:**
- Create: `tests/release_contracts.py`
- Modify: `tests/test_release_scripts.py`
- Modify: `tests/test_lockfile.py`

**Interfaces:**
- Produces: `markdown_section(text: str, heading: str) -> str`
- Produces: `workflow_jobs(path: Path) -> dict[str, Any]`
- Produces: `run_scripts(job: Mapping[str, Any]) -> tuple[str, ...]`

- [ ] **Step 1: Write parser tests**

Add focused tests to `tests/test_release_scripts.py`:

```python
def test_markdown_section_stops_at_the_next_peer_heading() -> None:
    text = "# Title\n## Release\nkeep\n### Child\nkeep child\n## Cleanup\ndrop\n"
    assert markdown_section(text, "Release") == "keep\n### Child\nkeep child"


def test_run_scripts_returns_only_shell_steps() -> None:
    job = {"steps": [{"uses": "actions/checkout@sha"}, {"run": "uv build"}, {"run": "uv publish"}]}
    assert run_scripts(job) == ("uv build", "uv publish")
```

- [ ] **Step 2: Run the parser tests and confirm RED**

```bash
uv run --no-sync pytest -p no:tach \
  tests/test_release_scripts.py::test_markdown_section_stops_at_the_next_peer_heading \
  tests/test_release_scripts.py::test_run_scripts_returns_only_shell_steps -q
```

Expected: import or name failures because the helpers do not exist.

- [ ] **Step 3: Implement the parsers**

Create `tests/release_contracts.py`:

```python
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def markdown_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    before, separator, after = text.partition(marker)
    del before
    if not separator:
        raise AssertionError(f"missing markdown section: {heading}")
    return after.split("\n## ", 1)[0].strip()


def workflow_jobs(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        raise AssertionError(f"{path} has no jobs mapping")
    return jobs


def run_scripts(job: Mapping[str, Any]) -> tuple[str, ...]:
    steps = job.get("steps", ())
    return tuple(str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step)
```

- [ ] **Step 4: Pass the focused tests**

Run the Step 2 command.

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add tests/release_contracts.py tests/test_release_scripts.py
git commit -m "test: add structural release contract helpers"
```

### Task 2: Separate Policy Documentation Tests

**Files:**
- Create: `tests/test_release_policy.py`
- Modify: `tests/test_release_scripts.py:1199-1247`
- Modify: `tests/test_release_scripts.py:1354-1739`
- Modify: `tests/test_release_scripts.py:1987-2050`

**Interfaces:**
- Consumes: `markdown_section`
- Produces: a focused policy-test module

- [ ] **Step 1: Record existing policy test names**

```bash
uv run --no-sync pytest -p no:tach tests/test_release_scripts.py --collect-only -q \
  | rg 'agent_instructions|release_docs|release_notes|readme|runbook'
```

Expected: the existing documentation and policy tests are listed before moving.

- [ ] **Step 2: Move stable semantic checks**

Move policy tests into `tests/test_release_policy.py` and consolidate them into
these four contracts:

```python
def test_agent_policy_forbids_agent_controlled_merge_paths() -> None:
    pull_requests = markdown_section(_AGENTS.read_text(), "Pull Requests")
    review_loop = markdown_section(_AGENTS.read_text(), "Review Loop")
    policy = f"{pull_requests}\n{review_loop}"
    assert "gh pr merge" not in policy
    assert "auto-merge" in policy
    assert "maintainer" in policy.lower()
    assert "Never approve your own work" in policy


def test_release_runbook_covers_irreversible_boundaries_and_recovery() -> None:
    runbook = _RUNBOOK.read_text()
    for command in ("gh workflow run", "gh run watch", "gh attestation verify"):
        assert command in runbook
    for heading in ("Release", "Recovery", "Cleanup"):
        assert f"## {heading}" in runbook


def test_release_docs_cover_retained_state_and_opt_in_cleanup() -> None:
    cleanup = markdown_section(_RUNBOOK.read_text(), "Cleanup")
    for retained_path in ("config", "audit", "keyring", "mcp"):
        assert retained_path in cleanup.lower()
    assert "--force" not in cleanup


def test_current_release_install_commands_use_the_project_version() -> None:
    version = _project_version()
    text = f"{_README.read_text()}\n{_release_notes(version)}"
    assert f"korvid[all]=={version}" in text
    assert f"v{version}" in text
```

Preserve existing link validation and relative-link rejection as separate tests
because they verify rendered-package behavior, not prose.

- [ ] **Step 3: Remove superseded exact-sentence tests**

Delete tests whose only remaining assertion is an exact explanatory sentence
already covered by the four contracts. Do not delete workflow ordering,
permissions, artifact comparison, tag binding, attestation, or dry-run tests.

- [ ] **Step 4: Verify policy and release tests**

```bash
uv run --no-sync pytest -p no:tach \
  tests/test_release_policy.py tests/test_release_scripts.py tests/test_lockfile.py -q
uv run --no-sync ruff check \
  tests/release_contracts.py tests/test_release_policy.py \
  tests/test_release_scripts.py tests/test_lockfile.py
uv run --no-sync ruff format --check \
  tests/release_contracts.py tests/test_release_policy.py \
  tests/test_release_scripts.py tests/test_lockfile.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add tests/release_contracts.py tests/test_release_policy.py \
  tests/test_release_scripts.py tests/test_lockfile.py
git commit -m "test: replace release prose checks with policy contracts"
```

### Task 3: Parse Workflow Structure Instead of Slicing YAML Text

**Files:**
- Modify: `tests/test_release_scripts.py`
- Modify: `tests/test_lockfile.py`
- Reuse: `tests/release_contracts.py`

**Interfaces:**
- Consumes: `workflow_jobs`, `run_scripts`
- Produces: no new interface

- [ ] **Step 1: Refactor job ordering assertions**

Replace `workflow.index("\n  job-name:")` and string slices with job dependency
assertions:

```python
jobs = workflow_jobs(_RELEASE_WORKFLOW)
assert jobs["publish-pypi"]["needs"] == ["stage-github-release"]
assert jobs["finalize-github-release"]["needs"] == ["publish-pypi"]
```

Where `needs` contains multiple jobs, compare sets instead of ordered lists.

- [ ] **Step 2: Refactor permissions and command-placement assertions**

Use parsed mappings:

```python
jobs = workflow_jobs(_RELOCK_WORKFLOW)
assert jobs["relock"]["permissions"] == {"contents": "read"}
assert jobs["propose"]["permissions"] == {
    "contents": "write",
    "pull-requests": "write",
}
assert any("uv lock" in script for script in run_scripts(jobs["relock"]))
assert not any("uv run pytest" in script for script in run_scripts(jobs["propose"]))
```

- [ ] **Step 3: Retain shell-content checks only where shell syntax is the contract**

Checks for `${{ ... }}` inside shell bodies, environment propagation, quoting,
and command flags remain text assertions over `run_scripts(job)`.

- [ ] **Step 4: Verify the complete release-contract surface**

Run the Task 2 pytest and ruff commands.

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add tests/release_contracts.py tests/test_release_scripts.py tests/test_lockfile.py
git commit -m "test: validate release workflows structurally"
```

### Task 4: Confirm Invariants and Measure Reduction

**Files:**
- Verify: `tests/test_release_policy.py`
- Verify: `tests/test_release_scripts.py`
- Verify: `tests/test_lockfile.py`

**Interfaces:**
- Consumes: all prior tasks
- Produces: before/after collection and source-inspection metrics

- [ ] **Step 1: Collect the release tests**

```bash
uv run --no-sync pytest -p no:tach \
  tests/test_release_policy.py tests/test_release_scripts.py tests/test_lockfile.py \
  --collect-only -q
```

Expected: collection succeeds without duplicate test names.

- [ ] **Step 2: Run the tests**

```bash
uv run --no-sync pytest -p no:tach \
  tests/test_release_policy.py tests/test_release_scripts.py tests/test_lockfile.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Check remaining source-string assertions**

```bash
rg -n 'assert .* in (workflow|runbook|readme|instructions)' \
  tests/test_release_policy.py tests/test_release_scripts.py tests/test_lockfile.py
```

Expected: each remaining match asserts a command, URL, prohibited action, shell
expression, or user-visible installation contract; no preferred explanatory
sentence remains.

- [ ] **Step 4: Run static checks**

```bash
uv run --no-sync ruff check tests/release_contracts.py \
  tests/test_release_policy.py tests/test_release_scripts.py tests/test_lockfile.py
uv run --no-sync ruff format --check tests/release_contracts.py \
  tests/test_release_policy.py tests/test_release_scripts.py tests/test_lockfile.py
uv run --no-sync mypy tests/release_contracts.py tests/test_release_policy.py \
  tests/test_release_scripts.py tests/test_lockfile.py
```

Expected: all commands pass.
