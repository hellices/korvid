# AgentPanel Event Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `AgentPanel.apply_event()`'s large event-type chain with a thin, type-narrowing dispatcher and dedicated handlers while preserving every visible and stateful behavior.

**Architecture:** Keep `apply_event()` as the single public entry point and use Python class-pattern matching to narrow the closed `AgentEvent` union. Move each branch body into an exact-type private method, and extract only the token-header calculation shared by complete and interrupted turns.

**Tech Stack:** Python 3.11+, Textual, pytest, Ruff, strict mypy

## Global Constraints

- Event rendering order and all mounted text/classes remain unchanged.
- Tool-start bookkeeping stores both widget and raw arguments before mounting.
- Tool-finish handling returns status to `thinking`.
- Error and terminal events re-enable input exactly as before.
- Interrupted turns retain partial output and never add a duplicate marker.
- Interrupt handling restores input focus.
- Citation warnings are processed only for `TurnComplete`.
- Token totals and estimated status are updated identically for complete and interrupted turns.
- No agent write approval, audit, tool execution, or provider behavior changes.
- Do not use `Any`, casts, `type: ignore`, or a heterogeneous callable registry to dispatch event types.

---

### Task 1: Extract typed AgentPanel event handlers

**Files:**
- Modify: `src/korvid/ui/widgets/agent_panel.py:295-362`
- Modify: `tests/ui/test_agent_panel.py`

**Interfaces:**
- Consumes: `AgentEvent = TextDelta | ToolCallStarted | ToolCallFinished | TurnComplete | TurnInterrupted | AgentError`
- Produces: `AgentPanel.apply_event(event: AgentEvent) -> None`
- Produces: six private handlers with exact event argument types
- Produces: `_finish_turn_header(input_tokens: int, output_tokens: int, estimated: bool) -> None`

- [ ] **Step 1: Write the failing dispatch regression**

Extend the test app so it can mount a supplied panel subclass:

```python
class PanelApp(App[None]):
    def __init__(self, panel_type: type[AgentPanel] = AgentPanel) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self._panel_type = panel_type

    def compose(self) -> ComposeResult:
        yield self._panel_type()
```

Add a probe subclass with six exact handler overrides:

```python
class DispatchProbePanel(AgentPanel):
    calls: list[str]

    def __init__(self) -> None:
        super().__init__()
        self.calls = []

    def _apply_text_delta(self, event: TextDelta) -> None:
        self.calls.append("text")

    def _apply_tool_started(self, event: ToolCallStarted) -> None:
        self.calls.append("tool-started")

    def _apply_tool_finished(self, event: ToolCallFinished) -> None:
        self.calls.append("tool-finished")

    def _apply_agent_error(self, event: AgentError) -> None:
        self.calls.append("error")

    def _apply_turn_complete(self, event: TurnComplete) -> None:
        self.calls.append("complete")

    def _apply_turn_interrupted(self, event: TurnInterrupted) -> None:
        self.calls.append("interrupted")
```

Exercise the six concrete event values through the public dispatcher:

```python
async def test_apply_event_dispatches_every_event_to_its_typed_handler() -> None:
    events = [
        (TextDelta(text="x"), "text"),
        (ToolCallStarted(call_id="1", name="get_logs", arguments="{}"), "tool-started"),
        (ToolCallFinished(call_id="1", name="get_logs", ok=True, summary=""), "tool-finished"),
        (AgentError(message="boom"), "error"),
        (TurnComplete(input_tokens=1, output_tokens=2, estimated=False), "complete"),
        (TurnInterrupted(input_tokens=1, output_tokens=2, estimated=False), "interrupted"),
    ]
    app = PanelApp(DispatchProbePanel)
    async with app.run_test():
        panel = app.query_one(DispatchProbePanel)
        for event, expected in events:
            panel.apply_event(event)
            assert panel.calls.pop() == expected
```

- [ ] **Step 2: Run the regression to verify RED**

Run:

```bash
uv run pytest -p no:tach \
  tests/ui/test_agent_panel.py::test_apply_event_dispatches_every_event_to_its_typed_handler -q
```

Expected: FAIL because the current `apply_event()` executes its inline branch
bodies and never calls the probe handler overrides.

- [ ] **Step 3: Implement the thin class-pattern dispatcher**

Replace the branch bodies in `apply_event()` with:

```python
def apply_event(self, event: AgentEvent) -> None:
    match event:
        case TextDelta():
            self._apply_text_delta(event)
        case ToolCallStarted():
            self._apply_tool_started(event)
        case ToolCallFinished():
            self._apply_tool_finished(event)
        case AgentError():
            self._apply_agent_error(event)
        case TurnComplete():
            self._apply_turn_complete(event)
        case TurnInterrupted():
            self._apply_turn_interrupted(event)
```

Move each original branch body unchanged into the corresponding private
handler. Do not catch handler exceptions and do not add a default assertion.

- [ ] **Step 4: Extract only the common terminal header calculation**

Add:

```python
def _finish_turn_header(
    self,
    input_tokens: int,
    output_tokens: int,
    estimated: bool,
) -> None:
    self.set_header(
        self._model,
        self._tok_in + input_tokens,
        self._tok_out + output_tokens,
        self._estimated or estimated,
        tier=self._tier,
    )
```

Call it from `_apply_turn_complete()` and `_apply_turn_interrupted()`. Keep
stream finalization, citations, timers, status, marker handling, input enablement,
and focus in their event-specific handlers.

- [ ] **Step 5: Run focused GREEN checks**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_agent_panel.py -q
uv run ruff check src/korvid/ui/widgets/agent_panel.py tests/ui/test_agent_panel.py
uv run ruff format --check src/korvid/ui/widgets/agent_panel.py tests/ui/test_agent_panel.py
uv run mypy src/korvid/ui/widgets/agent_panel.py tests/ui/test_agent_panel.py
```

Expected: all AgentPanel tests and static checks pass.

- [ ] **Step 6: Commit**

```bash
git add \
  src/korvid/ui/widgets/agent_panel.py \
  tests/ui/test_agent_panel.py \
  docs/superpowers/specs/2026-09-04-conditional-dispatch-audit-design.md \
  docs/superpowers/plans/2026-09-04-agent-panel-event-dispatch.md
git commit -m "refactor: dispatch agent panel events by type" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Repository verification and PR review loop

**Files:**
- Modify: none
- Test: repository-wide quality gates

**Interfaces:**
- Consumes: Task 1's committed event dispatcher
- Produces: a pushed branch and reviewed pull request with all required checks successful

- [ ] **Step 1: Run repository gates**

Run:

```bash
make check
```

Expected: Ruff, strict mypy, pytest, and tach pass. Behind the corporate mirror,
restore `uv.lock` after any `uv run` command and use the established complete
environment without changing dependency manifests.

- [ ] **Step 2: Push and create the PR**

Run:

```bash
git push -u origin agents/conditional-dispatch-audit
gh pr create --base main --head agents/conditional-dispatch-audit --fill
```

- [ ] **Step 3: Complete review rounds**

Read every review body and inline thread, including suppressed findings. Fix
credible correctness, security, data-loss, architecture, or required-check
findings with a RED regression and GREEN implementation. After each fix run the
full gate, commit without amending, reply to each comment with the commit and
test, resolve its thread, push, and request Copilot review again.

Stop speculative changes after two consecutive low-confidence-only rounds.

- [ ] **Step 4: Verify final status and hand off**

Run:

```bash
gh pr view --json statusCheckRollup
```

Expected: every required check is `SUCCESS`, no review request remains, and no
review thread is unresolved. Report the PR ready for maintainer review. Do not
merge or enable auto-merge.
