# Session Timeline Controller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract session-timeline production, Warning Event lifecycle, and modal navigation from `KorvidApp` into one independently testable controller.

**Architecture:** `SessionTimelineController` owns timeline policy while `KorvidApp` retains context-switch ordering, selected-row capture, navigation implementation, durable audit persistence, and worker failure translation. Textual operations remain app-owned capabilities exposed through an expanded `UiSurface`.

**Tech Stack:** Python 3.11+, asyncio, Textual, pytest, Ruff, mypy strict, Tach.

## Global Constraints

- Preserve approval, epoch revalidation, write reservation, fail-closed intent audit, mutation, and outcome-audit ordering.
- Timeline append failures remain non-fatal and visible.
- Warning Event cancellation propagates; HTTP 401, 403, and 405 do not retry.
- Cluster-controlled notification text uses `markup=False`.
- Controllers start and cancel work only through `UiSurface`.
- Do not change pane composition, navigation behavior, or context-switch ordering.
- Do not modify `uv.lock`.

---

### Task 1: Extend the UI Surface for Supervised Timeline Work

**Files:**
- Modify: `src/korvid/ui/ui_surface.py`
- Modify: `src/korvid/ui/app.py`
- Modify: `tests/ui/test_view_state_seam.py`

**Interfaces:**
- Produces: `UiSurface.notify(..., markup: bool = True) -> None`
- Produces: `UiSurface.run_worker(..., exit_on_error: bool = True) -> Worker[Any]`
- Produces: `UiSurface.cancel_workers(group: str) -> Awaitable[None]`

- [ ] **Step 1: Write failing adapter contract tests**

Add tests that call `AppUiSurface.notify(..., markup=False)`,
`AppUiSurface.run_worker(..., exit_on_error=False)`, and
`await AppUiSurface.cancel_workers("timeline-test")`. Spy on the app methods and
worker manager so the tests assert that every keyword and cancellation wait is
delegated rather than reimplemented.

```python
def test_app_ui_surface_forwards_untrusted_markup_and_worker_error_policy() -> None:
    app = _app()
    surface: Any = AppUiSurface(app)
    calls: list[tuple[str, object]] = []
    app.notify = lambda message, **kwargs: calls.append(("notify", kwargs["markup"]))
    app.run_worker = lambda work, **kwargs: calls.append(
        ("worker", kwargs["exit_on_error"])
    )

    surface.notify("cluster text", markup=False)
    surface.run_worker(lambda: None, exit_on_error=False)

    assert calls == [("notify", False), ("worker", False)]
```

- [ ] **Step 2: Run the tests and verify the new keywords fail**

Run:

```bash
uv run pytest -p no:tach \
  tests/ui/test_view_state_seam.py::test_app_ui_surface_forwards_untrusted_markup_and_worker_error_policy \
  tests/ui/test_view_state_seam.py::test_app_ui_surface_cancels_and_awaits_a_worker_group -q
```

Expected: failure because `UiSurface` and `AppUiSurface` do not expose the new
arguments or cancellation method.

- [ ] **Step 3: Implement the narrow capabilities**

Extend the abstract signatures and forward `markup` and `exit_on_error` from
`AppUiSurface`. Implement cancellation by iterating
`self._app.workers.cancel_group(self._app, group)` and awaiting each worker
under `contextlib.suppress(WorkerError)`.

```python
async def cancel_workers(self, group: str) -> None:
    for worker in self._app.workers.cancel_group(self._app, group):
        with contextlib.suppress(WorkerError):
            await worker.wait()
```

- [ ] **Step 4: Run the seam tests and static checks**

```bash
uv run pytest -p no:tach tests/ui/test_view_state_seam.py -q
uv run ruff check src/korvid/ui/ui_surface.py src/korvid/ui/app.py \
  tests/ui/test_view_state_seam.py
uv run mypy src/korvid/ui/ui_surface.py src/korvid/ui/app.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/ui_surface.py src/korvid/ui/app.py \
  tests/ui/test_view_state_seam.py
git commit -m "refactor: supervise controller worker lifecycle"
```

### Task 2: Add the Session Timeline Controller

**Files:**
- Create: `src/korvid/ui/session_timeline_controller.py`
- Create: `tests/ui/test_session_timeline_controller.py`

