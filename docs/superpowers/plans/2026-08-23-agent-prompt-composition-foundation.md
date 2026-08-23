# Agent Prompt Composition Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize all model-facing system, evidence, screen-context, and context-switch message composition behind a typed `PromptComposer` without changing current `full` or `small` behavior.

**Architecture:** Add a pure agent-layer composer and typed screen-context values, then make `AgentRuntime` the consumer and `AgentUiController` the producer of those values. Keep prompt wording in `agent/prompts.py`, evidence ownership in `EvidenceLedger`, sanitization in `OutboundPolicy`, and all provider/tool/write behavior unchanged.

**Tech Stack:** Python 3.11+, frozen dataclasses, asyncio, pytest, mypy strict, ruff, tach.

## Global Constraints

- This plan implements migration slice 1 of issue #316 only. Resolved model policy, `AgentEngine`, `RequestGateway`, and framework spikes require separate plans.
- Preserve the exact current `full` and `small` prompts, tool surfaces, budgets, iteration behavior, and context-switch behavior.
- Preserve the external-AI fail-closed boundary, exact outbound snapshot, Secret masking, evidence validation, approval gate, and fail-closed audit.
- Add no dependency and do not modify `pyproject.toml` or `uv.lock`.
- `core/` must not import `agent/`; `agent/` may import `tools/`; `ui/` may import `agent/`.
- `PromptComposer` is stateless. Composition-root wiring may share one instance across runtime rebuilds.
- A context name, resource name, namespace, filter, or context-switch notice is untrusted input even when the UI supplies it.
- Every test command uses `uv run --frozen` so validation cannot rewrite the lock file behind a corporate mirror.

---

## File Map

### New files

- `src/korvid/agent/prompting.py` — typed screen context, system/user prompt composition, and evidence-note formatting.
- `tests/agent/test_prompting.py` — pure unit tests for every conditional prompt fragment and screen-context sanitization.

### Modified production files

- `src/korvid/agent/prompts.py` — wording constants only; remove composition after callers migrate.
- `src/korvid/agent/runtime.py` — delegate system and user message construction to `PromptComposer`.
- `src/korvid/ui/agent_ui_controller.py` — produce typed `ScreenContext` rather than model-facing text.
- `src/korvid/__main__.py` — construct and inject one `PromptComposer` into initial and rebuilt runtimes.

### Modified tests and documentation

- `tests/agent/runtime_fakes.py`
- `tests/agent/test_evidence.py`
- `tests/agent/test_interrupt.py`
- `tests/agent/test_profiles.py`
- `tests/agent/test_runtime_contracts.py`
- `tests/agent/test_runtime_core.py`
- `tests/providers/test_ollama.py`
- `tests/test_main_wiring.py`
- `tests/ui/test_agent_follow.py`
- `tests/ui/test_agent_interrupt.py`
- `tests/ui/test_agent_ui_controller.py`
- `tests/ui/test_agent_wiring.py`
- `tests/ui/test_ctx_switch.py`
- `tests/ui/test_protected_contexts.py`
- `tests/ui/test_secret_screen.py`
- `docs/dev/specs/2026-08-12-korvid-architecture.md`

---

### Task 1: Extract system and evidence composition

**Files:**
- Create: `src/korvid/agent/prompting.py`
- Create: `tests/agent/test_prompting.py`
- Modify: `src/korvid/agent/prompts.py:1-18,172-203`
- Modify: `src/korvid/agent/runtime.py:20,151-209`
- Modify: `tests/agent/test_evidence.py:18`

**Interfaces:**
- Consumes: existing prompt constants, `Evidence`, `UI_TOOL_NAMES`, and `WRITE_TOOL_NAMES`.
- Produces: `PromptComposer.compose_system(...) -> str` and `evidence_note(items: Sequence[Evidence]) -> str`.

- [ ] **Step 1: Write failing pure composition tests**

Create `tests/agent/test_prompting.py`:

