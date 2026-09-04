# Agent Setup Widget Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `AgentSetupScreen`'s option-list and input widget-ID chains with typed bound-handler maps without changing wizard behavior.

**Architecture:** Keep Textual's two public event handlers as thin dispatch boundaries. Build small typed maps from the current screen instance so each widget ID resolves to a bound private method, while the extracted methods retain the original state transitions, validation, focus, and worker calls.

**Tech Stack:** Python 3.11+, Textual, pytest, Ruff, strict mypy

## Global Constraints

- Unknown or missing widget IDs remain stopped no-ops.
- Provider selection hides the provider list before advancing.
- Azure selection displays and focuses the auth list, retaining Entra when reconnecting.
- Non-Azure providers retain a configured auth method when reconnecting.
- GitHub Copilot still starts its connection worker from `_after_auth_method()`.
- Endpoint and API-key environment values are stripped and normalized to `None`.
- API-key auth displays, prefills, and focuses the environment input.
- Non-API-key auth starts model fetching immediately.
- Empty direct model submission displays `"Model is required"` and does not advance.
- Model-filter submission selects only the highlighted option when options exist.
- Worker calls retain `exclusive=True` and their existing order.
- Do not add a wizard state machine, change visible copy, or change widget IDs.
- Do not use `Any`, casts, `type: ignore`, string `getattr`, or import-time unbound method capture for dispatch.

---

### Task 1: Extract typed option and input handlers

**Files:**
- Modify: `src/korvid/ui/widgets/agent_setup_screen.py:11-275`
- Modify: `tests/ui/test_agent_setup_screen.py`

**Interfaces:**
- Produces: `_OptionHandler = Callable[[OptionList.OptionSelected], None]`
- Produces: `_InputHandler = Callable[[Input.Submitted], None]`
- Produces: `_option_handlers(self) -> dict[str, _OptionHandler]`
- Produces: `_input_handlers(self) -> dict[str, _InputHandler]`
- Preserves: `on_option_list_option_selected(event) -> None`
- Preserves: `on_input_submitted(event) -> None`

- [ ] **Step 1: Write failing public-dispatch regressions**

Add a probe subclass:

```python
class _DispatchProbeScreen(AgentSetupScreen):
    def __init__(self) -> None:
        super().__init__(FakeConfigurator())
        self.dispatch_calls: list[str] = []

    def _select_provider(self, event: OptionList.OptionSelected) -> None:
        self.dispatch_calls.append("provider")

    def _select_auth(self, event: OptionList.OptionSelected) -> None:
        self.dispatch_calls.append("auth")

    def _select_model_option(self, event: OptionList.OptionSelected) -> None:
        self.dispatch_calls.append("model-option")

    def _select_tier_option(self, event: OptionList.OptionSelected) -> None:
        self.dispatch_calls.append("tier-option")

    def _submit_base_url(self, event: Input.Submitted) -> None:
        self.dispatch_calls.append("base-url")

    def _submit_api_key_env(self, event: Input.Submitted) -> None:
        self.dispatch_calls.append("api-key-env")

    def _submit_model_filter(self, event: Input.Submitted) -> None:
        self.dispatch_calls.append("model-filter")

    def _submit_model(self, event: Input.Submitted) -> None:
        self.dispatch_calls.append("model")
```

Add helpers that construct real Textual events:

```python
def _option_event(widget_id: str) -> OptionList.OptionSelected:
    option = Option("value", id="value")
    option_list = OptionList(option, id=widget_id)
    return OptionList.OptionSelected(option_list, option, 0)


def _input_event(widget_id: str) -> Input.Submitted:
    widget = Input(id=widget_id)
    return Input.Submitted(widget, "value")
```

Add parameterized dispatch tests:

```python
@pytest.mark.parametrize(
    ("widget_id", "expected"),
    [
        ("setup-provider", "provider"),
        ("setup-auth", "auth"),
        ("setup-model-list", "model-option"),
        ("setup-tier", "tier-option"),
    ],
)
def test_option_event_dispatches_to_bound_handler(widget_id: str, expected: str) -> None:
    screen = _DispatchProbeScreen()
    event = _option_event(widget_id)

    screen.on_option_list_option_selected(event)

    assert event.is_stopped
    assert screen.dispatch_calls == [expected]
```

Create the equivalent input test for `setup-base-url`, `setup-api-key-env`,
`setup-model-filter`, and `setup-model`.

Add one unknown-ID test for each event type:

```python
def test_unknown_widget_events_are_stopped_no_ops() -> None:
    screen = _DispatchProbeScreen()
    option_event = _option_event("future-option")
    input_event = _input_event("future-input")

    screen.on_option_list_option_selected(option_event)
    screen.on_input_submitted(input_event)

    assert option_event.is_stopped
    assert input_event.is_stopped
    assert screen.dispatch_calls == []
```

- [ ] **Step 2: Run dispatch tests to verify RED**

Run:

```bash
uv run pytest -p no:tach \
  tests/ui/test_agent_setup_screen.py::test_option_event_dispatches_to_bound_handler \
  tests/ui/test_agent_setup_screen.py::test_input_event_dispatches_to_bound_handler \
  tests/ui/test_agent_setup_screen.py::test_unknown_widget_events_are_stopped_no_ops -q
```

Expected: known-ID tests fail because the current inline branches do not call
the probe handler overrides; the unknown-ID test already passes and pins the
existing fallback behavior.

- [ ] **Step 3: Define typed handler aliases and maps**

Near the module constants, add:

```python
_OptionHandler = Callable[[OptionList.OptionSelected], None]
_InputHandler = Callable[[Input.Submitted], None]
```

Inside `AgentSetupScreen`, add:

```python
def _option_handlers(self) -> dict[str, _OptionHandler]:
    return {
        "setup-provider": self._select_provider,
        "setup-auth": self._select_auth,
        "setup-model-list": self._select_model_option,
        "setup-tier": self._select_tier_option,
    }


def _input_handlers(self) -> dict[str, _InputHandler]:
    return {
        "setup-base-url": self._submit_base_url,
        "setup-api-key-env": self._submit_api_key_env,
        "setup-model-filter": self._submit_model_filter,
        "setup-model": self._submit_model,
    }
```

- [ ] **Step 4: Replace public ID chains with thin dispatchers**

Implement:

```python
def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
    event.stop()
    widget_id = event.option_list.id
    if widget_id is None:
        return
    handler = self._option_handlers().get(widget_id)
    if handler is not None:
        handler(event)


def on_input_submitted(self, event: Input.Submitted) -> None:
    event.stop()
    widget_id = event.input.id
    if widget_id is None:
        return
    handler = self._input_handlers().get(widget_id)
    if handler is not None:
        handler(event)
```

Do not raise for unknown IDs and do not catch handler exceptions.

- [ ] **Step 5: Move branch bodies unchanged into exact handlers**

Create all eight private methods named in Step 1. Move each original branch body
without reordering statements:

- `_select_provider()` owns Azure retained-auth handling and non-Azure auth selection.
- `_select_auth()` hides the auth widget and advances.
- `_select_model_option()` calls `_choose_model(str(event.option.prompt))`.
- `_select_tier_option()` calls `_choose_tier(event.option.id or "automatic")`.
- `_submit_base_url()` owns endpoint normalization and the API-key/model-fetch split.
- `_submit_api_key_env()` normalizes the environment name and fetches models.
- `_submit_model_filter()` keeps the highlighted/option-count guard.
- `_submit_model()` keeps the empty-model status guard.

- [ ] **Step 6: Run focused GREEN checks**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_agent_setup_screen.py -q
uv run ruff check \
  src/korvid/ui/widgets/agent_setup_screen.py \
  tests/ui/test_agent_setup_screen.py
uv run ruff format --check \
  src/korvid/ui/widgets/agent_setup_screen.py \
  tests/ui/test_agent_setup_screen.py
uv run mypy \
  src/korvid/ui/widgets/agent_setup_screen.py \
  tests/ui/test_agent_setup_screen.py
```

Expected: all setup-screen tests and static checks pass.

- [ ] **Step 7: Commit**

```bash
git add \
  src/korvid/ui/widgets/agent_setup_screen.py \
  tests/ui/test_agent_setup_screen.py \
  docs/superpowers/specs/2026-09-04-agent-setup-widget-dispatch-design.md \
  docs/superpowers/plans/2026-09-04-agent-setup-widget-dispatch.md
git commit -m "refactor: dispatch agent setup widget events" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Repository verification and PR review loop

**Files:**
- Modify: none
- Test: repository-wide gates

**Interfaces:**
- Consumes: Task 1's committed widget dispatch
- Produces: a pushed, reviewed PR with all required checks successful

- [ ] **Step 1: Run repository gates**

Run:

```bash
make check
```

Behind the corporate mirror, restore `uv.lock` after `uv run` and use the
established complete environment without changing dependency manifests.

- [ ] **Step 2: Push and create the PR**

Run:

```bash
git push -u origin agents/agent-setup-dispatch
gh pr create --base main --head agents/agent-setup-dispatch --fill
```

- [ ] **Step 3: Complete review rounds**

Read every review body and inline thread, including suppressed findings. Fix
credible correctness, security, data-loss, architecture, or required-check
findings with a RED regression and GREEN implementation. After each fix, run the
full gate, commit without amending, reply to each comment with the commit and
test, resolve the thread, push, and request Copilot review again.

Stop speculative changes after two consecutive low-confidence-only rounds.

- [ ] **Step 4: Verify final status and hand off**

Run:

```bash
gh pr view --json statusCheckRollup
```

Expected: every required check is `SUCCESS`, no review request remains, and no
review thread is unresolved. Report ready for maintainer review without merging
or enabling auto-merge.