**Interfaces:**
- Consumes: `UiSurface`, `ViewState`, `WatchManager`, `SessionTimeline`
- Produces: `TIMELINE_EVENT_GROUP = "timeline-warning-events"`
- Produces: `TIMELINE_NAVIGATION_GROUP = "timeline"`
- Produces: `SessionTimelineController.start() -> None`
- Produces: `SessionTimelineController.stop() -> Awaitable[None]`
- Produces: `SessionTimelineController.start_warning_watch() -> None`
- Produces: `SessionTimelineController.record_watch_event(kind, scope, event_type, obj) -> None`
- Produces: `SessionTimelineController.record_context_switch(...) -> None`
- Produces: `SessionTimelineController.record_write(...) -> None`
- Produces: `SessionTimelineController.open() -> None`

- [ ] **Step 1: Write failing controller tests**

Create a focused fake `UiSurface` and `ViewState`. Cover:

```python
def test_start_is_inert_without_timeline() -> None:
    controller, watch_manager, ui = make_controller(timeline=None)

    controller.start()

    assert watch_manager.on_event is None
    assert ui.workers == []


def test_watch_event_records_live_epoch_and_canonical_alias() -> None:
    timeline = SessionTimeline(8, 4096)
    controller, _, _ = make_controller(timeline=timeline, epoch=7)

    controller.record_watch_event(
        "po",
        "default",
        "ADDED",
        GenericSummary(
            name="api",
            namespace="default",
            kind="Pod",
            created="",
            uid="pod-1",
        ),
    )

    entry = timeline.snapshot(epoch=7, source=TimelineSource.WATCH, resource=None).entries[0]
    assert entry.resource is not None
    assert entry.resource.kind_alias == "pods"
```

Also add async tests for permanent denial, bounded retry, cancellation, worker
group cancellation, modal opening, and stale navigation.

- [ ] **Step 2: Run the new test module and verify import failure**

```bash
uv run pytest -p no:tach tests/ui/test_session_timeline_controller.py -q
```

Expected: collection fails because
`korvid.ui.session_timeline_controller` does not exist.

- [ ] **Step 3: Implement producer and append policy**

Move the watch verb map, denial status set, backoff limit, append wrapper,
watch-delta recording, Event alias resolution, context-switch recording, and
write recording into `SessionTimelineController`.

`record_write` takes the already-authoritative write metadata:

```python
def record_write(
    self,
    *,
    epoch: int,
    action: str,
    kind_alias: str,
    display_kind: str,
    namespace: str | None,
    name: str,
    outcome: str,
) -> None:
    ...
```

- [ ] **Step 4: Implement Warning Event and modal lifecycle**

Use `UiSurface.run_worker(..., exit_on_error=False)` for both named worker
groups and `await UiSurface.cancel_workers(TIMELINE_EVENT_GROUP)` in `stop()`.
`open()` constructs `SessionTimelineScreen`, captures one epoch and selection,
and invokes the injected navigation callback only when that epoch is current.

- [ ] **Step 5: Run direct tests and static checks**

```bash
uv run pytest -p no:tach tests/ui/test_session_timeline_controller.py -q
uv run ruff check src/korvid/ui/session_timeline_controller.py \
  tests/ui/test_session_timeline_controller.py
uv run mypy src/korvid/ui/session_timeline_controller.py
```

Expected: all commands pass.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/ui/session_timeline_controller.py \
  tests/ui/test_session_timeline_controller.py
git commit -m "refactor: extract session timeline controller"
```

### Task 3: Wire the Controller into KorvidApp

**Files:**
- Modify: `src/korvid/ui/app.py`
- Modify: `tests/ui/test_write_ops.py`
- Test: `tests/ui/test_app.py`
- Test: `tests/ui/test_ctx_switch.py`
- Test: `tests/ui/test_session_timeline_flow.py`
- Test: `tests/ui/test_keybindings.py`

**Interfaces:**
- Consumes: every public `SessionTimelineController` method from Task 2
- Preserves: `KorvidApp.action_timeline() -> None`
- Preserves: app worker error labels for both timeline worker groups

- [ ] **Step 1: Add an app wiring assertion**

Add an integration assertion proving that startup installs the controller's
bound `record_watch_event` method as `WatchManager.on_event`, and update the
qualified-alias test to call the controller API rather than the removed app
private method.

```python
assert app.watch_manager.on_event == app._timeline.record_watch_event
```

- [ ] **Step 2: Run focused tests before wiring**

```bash
uv run pytest -p no:tach \
  tests/ui/test_app.py -k timeline \
  tests/ui/test_write_ops.py -k timeline \
  tests/ui/test_ctx_switch.py -k timeline \
  tests/ui/test_session_timeline_flow.py \
  tests/ui/test_keybindings.py -k timeline -q
