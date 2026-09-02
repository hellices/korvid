# Pod Transfer UID Revalidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refuse name-based Pod transfer unless the final bounded lookup returns the exact UID captured when the transfer dialog opened.

**Architecture:** Keep `KorvidApp._pod_uid_unchanged` as the shared transfer/debug identity boundary. Change only its interpretation of the existing `str | None` lookup result: `None` becomes a retryable verification failure, while confirmed deletion, replacement, and exact match retain distinct outcomes.

**Tech Stack:** Python 3.11+, asyncio, Textual Pilot tests, pytest, Ruff, mypy, Tach

## Global Constraints

- Only a retrieved, non-`None` UID equal to the captured UID permits execution.
- Lookup failure must be retryable and distinct from confirmed deletion or replacement.
- Transfer intent auditing remains fail-closed and occurs only after identity verification.
- The existing `_UID_LOOKUP_TIMEOUT` stays unchanged.
- General write-target lookup and server-side UID precondition behavior stay unchanged.
- Do not modify `uv.lock`; use the existing corporate PyPI proxy configuration.

---

### Task 1: Fail Closed on Unavailable Final Pod UID

**Files:**
- Modify: `src/korvid/ui/app.py:3799-3818`
- Modify: `tests/ui/test_transfer.py:428-524`
- Modify: `tests/ui/test_shell.py:1214-1277`

**Interfaces:**
- Consumes: `KorvidApp._target_uid(kind_alias: str, ns: str | None, name: str) -> str | None`
- Produces: `KorvidApp._pod_uid_unchanged(namespace: str, name: str, approved_uid: str, *, action: str) -> bool`, where only an exact non-`None` match returns `True`

- [ ] **Step 1: Add failing transfer regressions for normalized timeout and infrastructure failure**

Add a parameterized integration test beside the existing transfer UID tests. Use a helper that raises the supplied exception from `get_manifest`, start an upload or download with captured UID `uid-approved`, and assert the final lookup never reaches audit or exec:

```python
@pytest.mark.parametrize("direction", ["upload", "download"])
@pytest.mark.parametrize("failure", [TimeoutError(), RuntimeError("api unavailable")])
async def test_transfer_blocked_when_final_uid_lookup_unavailable(
    tmp_path: Path,
    direction: str,
    failure: Exception,
) -> None:
    opener = FakeExecOpener()
    audit_path = tmp_path / "audit.jsonl"

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise failure

    app = make_app(
        [_pod("api-1", uid="uid-approved")],
        open_pod_exec=opener,
        audit=AuditLog(audit_path, context="test"),
        get_manifest=get_manifest,
    )
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="rows")
        await pilot.press("ctrl+t")
        await until(pilot, lambda: isinstance(app.screen, TransferScreen), label="dialog")
        if direction == "upload":
            source = tmp_path / "source"
            source.write_bytes(b"x")
            _dialog(app).select_upload()
            _dialog(app).query_one("#transfer-local", Input).value = str(source)
            _dialog(app).query_one("#transfer-remote", Input).value = "/tmp/source"
        else:
            _dialog(app).query_one("#transfer-remote", Input).value = "/tmp/source"
            _dialog(app).query_one("#transfer-local", Input).value = str(tmp_path / "source")
        await pilot.press("enter")
        if direction == "upload":
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
            await pilot.press("y")
        await until(
            pilot,
            lambda: any("could not be verified" in str(n.message) for n in app._notifications),
            label="retryable verification warning",
        )

    assert opener.calls == []
    assert audit_entries(audit_path) == []
    messages = [str(notification.message) for notification in app._notifications]
    assert any("Retry" in message for message in messages)
    assert all("no longer exists" not in message and "was replaced" not in message for message in messages)
```

If pytest rejects shared exception instances in the parameter table, parameterize exception factories instead:

```python
@pytest.mark.parametrize(
    "failure_factory",
    [lambda: TimeoutError(), lambda: RuntimeError("api unavailable")],
)
```

and `raise failure_factory()`.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py::test_transfer_blocked_when_final_uid_lookup_unavailable \
  -q
```

Expected: all four parameter cases fail because transfer proceeds and no `could not be verified` notification appears.

- [ ] **Step 3: Implement the minimal fail-closed `None` branch**

Update the shared helper without changing `_target_manifest` or `_target_uid`:

```python
async def _pod_uid_unchanged(
    self, namespace: str, name: str, approved_uid: str, *, action: str
) -> bool:
    """Re-verify the approved pod incarnation just before `action` executes."""
    try:
        current_uid = await self._target_uid("pods", namespace, name)
    except ApiStatusError:
        self.notify(
            f"{action} cancelled - pod {name} no longer exists.",
            severity="warning",
        )
        return False
    if current_uid is None:
        self.notify(
            f"{action} cancelled - pod {name} could not be verified. "
            "Retry when the cluster is reachable.",
            severity="warning",
        )
        return False
    if current_uid != approved_uid:
        self.notify(
            f"{action} cancelled - pod {name} was replaced since the prompt was shown.",
            severity="warning",
        )
        return False
    return True