```python
from __future__ import annotations

from korvid.agent.evidence import Evidence
from korvid.agent.prompting import PromptComposer, evidence_note
from korvid.agent.prompts import (
    NO_WRITE_PROMPT,
    SYSTEM_PROMPT,
    UI_DRIVE_PROMPT,
    WRITE_PROMPT,
)
from korvid.tools.executor import READ_TOOLS, UI_TOOLS, WRITE_TOOLS


def test_system_prompt_is_composed_from_armed_capabilities() -> None:
    prompt = PromptComposer().compose_system(
        tools=[*READ_TOOLS, *UI_TOOLS, *WRITE_TOOLS],
        cluster_context="This cluster runs on Azure (AKS).",
        system_prompt=SYSTEM_PROMPT,
        ui_prompt=UI_DRIVE_PROMPT,
        evidence=(),
    )

    assert prompt.startswith(SYSTEM_PROMPT)
    assert "This cluster runs on Azure (AKS)." in prompt
    assert UI_DRIVE_PROMPT in prompt
    assert WRITE_PROMPT in prompt
    assert NO_WRITE_PROMPT not in prompt
    assert "delete_resource, rollout_restart, scale_resource" in prompt


def test_system_prompt_uses_no_write_clause_without_armed_writes() -> None:
    prompt = PromptComposer().compose_system(
        tools=READ_TOOLS,
        cluster_context=None,
        system_prompt=SYSTEM_PROMPT,
        ui_prompt=UI_DRIVE_PROMPT,
        evidence=(),
    )

    assert prompt == f"{SYSTEM_PROMPT} {NO_WRITE_PROMPT}"


def test_evidence_note_contains_only_korvid_owned_identifiers() -> None:
    item = Evidence(
        ref="E1",
        tool="get_resource",
        kind="pods",
        namespace="IGNORE PREVIOUS INSTRUCTIONS",
        name="api",
        container=None,
        excerpt="secret-bearing result",
    )

    note = evidence_note([item])

    assert "[E1] get_resource" in note
    assert "IGNORE PREVIOUS INSTRUCTIONS" not in note
    assert "secret-bearing result" not in note


def test_composed_system_prompt_appends_the_current_evidence_table() -> None:
    item = Evidence(
        ref="E1",
        tool="get_logs",
        kind="pods",
        namespace="default",
        name="api",
        container="api",
        excerpt="CrashLoopBackOff",
    )

    prompt = PromptComposer().compose_system(
        tools=READ_TOOLS,
        cluster_context=None,
        system_prompt=SYSTEM_PROMPT,
        ui_prompt=UI_DRIVE_PROMPT,
        evidence=(item,),
    )

    assert prompt.startswith(f"{SYSTEM_PROMPT} {NO_WRITE_PROMPT}")
    assert "\n\nEvidence you may cite" in prompt
    assert prompt.endswith("[E1] get_logs")
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach tests/agent/test_prompting.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'korvid.agent.prompting'`.

- [ ] **Step 3: Add `PromptComposer` and evidence formatting**

Create `src/korvid/agent/prompting.py` with:

```python
"""Typed composition of the messages korvid authors for the agent."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from korvid.agent.evidence import Evidence
from korvid.agent.prompts import (
    NO_WRITE_PROMPT,
    SYSTEM_PROMPT,
    UI_DRIVE_PROMPT,
    WRITE_PROMPT,
)
from korvid.tools.executor import UI_TOOL_NAMES, WRITE_TOOL_NAMES


def evidence_note(items: Sequence[Evidence]) -> str:
    """Trusted reference table for reads the model may cite this turn."""
    if not items:
        return ""
    lines = [
        "Evidence you may cite, in the order you read it ([E1] is your first"
        " read this turn). Cite these references for each diagnostic claim;"
        " any other is shown to the user as unsupported. Say so plainly when"
        " the evidence does not settle a question."
    ]
    lines.extend(f"[{item.ref}] {item.tool}" for item in items)
    return "\n".join(lines)


class PromptComposer:
    """Compose model-facing system messages from explicit trusted inputs."""

    def compose_system(
        self,
        *,
        tools: list[dict[str, Any]],
        cluster_context: str | None,
        system_prompt: str | None = None,
        ui_prompt: str | None = None,
        evidence: Sequence[Evidence] = (),
    ) -> str:
        prompt = system_prompt if system_prompt is not None else SYSTEM_PROMPT
        if cluster_context:
            prompt = f"{prompt} {cluster_context}"
        armed = {tool.get("function", {}).get("name") for tool in tools}
        if armed & UI_TOOL_NAMES:
            prompt = f"{prompt} {ui_prompt if ui_prompt is not None else UI_DRIVE_PROMPT}"
        armed_writes = sorted(armed & WRITE_TOOL_NAMES)
        if armed_writes:
            names = ", ".join(armed_writes)
            prompt = f"{prompt} You can request cluster writes with {names}. {WRITE_PROMPT}"
        else:
            prompt = f"{prompt} {NO_WRITE_PROMPT}"
        note = evidence_note(evidence)
        return f"{prompt}\n\n{note}" if note else prompt
```

The evidence rows intentionally exclude kind, namespace, name, container, and
excerpt because those fields originate in model-authored tool arguments or
untrusted results.