```

Expected: the new wiring assertion fails while all existing characterization
tests pass.

- [ ] **Step 3: Construct and delegate to the controller**

Construct one controller in `KorvidApp.__init__` with live adapters:

```python
self._timeline = SessionTimelineController(
    timeline=session_timeline,
    watch_warning_events=watch_warning_events,
    watch_manager=self.watch_manager,
    view=AppViewState(self),
    ui=AppUiSurface(self),
    epoch=lambda: self._ctx_epoch,
    selected_resource=self._selected_timeline_resource,
    navigate=lambda kind, namespace, name, epoch: self._jump_to_object(
        kind, namespace, name, epoch=epoch
    ),
)
```

Replace app-owned timeline producer calls with `start`, `stop`,
`start_warning_watch`, `record_context_switch`, and `record_write`.
`action_timeline()` remains as a Textual-discovered one-line delegate.

- [ ] **Step 4: Remove migrated app implementation**

Delete the moved timeline constants, model/stream fields, producer methods,
modal-result handler, navigation-worker starter, and timeline-specific cancel
helper. Keep the selected-resource capture and worker failure labels in the
app.

- [ ] **Step 5: Run all timeline integration tests**

```bash
uv run pytest -p no:tach \
  tests/ui/test_app.py -k timeline \
  tests/ui/test_write_ops.py -k timeline \
  tests/ui/test_ctx_switch.py -k timeline \
  tests/ui/test_session_timeline_flow.py \
  tests/ui/test_session_timeline_screen.py \
  tests/ui/test_keybindings.py -k timeline -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Run changed-file checks**

```bash
uv run ruff check src/korvid/ui/app.py \
  src/korvid/ui/session_timeline_controller.py src/korvid/ui/ui_surface.py \
  tests/ui/test_session_timeline_controller.py tests/ui/test_view_state_seam.py \
  tests/ui/test_write_ops.py
uv run ruff format --check src/korvid/ui/app.py \
  src/korvid/ui/session_timeline_controller.py src/korvid/ui/ui_surface.py \
  tests/ui/test_session_timeline_controller.py tests/ui/test_view_state_seam.py \
  tests/ui/test_write_ops.py
uv run mypy src/korvid/ui/app.py src/korvid/ui/session_timeline_controller.py \
  src/korvid/ui/ui_surface.py
uv run tach check
```

Expected: all commands pass.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/ui/app.py src/korvid/ui/session_timeline_controller.py \
  src/korvid/ui/ui_surface.py tests/ui
git commit -m "refactor: route timeline flows through controller"
```

### Task 4: Correct UI Controller Documentation and Verify the Repository

**Files:**
- Modify: `docs/dev/ui-controllers.md`

**Interfaces:**
- Documents: the timeline controller boundary and current logs/describe
  extraction evidence
- Preserves: all production interfaces

- [ ] **Step 1: Correct the stale extraction record**

Document that the later issue #238 measurement found logs and describe
technically extractable without a new pane-composition seam, while describe
remained a deliberate low-ROI non-extraction. Add `SessionTimelineController`
to the controller inventory and explain why context retargeting remains in the
app.

- [ ] **Step 2: Run the complete repository gate**

```bash
make check
```

Expected: Ruff, formatting, mypy, pytest, coverage, Tach, and dependency checks
all pass.

- [ ] **Step 3: Inspect the final diff**

```bash
git --no-pager diff --check
git --no-pager diff --stat HEAD
git --no-pager status --short
```

Expected: no whitespace errors; only the timeline extraction, seam extension,
tests, and controller documentation are changed. The pre-existing `uv.lock`
modification remains unstaged and untouched.

- [ ] **Step 4: Commit**

```bash
git add docs/dev/ui-controllers.md
git commit -m "docs: update UI controller boundaries"
```
