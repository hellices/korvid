# Refactoring Roadmap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the defects a whole-codebase review found, collapse the hand-copied rules that have already drifted into bugs, and win back headroom in the size-frozen modules, without a rewrite and without weakening any security invariant.

**Architecture:** The review (8 parallel subsystem audits at `1c02aed4`, claims spot-verified, Phase 1 executed end to end in a scratch copy) found function-level hygiene and layering in good shape. The debt sits in three places:

- Nine modules are frozen at their size cap with 0-23 lines of headroom.
- Security and validation rules were copied by hand and the copies have drifted.
- Docs describe paths the code no longer takes.

Phase 1 is fully specified TDD work: one PR of seven commits. Phases 2-4 are scoped work packages. Each needs its own detailed plan, written from the package description here, before execution.

**Tech Stack:** Python 3.11+, Textual 8.2.8, kubernetes_asyncio, PyYAML, pytest (+ pytest-asyncio, pytest-randomly), ruff, mypy `--strict`, tach, deptry, `scripts/check_source_size.py`.

## Global Constraints

- **Size caps** (`scripts/check_source_size.py`) never rise. A task that adds lines to a frozen module must free them in the same task.
  - After an extraction, lower the module's cap to its new count; that step is the RED step of a pure move.
  - `docs/dev/quality-gates.md`: "Never bypass the hook or raise a baseline merely to make a change pass."
- **Security invariants must not weaken:**
  - Approval dialogs are confirmed only by user keystrokes.
  - `run_kubectl` validates verb × resource × flags.
  - Sensitive reads go through masking.
  - Audit logging is fail-closed.
  - Context, UID and Helm incarnation are revalidated before mutation.
  - Config refuses credential-shaped keys.
- **Protected files are never edited:** `uv.lock`, `.github/workflows/`, `tach.toml`, `.pre-commit-config.yaml`.
- **Behind the corporate mirror:**
  - Run every `uv` command as `UV_NO_SYNC=1 uv run …`.
  - Run `git restore uv.lock` before every commit.
  - Git hooks do not run locally, so run `UV_NO_SYNC=1 uv run pre-commit run --all-files` before pushing.
- **Full gate:** `UV_NO_SYNC=1 make check` (source-size, lint, typecheck, test) plus `UV_NO_SYNC=1 uv run tach check`.
- **Fallback when the worktree has no `.venv`.** Use the main checkout's interpreter:

  ```bash
  PYTHONPATH="$PWD/src:$PWD" /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest -p no:tach -p no:randomly -q <path>
  ```

  `ruff`, `mypy` and `tach` live in the same `bin/`. That venv lacks httpx, openai and mcp. The resulting `test_main_wiring`, `test_outbound` ollama-hook, `test_agent_setup_screen` real-probe, `test_mcp_ui_context` and `test_mcp_stdio_safety` failures are environmental, and also happen on an untouched checkout.
- **Moves are verbatim.**
  - Do not leave re-export shims behind; edit the importers instead. mypy's `no_implicit_reexport` catches a missed one.
  - Exceptions: where a package says otherwise (Phase 3 `config_observability`), and the test seams named in a package.
- **Test rules:**
  - `pytest.raises` always takes `match=`.
  - Textual notification waits use `tests/ui/waits.py::until(pilot, cond, label=...)`, never `pilot.pause()` loops.
  - Tests must not depend on order (pytest-randomly).
  - Read and write files as UTF-8 explicitly: Windows CI runs py3.12 under cp1252.
- **One PR per phase, one commit per task.**
  - Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  - Never `--no-verify`, never `--amend` after a hook rewrite (`git add -A` and commit again).
  - Open a PR only on explicit human instruction. The maintainer merges; agents never merge, approve, or enable auto-merge.
- **Stash safety:** never use bare `git stash`. If you must, run `git stash push -u -m "<tag>"` and apply by SHA.

---

## Assessment

| Area | Verdict | Why |
|---|---|---|
| Function-level code | No action | ruff C901 ≤ 10 holds with zero suppressions. All 17 `type: ignore` carry codes. Literal duplication is down to four 8-line regions. |
| Layering / composition | No action | tach is clean. `__main__` is the only constructor of the runtime graph. ABC ports sit at the boundaries. |
| Write path (`ui/*_controller.py`) | **Fix + consolidate** | The node-shell UID check is a fail-open copy of the pod check that #354 made fail-closed (Task 1.1). The reservation primitive, the epoch-crossed check and the approval callbacks are hand-copied 3-9 times (Phase 2). |
| `core/config.py` | **Fix + split** | `log_buffer_lines: .inf` crashes startup (1.4). The credential refusal points to a key startup rejects (1.5). The top-bar save overwrites a non-mapping file (1.6). Malformed YAML escapes as a traceback quoting the file (1.7). The file is at 1,683/1,684 (Phase 3). |
| Agent engine / providers | Small fixes | korvid's own response budget is reported as a withheld provider failure (1.3). Adapter usage rules disagree (Phase 2). Provenance is dropped at merge (Phase 2). |
| Proposal path tests | Fix | The "size bound before cluster I/O" test patches a seam the proposal path never calls, so it cannot fail (1.2). |
| Frozen god modules | Extract, narrowly | `__main__` 1,767/1,773 has 6 lines of headroom. `agent_ui_controller`, `executor`, `registry`, `k8s/client` and `resource_table` have 0. The next feature PR in any of them is blocked (Phase 3). |
| Docs | Fix | `ui-controllers.md`, the architecture spec, AGENTS.md's fake list and the `#297` comments describe paths that no longer exist (Phase 4). |

