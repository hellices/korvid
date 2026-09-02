# Inline Input Approval Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep agent write approvals pending while an inline input surface owns focus so typing `y` into command, filter, namespace-selection, or agent chat UI can never approve a cluster mutation.

**Architecture:** Extend the `UiSurface` seam with an inline-focus ownership query, teach `AgentUiController.can_surface_approval()` to consult it, and keep the pending toast blocker-specific (`Ctrl-A` for a collapsed panel, `Tab` for active inline input). The wait loop keeps its existing timeout, `0.05` poll, and 30-second cadence for unchanged blockers, but must emit a new reminder immediately if the pending blocker changes. Cover that controller path with unit tests first. Then add Textual Pilot regressions that prove each inline surface keeps the `y` keystroke for itself and that the same pending approval surfaces only after focus returns to a non-inline widget.

**Tech Stack:** Python 3.11+, Textual, Textual Pilot, pytest, ruff, mypy, tach, gh CLI

## Global Constraints

- Prefix every Python command in this plan with `UV_FROZEN=1 uv run`.
- Keep the existing approval timeout and the `0.05` second wait-loop sleep unchanged. Unchanged blockers keep the existing 30-second reminder cadence, but a new blocker-specific reminder must be emitted immediately when the pending blocker changes. Pending toast text may differ by blocker: collapsed panel uses `Agent write approval pending - open the agent panel (Ctrl-A) to review`; active inline input uses `Agent write approval pending - leave the active input using Tab to review`.
- Preserve the security invariants: approvals still require an explicit user keystroke, active input keeps focus until the user leaves it, `run_kubectl` validation remains unchanged, and fail-closed audit logging must still block writes when the audit sink is unavailable.
- Only focused `Input` widgets and the inline `NamespacePicker` block approval surfacing; ordinary table focus must not block it.
- Use `tests/ui/waits.py::until()` for new UI state transitions instead of fixed sleeps.
- Update every `UiSurface` fake explicitly when the abstract port changes.
- Keep reviewer-meaningful history: one commit for the seam/controller unit work, one commit for the pilot regressions, then request review from a clean tree.

---

## File Structure

- `src/korvid/ui/ui_surface.py` — the abstract UI seam; add the inline-focus ownership query here so controllers never reach directly into Textual state.
- `src/korvid/ui/app.py` — `AppUiSurface` over `KorvidApp`; first detect focused `Input` widgets, then widen the production check to the inline `NamespacePicker` once the pilot regression demonstrates the missing branch.
- `src/korvid/ui/agent_ui_controller.py` — the agent approval gate; keep the existing deadline/reminder behaviour while adding the new inline-focus condition.
- `tests/ui/test_view_state_seam.py` — seam tests that pin the new `AppUiSurface` focus detection behaviour.
- `tests/ui/test_write_coordinator.py` — shared `FakeUi` used by agent/controller unit tests; add a controllable inline-focus flag here.
- `tests/ui/test_workspace_controller.py` — `FakeUi` for workspace controller tests; implement the new seam method explicitly with a false default.
- `tests/ui/test_session_timeline_controller.py` — `FakeUiSurface` for timeline controller tests; implement the new seam method explicitly with a false default.
- `tests/ui/test_debug_controller.py` — `FakeUi` for debug controller tests; implement the new seam method explicitly with a false default.
- `tests/ui/test_log_controller.py` — `FakeUiSurface` for log controller tests; implement the new seam method explicitly with a false default.
- `tests/ui/test_agent_ui_controller.py` — unit coverage for `can_surface_approval()` and for the pending approval path while inline focus is active.
- `tests/ui/test_agent_write.py` — Textual Pilot regressions for command bar, filter bar, namespace picker, and agent input.

### Task 1: UiSurface inline-focus gate

**Files:**
- Modify: `src/korvid/ui/ui_surface.py:41-161`
- Modify: `src/korvid/ui/app.py:38-39,2528-2606`
- Modify: `src/korvid/ui/agent_ui_controller.py:2000-2024`
- Modify: `tests/ui/test_view_state_seam.py:11-25,145-229`
- Modify: `tests/ui/test_write_coordinator.py:58-159`
- Modify: `tests/ui/test_workspace_controller.py:75-145`
- Modify: `tests/ui/test_session_timeline_controller.py:50-130`
- Modify: `tests/ui/test_debug_controller.py:25-103`
- Modify: `tests/ui/test_log_controller.py:39-93`
- Modify: `tests/ui/test_agent_ui_controller.py:1554-1604`
- Test: `tests/ui/test_view_state_seam.py`
- Test: `tests/ui/test_agent_ui_controller.py`

