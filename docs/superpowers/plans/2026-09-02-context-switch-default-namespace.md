# Context-Switch Default Namespace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a successful context switch to an unset kubeconfig namespace adopt Kubernetes' concrete `default` namespace, while a failed mid-swap recovery restores the exact pre-switch concrete session namespace.

**Architecture:** Preserve `ContextSwitchResult.context_namespace` as the raw `str | None` kubeconfig fact. Successful target adoption continues to normalize it at `AppSessionConfiguration.adopt()`, while the coordinator snapshots the session's concrete default namespace before the first swap and passes that value only to recovery adoption after a failed retarget. Existing ordering still resets workspace, watches, metrics, and namespace fallbacks from the adopted concrete config value.

**Tech Stack:** Python 3.11+, Textual, asyncio, pytest, Ruff, mypy

## Global Constraints

- An unset target context namespace becomes exactly `"default"`.
- The previous context's namespace must never be inherited or requested on the new cluster.
- Failed target-switch recovery must restore the exact pre-switch concrete session namespace.
- `ContextSwitchResult.context_namespace` remains `str | None`.
- Probe/switch failure must not modify the old session.
- Preserve context-switch ordering, proposal expiry, audit retargeting, and recovery.
- Do not change kubeconfig resolution, all-namespaces semantics, or unrelated controllers.
- Use `tests/ui/waits.py::until()` rather than wall-clock assertions.

---

### Task 1: Distinguish target adoption from recovery restoration

**Files:**
- Modify: `src/korvid/ui/context_switch_coordinator.py`
- Modify: `src/korvid/ui/app.py:2662-2682`
- Modify: `tests/ui/test_context_switch_coordinator.py`
- Test: `tests/ui/test_ctx_switch.py`
- Test: `tests/ui/test_context_switch_coordinator.py`

**Interfaces:**
- Consumes: `ContextSwitchResult.context_namespace: str | None`
- Consumes: `SessionConfiguration.default_namespace() -> str`
- Produces: `SessionConfiguration.adopt(..., namespace: str | None = None)`
- Produces: successful adoption resolves `result.context_namespace or "default"`
- Produces: recovery adoption reuses the snapped pre-switch concrete namespace

- [ ] **Step 1: Add the failing coordinator recovery regression**

Extend the coordinator harness to accept `session_namespace="team-old"` and add:

```python
async def test_a_failed_swap_restores_the_previous_concrete_session_namespace(
    tmp_path: Path,
) -> None:
    env = Env(
        tmp_path,
        session_namespace="team-old",
        switch_error=RuntimeError("target unreachable"),
        result=ContextSwitchResult(
            pod_resize_supported=True,
            provider_hint="AKS",
            context_namespace=None,
        ),
    )
    await env.switch()
    assert env.session.context == "ctx-a"
    assert env.session.namespace == "team-old"
    assert env.view.scope == "team-old"
    assert env.log.has("watch-start:pods/team-old")
    assert not env.log.has("watch-start:pods/default")
```

- [ ] **Step 2: Run the coordinator regression to verify RED**

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_context_switch_coordinator.py::test_a_failed_swap_restores_the_previous_concrete_session_namespace -q
```

Expected: FAIL because recovery normalizes the restore result's `None` to
`"default"` instead of restoring `team-old`.

- [ ] **Step 3: Implement the explicit recovery namespace contract**

In `src/korvid/ui/context_switch_coordinator.py`, add:

```python
class SessionConfiguration(ABC):
    @abstractmethod
    def default_namespace(self) -> str: ...

    @abstractmethod
    def adopt(
        self,
        context: str | None,
        result: ContextSwitchResult,
        *,
        namespace: str | None = None,
    ) -> None: ...
```

Snapshot `old_namespace = self._session.default_namespace()` before the first
swap attempt, then pass it only on recovery:

```python
ok, applied = await self._retarget(name, old, old_namespace)
...
self._apply(old, old, result, namespace=old_namespace)
```

and forward the override through `_apply()`:

```python
def _apply(
    self,
    name: str | None,
    old: str | None,
    result: ContextSwitchResult,
    *,
    namespace: str | None = None,
) -> None:
    self._session.adopt(name, result, namespace=namespace)
```

In `src/korvid/ui/app.py`, expose the concrete session namespace and keep
successful adoption normalized:

```python
def default_namespace(self) -> str:
    return self._app.config.namespace or "default"

adopted_namespace = (
    namespace if namespace is not None else (result.context_namespace or "default")
)
```

- [ ] **Step 4: Update the coordinator fake and recovery expectation**

Teach `tests/ui/test_context_switch_coordinator.py::FakeSession` to expose
`default_namespace()` and honor the optional `namespace=` override. Update
`test_a_recovered_session_still_restarts_watches_and_metrics` to assert the
restored watch restarts in `"default"` for the default-namespace session.

- [ ] **Step 5: Re-run focused GREEN checks**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m pytest -p no:tach \
  tests/ui/test_context_switch_coordinator.py tests/ui/test_ctx_switch.py -q
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff check \
  src/korvid/ui/context_switch_coordinator.py src/korvid/ui/app.py \
  tests/ui/test_context_switch_coordinator.py tests/ui/test_ctx_switch.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/ruff format --check \
  src/korvid/ui/context_switch_coordinator.py src/korvid/ui/app.py \
  tests/ui/test_context_switch_coordinator.py tests/ui/test_ctx_switch.py
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m mypy \
  src/korvid/ui/context_switch_coordinator.py src/korvid/ui/app.py
/Users/hwang-inhwan/workspace/kube/.venv/bin/tach check
```

Expected: focused tests and all static checks pass.

- [ ] **Step 6: Commit**

```bash
git add \
  src/korvid/ui/context_switch_coordinator.py \
  src/korvid/ui/app.py \
  tests/ui/test_context_switch_coordinator.py \
  docs/superpowers/specs/2026-09-02-context-switch-default-namespace-design.md \
  docs/superpowers/plans/2026-09-02-context-switch-default-namespace.md
git commit -m "fix: preserve namespace on context recovery" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Verify and publish issue #332

**Files:**
- Modify: none
- Test: repository-wide gates

**Interfaces:**
- Consumes: Task 1 implementation and regression
- Produces: reviewed PR closing #332 and updating milestone `v0.4.0`

- [ ] **Step 1: Run repository gates**

Run:

```bash
UV_FROZEN=1 make check
UV_FROZEN=1 uv run ruff format --check src tests
```

Expected: all gates pass. If the unsupported local Python 3.14 deep-JSON test
is nondeterministic, preserve its exact output, verify the affected suite
independently, and require supported Python 3.11–3.13 CI to pass.

- [ ] **Step 2: Verify branch integrity**

Run:

```bash
git status --short
git diff --check origin/main...HEAD
test "$(git hash-object uv.lock)" = "$(git rev-parse HEAD:uv.lock)"
```

Expected: clean branch, valid diff, unchanged lockfile.

- [ ] **Step 3: Create and review the PR**

Push `fix/332-context-namespace`, create a PR that closes #332, then run the
complete AGENTS.md review loop. Fix every credible finding with TDD, reply to
and resolve every thread, and stop only when required CI is green and the
review stopping rule is satisfied.

- [ ] **Step 4: Report and wait for maintainer merge**

Report the implementation, review fixes, test/CI results, milestone progress,
and PR link in the conversation. Do not merge automatically.