- [ ] **Step 4: Keep current callers compatible while moving ownership**

In `src/korvid/agent/prompts.py`, replace the body of `compose_system_prompt`
with a temporary local-import adapter:

```python
def compose_system_prompt(
    tools: list[dict[str, Any]],
    cluster_context: str | None,
    *,
    system_prompt: str | None = None,
    ui_prompt: str | None = None,
) -> str:
    """Compatibility adapter; new code uses `PromptComposer`."""
    from korvid.agent.prompting import PromptComposer

    return PromptComposer().compose_system(
        tools=tools,
        cluster_context=cluster_context,
        system_prompt=system_prompt,
        ui_prompt=ui_prompt,
    )
```

In `src/korvid/agent/runtime.py`, delete the local `evidence_note` and
`_describe` functions, remove `Evidence` from the evidence import, and import
the moved function:

```python
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.prompting import evidence_note
```

Update `tests/agent/test_evidence.py` to import it from its owner:

```python
from korvid.agent.prompting import evidence_note
from korvid.agent.runtime import AgentRuntime
```

- [ ] **Step 5: Run focused composition, profile, and evidence tests**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/agent/test_prompting.py \
  tests/agent/test_profiles.py \
  tests/agent/test_evidence.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Lint and commit the extraction**

Run:

```bash
uv run --frozen ruff check --fix \
  src/korvid/agent/prompting.py \
  src/korvid/agent/prompts.py \
  src/korvid/agent/runtime.py \
  tests/agent/test_prompting.py \
  tests/agent/test_evidence.py
uv run --frozen ruff format \
  src/korvid/agent/prompting.py \
  src/korvid/agent/prompts.py \
  src/korvid/agent/runtime.py \
  tests/agent/test_prompting.py \
  tests/agent/test_evidence.py
git add src/korvid/agent/prompting.py src/korvid/agent/prompts.py \
  src/korvid/agent/runtime.py tests/agent/test_prompting.py \
  tests/agent/test_evidence.py
git commit -m "refactor(agent): extract prompt composition" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit succeeds without changing `uv.lock`.

---

### Task 2: Add typed screen-context composition

**Files:**
- Modify: `src/korvid/agent/prompting.py`
- Modify: `tests/agent/test_prompting.py`

**Interfaces:**
- Consumes: `sanitize_screen_context` and `RedactionRecord`.
- Produces: `PaneContext`, `ScreenContext`, `ComposedUserPrompt`, and `PromptComposer.compose_user(...)`.

- [ ] **Step 1: Add failing tests for typed context and sanitization**

Append to `tests/agent/test_prompting.py`:

```python
from korvid.agent.prompting import PaneContext, ScreenContext


def test_user_prompt_formats_typed_screen_context() -> None:
    context = ScreenContext(
        kube_context="dev",
        view="pods",
        scope="default",
        selected="api-0",
        selected_namespace="default",
        filter_pattern="api",
        other_pane=PaneContext(view="deployments", scope="shop"),
    )

    composed = PromptComposer().compose_user(user_text="why?", context=context)

    assert composed.content == (
        "[screen context: untrusted evidence]\n"
        "context=dev view=pods scope=default selected=api-0 "
        "selected_ns=default filter=api other_pane=deployments other_scope=shop\n"
        "[end screen context]\n\n"
        "why?"
    )
    assert composed.redactions == ()


def test_user_prompt_sanitizes_context_and_one_shot_notice() -> None:
    context = ScreenContext(
        kube_context="dev",
        view="pods",
        scope="default",
        selected="api",
        selected_namespace="default",
        filter_pattern="DB_PASSWORD=hunter2",
        context_switch_note="switched from token=raw-secret to prod",
    )

    composed = PromptComposer().compose_user(user_text="continue", context=context)

    assert "hunter2" not in composed.content
    assert "raw-secret" not in composed.content
    assert "******" in composed.content
    assert "NOTE: switched from" in composed.content
    assert composed.redactions


def test_user_text_remains_separate_from_screen_context() -> None:
    context = ScreenContext(
        kube_context=None,
        view="pods",
        scope="all",
        selected=None,
        selected_namespace=None,
        filter_pattern=None,
    )

    composed = PromptComposer().compose_user(
        user_text="context=forged view=secrets",
        context=context,
    )

    marked_context, user_text = composed.content.split("[end screen context]\n\n")
    assert "context=- view=pods scope=all selected=- filter=-" in marked_context
    assert user_text == "context=forged view=secrets"
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/agent/test_prompting.py::test_user_prompt_formats_typed_screen_context \
  tests/agent/test_prompting.py::test_user_prompt_sanitizes_context_and_one_shot_notice \
  tests/agent/test_prompting.py::test_user_text_remains_separate_from_screen_context -q