**Interfaces:**
- Consumes: `AgentUiController.__init__(*, panel: AgentPanelPort, screens: AgentScreens, ui: UiSurface, view: ViewState, context: ContextGuard, writes: WriteCoordinator, workspace: WorkspaceState, navigation: WorkspaceOps, logs: AgentLogOps, proposals: AgentProposals, dispatch: BridgeDispatch, config: Callable[[], KorvidConfig], get_manifest: Callable[[], ManifestFetcher | None], get_events: Callable[[], Any | None], stream_logs: Callable[[], Any | None], pod_containers: Callable[[str, str], tuple[str, ...]], write_ops: Callable[[], WriteOps | None], audit: Callable[[], AuditLog | None], pod_resize_supported: Callable[[], bool], provider_hint: Callable[[], str | None], approval_timeout_seconds: float | None = None, refresh_status: Callable[[], None] = lambda: None, follow_bridge: Callable[[], UIBridge | None] = lambda: None, tasks: TurnTasks | None = None, session: AgentSession | None = None, model_name: str | None = None, configurator: AgentConfigurator | None = None, rebuild: Callable[[AgentSettings], AgentSession | None] | None = None, disconnect: Callable[[], None] | None = None, available: bool = True) -> None`
- Consumes: `AgentPanelPort.expanded(self) -> bool`
- Consumes: `UiSurface.screen_depth(self) -> int`
- Produces: `UiSurface.inline_input_active(self) -> bool`
- Produces: `AppUiSurface.inline_input_active(self) -> bool`
- Produces: `AgentUiController.can_surface_approval(self) -> bool`

- [ ] **Step 1: Write the failing seam and controller tests**

```python
# tests/ui/test_view_state_seam.py
from types import SimpleNamespace
from typing import Any, cast

from textual.widgets import Input


def test_app_ui_surface_reports_inline_input_focus() -> None:
    command_bar = Input()
    surface: Any = AppUiSurface(cast("KorvidApp", SimpleNamespace(focused=command_bar)))

    assert surface.inline_input_active() is True


def test_app_ui_surface_ignores_non_input_focus() -> None:
    surface: Any = AppUiSurface(cast("KorvidApp", SimpleNamespace(focused=object())))

    assert surface.inline_input_active() is False


# tests/ui/test_agent_ui_controller.py
async def test_can_surface_approval_requires_no_inline_input(tmp_path: Path) -> None:
    env = Env(tmp_path=tmp_path, session=ScriptedSession())
    env.panel.mounted = True
    env.panel.visible = True
    env.ui.inline_active = True

    assert env.controller.can_surface_approval() is False

    env.ui.inline_active = False
    assert env.controller.can_surface_approval() is True


async def test_agent_write_waits_for_inline_input_focus_to_clear(tmp_path: Path) -> None:
    ops = RecordingOps()
    env = Env(tmp_path=tmp_path, ops=ops)
    env.panel.mounted = True
    env.panel.visible = True
    env.ui.inline_active = True

    request = asyncio.ensure_future(
        env.controller.agent_request_write("delete", "pods", "web-1", "default")
    )
    await settle()
    assert env.ui.screens == []
    assert any("Agent write approval pending" in message for message in env.ui.messages())

    env.ui.inline_active = False
    await env.ui.wait_for_screens()
    assert isinstance(env.ui.screens[-1][0], ConfirmScreen)
    env.ui.answer(False)
    out = await request
    assert out.startswith("denied")
    assert ops.calls == []
```

- [ ] **Step 2: Run the new tests to verify RED**

Run:

```bash
UV_FROZEN=1 uv run pytest -p no:tach \
  tests/ui/test_view_state_seam.py::test_app_ui_surface_reports_inline_input_focus \
  tests/ui/test_view_state_seam.py::test_app_ui_surface_ignores_non_input_focus \
  tests/ui/test_agent_ui_controller.py::test_can_surface_approval_requires_no_inline_input \
  tests/ui/test_agent_ui_controller.py::test_agent_write_waits_for_inline_input_focus_to_clear \
  -q
```

Expected: FAIL because `AppUiSurface`/`UiSurface` do not expose `inline_input_active()` yet and `AgentUiController.can_surface_approval()` still ignores inline focus.

- [ ] **Step 3: Write the minimal seam/controller implementation**

