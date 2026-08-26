# Low-Model Fast Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce low-resource Ollama latency by capping output, shortening LOW responses, and skipping redundant provider rounds after successful direct UI open operations.

**Architecture:** The tool schema carries explicit user-intent continuation metadata, the tool harness converts a successful direct open into a trusted terminal acknowledgement, and the engine closes the turn without another provider request. Ollama output limiting remains an adapter/config concern and the LOW prompt teaches the model when to request continuation.

**Tech Stack:** Python 3.11+, asyncio, Ollama native `/api/chat`, pytest, Ruff, mypy, tach.

## Global Constraints

- Never bypass write approval, masking, UID revalidation, or fail-closed audit behavior.
- Never interpolate model arguments or untrusted tool results into a trusted terminal acknowledgement.
- `continue_analysis` defaults to `false`; failures never terminate early.
- `agent.ollama.num_predict` accepts only positive integers and is omitted from the payload when unset.
- No new runtime dependency.

---

### Task 1: Ollama output-token cap

**Files:**
- Modify: `src/korvid/providers/ollama.py`
- Modify: `src/korvid/core/config.py`
- Modify: `src/korvid/__main__.py`
- Modify: `tests/providers/test_ollama.py`
- Modify: `tests/core/test_config.py`
- Modify: `tests/test_main_wiring.py`
- Modify: `docs/agent.md`

**Interfaces:**
- Produces: `OllamaOptions.num_predict: int | None`
- Consumes: `agent.ollama.num_predict` from YAML configuration

- [ ] **Step 1: Write failing provider, config, and wiring tests**

Add assertions that `num_predict: 192` parses into config, reaches
`OllamaOptions`, and appears at `payload["options"]["num_predict"]`; add an
omission assertion for `None`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:
`uv run pytest -p no:tach tests/providers/test_ollama.py tests/core/test_config.py tests/test_main_wiring.py -q`

Expected: failures because `num_predict` is not represented or parsed.

- [ ] **Step 3: Implement the option**

Add `num_predict: int | None = None` to `OllamaOptions`, parse only positive
integers in config, pass it through the composition root, and conditionally add
it to the Ollama request options.

- [ ] **Step 4: Document and verify GREEN**

Add `num_predict: 192` to the native Ollama example and explain that it caps
generation rather than reasoning. Re-run the focused tests and expect all to
pass.

### Task 2: Terminal direct-open operations

**Files:**
- Modify: `src/korvid/tools/registry.py`
- Modify: `src/korvid/agent/tool_harness.py`
- Modify: `src/korvid/agent/native_engine.py`
- Modify: `tests/agent/test_tool_harness.py`
- Modify: `tests/agent/test_native_engine.py`
- Modify: `tests/agent/test_conversation.py`

**Interfaces:**
- Produces: `ToolExecution.terminal_message: str | None`
- Consumes: optional tool argument `continue_analysis: bool`

- [ ] **Step 1: Write failing harness tests**

Test that successful `open_logs`/`open_describe` calls with omitted or false
`continue_analysis` return a fixed terminal message, while true and failed
calls return `None`.

- [ ] **Step 2: Write failing engine tests**

Script one `open_logs` tool round with no second provider response. Assert one
provider call, one bridge action, one trusted `TextDelta`, terminal accounting,
and protocol-complete conversation history. Add cases proving
`continue_analysis=true` and failed bridge results still consume the scripted
second round.

- [ ] **Step 3: Run focused tests and verify RED**

Run:
`uv run pytest -p no:tach tests/agent/test_tool_harness.py tests/agent/test_native_engine.py tests/agent/test_conversation.py -q`

Expected: failures because terminal metadata and engine completion do not
exist.

- [ ] **Step 4: Implement terminal metadata and completion**

Add the optional boolean to both schemas. Have `ToolHarness._run_ui` set a
constant acknowledgement only for successful `open_logs` and
`open_describe` calls that did not request continuation. Let `_Round` carry the
message; after dispatch, append a final assistant message, emit `TextDelta`,
and call `_complete` without another iteration.

- [ ] **Step 5: Run focused tests and verify GREEN**

Re-run the Task 2 command and expect all tests to pass.

### Task 3: LOW operation-first prompt and validation

**Files:**
- Modify: `src/korvid/agent/prompt_packs.py`
- Modify: `tests/agent/test_prompt_packs.py`
- Modify: `docs/agent.md`

**Interfaces:**
- Consumes: `continue_analysis` tool argument introduced by Task 2
- Produces: updated `LOW_KORVID_OPERATOR_PACK`

- [ ] **Step 1: Write a failing prompt-contract test**

Assert the LOW pack requires immediate tool dispatch, names
`continue_analysis`, limits final diagnosis to root cause/evidence/next
operation, and forbids plan narration and generic advice.

- [ ] **Step 2: Run the prompt test and verify RED**

Run: `uv run pytest -p no:tach tests/agent/test_prompt_packs.py -q`

Expected: failure because the new clauses are absent.

- [ ] **Step 3: Update the LOW pack and documentation**

Add the concise clauses without changing `SAFETY_CONTRACT`. Document the
operation-first behavior and how to opt into continued analysis.

- [ ] **Step 4: Run targeted quality gates**

Run:
`uv run ruff check src/korvid/providers/ollama.py src/korvid/core/config.py src/korvid/__main__.py src/korvid/tools/registry.py src/korvid/agent/tool_harness.py src/korvid/agent/native_engine.py src/korvid/agent/prompt_packs.py tests/providers/test_ollama.py tests/core/test_config.py tests/test_main_wiring.py tests/agent/test_tool_harness.py tests/agent/test_native_engine.py tests/agent/test_conversation.py tests/agent/test_prompt_packs.py`

Run:
`uv run ruff format --check` with the same files.

Run:
`uv run mypy src/korvid`

Run:
`uv run tach check`

Run:
`uv run pytest -p no:tach tests/providers/test_ollama.py tests/core/test_config.py tests/test_main_wiring.py tests/agent/test_tool_harness.py tests/agent/test_native_engine.py tests/agent/test_conversation.py tests/agent/test_prompt_packs.py -q`

Expected: every command exits 0.