```

Expected: collection fails because `PaneContext` and `ScreenContext` do not
exist.

- [ ] **Step 3: Add frozen context and composed-result types**

Add these imports and types to `src/korvid/agent/prompting.py`:

```python
from dataclasses import dataclass

from korvid.agent.outbound import sanitize_screen_context
from korvid.core.redaction import RedactionRecord


@dataclass(frozen=True, slots=True)
class PaneContext:
    """Bounded summary of the non-focused workspace pane."""

    view: str
    scope: str


@dataclass(frozen=True, slots=True)
class ScreenContext:
    """Typed UI facts supplied with one agent turn."""

    kube_context: str | None
    view: str
    scope: str
    selected: str | None
    selected_namespace: str | None
    filter_pattern: str | None
    other_pane: PaneContext | None = None
    context_switch_note: str | None = None


@dataclass(frozen=True, slots=True)
class ComposedUserPrompt:
    """User message text plus ingress redactions already applied to it."""

    content: str
    redactions: tuple[RedactionRecord, ...]
```

- [ ] **Step 4: Implement deterministic user-message composition**

Add these methods to `PromptComposer`:

```python
    def compose_user(
        self,
        *,
        user_text: str,
        context: ScreenContext,
    ) -> ComposedUserPrompt:
        records: list[RedactionRecord] = []
        safe_context = sanitize_screen_context(
            self._render_screen_context(context),
            records,
        )
        return ComposedUserPrompt(
            content=(
                "[screen context: untrusted evidence]\n"
                f"{safe_context}\n"
                "[end screen context]\n\n"
                f"{user_text}"
            ),
            redactions=tuple(records),
        )

    def _render_screen_context(self, context: ScreenContext) -> str:
        parts = [
            f"context={context.kube_context or '-'}",
            f"view={context.view}",
            f"scope={context.scope}",
            f"selected={context.selected or '-'}",
        ]
        if context.selected_namespace is not None:
            parts.append(f"selected_ns={context.selected_namespace}")
        parts.append(f"filter={context.filter_pattern or '-'}")
        if context.other_pane is not None:
            parts.extend(
                (
                    f"other_pane={context.other_pane.view}",
                    f"other_scope={context.other_pane.scope}",
                )
            )
        if context.context_switch_note is not None:
            parts.append(f"NOTE: {context.context_switch_note}")
        return " ".join(parts)
```

Keep `_render_screen_context` private: callers supply facts and must not depend
on the serialized wire wording.

- [ ] **Step 5: Run the pure composer suite**

Run:

```bash
uv run --frozen pytest -p no:tach tests/agent/test_prompting.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Lint and commit typed context composition**

Run:

```bash
uv run --frozen ruff check --fix \
  src/korvid/agent/prompting.py tests/agent/test_prompting.py
uv run --frozen ruff format \
  src/korvid/agent/prompting.py tests/agent/test_prompting.py
git add src/korvid/agent/prompting.py tests/agent/test_prompting.py
git commit -m "refactor(agent): type screen prompt context" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit succeeds.

---

### Task 3: Make `AgentRuntime` consume `PromptComposer`

**Files:**
- Modify: `src/korvid/agent/runtime.py:20-34,233-309,369-404,574-590,966-998`
- Modify: `src/korvid/agent/prompts.py:1-18,172-203`
- Modify: `tests/agent/runtime_fakes.py:7-47`
- Modify: `tests/agent/test_evidence.py`
- Modify: `tests/agent/test_interrupt.py`
- Modify: `tests/agent/test_profiles.py:15-23,379-394`
- Modify: `tests/agent/test_runtime_contracts.py`
- Modify: `tests/agent/test_runtime_core.py`
- Modify: `tests/providers/test_ollama.py`

**Interfaces:**
- Consumes: `PromptComposer`, `ScreenContext`, and `ComposedUserPrompt`.
- Produces: `AgentRuntime.run_turn(user_text: str, screen_context: ScreenContext)` with identical events and outbound messages.

- [ ] **Step 1: Add failing runtime ownership tests**

In `tests/agent/test_runtime_core.py`, import the new context and composer:

```python
from korvid.agent.prompting import PromptComposer, ScreenContext
```

Add:

```python
async def test_runtime_delegates_system_and_user_messages_to_the_composer() -> None:
    class RecordingComposer(PromptComposer):
        def __init__(self) -> None:
            self.system_calls = 0
            self.user_calls = 0

        def compose_system(self, **kwargs: Any) -> str:
            self.system_calls += 1
            return super().compose_system(**kwargs)

        def compose_user(self, **kwargs: Any) -> Any:
            self.user_calls += 1
            return super().compose_user(**kwargs)

    composer = RecordingComposer()
    provider = ScriptedProvider([[{"type": "text_delta", "text": "ok"}, {"type": "done"}]])
    runtime = AgentRuntime(
        provider,
        EchoExecutor(),
        prompt_composer=composer,
    )

    await collect(runtime, "hello")

    assert composer.system_calls >= 2
    assert composer.user_calls == 1
