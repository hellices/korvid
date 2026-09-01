# Context-Switch Default Namespace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a successful context switch to an unset kubeconfig namespace adopt Kubernetes' concrete `default` namespace instead of the old cluster's namespace.

**Architecture:** Preserve `ContextSwitchResult.context_namespace` as the raw `str | None` kubeconfig fact, and normalize it at `AppSessionConfiguration.adopt()`, the atomic UI session boundary. Existing coordinator ordering then resets workspace, watches, metrics, and namespace fallbacks from the new concrete config value.

**Tech Stack:** Python 3.11+, Textual, asyncio, pytest, Ruff, mypy

## Global Constraints

- An unset target context namespace becomes exactly `"default"`.
- The previous context's namespace must never be inherited or requested on the new cluster.
- `ContextSwitchResult.context_namespace` remains `str | None`.
- Probe/switch failure must not modify the old session.
- Preserve context-switch ordering, proposal expiry, audit retargeting, and recovery.
- Do not change kubeconfig resolution, all-namespaces semantics, or unrelated controllers.
- Use `tests/ui/waits.py::until()` rather than wall-clock assertions.

---

### Task 1: Adopt the target context's effective namespace

**Files:**
- Modify: `src/korvid/ui/app.py:2665-2674`
- Modify: `tests/ui/test_ctx_switch.py:60-127`
- Test: `tests/ui/test_ctx_switch.py`

**Interfaces:**
- Consumes: `ContextSwitchResult.context_namespace: str | None`
- Produces: `KorvidApp.config.namespace == "default"` when the result is `None`
- Produces: workspace/watch/metrics state retargeted to the concrete namespace

- [ ] **Step 1: Extend the app context-switch fixture**

Add `namespace: str = "default"` to `_CtxEnv.__init__`, initialize a watch-call
record, append every source invocation, and construct the app with the supplied
namespace:

```python
    def __init__(
        self,
        *,
        contexts: tuple[str, ...] = ("ctx-a", "ctx-b"),
        namespace: str = "default",
        probe_error: Exception | None = None,
        switch_error: Exception | None = None,
        result: ContextSwitchResult | None = None,
        audit_path: Path | None = None,
        stream_logs: Any = None,
        probe_gate: asyncio.Event | None = None,
        metrics: Any = None,
        timeline: SessionTimeline | None = None,
        watch_warning_events: Any = None,
    ) -> None:
        self.watch_calls: list[tuple[str, str, str]] = []
```

At the start of the nested `source`:

```python
            self.watch_calls.append((self.cluster, kind, scope))
```

Replace the hard-coded app config with:

```python
            config=KorvidConfig(namespace=namespace, kube_context="ctx-a"),
```

- [ ] **Step 2: Add the failing app-level regression**

Add beside `test_switch_adopts_context_namespace_as_session_default`:

```python
async def test_switch_without_context_namespace_resets_every_target_to_default() -> None:
    from korvid.k8s.metrics import MetricsPoller, PodMetrics

    metrics_calls: list[str | None] = []

    async def fetch(namespace: str | None) -> list[PodMetrics]:
        metrics_calls.append(namespace)
        return []

    env = _CtxEnv(
        namespace="team-old",
        result=ContextSwitchResult(
            pod_resize_supported=True,
            provider_hint=None,
            context_namespace=None,
        ),
        metrics=MetricsPoller(fetch, interval=0.05),
    )
    app = env.app

    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        assert app.current_scope == "team-old"
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: app.config.kube_context == "ctx-b",
            label="context switched",
        )
        await until(
            pilot,
            lambda: app.current_scope == "default",
            label="unset namespace became default",
        )
        await _first_pod_visible(env, pilot, "pod-b")
        await until(
            pilot,
            lambda: bool(metrics_calls) and metrics_calls[-1] == "default",
            label="metrics retargeted to default",
        )

        assert app.config.namespace == "default"
        assert ("b", "pods", "default") in env.watch_calls
        assert not any(
            cluster == "b" and scope == "team-old"
            for cluster, _kind, scope in env.watch_calls
        )

        await app.action_toggle_all_namespaces()
        await until(
            pilot,
            lambda: app.current_scope != "default",
            label="all namespaces enabled",
        )
        await app.action_toggle_all_namespaces()
        await until(
            pilot,
            lambda: app.current_scope == "default",
            label="toggle returned to default",
        )
```

- [ ] **Step 3: Run the regression and verify RED**

Run:

```bash
UV_FROZEN=1 uv run pytest -p no:tach \
  tests/ui/test_ctx_switch.py::test_switch_without_context_namespace_resets_every_target_to_default -q
```

Expected: FAIL because `app.config.namespace` and `current_scope` remain
`"team-old"`.

- [ ] **Step 4: Fix the session adoption boundary**

In `AppSessionConfiguration.adopt()`, replace:

```python
            namespace=result.context_namespace or self._app.config.namespace,
```

with:

```python
            namespace=result.context_namespace or "default",
```

Update the nearby comment to say an unset target namespace materializes
Kubernetes' `default`; it must not mention falling back to prior session state.

- [ ] **Step 5: Run focused GREEN checks**

Run:

```bash
UV_FROZEN=1 uv run pytest -p no:tach \
  tests/ui/test_context_switch_coordinator.py tests/ui/test_ctx_switch.py -q
UV_FROZEN=1 uv run ruff check src/korvid/ui/app.py tests/ui/test_ctx_switch.py
UV_FROZEN=1 uv run ruff format --check src/korvid/ui/app.py tests/ui/test_ctx_switch.py
UV_FROZEN=1 uv run mypy src/korvid/ui/app.py
UV_FROZEN=1 uv run tach check
```

Expected: focused tests and all static checks pass.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/ui/app.py tests/ui/test_ctx_switch.py
git commit -m "fix: reset namespace on context switch" \
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