```python
# src/korvid/ui/ui_surface.py
    @abstractmethod
    def inline_input_active(self) -> bool:
        """Whether an inline editor owns the next keystroke on the base screen."""


# src/korvid/ui/app.py
from textual.widgets import DataTable, Input, Static


    def inline_input_active(self) -> bool:
        return isinstance(self._app.focused, Input)


# src/korvid/ui/agent_ui_controller.py
    def can_surface_approval(self) -> bool:
        """An approval dialog may only appear when the panel is expanded, the
        base screen is the only stacked screen, and no inline text input owns
        the next key."""
        return (
            self._panel.expanded()
            and self._ui.screen_depth() == 1
            and not self._ui.inline_input_active()
        )


# tests/ui/test_write_coordinator.py
    depth: int = 1
    inline_active: bool = False

    def inline_input_active(self) -> bool:
        return self.inline_active


# tests/ui/test_workspace_controller.py
# tests/ui/test_session_timeline_controller.py
# tests/ui/test_debug_controller.py
# tests/ui/test_log_controller.py
    def inline_input_active(self) -> bool:
        return False  # pragma: no cover
```

- [ ] **Step 4: Run the seam/controller unit suite to verify GREEN**

Run:

```bash
UV_FROZEN=1 uv run pytest -p no:tach \
  tests/ui/test_view_state_seam.py \
  tests/ui/test_write_coordinator.py \
  tests/ui/test_workspace_controller.py \
  tests/ui/test_session_timeline_controller.py \
  tests/ui/test_debug_controller.py \
  tests/ui/test_log_controller.py \
  tests/ui/test_agent_ui_controller.py \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/ui_surface.py src/korvid/ui/app.py src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_view_state_seam.py tests/ui/test_write_coordinator.py \
  tests/ui/test_workspace_controller.py tests/ui/test_session_timeline_controller.py \
  tests/ui/test_debug_controller.py tests/ui/test_log_controller.py \
  tests/ui/test_agent_ui_controller.py
git commit -m $'fix: block agent approvals during inline input focus\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>'
```

### Task 2: Textual Pilot inline-surface regressions

**Files:**
- Modify: `src/korvid/ui/app.py:2528-2606`
- Modify: `tests/ui/test_view_state_seam.py:11-25,173-229`
- Modify: `tests/ui/test_agent_write.py:1-327`
- Test: `tests/ui/test_agent_write.py`

**Interfaces:**
- Consumes: `UiSurface.inline_input_active(self) -> bool`
- Consumes: `AppUiSurface.inline_input_active(self) -> bool`
- Consumes: `AgentUiController.agent_request_write(self, action: str, kind: str, name: str, namespace: str | None = None, replicas: int | None = None, resources: dict[str, dict[str, dict[str, str]]] | None = None) -> str`
- Consumes: `tests/ui/waits.py::until(pilot: Any, cond: Callable[[], object], timeout: float = 5.0, label: str = "condition") -> None`
- Consumes: `CommandBar.open(self) -> None`
- Consumes: `FilterBar.open(self) -> None`
- Consumes: `NamespacePicker.open(self, namespaces: list[str]) -> None`
- Produces: `AppUiSurface.inline_input_active(self) -> bool` that also blocks when the focused widget is the inline `NamespacePicker`
- Produces: four pilot regressions proving `y` stays with the focused inline surface, the pending toast reflects the blocking surface, and the approval appears only after focus release

- [ ] **Step 1: Write the failing pilot regressions**