```

The system composer runs at construction and at turn start when stale evidence
is cleared.

- [ ] **Step 2: Run the ownership test and verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/agent/test_runtime_core.py::test_runtime_delegates_system_and_user_messages_to_the_composer -q
```

Expected: FAIL because `AgentRuntime.__init__` does not accept
`prompt_composer`.

- [ ] **Step 3: Replace runtime-owned system composition**

In `src/korvid/agent/runtime.py`, use:

```python
from korvid.agent.evidence import EvidenceLedger
from korvid.agent.prompting import PromptComposer, ScreenContext
```

Remove imports of `sanitize_screen_context` and `compose_system_prompt`.

Add the constructor parameter:

```python
        prompt_composer: PromptComposer | None = None,
```

Replace the existing prompt initialization with:

```python
        self._prompt_composer = prompt_composer or PromptComposer()
        self._cluster_context = cluster_context
        self._system_prompt_override = system_prompt
        self._ui_prompt_override = ui_prompt
        self._max_iterations = max_iterations
        self._max_history_chars = max_history_chars
```

After `_evidence = EvidenceLedger()` is initialized, create the message list:

```python
        self._messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._compose_system_prompt()}
        ]
        self._turn_base = len(self._messages)
```

Move the existing `_turn_base` assignment so it occurs only after
`self._messages` exists. Add:

```python
    def _compose_system_prompt(self) -> str:
        items = [
            item
            for ref in self._evidence.references()
            if (item := self._evidence.resolve(ref)) is not None
        ]
        return self._prompt_composer.compose_system(
            tools=self._tools,
            cluster_context=self._cluster_context,
            system_prompt=self._system_prompt_override,
            ui_prompt=self._ui_prompt_override,
            evidence=items,
        )
```

Replace `_refresh_evidence_note` with:

```python
    def _refresh_evidence_note(self) -> None:
        """Recompose the system message from current trusted inputs."""
        self._messages[0] = {
            "role": "system",
            "content": self._compose_system_prompt(),
        }
```

In `retarget`, replace `_base_prompt` composition with:

```python
        self._cluster_context = cluster_context
        self._refresh_evidence_note()
```

Delete `_base_prompt`; evidence is now another explicit composer input.

- [ ] **Step 4: Replace runtime-owned user-message composition**

Change the public signature:

```python
    async def run_turn(
        self,
        user_text: str,
        screen_context: ScreenContext,
    ) -> AsyncIterator[AgentEvent]:
```

Replace the manual marked-string block with:

```python
            composed = self._prompt_composer.compose_user(
                user_text=user_text,
                context=screen_context,
            )
            user_message = {"role": "user", "content": composed.content}
            self._messages.append(user_message)
            self._remember_ingress(user_message, composed.redactions)
```

The user text still receives the full outbound sanitization pass later. The
composer's ingress records preserve redactions applied specifically while
formatting screen context.

- [ ] **Step 5: Migrate runtime tests to typed context**

In `tests/agent/runtime_fakes.py`, add:

```python
from korvid.agent.prompting import ScreenContext

DEFAULT_SCREEN_CONTEXT = ScreenContext(
    kube_context=None,
    view="pods",
    scope="default",
    selected=None,
    selected_namespace=None,
    filter_pattern=None,
)
```

Change `collect` to:

```python
async def collect(
    runtime: AgentRuntime,
    text: str,
    screen_context: ScreenContext = DEFAULT_SCREEN_CONTEXT,
) -> list[Any]:
    return [event async for event in runtime.run_turn(text, screen_context)]
```

In these files, replace direct string context arguments with
`DEFAULT_SCREEN_CONTEXT` and import it from `tests.agent.runtime_fakes`:

- `tests/agent/test_evidence.py`
- `tests/agent/test_interrupt.py`
- `tests/agent/test_runtime_contracts.py`
- `tests/agent/test_runtime_core.py`
- `tests/providers/test_ollama.py`

For the control-character assertion in `tests/providers/test_ollama.py`, create
a dedicated typed value:

```python
unsafe_context = ScreenContext(
    kube_context=None,
    view="pods\x07",
    scope="default",
    selected=None,
    selected_namespace=None,
    filter_pattern=None,
)
```

Update `tests/agent/test_profiles.py` to import `PromptComposer` from
`korvid.agent.prompting`. Replace its helper's call to
`compose_system_prompt(...)` with:

```python
    return PromptComposer().compose_system(
        tools=profile.tools,
        cluster_context=None,
        system_prompt=profile.system_prompt,
        ui_prompt=profile.ui_prompt,
    )
```

Update `tests/agent/test_evidence.py` to import `evidence_note` only from
`korvid.agent.prompting`.

- [ ] **Step 6: Remove the old composition adapter**

Delete `compose_system_prompt` from `src/korvid/agent/prompts.py`, remove its
`Any` import if now unused, and rewrite the module docstring to:

```python
"""Shipped prompt wording and profile-specific tool descriptions.

This module owns stable wording. `korvid.agent.prompting` owns how wording,
armed capabilities, environment facts, screen context, and evidence become
model-facing messages.
"""
```

- [ ] **Step 7: Run the focused runtime suites**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/agent/test_prompting.py \
  tests/agent/test_profiles.py \
  tests/agent/test_runtime_core.py \
  tests/agent/test_runtime_contracts.py \
  tests/agent/test_evidence.py \
  tests/agent/test_interrupt.py \
  tests/providers/test_ollama.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Lint and commit runtime adoption**

Run:

```bash
uv run --frozen ruff check --fix \
  src/korvid/agent/prompting.py \
  src/korvid/agent/prompts.py \
  src/korvid/agent/runtime.py \
  tests/agent/runtime_fakes.py \
  tests/agent/test_evidence.py \
  tests/agent/test_interrupt.py \
  tests/agent/test_profiles.py \
  tests/agent/test_runtime_contracts.py \
  tests/agent/test_runtime_core.py \
  tests/providers/test_ollama.py
uv run --frozen ruff format \
  src/korvid/agent/prompting.py \
  src/korvid/agent/prompts.py \
  src/korvid/agent/runtime.py \
  tests/agent/runtime_fakes.py \
  tests/agent/test_evidence.py \
  tests/agent/test_interrupt.py \
  tests/agent/test_profiles.py \
  tests/agent/test_runtime_contracts.py \
  tests/agent/test_runtime_core.py \
  tests/providers/test_ollama.py
git add src/korvid/agent/prompting.py src/korvid/agent/prompts.py \
  src/korvid/agent/runtime.py tests/agent/runtime_fakes.py \
  tests/agent/test_evidence.py tests/agent/test_interrupt.py \
  tests/agent/test_profiles.py tests/agent/test_runtime_contracts.py \
  tests/agent/test_runtime_core.py tests/providers/test_ollama.py
git commit -m "refactor(agent): delegate messages to prompt composer" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit succeeds.

---

### Task 4: Make the UI produce typed screen context

**Files:**
- Modify: `src/korvid/ui/agent_ui_controller.py:76-83,895-939`
- Modify: `tests/ui/test_agent_follow.py`
- Modify: `tests/ui/test_agent_interrupt.py`
- Modify: `tests/ui/test_agent_ui_controller.py:332-364,695-704,795-822`
- Modify: `tests/ui/test_agent_wiring.py`
- Modify: `tests/ui/test_ctx_switch.py`
- Modify: `tests/ui/test_protected_contexts.py`
- Modify: `tests/ui/test_secret_screen.py`

**Interfaces:**
- Consumes: `PaneContext` and `ScreenContext`.
- Produces: `AgentUiController.screen_context() -> ScreenContext`; the one-shot context-switch notice is a typed field consumed exactly once.

- [ ] **Step 1: Change UI tests to assert typed facts**

In `tests/ui/test_agent_ui_controller.py`, import:

```python
from korvid.agent.prompting import ScreenContext
```

Change `ScriptedRuntime.contexts` and its signature:

```python
        self.contexts: list[ScreenContext] = []

    async def run_turn(self, text: str, screen_context: ScreenContext) -> Any:
```

Replace the focused-pane assertions with:

```python
async def test_screen_context_reports_the_focused_pane(env: Env) -> None:
    env.workspace.filter_pattern = "web"

    context = env.controller.screen_context()

    assert context.kube_context is None
    assert context.view == "pods"
    assert context.scope == "default"
    assert context.selected == "web-1"
    assert context.selected_namespace == "default"
    assert context.filter_pattern == "web"
    assert context.other_pane is None