Things that look bad but are fine are listed under [Do Not Refactor](#do-not-refactor). Treat that list as part of the plan.

---

## Phase 1 — Verified defects (one PR, seven commits)

All seven tasks were executed in a scratch copy of `1c02aed4`. Each listed RED failure was observed, and every GREEN run passed. The broad run passed 6,238 tests with no non-environmental failure.

The tasks are independent except that 1.7 must follow 1.6, for `core/config.py`'s line budget. Task 1.1 changes user-visible behaviour and needs the maintainer's sign-off. Without it, drop the task from the PR; nothing else depends on it.

`core/config.py` line budget (cap 1,684):

| Point in Phase 1 | Lines |
|---|---|
| Start | 1,683 |
| After 1.4 | 1,681 |
| After 1.5 | 1,681 |
| After 1.6 | 1,666 |
| After 1.7 | 1,681 |

### Task 1.1: Node shell fails closed when the node's identity cannot be verified

**Why:**
- `ShellController._run_node_shell` checks the node UID only when `approved_uid is not None`.
- `_node_uid_unchanged` treats a `None` lookup or a transport error as "unchanged".
- kubectl addresses the node by name alone. A node replaced under the same name while the dialog was open, during an API outage, therefore gets a privileged shell on the strength of the old approval.
- Pod debug and transfer have refused in exactly this case since #334/#354 (`docs/superpowers/specs/2026-09-02-pod-transfer-uid-revalidation-design.md`).

**Decision gate:** this is a deliberate behaviour change. With it, the node shell refuses while the API server cannot confirm the node. Get the maintainer's explicit sign-off, as #354 did.

**Files:**
- Modify: `src/korvid/ui/shell_controller.py` (import at :46, call at :670, method at :956-974)
- Modify: `docs/ops.md:214-216`
- Test: `tests/ui/test_node_shell.py`

**Interfaces:**
- Produces: `ShellController._node_uid_unchanged(self, name: str, approved_uid: str | None) -> bool` and `ShellController._node_unverified(self, name: str) -> None`. Both are private and have no external callers.

- [ ] **Step 1: Make the test app's node lookup answer like the API server**

In `tests/ui/test_node_shell.py`:

1. Add `import pytest` after the `from unittest.mock import patch` line.
2. Add `from korvid.k8s.errors import ApiStatusError` after `from korvid.k8s.discovery import ResourceMeta`.
3. In `make_app`, directly after the nested `while True: await asyncio.sleep(0.01)` helper, add:

```python
    async def node_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        # The fixture nodes as the API server returns them: the node shell
        # re-reads the approved UID before it creates the privileged pod.
        uids = {"worker-1": "node-uid-1", **{extra: f"uid-{extra}" for extra in extra_nodes}}
        if name not in uids:
            raise ApiStatusError(404, "NotFound")
        return {"metadata": {"name": name, "uid": uids[name]}}
```

4. In the `KorvidApp(...)` call at the end of `make_app`, change `get_manifest=get_manifest,` to `get_manifest=get_manifest or node_manifest,`.

5. Two tests call the runner directly with no approved UID. In both `test_node_shell_cancelled_worker_still_deletes_pod` and `test_node_shell_cancelled_during_create_still_deletes_pod`, replace:

```python
                app._shell._run_node_shell(rec, "worker-1", "default", DEBUG_IMAGE, None)
```

with:

```python
                app._shell._run_node_shell(rec, "worker-1", "default", DEBUG_IMAGE, "node-uid-1")
```

- [ ] **Step 2: Write the failing tests**

Insert directly after `test_node_shell_aborts_when_node_replaced_after_prompt`:

```python
@pytest.mark.parametrize(
    "lookup",
    [
        pytest.param({"metadata": {}}, id="no-uid-in-manifest"),
        pytest.param(ApiStatusError(503, "ServiceUnavailable"), id="apiserver-unavailable"),
    ],
)
async def test_node_shell_refuses_when_node_identity_cannot_be_verified(
    tmp_path: Path, lookup: dict[str, Any] | Exception
) -> None:
    """kubectl addresses the node by name only, so the approved incarnation
    must be read back before the privileged pod exists: a lookup that cannot
    answer refuses (fail closed, as pod debug and transfer do since #334)
    rather than shelling into whichever node holds the name now."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if isinstance(lookup, Exception):
            raise lookup
        return lookup

    app = make_app(rec, audit_path, get_manifest=get_manifest)
    run_fake, run_calls = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(
                pilot,
                lambda: isinstance(app.screen, ConfirmScreen),
                label="node-shell approval dialog opened",
            )
            await pilot.press("y")

            def _refused() -> bool:
                return any("could not be verified" in n.message for n in app._notifications)

            await until(pilot, _refused, label="unverified-node cancel notification")
    assert call_records == []
    assert not any("debug" in argv for argv in run_calls)
    assert not audit_path.is_file() or "intent" not in audit_path.read_text()


async def test_node_shell_refuses_an_approval_without_a_node_uid(tmp_path: Path) -> None:
    """A node row without a UID gives the approval nothing to bind to: the
    privileged shell must not run on the strength of the name alone."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, run_calls = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await app._shell._run_node_shell(rec, "worker-1", "default", DEBUG_IMAGE, None)
            await until(
                pilot,
                lambda: any("could not be verified" in n.message for n in app._notifications),
                label="unverified-node cancel notification",
            )
    assert call_records == []
    assert run_calls == []
    assert not audit_path.is_file()


async def test_node_shell_reports_a_node_deleted_after_prompt(tmp_path: Path) -> None:
    """Deletion stays distinguishable from an unreachable cluster."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise ApiStatusError(404, "NotFound")

    app = make_app(rec, audit_path, get_manifest=get_manifest)
    run_fake, run_calls = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await app._shell._run_node_shell(rec, "worker-1", "default", DEBUG_IMAGE, "node-uid-1")
            await until(
                pilot,
                lambda: any("no longer exists" in n.message for n in app._notifications),
                label="deleted-node cancel notification",
            )
    assert call_records == []
    assert run_calls == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/ui/test_node_shell.py -k "cannot_be_verified or without_a_node_uid or deleted_after_prompt"`

Expected: 3 failed, 1 passed.
- The two parametrized cases and the no-UID case time out in `until` waiting for "could not be verified".
- The deletion case already passes and pins the existing 404 path.

- [ ] **Step 4: Implement fail-closed verification**

In `src/korvid/ui/shell_controller.py`:

1. Change the import `from korvid.k8s.errors import ApiStatusError` to:

```python
from korvid.k8s.errors import ApiStatusError, KubeClientError
```

2. In `_run_node_shell`, replace:

```python
        if approved_uid is not None and not await self._node_uid_unchanged(node, approved_uid):
            return
```

with:

```python
        if not await self._node_uid_unchanged(node, approved_uid):
            return
```

3. Replace the whole `_node_uid_unchanged` method with:

```python
    async def _node_uid_unchanged(self, name: str, approved_uid: str | None) -> bool:
        """Re-verify the approved node incarnation just before the shell
        launches. kubectl addresses the node by name only, so only the
        approved UID read back from the cluster permits the shell: a node
        that is gone, was replaced, or cannot be verified right now refuses
        (fail closed, like `pod_uid_unchanged` for pod debug and transfer)."""
        if approved_uid is None:
            self._node_unverified(name)
            return False
        try:
            current_uid = await self._target_uid_fn("nodes", None, name)
        except ApiStatusError:
            self._ui.notify(
                f"node shell cancelled - node {name} no longer exists.",
                severity="warning",
            )
            return False
        except KubeClientError:
            current_uid = None
        if current_uid is None:
            self._node_unverified(name)
            return False
        if current_uid != approved_uid:
            self._ui.notify(
                f"node shell cancelled - node {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return False
        return True

    def _node_unverified(self, name: str) -> None:
        self._ui.notify(
            f"node shell cancelled - node {name} could not be verified."
            " Retry when the cluster is reachable.",
            severity="warning",
        )
```

Keep the existing "no longer exists" message text unchanged when you copy it; tests assert on it.

`ApiStatusError` and `KubeClientError` are sibling `Exception` subclasses (`k8s/errors.py`). `target_uid` re-raises only a 404 `ApiStatusError`, so that branch means deletion. The 503 case in Step 2 reaches the "could not be verified" branch only because `AgentUiController.target_uid` maps a non-404 status to `None`. Do not reorder these branches.

- [ ] **Step 5: Document the check**

In `docs/ops.md`, replace:

```markdown
  `kubectl debug node/` session with the host filesystem at `/host`. Both pass
  the approval gate and are audited fail-closed, but they end differently. The
```

with:

```markdown
  `kubectl debug node/` session with the host filesystem at `/host`. Both pass
  the approval gate, re-read the approved pod's or node's UID just before they
  run (refusing when the cluster cannot confirm it), and are audited
  fail-closed, but they end differently. The
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/ui/test_node_shell.py`

Expected: 36 passed.

Run: `UV_NO_SYNC=1 uv run ruff check src/korvid/ui/shell_controller.py tests/ui/test_node_shell.py && UV_NO_SYNC=1 uv run mypy src/korvid/ui/shell_controller.py`

Expected: clean. `shell_controller.py` is 1,027 lines, under the default 1,200.

- [ ] **Step 7: Commit**

```bash
git restore uv.lock
git add src/korvid/ui/shell_controller.py tests/ui/test_node_shell.py docs/ops.md
git commit -m "fix(shell): refuse a node shell whose node identity cannot be verified

kubectl addresses the node by name only. The approved UID is now required
and must be read back unchanged; an unknown or unreachable answer refuses,
as pod debug and transfer have since #354.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.2: The proposal size-bound test can fail again

**Why:**
- `test_oversized_arguments_are_rejected_before_any_cluster_io` counts calls by patching `app._target_uid`.
- The proposal path never calls `app._target_uid`. Its order (`src/korvid/ui/proposal_controller.py:304-313`) is size check, then `self._writes.permitted`, then `self._builder().target_uid`, and the builder reads `app._get_manifest`.
- A mutation that moves either cluster call above the size check leaves the test green.
- `KorvidApp._target_uid`'s docstring makes the same wrong claim.

**Files:**
- Modify: `tests/ui/test_proposals_ui.py` (`test_oversized_arguments_are_rejected_before_any_cluster_io`, ~:927-954)
- Modify: `src/korvid/ui/app.py:1414-1418` (docstring only; the line count does not change)

**Interfaces:**
- Produces: test helper `_io_probe_app(tmp_path: Path, store: ProposalStore, io_calls: list[str]) -> KorvidApp`, local to the test module.

- [ ] **Step 1: Write the probe, its control test, and the rewritten test**

In `tests/ui/test_proposals_ui.py`, insert directly before `test_oversized_arguments_are_rejected_before_any_cluster_io`:

```python
def _io_probe_app(tmp_path: Path, store: ProposalStore, io_calls: list[str]) -> KorvidApp:
    """An app whose RBAC check and manifest reads record themselves in
    `io_calls`: the two cluster round trips a proposal can trigger before
    it is queued (the UID lookup reads the manifest)."""

    async def counting_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        io_calls.append(f"rbac:{verb}")
        return True

    app = make_app(Recorder(), tmp_path / "a.jsonl", store, check_permission=counting_permission)
    original = app._get_manifest
    assert original is not None

    async def counting_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        io_calls.append(f"manifest:{name}")
        return await original(kind, ns, name)

    app._get_manifest = counting_manifest
    return app


async def test_io_probe_sees_a_normal_proposal_reach_the_cluster(tmp_path: Path) -> None:
    """Control for the test below: without this, a probe on a seam the
    proposal path never calls would pass vacuously."""
    store = ProposalStore()
    io_calls: list[str] = []
    app = _io_probe_app(tmp_path, store, io_calls)
    async with app.run_test():
        result = await _submit(app)
    assert not result.startswith("ERROR:")
    assert {"rbac:delete", "manifest:web"} <= set(io_calls)
```

Then replace the setup of `test_oversized_arguments_are_rejected_before_any_cluster_io`. The docstring stays. The lines from `rec = Recorder()` through `app._target_uid = counting_uid  # type: ignore[assignment]  # test seam` become:

```python
    store = ProposalStore()
    io_calls: list[str] = []
    app = _io_probe_app(tmp_path, store, io_calls)
```

Replace its assertion `assert manifest_calls == []  # no UID lookup: rejected before cluster I/O` with:

```python
    assert io_calls == []  # rejected before the RBAC check and the UID lookup
```

- [ ] **Step 2: Prove the rewritten test can fail (temporary mutation)**

In `src/korvid/ui/proposal_controller.py`, temporarily move the size-check block (:304-308) below the `permitted` block (:309-311).

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/ui/test_proposals_ui.py -k "oversized_arguments or io_probe"`

Expected: `test_oversized_arguments_are_rejected_before_any_cluster_io` FAILS with `assert ['rbac:delete'] == []`, and the control passes.

Revert the mutation: `git checkout src/korvid/ui/proposal_controller.py`.

- [ ] **Step 3: Run the tests on the unmutated source**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/ui/test_proposals_ui.py`

Expected: all pass.

- [ ] **Step 4: Correct the misleading docstring**

In `src/korvid/ui/app.py`, replace `KorvidApp._target_uid`'s docstring:

```python
        """Uid of a write target at request time, for the flows that are not
        the agent's own: the interactive shell, the transfer pre-checks and
        the proposal execution path all bind their approval to one exact
        object incarnation through the same lookup."""
```

with:

```python
        """Uid of a write target at request time, for the interactive flows
        outside the agent's own: pod debug, the node shell and the transfer
        pre-checks bind their approval to one exact object incarnation through
        the same lookup."""
```

Run: `UV_NO_SYNC=1 uv run python scripts/check_source_size.py`

Expected: exit 0, with `app.py` still at 1,477 lines.

- [ ] **Step 5: Commit**

```bash
git restore uv.lock
git add tests/ui/test_proposals_ui.py src/korvid/ui/app.py
git commit -m "test(proposals): count the cluster calls the proposal path really makes

The size-bound test patched app._target_uid, which proposals never call, so
moving the RBAC check or UID lookup above the size bound stayed green. Probe
check_permission and get_manifest instead, with a control that sees them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.3: The engine's own response budget is reported as korvid's limit

**Why:**
- `ProviderResponseLimitError(RuntimeError)` predates the operator-safe vocabulary (#336).
- `_failure_message` therefore shows the operator "the provider request failed (ProviderResponseLimitError) — its own message is withheld because provider errors can quote the request or a credential". That is wrong, and it is no help to the operator. The bound is korvid's, and the low tier hits it in normal use with thinking models.
- `agent/provider.py` already declares the right sentence, `STREAM_LIMIT`, on `ProviderStreamLimitError`.
- `docs/provider-plugins.md:315` cites the class name, so it stays.

**Files:**
- Modify: `src/korvid/agent/native_engine.py` (import at :70, class at :95-96, raise at :527-529)
- Test: `tests/agent/test_engine_contract.py` (assertions at :436 and :461)

**Interfaces:**
- Consumes: `korvid.agent.provider.STREAM_LIMIT: str` and `ProviderStreamLimitError(OperatorSafeProviderError)`.
- Produces: `ProviderResponseLimitError(ProviderStreamLimitError)`. The public name is unchanged.

- [ ] **Step 1: Write the failing assertions**

In `tests/agent/test_engine_contract.py`:

1. Add `from korvid.agent.provider import STREAM_LIMIT` after the `from korvid.agent.events import (...)` block.
2. Replace both occurrences, in the two response-budget tests near :436 and :461, of:

```python
    assert "ProviderResponseLimitError" in errors[-1].message
```

with:

```python
    assert errors[-1].message == STREAM_LIMIT  # korvid's own bound, not a withheld failure
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/agent/test_engine_contract.py`

Expected: 2 failed. The message is `"the provider request failed (ProviderResponseLimitError) — its own message is withheld …"`.

- [ ] **Step 3: Implement**

In `src/korvid/agent/native_engine.py`:

1. Replace `from korvid.agent.provider import OperatorSafeProviderError` with:

```python
from korvid.agent.provider import (
    STREAM_LIMIT,
    OperatorSafeProviderError,
    ProviderStreamLimitError,
)
```

2. Replace:

```python
class ProviderResponseLimitError(RuntimeError):
    """A provider stream exceeded the resolved turn response budget."""
```

with:

```python
class ProviderResponseLimitError(ProviderStreamLimitError):
    """A provider stream exceeded the resolved turn response budget.

    The bound is korvid's own, so the operator sees `STREAM_LIMIT` rather
    than the withheld-failure text reserved for a provider's own errors.
    """
```

3. Replace:

```python
                    raise ProviderResponseLimitError(
                        f"provider response exceeded the {response_limit}-character policy limit"
                    )
```

with:

```python
                    raise ProviderResponseLimitError(STREAM_LIMIT)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/agent/ && UV_NO_SYNC=1 uv run mypy src/korvid/agent/native_engine.py`

Expected: pass and clean. `native_engine.py` is 1,024 lines, under 1,200.

- [ ] **Step 5: Commit**

```bash
git restore uv.lock
git add src/korvid/agent/native_engine.py tests/agent/test_engine_contract.py
git commit -m "fix(agent): report the response budget as korvid's limit, not a withheld failure

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.4: `log_buffer_lines` refuses fractions and infinities

**Why:**
- `_parse_buffer_lines` catches only `TypeError` and `ValueError`.
- `log_buffer_lines: .inf` raises `OverflowError` out of `load_config`; this was reproduced, and startup crashes with a traceback.
- `12.9` is silently truncated to a 12-line buffer.
- `_parse_port` already handles both cases. One shared helper fixes the bug and shortens the file by 2 lines.

**Files:**
- Modify: `src/korvid/core/config.py` (`_parse_port` and `_parse_buffer_lines`, ~:1159-1182)
- Test: `tests/core/test_config.py` (after `test_log_buffer_lines_invalid_falls_back`)

**Interfaces:**
- Produces: `_whole_number(value: Any) -> int | None`, private to `core/config.py`.

- [ ] **Step 1: Write the failing test**

Insert directly after `test_log_buffer_lines_invalid_falls_back` in `tests/core/test_config.py`:

```python
def test_log_buffer_lines_rejects_fractional_and_infinite_floats(tmp_path: Path) -> None:
    """The floats mcp.port already refuses: int() would truncate 12.9 to a
    12-line buffer and raise OverflowError on .inf, crashing startup."""
    for raw in ("12.9", ".inf", "-.inf", ".nan"):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(f"log_buffer_lines: {raw}\n")
        assert load_config(cfg_file).log_buffer_lines == 5000
```

- [ ] **Step 2: Run it to verify it fails**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/test_config.py -k fractional_and_infinite`

Expected: FAIL with `assert 12 == 5000`.

- [ ] **Step 3: Implement**

In `src/korvid/core/config.py`, replace both `_parse_port` and `_parse_buffer_lines` with:

```python
def _whole_number(value: Any) -> int | None:
    """`value` as an int, or None when YAML gave something else: a bool
    (`true` would become 1), a fraction (int() truncates 7878.9), or
    .inf/.nan (int() raises OverflowError/ValueError)."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _parse_port(value: Any) -> int:
    """Coerce mcp.port to a valid TCP port; fall back to 7878."""
    port = _whole_number(value)
    return port if port is not None and 0 < port < 65536 else 7878


def _parse_buffer_lines(value: Any) -> int:
    """Coerce log_buffer_lines to a sane positive int; fall back to 5000."""
    lines = _whole_number(value)
    return lines if lines is not None and lines > 0 else 5000
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/test_config.py && UV_NO_SYNC=1 uv run python scripts/check_source_size.py`

Expected: pass. `config.py` is at 1,681/1,684.

- [ ] **Step 5: Commit**

```bash
git restore uv.lock
git add src/korvid/core/config.py tests/core/test_config.py
git commit -m "fix(config): log_buffer_lines refuses fractions and infinities like mcp.port

log_buffer_lines: .inf raised OverflowError out of load_config.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.5: The inline-secret refusal points at the profile's `auth.key`

**Why:**
- `_raise_if_secret_key_segment` tells the operator to "keep secrets in env vars such as agent.api_key_env".
- Flat `agent.api_key_env` is not in `_SUPPORTED_AGENT_KEYS`. Since profiles replaced it, startup rejects it, so the advice sends people from one error to another.

**Files:**
- Modify: `src/korvid/core/config.py` (`_raise_if_secret_key_segment`, ~:1541-1546)
- Test: `tests/core/test_config_profiles.py` (before `test_a_plural_inline_secret_is_refused_and_the_profile_still_survives`)

- [ ] **Step 1: Write the failing test**

```python
def test_the_inline_secret_refusal_points_at_the_profile_auth_key(tmp_path: Path) -> None:
    """The refusal tells the operator where the key belongs now. Flat
    `agent.api_key_env` is itself a configuration error since profiles
    replaced it, so recommending it sent people from one error to another."""
    path = _write(
        tmp_path,
        """
agent:
  active: main
  profiles:
    main:
      model: openai/gpt-4o
      options:
        api_key: inline-secret-value
""",
    )
    error = load_config(path).model_connections.profiles["main"].config_error

    assert error is not None
    assert "auth.key" in error
    assert "api_key_env" not in error
```

- [ ] **Step 2: Run it to verify it fails**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/test_config_profiles.py -k points_at_the_profile_auth_key`

Expected: FAIL. `"auth.key" in error` is false, because the message names `agent.api_key_env`.

- [ ] **Step 3: Implement**

Replace the message in `_raise_if_secret_key_segment`:

```python
        f"{_agent_options_path(f'{path}.{key}')} uses reserved "
        f"secret-bearing key segment {segment!r}; keep secrets in "
        f"env vars such as agent.api_key_env"
```

with:

```python
        f"{_agent_options_path(f'{path}.{key}')} uses reserved "
        f"secret-bearing key segment {segment!r}; keep secrets out of options "
        "and name an environment variable or keychain entry in the profile's auth.key"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/`

Expected: pass. `config.py` stays at 1,681.

- [ ] **Step 5: Commit**

```bash
git restore uv.lock
git add src/korvid/core/config.py tests/core/test_config_profiles.py
git commit -m "fix(config): point the inline-secret refusal at the profile's auth.key

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.6: One safe read-modify-write for config.yaml writers

**Why:** `save_topbar_state` mishandles several kinds of file:
- It replaces a list-root `config.yaml` with `{ui: ...}`, silently dropping the operator's content.
- It reads with the locale encoding.
- It lets `yaml.YAMLError` reach `action_toggle_topbar`'s notification (`app.py:1241-1254`), and YAML's message quotes the offending line, which may be a secret.

`save_keybindings` (#422) already reads safely, but its reader is private. A new `core/config_store.py` hosts the shared reader, takes `save_topbar_state` out of the frozen `config.py`, and is the module Phase 2 and Phase 3 extend.

**Files:**
- Create: `src/korvid/core/config_store.py`
- Modify: `src/korvid/core/config.py` (delete `save_topbar_state` ~:1115-1127; comment at ~:410)
- Modify: `src/korvid/core/keybinding_config.py`
- Modify: `src/korvid/__main__.py` (config import block, ~:75-84)
- Modify: `tests/ui/test_top_bar.py:14`
- Test: `tests/core/test_config_store.py` (new)

**Interfaces:**
- Consumes: `korvid.core.config.ConfigError` and `korvid.core.config._atomic_write_text(path: Path, text: str) -> None`. The private import is deliberate: Phase 3 moves `_atomic_write_text` into `config_store` and makes it public.
- Produces:
  - `korvid.core.config_store.read_config_document(path: Path, *, action: str) -> dict[str, Any]`
  - `korvid.core.config_store.save_topbar_state(path: Path, *, expanded: bool) -> None`

- [ ] **Step 1: Move `save_topbar_state` verbatim (no behaviour change)**

Create `src/korvid/core/config_store.py`:

```python
"""Read-modify-write persistence for the user's config.yaml.

`load_config` parses the file once, at startup. A writer here re-reads the
latest file at save time and changes only its own keys, so whatever the
operator edited by hand while korvid was running survives the save.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from korvid.core.config import _atomic_write_text


def save_topbar_state(path: Path, *, expanded: bool) -> None:
    """Persist the top bar collapse/expand choice (issue #142), preserving
    unrelated keys (same read-modify-write shape as save_model_connections)."""
    raw: dict[str, Any] = {}
    if path.is_file():
        loaded = yaml.safe_load(path.read_text())
        raw = loaded if isinstance(loaded, dict) else {}
    existing = raw.get("ui")
    ui: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    ui["topbar"] = "expanded" if expanded else "collapsed"
    raw["ui"] = ui
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(raw, sort_keys=False))
```

Then:

1. Delete the function from `src/korvid/core/config.py`, along with the two blank lines that separate it.
2. In `config.py`, change the `ui_topbar_expanded` field comment `#: runtime toggle persists the choice back through save_topbar_state.` to:

```python
    #: runtime toggle persists it through config_store.save_topbar_state.
```

3. In `src/korvid/__main__.py`, remove `save_topbar_state,` from the `from korvid.core.config import (...)` block. Add this line directly before `from korvid.core.keybinding_config import save_keybindings`:

```python
from korvid.core.config_store import save_topbar_state
```

   The net line change is zero; `__main__.py` stays at 1,767.

4. In `tests/ui/test_top_bar.py`, replace `from korvid.core.config import KorvidConfig, load_config, save_topbar_state` with:

```python
from korvid.core.config import KorvidConfig, load_config
from korvid.core.config_store import save_topbar_state
```

Run:

```bash
UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/ui/test_top_bar.py tests/core/
UV_NO_SYNC=1 uv run tach check
UV_NO_SYNC=1 uv run python scripts/check_source_size.py
```

Expected: pass, tach "All modules validated", and size exit 0. `config.py` is now 1,666.

- [ ] **Step 2: Write the failing tests**

Create `tests/core/test_config_store.py`:

```python
"""The config.yaml writers re-read the latest file and refuse one they
could not write back without losing what the operator put in it."""

from pathlib import Path

import pytest
import yaml

from korvid.core.config import ConfigError
from korvid.core.config_store import read_config_document, save_topbar_state


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(b"- prod\n- dev\n", "must be a mapping", id="list-root"),
        pytest.param(b"namespace: [prod\n", "malformed YAML", id="malformed"),
        pytest.param(b"namespace: \xff\xfe\n", "not UTF-8", id="not-utf8"),
    ],
)
def test_topbar_save_refuses_a_document_it_cannot_round_trip(
    tmp_path: Path, content: bytes, reason: str
) -> None:
    """The toggle used to replace a list root with `{ui: ...}`, dropping the
    operator's content, and let YAML errors (which quote the offending
    line) reach the notification."""
    path = tmp_path / "config.yaml"
    path.write_bytes(content)

    with pytest.raises(ConfigError, match=reason):
        save_topbar_state(path, expanded=True)

    assert path.read_bytes() == content


def test_topbar_save_keeps_non_ascii_content_as_utf8(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("namespace: café\n", encoding="utf-8")

    save_topbar_state(path, expanded=True)

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved == {"namespace": "café", "ui": {"topbar": "expanded"}}


@pytest.mark.parametrize("content", ["", "# only a comment\n"])
def test_an_empty_document_reads_as_an_empty_mapping(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")

    assert read_config_document(path, action="save the top bar state") == {}


def test_a_missing_file_reads_as_an_empty_mapping(tmp_path: Path) -> None:
    assert read_config_document(tmp_path / "absent.yaml", action="save keybindings") == {}
```