```

- [ ] **Step 4: Run the transfer regressions and verify GREEN**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py::test_transfer_blocked_when_final_uid_lookup_unavailable \
  tests/ui/test_transfer.py::test_upload_blocked_when_pod_replaced_after_approval \
  tests/ui/test_transfer.py::test_download_blocked_when_pod_replaced \
  tests/ui/test_transfer.py::test_upload_proceeds_when_uid_unchanged \
  -q
```

Expected: 7 passed.

- [ ] **Step 5: Pin the shared debug boundary**

Add a focused test after `test_debug_aborts_when_pod_replaced_after_prompt` using a `get_manifest` callback that returns the original UID for pre-prompt capture and raises `TimeoutError` on the final lookup:

```python
async def test_debug_aborts_when_final_pod_uid_lookup_unavailable(tmp_path: Path) -> None:
    calls = 0

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"metadata": {"name": name, "namespace": ns or "", "uid": "uid-original"}}
        raise TimeoutError

    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        [_pod("api-1")],
        audit=AuditLog(audit_path),
        get_manifest=get_manifest,
    )
    shell_calls: list[list[str]] = []
    debug_calls: list[list[str]] = []

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=_recording_call(shell_calls)),
        patch("korvid.ui.app.subprocess.Popen", side_effect=_fake_popen(debug_calls)),
        patch("korvid.ui.app.subprocess.run", return_value=SimpleNamespace(returncode=1)),
        patch.object(type(app), "suspend", side_effect=lambda: _noop_cm()),
    ):
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, PickScreen))
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
            await pilot.press("y")
            await until(
                pilot,
                lambda: any("could not be verified" in str(n.message) for n in app._notifications),
            )

    assert [argv[1] for argv in shell_calls] == ["exec"]
    assert debug_calls == []
    assert not audit_path.exists() or "debug" not in audit_path.read_text()
```

- [ ] **Step 6: Run the shared-boundary tests**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_shell.py::test_debug_aborts_when_final_pod_uid_lookup_unavailable \
  tests/ui/test_shell.py::test_debug_aborts_when_pod_replaced_after_prompt \
  tests/ui/test_shell.py::test_debug_runs_when_pod_uid_unchanged \
  -q
```

Expected: 3 passed.

- [ ] **Step 7: Run the complete targeted behavior suite**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach \
  tests/ui/test_transfer.py \
  tests/ui/test_transfer_controller.py \
  tests/ui/test_transfer_picker.py \
  tests/ui/test_shell.py \
  -q
```

Expected: all tests pass with zero warnings.

- [ ] **Step 8: Run source and architecture checks**

Run:

```bash
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/ui/app.py tests/ui/test_transfer.py tests/ui/test_shell.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/ui/app.py tests/ui/test_transfer.py tests/ui/test_shell.py
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/mypy \
  src/korvid/ui/app.py
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/tach check
git diff --check
```

Expected: every command exits 0.

- [ ] **Step 9: Commit the behavior and regressions**

```bash
git add src/korvid/ui/app.py tests/ui/test_transfer.py tests/ui/test_shell.py
git commit -m "security: fail closed on unavailable pod UID"
```

Include the required `Co-authored-by` trailer in the commit message.

### Task 2: Final Validation and Delivery Readiness

**Files:**
- Verify: `uv.lock`
- Verify: all branch changes against `origin/main`

**Interfaces:**
- Consumes: the exact-match-only `_pod_uid_unchanged` contract from Task 1
- Produces: a review-ready #334 branch with no lockfile drift

- [ ] **Step 1: Run the full project quality gate**

Run:

```bash
make check
```

Expected: Ruff, formatting, mypy, pytest, coverage, Tach, deptry, and repository-specific guards all pass.

- [ ] **Step 2: Verify lockfile and branch hygiene**

Run:

```bash
test "$(git hash-object uv.lock)" = "$(git rev-parse HEAD:uv.lock)"
git diff --check
git status --short
git diff --stat origin/main...HEAD
```

Expected: lockfile hashes match, diff check is clean, working tree is clean, and the diff contains only the #334 design, implementation plan, helper change, and regressions.

- [ ] **Step 3: Request high-confidence code review**

Review the entire `origin/main...HEAD` diff for correctness, security, regressions, and architecture invariants. Fix each credible finding with a new failing test before changing implementation, rerun `make check`, and commit fixes without amending.

- [ ] **Step 4: Create and review the pull request**

Push `fix/334-pod-transfer-uid-revalidation`, create a PR with `Closes #334`, request Copilot review, reply to and resolve every credible thread, and rerun the full gate after code changes.

- [ ] **Step 5: Verify required CI**

Run:

```bash
PR_NUMBER=$(gh pr view --json number --jq .number)
gh pr view "$PR_NUMBER" --json statusCheckRollup
```

Expected: every required check is `SUCCESS`; intentional deployment-only jobs may be `SKIPPED`. Report the implementation and review results in the conversation, then wait for the user to merge.