```

Replace the split-pane test with:

```python
async def test_screen_context_summarizes_the_other_pane_when_split(env: Env) -> None:
    env.workspace.split()
    env.workspace.focused.kind = "deployments"

    context = env.controller.screen_context()

    assert context.other_pane is not None
    assert context.other_pane.view == env.workspace.panes[0].kind
    assert context.other_pane.scope == env.workspace.panes[0].scope
```

Change the one-shot notice assertions:

```python
    assert runtime.contexts[0].context_switch_note == "kube context switched from a to b"
    assert runtime.contexts[1].context_switch_note is None
```

- [ ] **Step 2: Run the three changed tests and verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_agent_ui_controller.py::test_screen_context_reports_the_focused_pane \
  tests/ui/test_agent_ui_controller.py::test_screen_context_summarizes_the_other_pane_when_split \
  tests/ui/test_agent_ui_controller.py::test_a_context_switch_note_is_delivered_once -q
```

Expected: FAIL because `screen_context()` still returns `str`.

- [ ] **Step 3: Return typed context from `AgentUiController`**

Import:

```python
from korvid.agent.prompting import PaneContext, ScreenContext
```

Replace `screen_context` with:

```python
    def screen_context(self) -> ScreenContext:
        """Bounded facts about the focused and optional secondary pane."""
        selected_key = self._screens.selected_row_key()
        selected = selected_key
        selected_namespace: str | None = None
        if selected_key is not None and "/" in selected_key:
            selected_namespace, _, selected = selected_key.partition("/")
        other_pane: PaneContext | None = None
        if self._workspace.is_split:
            other = self._workspace.panes[1 - self._workspace.focused_index]
            other_pane = PaneContext(view=other.kind, scope=other.scope)
        return ScreenContext(
            kube_context=self._config().kube_context,
            view=self._view.current_kind(),
            scope=self._view.current_scope(),
            selected=selected,
            selected_namespace=selected_namespace,
            filter_pattern=self._workspace.filter_pattern or None,
            other_pane=other_pane,
        )
```

At turn start, replace string concatenation with:

```python
        screen_context = self.screen_context()
        if self._context_note is not None:
            screen_context = dataclasses.replace(
                screen_context,
                context_switch_note=self._context_note,
            )
            self._context_note = None
```

The existing `dataclasses` module import already supports `replace`.

- [ ] **Step 4: Update all UI runtime fakes**

Import `ScreenContext` and change the second parameter annotation from `str` to
`ScreenContext` in every fake `run_turn` implementation in:

- `tests/ui/test_agent_follow.py`
- `tests/ui/test_agent_interrupt.py`
- `tests/ui/test_agent_ui_controller.py`
- `tests/ui/test_agent_wiring.py`
- `tests/ui/test_ctx_switch.py`
- `tests/ui/test_protected_contexts.py`
- `tests/ui/test_secret_screen.py`

Do not convert a `ScreenContext` back to a string inside a fake. Tests that
record it must inspect typed fields.

- [ ] **Step 5: Run the agent UI test set**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/ui/test_agent_follow.py \
  tests/ui/test_agent_interrupt.py \
  tests/ui/test_agent_ui_controller.py \
  tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py \
  tests/ui/test_protected_contexts.py \
  tests/ui/test_secret_screen.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Type-check, lint, and commit UI adoption**

Run:

```bash
uv run --frozen mypy \
  src/korvid/agent/prompting.py \
  src/korvid/agent/runtime.py \
  src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_agent_follow.py \
  tests/ui/test_agent_interrupt.py \
  tests/ui/test_agent_ui_controller.py \
  tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py \
  tests/ui/test_protected_contexts.py \
  tests/ui/test_secret_screen.py
uv run --frozen ruff check --fix \
  src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_agent_follow.py \
  tests/ui/test_agent_interrupt.py \
  tests/ui/test_agent_ui_controller.py \
  tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py \
  tests/ui/test_protected_contexts.py \
  tests/ui/test_secret_screen.py
uv run --frozen ruff format \
  src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_agent_follow.py \
  tests/ui/test_agent_interrupt.py \
  tests/ui/test_agent_ui_controller.py \
  tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py \
  tests/ui/test_protected_contexts.py \
  tests/ui/test_secret_screen.py
git add src/korvid/ui/agent_ui_controller.py \
  tests/ui/test_agent_follow.py tests/ui/test_agent_interrupt.py \
  tests/ui/test_agent_ui_controller.py tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py tests/ui/test_protected_contexts.py \
  tests/ui/test_secret_screen.py
git commit -m "refactor(ui): pass typed agent screen context" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: type-check and commit succeed.

---

### Task 5: Wire the composer once and document the boundary

**Files:**
- Modify: `src/korvid/__main__.py:735-789,828-877`
- Modify: `tests/test_main_wiring.py:531-590`
- Modify: `docs/dev/specs/2026-08-12-korvid-architecture.md:363-421`

**Interfaces:**
- Consumes: `PromptComposer` and the updated `AgentRuntime` constructor.
- Produces: one shared stateless composer for initial and rebuilt runtimes; documented prompt ownership.

- [ ] **Step 1: Add a failing composition-root ownership assertion**

In `tests/test_main_wiring.py::test_agent_wiring_injects_cluster_context`, after
the first rebuild, add:

```python
    assert runtime._prompt_composer is rebuilt._prompt_composer