- [ ] **Step 3: Run them to verify they fail**

Run: `UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/test_config_store.py`

Expected: a collection-time `ImportError`, because `read_config_document` does not exist yet. To see the behavioural failures, temporarily import only `save_topbar_state`. The list-root case then fails with `DID NOT RAISE`, the malformed case with `yaml.parser.ParserError`, and the not-UTF-8 case with `UnicodeDecodeError`. Restore the import afterwards.

- [ ] **Step 4: Implement the shared reader and route both writers through it**

Replace the body of `src/korvid/core/config_store.py` below the module docstring with:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from korvid.core.config import ConfigError, _atomic_write_text


def read_config_document(path: Path, *, action: str) -> dict[str, Any]:
    """The latest config.yaml as a mapping a writer may update and save.

    A missing, empty or comment-only file is an empty mapping. A document
    that could not be written back without losing the operator's content
    is refused, and the refusal never quotes the file: a YAML error repeats
    the offending line, which may hold a secret.

    Args:
        path: The shared configuration file.
        action: What the caller saves, for the refusal ("save keybindings").

    Returns:
        The parsed document, which the caller may modify.

    Raises:
        ConfigError: The file is not UTF-8 text, is malformed YAML, or its
            root is not a mapping (an explicit `null` included).
        OSError: Reading the file failed for a reason other than absence.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Cannot {action}: config is not UTF-8 text") from exc
    try:
        document = yaml.safe_load(content)
        if document is None:
            node = yaml.compose(content, Loader=yaml.SafeLoader)
            if node is None or node.start_mark.index == node.end_mark.index:
                document = {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Cannot {action}: config contains malformed YAML") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"Cannot {action}: config must be a mapping")
    return document


def save_topbar_state(path: Path, *, expanded: bool) -> None:
    """Persist the top bar collapse/expand choice (issue #142), preserving
    every other key.

    Raises:
        ConfigError: The current file cannot be updated safely; see
            `read_config_document`.
        OSError: Reading or atomically writing the configuration failed.
    """
    document = read_config_document(path, action="save the top bar state")
    existing = document.get("ui")
    ui: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    ui["topbar"] = "expanded" if expanded else "collapsed"
    document["ui"] = ui
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, yaml.safe_dump(document, sort_keys=False))
```

The reader body is `save_keybindings`'s existing reader, made general by `action` and extended with the `UnicodeDecodeError` case. In `src/korvid/core/keybinding_config.py`:

1. Replace `from korvid.core.config import ConfigError, _atomic_write_text` with the two lines below. Keep `_atomic_write_text` imported by name, because `tests/core/test_keybinding_config.py` monkeypatches `keybinding_config._atomic_write_text`.

```python
from korvid.core.config import _atomic_write_text
from korvid.core.config_store import read_config_document
```

2. Replace the inline reader, from `try:` / `content = path.read_text(encoding="utf-8")` through `raise ConfigError("Cannot save keybindings: config must be a mapping")`, with:

```python
    document = read_config_document(path, action="save keybindings")
```

3. Update its `Raises:` entry to:

```python
        ConfigError: The current file is not UTF-8 text, is malformed YAML,
            or its root is not a mapping.
```

- [ ] **Step 5: Run the tests to verify they pass**

```bash
UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/test_config_store.py tests/core/test_keybinding_config.py tests/ui/test_top_bar.py tests/ui/test_keybinding_editor_screen.py tests/ui/test_keybinding_editor_workflow.py
UV_NO_SYNC=1 uv run mypy src/korvid/core/config_store.py src/korvid/core/keybinding_config.py
UV_NO_SYNC=1 uv run tach check
```

Expected: everything passes. The existing keybinding tests still match `"YAML"` and `"mapping"`.

- [ ] **Step 6: Commit**

```bash
git restore uv.lock
git add src/korvid/core/config_store.py src/korvid/core/config.py src/korvid/core/keybinding_config.py src/korvid/__main__.py tests/core/test_config_store.py tests/ui/test_top_bar.py
git commit -m "fix(config): top-bar save refuses a config it cannot round-trip

Move save_topbar_state into core/config_store.py beside a shared
read_config_document (save_keybindings' reader, plus a UTF-8 check). A
list root is no longer replaced, and a YAML error no longer quotes the
file into the toggle's notification.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

### Task 1.7: An unreadable config.yaml is one `korvid: ...` line, not a traceback

**Why:**
- `load_config` calls `yaml.safe_load(cfg_path.read_text())` unguarded.
- `__main__._load_startup_config` (:883-892) turns only `ConfigError` into `SystemExit(f"korvid: {exc}")`. A malformed file therefore escapes as a traceback whose YAML message quotes the offending line, secret included.
- The locale-default read also garbles UTF-8 on Windows (cp1252).

**Prerequisite:** Task 1.6, which frees the 15 lines this task spends.

**Files:**
- Modify: `src/korvid/core/config.py` (`load_config` ~:492; new `_unreadable` before `_whole_number`)
- Test: `tests/core/test_config.py`, `tests/core/test_config_profiles.py` (`test_a_sequence_profile_key_never_reaches_korvid` ~:1025-1039)

**Interfaces:**
- Produces: `_unreadable(exc: OSError | UnicodeDecodeError | yaml.YAMLError) -> str`, private.
- Changes: `load_config` now raises `ConfigError` (with `__cause__` set) for unreadable, non-UTF-8 or malformed files. Its only caller is `__main__.py:887`.

- [ ] **Step 1: Write the failing tests**

In `tests/core/test_config.py`, insert after `test_config_root_must_be_a_mapping` and before `test_unknown_agent_setting_is_rejected`:

```python
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        pytest.param(
            b"namespace: prod\nfavorite_namespaces: [prod, dev\n",
            "malformed YAML at line 3, column 1",
            id="unclosed-flow-sequence",
        ),
        pytest.param(
            b"namespace: prod\n  api_key: sk-live-SECRET\n",
            "malformed YAML at line 2, column 10",
            id="bad-indent",
        ),
        pytest.param(b"namespace: \xff\xfe\n", "the file is not UTF-8 text", id="not-utf8"),
    ],
)
def test_an_unreadable_config_is_one_config_error_that_never_quotes_the_file(
    tmp_path: Path, content: bytes, expected: str
) -> None:
    """Startup turns ConfigError into one `korvid: ...` line; anything else
    escaped as a traceback that quoted the offending line, secret included."""
    path = tmp_path / "config.yaml"
    path.write_bytes(content)

    with pytest.raises(ConfigError, match=expected) as caught:
        load_config(path)

    assert "SECRET" not in str(caught.value)
    assert "\n" not in str(caught.value)


def test_config_is_read_as_utf8_whatever_the_locale(tmp_path: Path) -> None:
    """Windows CI runs under a cp1252 locale, where `read_text()` without an
    encoding garbles a UTF-8 "café" into mojibake."""
    path = tmp_path / "config.yaml"
    path.write_text("namespace: café\n", encoding="utf-8")

    assert load_config(path).namespace == "café"
```

In `tests/core/test_config_profiles.py`:

1. Add `ConfigError,` to the `from korvid.core.config import (...)` block, before `ConfigFileModelConnectionsWriter,`.
2. Replace `test_a_sequence_profile_key_never_reaches_korvid`'s docstring and assertion. It pins the old raw `yaml.YAMLError`, and it already proves the document fails as a whole.

```python
def test_a_sequence_profile_key_never_reaches_korvid(tmp_path: Path) -> None:
    """The tuple-like key YAML can spell is one `safe_load` refuses to
    build, so the document fails as a document — korvid is never handed
    half a profile set to preserve. The refusal names the place, not the
    text: startup prints it as one `korvid: ...` line."""
    path = _write(
        tmp_path,
        """
agent:
  profiles:
    ? [a, b]
    : {model: openai/gpt-4o}
""",
    )
    with pytest.raises(ConfigError, match="malformed YAML at line 4, column 7") as caught:
        load_config(path)

    assert isinstance(caught.value.__cause__, yaml.YAMLError)
```

- [ ] **Step 2: Run them to verify they fail**

```bash
UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/test_config.py -k "never_quotes_the_file or utf8_whatever_the_locale"
UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/test_config_profiles.py -k sequence_profile_key
```

Expected:
- The first command shows 3 failed and 1 passed. The three parametrized cases fail with a raw `yaml` `ScannerError`/`ParserError` or a `UnicodeDecodeError`. The UTF-8 test passes on macOS/Linux and guards Windows.
- The second command shows 1 failed, with a raw `ConstructorError` instead of `ConfigError`.

- [ ] **Step 3: Implement**

In `load_config`, replace:

```python
    loaded = yaml.safe_load(cfg_path.read_text())
```

with:

```python
    try:
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot load {cfg_path}: {_unreadable(exc)}") from exc
```

Insert directly before `_whole_number` (added in Task 1.4):

```python
def _unreadable(exc: OSError | UnicodeDecodeError | yaml.YAMLError) -> str:
    """Why config.yaml could not be read, on one line that never quotes the
    file: YAML's own message repeats the offending line, which may be a secret."""
    if isinstance(exc, UnicodeDecodeError):
        return "the file is not UTF-8 text"
    if isinstance(exc, OSError):
        return exc.strerror or type(exc).__name__
    mark = getattr(exc, "problem_mark", None)
    where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
    return f"malformed YAML{where}"
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
UV_NO_SYNC=1 uv run pytest -p no:tach -q tests/core/
UV_NO_SYNC=1 uv run mypy src/korvid/core/config.py
UV_NO_SYNC=1 uv run python scripts/check_source_size.py
```

Expected: pass, clean, and exit 0. `config.py` is at 1,681/1,684.

- [ ] **Step 5: Run the full gate for the phase**

Run: `UV_NO_SYNC=1 make check && UV_NO_SYNC=1 uv run tach check`

Expected: green. Without a worktree `.venv`, use the fallback command from Global Constraints on `tests/`. The only failures should be the environmental ones listed there.

- [ ] **Step 6: Commit**

```bash
git restore uv.lock
git add src/korvid/core/config.py tests/core/test_config.py tests/core/test_config_profiles.py
git commit -m "fix(config): an unreadable config.yaml is one ConfigError that never quotes it

load_config read with the locale encoding and let YAML errors escape as a
traceback quoting the offending line. Read UTF-8 and report only the
position.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase 2 — Consolidate rules that drifted (work packages)

Write a detailed per-phase plan for these before you execute them. Each package lists its evidence at `1c02aed4`. Each package is one commit with TDD where behaviour is pinned. Moved code stays verbatim.

### 2.1 Write-path primitives on the gate

**Approach:** collapse the hand-copied write-path primitives onto `WriteGate`, in this order. 2.1.c needs 2.1.b.

**a. One reservation primitive.** Today it is copied three times:
- `write_coordinator.py:282-292`
- `shell_controller.py:125-141`
- `operator_controller.py:611-637`

Add a module-level `reserved_call(...)` function in `ui/write_gate.py`. It cannot be an ABC method, because `tests/ui/test_shell_reservation.py`'s duck-typed `_RecordingGate` would break. Frees about 50 lines.

**b. Bring `WriteGate` into line with what controllers call.**
- Add `crossed(epoch) -> bool` to replace 9 inline epoch comparisons:
  - forward :282/:374/:384
  - helm :501
  - shell :294/:431
  - transfer :258/:313/:331
- Add `approval_guard`/`impact_lines` to the abstract `confirm`.
- Make `confirm_screen` and `audit_write` abstract.
- Type the `Callable[..., X]` parameters.
- `StubGate` (`tests/ui/test_forward_controller.py:46-86`) gains stubs.

**c. Move approval callbacks onto the gate.** Transfer, debug retry and operator install currently hand-roll their callbacks; move them onto the gate's approval path.
- Keep the phrase "changed during the approval dialog"; tests assert it.
- Keep the pins in `tests/ui/test_debug_controller.py:354`.

**d. One UID re-check.** Generalise Task 1.1's node check and `pod_uid_unchanged` into `uid_unchanged(kind, namespace, name, approved_uid, *, action)`, used by pod debug, transfer and the node shell.

**e. Single target-manifest lookup.** Route `shell_controller._debug_manifest` (:506-539, with its own `_UID_LOOKUP_TIMEOUT`) through the shared target-manifest lookup. Do this together with 3.4 if that lands first.

**Risk:** low-med. Approval and revalidation are security code: keep the step order, and characterise each flow before you move it. The node-shell fail-closed behaviour from 1.1 must survive 2.1.d unchanged.

**Acceptance:**
- `grep -n "epoch !=" src/korvid/ui/*_controller.py` returns nothing.
- `reserved_call` is used in all three call sites.
- The full gate is green.

### 2.2 Config writers share one reader; observability scopes share one mask

- `save_model_connections` (`config.py:~1073`, `yaml.safe_load(...) or {}`) raises `AttributeError` on a list-root file. Route it through `read_config_document` (Task 1.6), with a list-root RED test like Task 1.6's.
- **Break the `config` ↔ `config_store` cycle first.** `config_store.py` imports `ConfigError` and `_atomic_write_text` from `config.py` at module level, so `config.py` cannot import `config_store` back.
  - Move `ConfigError` and `_atomic_write_text` into `config_store` (public `atomic_write_text`). `config.py` then imports them from there, and `config_store` no longer imports `config`.
  - Keep `korvid.core.config.ConfigError` importable for its many importers, either as a re-export of the moved name or by editing them; decide in the 2.2 plan. This pulls Phase 3.2's `_atomic_write_text` move forward.
- Align the two readers' wording while there: `read_config_document` reuses `_unreadable` for the line/column, and one capitalisation is used. Decide whether a `null`/`~` document reads as `{}` in both (today startup accepts it and the savers refuse it).
- Add `QueryScope.masked()` to replace the two `_masked_scope` copies, at `obs/loki.py:173-182` and `obs/prometheus.py:175-184`.

**Size:** S. **Risk:** low.

### 2.3 UI wiring hygiene

- Remove the dead `pattern` parameter of `ResourceTable.show` end to end: 31 occurrences, about 22 test edits. `app.py:620-627` passes `pattern=""` with the comment "no name pattern remains".
- Give `LogController` and `HintController` one `context: ContextGuard` instead of bound methods. This frees 4 lines in `__main__.py:1269-1272`.

**Size:** S. **Risk:** low.

### 2.4 Provider adapters agree on one usage rule and one fold

- **Usage rule.** Use one implementation of the documented rule (`docs/provider-plugins.md:321-327`: non-bool ints 0..1,000,000,000) in Copilot, native Ollama and LiteLLM.
  - Promote `agent/diagnostics.py::_metric_count` to public, or add a private `providers/_usage.py`.
  - Copilot's `int(...)` currently turns `True` and `3.9` into counts.
- **Copilot tool-call fold** (`flow_copilot.py:448-459`, the pre-#364 fold) gets LiteLLM's semantics:
  - skip a bad index
  - coerce non-string fields
  - first id/name wins
  - ignore a non-dict `usage`

  Test it through `complete()`; no test touches the private helpers.
- **Credential names.** Add `providers/credential_names.py` and use it from both the LiteLLM route and the native Ollama flow (`flow_ollama_thinking.py:563-583`).
  - The native flow does not strip the key name and has no `profile.model` keyring fallback that `docs/agent.md:279` promises.
  - Keep the names `_credentials_for` and `_from_keyring`; `tests/test_docs_agent_contracts.py` slices and imports them.
- **Capability provenance.** `agent/model_policy.py:173` stamps `PROVIDER` on every provider-supplied fact and ignores `provider.provenance`. Keep the declared `CapabilitySource` (guard with `isinstance`), and add one sentence to `docs/evals/methodology.md`. Do not relabel native Ollama's `num_ctx`.

**Size:** S each. **Risk:** low; only malformed input changes behaviour.

### 2.5 k8s errors, write requests and telemetry

All of this stays inside `src/korvid/k8s`.

- **K1 transport errors.** Writes, exec, log streams and the Warning watch still leak raw transport exceptions. Put them under the `errors.py` contract that #367 gave reads, with a `translate_transport_errors` helper in `k8s/transport.py`.
  - Add a `WriteOutcomeUnknown` for timeouts after a write was sent.
  - `drain.py:242` currently treats any `TimeoutError` as its own settle timeout; keep that working.
- **K2 write requests.** Each write and its dry-run preview restate the request by hand. Build both from one private `_WriteRequest` plus `_preview_diff`.
- **K5 read telemetry.** Replace the 8 copy-pasted read-telemetry sites, which have drifted, with one `_observed()`.

**Size:** S each, net negative in `client.py` (1,944/1,944). **Risk:** low-med for K1; it changes exception types callers see, so grep every `except` on the write path.

---

## Phase 3 — Win back headroom in frozen modules (work packages)

Each package ends by lowering, or deleting, the module's `MODULE_LIMITS` entry. Each also updates `docs/dev/ui-controllers.md` where controllers move. Lowering the cap is the RED step, and the verbatim move is the GREEN step.

| # | Package | Frees | Notes |
|---|---|---|---|
| 3.1 | `__main__` lifecycle → `composition_support.py` | ~105 lines | Keep the cap at 1,773; the room is for wiring. **Step A:** constants are single-sourced in support; delete `_sync_cleanup_limits` and the `_shutdown`/`_teardown` pass-throughs; retarget the 14 test writes plus `test_main_recovery.py:153`, dropping `raising=False`; add a source-scan test rejecting `main_mod._CLEANUP_`/`_MCP_SHUTDOWN_`/`_RUNNER_SHUTDOWN_`. **Step B:** move `RESTART_CAP`, `RESTART_WINDOW_SECONDS`, `_run_with_recovery`, `_restart_prompt` and `_close_runner`, re-exported in the root's `__all__`. |
| 3.2 | Config split | `config.py` → ~1,130 lines; exception deleted | Create `core/config_observability.py` (338 lines, re-exported from `config.py`, since many importers use it). Move `save_model_connections` and `_atomic_write_text` (made public) into `config_store.py`. Add new files to `tests/test_vendor_neutrality.py:54`. The options security check stays in `config.py`. |
| 3.3 | `ui/context_guard.py` + `ui/workspace_ports.py` | `workspace_controller.py` cap → ~1,508 | Move `ContextGuard` and `WorkspaceSurface` (:115-214) out. Update the 12 importers directly; this also lifts `object_navigation.py:15`'s `TYPE_CHECKING` workaround. Delete the dead `KeyEvent`. Fix `ui-controllers.md:331,338`. |
| 3.4 | `ui/write_targets.py` (`WriteTargets`) | ~300 lines out of `agent_ui_controller.py` | 13 stateless methods (:1999-2293) plus `UID_LOOKUP_TIMEOUT`/`TargetIdentityUnavailable`. Inject into proposals, inspect, shell, transfer, resource writes and the agent controller with late-bound lambdas. Delete `app.py:1414-1429`. Add to `RUNTIME_COMPONENTS`. Needs a 1-line `__main__` trim first, or 3.1. The 3 `UID_LOOKUP_TIMEOUT` string patches move with no re-export. |
| 3.5 | `ui/manifest_edit.py` | ~150 lines out of `resource_write_controller.py` (1,193/1,200) | Pure helpers with existing tests. |
| 3.6 | `ui/widgets/incremental_table.py` (`IncrementalTable(DataTable)`) | `resource_table.py` exception deleted | ~450 lines from :203-298, :741-1052 and :1065-1082. **Initialize in `__init__`, not `on_mount`**: Textual runs `on_mount` on every class in the MRO. |
| 3.7 | `tools/` splits | Both exceptions deleted | **1a:** `tools/outcome.py`, `tools/list_facts.py`, `tools/tool_args.py`. **1b:** `tools/diagnosis_reads.py` (`DiagnosisReads(kube, aliases)`, :1271-2025 verbatim), built inside `ToolExecutor.__init__`. Keep the four `_diagnose_*` delegators on `ToolExecutor`, plus `_resource_parts`/`_projected` in `executor.py`. Then move the declarative tool catalog out of `registry.py`. Do not name a module `diagnosis.py` (`tools/diagnose.py` exists). |
| 3.8 | k8s headroom | ~110 lines out of `client.py`, then lower its cap | **K3:** `k8s/paths.py` with `path_segment`, restoring the traversal docstring #393 cut; `k8s/kubeconfig.py`; `_require_api()`/`_require_core()`; one `_install()`. Keep the test seams named in the k8s report: `load_refreshable_kube_config` by name, `k8s_config.load_kube_config` via module attribute, `_PROBE_TIMEOUT` in `client.py`. **K7:** split `k8s/models.py` (1,175) only when it blocks a change. |
| 3.9 | Shell forwarders and wiring guards | ~30 lines out of `app.py` | Delete the five one-line forwarders (`_edit_in_external_editor`, `_node_target`, `_target_uid`, `_managed_note`, `_managed_note_from`) by wiring from the late references; do this after 3.4. Pulse gets `get_manifest=lambda kind, ns, name: app._get_manifest(kind, ns, name)` (late-bound like the other seven). `RUNTIME_COMPONENTS` gains `PulseController`, `KeybindingController` and `AppKeybindingSurface`, plus a test that every `AppRuntime` field type is listed. Add an AST test that `_MODAL_SCREEN_TYPES` covers every `ModalScreen` subclass. |

**Order:** 3.1 → 3.4 → 3.9, because each frees the lines the next spends. The rest are independent.

---

## Phase 4 — Docs and dead code (one PR)

**Stale docs and comments:**
- `write_coordinator.py:1059-1062` cites `#297`.
- These say "stays on the app" for code that moved: `drain.py:9-11`, `operator_controller.py:1-18`, `helm_controller.py:150-158`.
- `docs/dev/ui-controllers.md`:
  - :331 and :338 (`ContextGuard` home, after 3.3)
  - :406
  - :500-524: profiles, not "settings"; `_AgentToolUIBridgeProxy`/`AgentToolUIBridge`; the proposal path goes through the builder, not `_target_uid`
  - :612-620 (late binding, after 3.9)
- `docs/dev/specs/2026-08-12-korvid-architecture.md`:
  - :129-133 (construction moved to `__main__` in #385)
  - §4 (agent writes refuse without an identity; not "UID if available")
- `AGENTS.md` "New `UIBridge` method" gotcha: replace the hand-kept fake list with the rule "update every `(UIBridge)` subclass: `grep -rn '(UIBridge)' src tests`". Today those subclasses are:
  - `tests/tools/executor_fakes.py:79`
  - `tests/evals/operation_app.py:130`
  - `tests/agent/test_tool_harness.py:152`
  - `ui/agent_ui_controller.py:401`
  - `composition_support.py:312`
- `mcp/server.py:139-140` and `tests/mcp/test_server.py:585` cite the deleted `agent/tools.py`.
- `agent/provider.py:319-322` and `providers/litellm_provider.py:121` name modules deleted in #364/#377.
- `config.py`: `_AgentOptionCounters.root`'s unused default; the `_AgentOptionsError` and `_parse_bounded_options` docstrings.

**Delete (grep-verified unreferenced or test-only):**

| Symbol | Location |
|---|---|
| `KeyEvent` | `ui/workspace_ports.py:61`, if 3.3 has not already deleted it |
| `_is_unparsed_name` | `ui/widgets/profile_manager_screen.py:79` |
| `_storage_class_is_default` | `k8s/models.py:482` |
| `_unpaired` | `evals/operation_grader.py:182` |
| `overlay_ids` | `evals/harness.py:256` |
| `make_http_client_factory` | `providers/net.py:79`; tests switch to `make_client(ca, timeout=15.0)` |
| `manual_entry` | `providers/litellm_catalog.py:445` |
| `_CATALOG_INDEX`/`get_catalog_entry` | `agent/model_catalog.py:48-56`, plus its `__all__` entry; rewrite 3 assertions through `ModelRouter(...).route_source` |
| `AGENT_EXTRA` | `providers/litellm_settings.py:9` |
| `_AUTH_ENV_KEY_SETTING` | `config.py:~1112` |
| `describe_body_text` | `ui/widgets/describe_screen.py:195`; its only caller is `tests/ui/test_describe.py:454-458` (which claims an agent-bridge use that no longer exists), so delete that test too |

`redact_manifest` (`core/redaction.py:511`): either delete it, or return redaction records and use it from `executor._mask_manifest`. Pick one in the per-phase plan.

**Size:** S. **Risk:** none.

---

## Product Decisions (needed before the packages that depend on them)

| Decision | Blocks | Options |
|---|---|---|
| Node shell fails closed when the cluster cannot confirm the node | Task 1.1, 2.1.d | Approve (recommended; matches #354) / keep fail-open and document it |
| `open_evidence` citation navigation | Any `agent_ui_controller` work after 3.4 | Its ~220 lines are reachable from no command or key, although `docs/agent.md:57-58` promises it. Wire it (e.g. `:ai evidence <ref>`, resolving through the evidence ledger) / retire the code, field, tests and 3 docs |
| Debug, transfer and node-shell writes on the session timeline | 2.1 | Route through `audit_write` like other writes / keep them off the timeline and document why |
| Unary API transport deadlines (K4) | 2.5 follow-up | Pick connect/idle-read values. The existing constants are 10 s. Watches and log follow stay unbounded |

## Deferred (revisit only when the trigger occurs)

| Item | Trigger |
|---|---|
| `AgentWriteGate` extraction from `agent_ui_controller.py` (~200 lines; characterise the step order first) | The agent write path next changes |
| `AgentProfileController` (~300 lines) | Profile/setup work next lands |
| `agent_view_tools.py`/`agent_ports.py`, to delete the 2,450 exception | After 3.4 and the two above |
| `hierarchy_controller.py` from `workspace_controller.py`; `NodeWriteController` | The workspace or node write flows need room |
| `target_intact` unification of revalidation in `ResourceWriteController` | Note that an edit with `origin` adds a user-visible cancel |
| `UIBridge` split | The bridge next changes |
| Helm list decodes only the newest revision per release | Helm list performance becomes a complaint |

## Do Not Refactor

These look like debt but are deliberate. Changing them costs more than it returns, or weakens a pinned guarantee.

- **Composition root:**
  - `_construct_app_runtime`'s 407 linear lines
  - the 43-keyword `KorvidApp` signature
  - `composition_support.py`
  - the 13 `App*` adapters (Textual's metaclass conflicts with `ABCMeta`)
  - `_LateReference` and the late-bound lambdas
- **UI:**
  - `ContextSwitchCoordinator` and `_switch_locked` (an ordered transaction pinned by 57 tests)
  - the `jump_to_object` and `ResourceTable.show` signatures
  - a `WriteRequest` template or a `confirm` parameter object
- **Agent:**
  - the session/turn/follow core of `AgentUiController`
  - `agent_request_write`'s 87 linear lines (splitting hides the security order)
  - the two `APPROVAL_TIMEOUT` constants
  - `agent/outbound.py`
  - the httpx duplication between the two native flows (revisit at a third)
  - the engine's lenient `_as_int`
  - evals mirroring `_build_session`
- **Core / obs:** `HttpBackend.get_json`, `ForwardRegistry.reattach`, `AuditLog.append`, `ProposalStore.submit`.
- **MCP:** `mcp/registry.py`, MCP `_dispatch`.
- **k8s:** splitting `KubeClient` into services or mixins (K6). Consumers already see narrow ABCs; 237 test constructions and 288 private patches are tied to the class.

## Sequencing

1. **Phase 1.** One PR; Task 1.1 only with sign-off. It unblocks nothing else, but its bugs are live.
2. **Phase 4.** Low-risk doc and dead-code PR. It can run in parallel with Phase 2.
3. **Phase 2:**
   - 2.1 a→b→c→d→e
   - 2.2, 2.3, 2.4 and 2.5 are independent.
4. **Phase 3:**
   - 3.1 → 3.4 → 3.9 first, because they unblock the next feature PRs in `__main__`, `agent_ui_controller` and `app.py`.
   - The remaining packages are independent; schedule each when a feature needs room in that module.
5. **Deferred items:** only when their trigger occurs.