```python
# tests/ui/test_agent_write.py
from textual.widgets import Input

from korvid.ui.widgets.command_bar import CommandBar
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.resource_table import ResourceTable


def _pending_delete(app: Any) -> asyncio.Task[str]:
    return asyncio.ensure_future(
        app._agent_ui.agent_request_write("delete", "deployments", "web", namespace="default")
    )


async def _decline_after_surface(pilot: Any, app: Any, task: asyncio.Task[str]) -> None:
    await until(
        pilot,
        lambda: isinstance(app.screen, ConfirmScreen),
        label="agent approval dialog opened after focus release",
    )
    await pilot.press("n")
    result = await task
    assert "denied" in result.lower() or "declined" in result.lower()


async def test_agent_write_stays_pending_while_command_bar_has_focus(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        _expand_panel(app)
        await pilot.press("colon")
        bar = app.query_one(CommandBar)
        await until(pilot, lambda: app.focused is bar, label="command bar focused")
        task = _pending_delete(app)
        await pilot.press("y")
        await until(
            pilot,
            lambda: bar.value == "y"
            and app.focused is bar
            and not task.done()
            and not isinstance(app.screen, ConfirmScreen),
            label="command bar kept the y key",
        )
        await pilot.press("escape")
        await until(
            pilot,
            lambda: bar.display is False and app.focused is app.query_one(ResourceTable),
            label="command bar dismissed",
        )
        await _decline_after_surface(pilot, app, task)
        assert rec.calls == []


async def test_agent_write_stays_pending_while_filter_bar_has_focus(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        _expand_panel(app)
        await pilot.press("slash")
        bar = app.query_one(FilterBar)
        await until(pilot, lambda: app.focused is bar, label="filter bar focused")
        task = _pending_delete(app)
        await pilot.press("y")
        await until(
            pilot,
            lambda: bar.value == "y"
            and app.filter_pattern == "y"
            and app.focused is bar
            and not task.done()
            and not isinstance(app.screen, ConfirmScreen),
            label="filter bar kept the y key",
        )
        await pilot.press("escape")
        await until(
            pilot,
            lambda: bar.display is False and app.focused is app.query_one(ResourceTable),
            label="filter bar dismissed",
        )
        await _decline_after_surface(pilot, app, task)
        assert rec.calls == []


async def test_agent_write_stays_pending_while_namespace_picker_has_focus(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(rec, tmp_path / "audit.jsonl")

    async def list_namespaces() -> list[str]:
        return ["default", "kube-system", "prod"]

    app._list_namespaces = list_namespaces
    async with app.run_test() as pilot:
        _expand_panel(app)
        await pilot.press("colon")
        await pilot.press("n")
        await pilot.press("s")
        await pilot.press("enter")
        picker = app.query_one(NamespacePicker)
        await until(
            pilot,
            lambda: picker.display is True and app.focused is picker,
            label="namespace picker focused",
        )
        task = _pending_delete(app)
        highlighted = picker.highlighted
        await pilot.press("y")
        await until(
            pilot,
            lambda: picker.display is True
            and app.focused is picker
            and picker.highlighted == highlighted
            and not task.done()
            and not isinstance(app.screen, ConfirmScreen),
            label="namespace picker kept focus",
        )
        await pilot.press("escape")
        await until(
            pilot,
            lambda: picker.display is False and app.focused is app.query_one(ResourceTable),
            label="namespace picker dismissed",
        )
        await _decline_after_surface(pilot, app, task)
        assert rec.calls == []


async def test_agent_write_stays_pending_while_agent_input_has_focus(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(
        rec,
        tmp_path / "audit.jsonl",
        agent_session=FakeSession(),
        agent_model_name="test-model",
    )
    async with app.run_test() as pilot:
        agent_input = app.query_one("#agent-input", Input)
        await pilot.press("ctrl+a")
        await until(pilot, lambda: app.focused is agent_input, label="agent input focused")
        task = _pending_delete(app)
        await until(
            pilot,
            lambda: any(
                "leave the active input using Tab to review" in str(notification.message)
                for notification in app._notifications
            ),
            label="agent-input-specific pending notification",
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: agent_input.value == "y"
            and app.focused is agent_input
            and not task.done()
            and not isinstance(app.screen, ConfirmScreen),
            label="agent input kept the y key",
        )
        await pilot.press("tab")
        await _decline_after_surface(pilot, app, task)
        assert rec.calls == []
```

- [ ] **Step 2: Run the new pilot regressions to verify RED**

Run:

```bash
UV_FROZEN=1 uv run pytest -p no:tach \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_command_bar_has_focus \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_filter_bar_has_focus \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_namespace_picker_has_focus \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_agent_input_has_focus \
  -q
```

Expected: FAIL on the namespace-picker case because `AppUiSurface.inline_input_active()` only recognizes focused `Input` widgets, so the picker can still be covered by a `ConfirmScreen`.

- [ ] **Step 3: Add the final picker branch and its seam test**

```python
# src/korvid/ui/app.py
    def inline_input_active(self) -> bool:
        focused = self._app.focused
        return isinstance(focused, Input) or focused is self._app._namespace_picker


# tests/ui/test_view_state_seam.py
from korvid.ui.widgets.namespace_picker import NamespacePicker


def test_app_ui_surface_reports_inline_namespace_picker_focus() -> None:
    picker = NamespacePicker()
    surface: Any = AppUiSurface(
        cast("KorvidApp", SimpleNamespace(focused=picker, _namespace_picker=picker))
    )

    assert surface.inline_input_active() is True
```

- [ ] **Step 4: Run the seam + pilot regressions to verify GREEN**

Run:

```bash
UV_FROZEN=1 uv run pytest -p no:tach \
  tests/ui/test_view_state_seam.py::test_app_ui_surface_reports_inline_namespace_picker_focus \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_command_bar_has_focus \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_filter_bar_has_focus \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_namespace_picker_has_focus \
  tests/ui/test_agent_write.py::test_agent_write_stays_pending_while_agent_input_has_focus \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/app.py tests/ui/test_view_state_seam.py tests/ui/test_agent_write.py
git commit -m $'test: cover inline approval focus regressions\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>'
```

### Task 3: Full verification and review handoff

**Files:**
- Verify: `src/korvid/ui/ui_surface.py`
- Verify: `src/korvid/ui/app.py`
- Verify: `src/korvid/ui/agent_ui_controller.py`
- Verify: `tests/ui/test_view_state_seam.py`
- Verify: `tests/ui/test_write_coordinator.py`
- Verify: `tests/ui/test_workspace_controller.py`
- Verify: `tests/ui/test_session_timeline_controller.py`
- Verify: `tests/ui/test_debug_controller.py`
- Verify: `tests/ui/test_log_controller.py`
- Verify: `tests/ui/test_agent_ui_controller.py`
- Verify: `tests/ui/test_agent_write.py`

**Interfaces:**
- Consumes: `UiSurface.inline_input_active(self) -> bool`
- Consumes: `AppUiSurface.inline_input_active(self) -> bool`
- Consumes: `AgentUiController.can_surface_approval(self) -> bool`
- Consumes: the four new Textual Pilot regressions in `tests/ui/test_agent_write.py`
- Produces: a clean, fully verified two-commit tip ready for review

- [ ] **Step 1: Run the focused approval-safety suite**

Run:

```bash
UV_FROZEN=1 uv run pytest -p no:tach \
  tests/ui/test_view_state_seam.py \
  tests/ui/test_write_coordinator.py \
  tests/ui/test_workspace_controller.py \
  tests/ui/test_session_timeline_controller.py \
  tests/ui/test_debug_controller.py \
  tests/ui/test_log_controller.py \
  tests/ui/test_agent_ui_controller.py \
  tests/ui/test_agent_write.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run touched-file lint and format checks**

Run:

```bash
UV_FROZEN=1 uv run ruff check \
  src/korvid/ui/ui_surface.py src/korvid/ui/app.py src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_view_state_seam.py tests/ui/test_write_coordinator.py \
  tests/ui/test_workspace_controller.py tests/ui/test_session_timeline_controller.py \
  tests/ui/test_debug_controller.py tests/ui/test_log_controller.py \
  tests/ui/test_agent_ui_controller.py tests/ui/test_agent_write.py
UV_FROZEN=1 uv run ruff format --check \
  src/korvid/ui/ui_surface.py src/korvid/ui/app.py src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_view_state_seam.py tests/ui/test_write_coordinator.py \
  tests/ui/test_workspace_controller.py tests/ui/test_session_timeline_controller.py \
  tests/ui/test_debug_controller.py tests/ui/test_log_controller.py \
  tests/ui/test_agent_ui_controller.py tests/ui/test_agent_write.py
```

Expected: PASS.

- [ ] **Step 3: Run the full repository gates before review**

Run:

```bash
UV_FROZEN=1 uv run mypy src/
UV_FROZEN=1 uv run pytest -x -q
UV_FROZEN=1 uv run tach check
```

Expected: PASS.

- [ ] **Step 4: Request review only from a clean tree**

Run:

```bash
git status --short
gh pr view --json number,headRefName,statusCheckRollup,reviews,reviewRequests
```

Expected: `git status --short` prints nothing. After that, invoke the `requesting-code-review` skill from the clean two-commit tip. If feedback arrives, follow the AGENTS.md review loop exactly: TDD the fix, rerun the same verification stack, reply per review comment, resolve addressed threads, and re-request review only when credible blocking findings remain.

- [ ] **Step 5: Keep the verification task commitless unless a check rewrites files**

Run:

```bash
git status --short
```

Expected: no output, so this task normally creates no commit. If a verification command rewrites tracked files, make only that delta its own final commit:

```bash
git add src/korvid/ui/ui_surface.py src/korvid/ui/app.py src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_view_state_seam.py tests/ui/test_write_coordinator.py \
  tests/ui/test_workspace_controller.py tests/ui/test_session_timeline_controller.py \
  tests/ui/test_debug_controller.py tests/ui/test_log_controller.py \
  tests/ui/test_agent_ui_controller.py tests/ui/test_agent_write.py
git commit -m $'chore: finalize inline approval safety verification\n\nCo-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>'
```

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-02-inline-input-approval-safety.md`. Two execution options:

1. **Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