```

This pins explicit composition-root ownership rather than two runtime-created
defaults.

- [ ] **Step 2: Run the wiring test and verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/test_main_wiring.py::test_agent_wiring_injects_cluster_context -q
```

Expected: FAIL because the two runtimes each create their own composer.

- [ ] **Step 3: Inject one composer into both runtime construction paths**

In `_build_agent_wiring`, add the lazy import:

```python
    from korvid.agent.prompting import PromptComposer
```

Create it beside the prompt overrides:

```python
    prompt_composer = PromptComposer()
    prompt_overrides = _prompt_overrides(config)
```

Pass it to the initial runtime and rebuilt runtime:

```python
            prompt_composer=prompt_composer,
```

Do not store the composer in another mutable box; it is stateless and does not
change on model, profile, or context switch.

- [ ] **Step 4: Document final prompt ownership**

In `docs/dev/specs/2026-08-12-korvid-architecture.md`, add a subsection before
the eval harness section:

```markdown
### Prompt composition boundary

`agent/prompts.py` owns shipped wording, while `agent/prompting.py` owns the
deterministic composition of that wording with armed tools, detected cluster
context, typed screen facts, and the current evidence table. The UI supplies
facts and the runtime supplies evidence; neither assembles model-facing
delimiter text.

`PromptComposer` sanitizes screen facts before they enter conversation history.
The complete message still passes through `OutboundPolicy`, which remains the
fail-closed boundary and records the exact payload handed to the provider.
Prompt composition does not replace or relax outbound enforcement.
```

- [ ] **Step 5: Run targeted integration and architecture checks**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/test_main_wiring.py \
  tests/agent/test_prompting.py \
  tests/agent/test_profiles.py \
  tests/agent/test_runtime_core.py \
  tests/agent/test_runtime_contracts.py \
  tests/agent/test_evidence.py \
  tests/agent/test_interrupt.py \
  tests/providers/test_ollama.py \
  tests/ui/test_agent_follow.py \
  tests/ui/test_agent_interrupt.py \
  tests/ui/test_agent_ui_controller.py \
  tests/ui/test_agent_wiring.py \
  tests/ui/test_ctx_switch.py \
  tests/ui/test_protected_contexts.py \
  tests/ui/test_secret_screen.py -q
uv run --frozen tach check
```

Expected: all tests pass and tach reports no dependency violations.

- [ ] **Step 6: Run the complete repository gate**

Run:

```bash
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy
uv run --frozen tach check
uv run --frozen deptry src/
uv run --frozen pytest --cov --cov-fail-under=80
```

Expected: ruff, mypy, pytest, and tach all succeed. Verify `git status --short`
does not list `uv.lock`.

- [ ] **Step 7: Commit wiring and documentation**

Run:

```bash
git add src/korvid/__main__.py tests/test_main_wiring.py \
  docs/dev/specs/2026-08-12-korvid-architecture.md
git commit -m "docs(agent): define prompt composition boundary" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit succeeds and the worktree is clean.

---

## Follow-up Plan Boundaries

Do not extend this PR into the remaining #316 work.

1. **Resolved policy plan**
   - `ModelDescriptor`, `ModelCapabilities`, provenance, policy inputs,
     `ResolvedAgentPolicy`, and `AgentPolicyResolver`.
   - Compatibility adapter for `build_profile`.
   - Atomic rebuild and retarget with one policy value.

2. **Engine and request boundary plan**
   - `AgentEngine` ABC.
   - UI and composition-root annotations.
   - Mandatory `RequestGateway`.
   - Reference-runtime decomposition only where shared engine contracts justify
     it.

3. **Framework evaluation plan**
   - Shared engine contract suite.
   - Isolated Pydantic AI candidate.
   - Measured adopt/reject report.
   - LangGraph candidate only after a graph-shaped product requirement exists.
