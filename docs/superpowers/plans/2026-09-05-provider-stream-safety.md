# Provider Stream Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reject truncated built-in provider streams and bound every hidden provider-side response accumulator.

**Architecture:** A private `providers/stream_limits.py` module owns fixed byte/count limits and fail-fast accumulation helpers that raise the existing `ProviderError`. OpenAI SSE and Ollama NDJSON retain protocol-specific parsing and terminal-marker rules. The configurator probe applies the shared text bound before concatenation.

**Tech Stack:** Python 3.11+, `httpx`, async generators, pytest, Ruff, mypy strict, Tach.

## Global Constraints

- OpenAI-compatible success requires an exact `[DONE]` SSE data value.
- Ollama success requires a chunk whose `done` value is exactly `True`.
- Stop reading at the first valid terminal marker.
- Maximum native tool calls per response: 64.
- Maximum serialized arguments per tool call: 65,536 UTF-8 bytes.
- Maximum Ollama reasoning per response: 262,144 UTF-8 bytes.
- Maximum connection-test text: 16,384 UTF-8 bytes.
- Limit exhaustion and missing terminal markers raise `ProviderError`; never truncate into success.
- Do not alter the public `LLMProvider` event contract or third-party plugin limits.
- Do not modify `uv.lock`; use the root virtualenv with `PYTHONPATH=src`.

---

### Task 1: Shared Built-in Stream Limits

**Files:**
- Create: `src/korvid/providers/errors.py`
- Create: `src/korvid/providers/stream_limits.py`
- Modify: `src/korvid/providers/openai_compat.py`
- Test: `tests/providers/test_stream_limits.py`

**Interfaces:**
- Produces: `MAX_TOOL_CALLS`, `MAX_TOOL_ARGUMENTS_BYTES`, `MAX_REASONING_BYTES`, `MAX_PROBE_TEXT_BYTES`.
- Produces: `append_bounded(current: str, fragment: str, *, max_bytes: int, label: str) -> str`.
- Produces: `require_count(current: int, *, max_count: int, label: str) -> None`.
- Moves and re-exports: `ProviderError` remains importable from
  `korvid.providers.openai_compat`.
- Raises: `ProviderError` without embedding accumulated provider content.

- [ ] **Step 1: Write failing helper tests**

```python
def test_append_bounded_counts_utf8_bytes() -> None:
    with pytest.raises(ProviderError, match="reasoning exceeds"):
        append_bounded("é", "é", max_bytes=3, label="reasoning")


def test_require_count_rejects_next_item() -> None:
    with pytest.raises(ProviderError, match="tool calls exceeds"):
        require_count(64, max_count=64, label="tool calls")
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/providers/test_stream_limits.py -q
```

Expected: import failure because `stream_limits.py` does not exist.

- [ ] **Step 3: Implement the helpers**

```python
from typing import Final

from korvid.providers.errors import ProviderError

MAX_TOOL_CALLS: Final = 64
MAX_TOOL_ARGUMENTS_BYTES: Final = 65_536
MAX_REASONING_BYTES: Final = 262_144
MAX_PROBE_TEXT_BYTES: Final = 16_384


def append_bounded(current: str, fragment: str, *, max_bytes: int, label: str) -> str:
    combined = current + fragment
    if len(combined.encode("utf-8")) > max_bytes:
        raise ProviderError(f"{label} exceeds {max_bytes} UTF-8 bytes")
    return combined


def require_count(current: int, *, max_count: int, label: str) -> None:
    if current >= max_count:
        raise ProviderError(f"{label} exceeds {max_count}")
```

Move `ProviderError` from `openai_compat.py` to `errors.py`, then import it in
`openai_compat.py`; this preserves the existing public import path as a module
re-export and avoids a dependency cycle.

- [ ] **Step 4: Verify GREEN and quality**

Run the test above plus Ruff and mypy on the new module.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/providers/errors.py src/korvid/providers/stream_limits.py \
  src/korvid/providers/openai_compat.py tests/providers/test_stream_limits.py
git commit -m "security: bound provider stream accumulators"
```

---

### Task 2: OpenAI SSE Terminal and Tool Buffer Safety

**Files:**
- Modify: `src/korvid/providers/openai_compat.py`
- Modify: `tests/providers/test_openai_compat.py`

**Interfaces:**
- Consumes: Task 1 limits and helpers.
- Preserves: `OpenAICompatProvider.complete(...) -> AsyncIterator[dict[str, Any]]`.
- Produces: `ProviderError` on missing `[DONE]`, more than 64 tool calls, or argument overflow.

- [ ] **Step 1: Add terminal regression tests**

Add tests that:

```python
async def test_missing_done_marker_raises_provider_error() -> None:
    body = 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
    with pytest.raises(ProviderError, match=r"missing \\[DONE\\]"):
        _ = [event async for event in _provider(body).complete([], [])]


async def test_data_after_done_is_ignored() -> None:
    body = _sse({"choices": [{"delta": {"content": "ok"}}]})
    body += 'data: {"choices":[{"delta":{"content":"ignored"}}]}\n\n'
    events = [event async for event in _provider(body).complete([], [])]
    assert not any(event.get("text") == "ignored" for event in events)
```

Build post-terminal input manually because `_sse` appends `[DONE]`.

- [ ] **Step 2: Verify terminal tests RED**

Expected: missing marker emits `done`; post-marker behavior documents the break.

- [ ] **Step 3: Add cumulative limit tests**

Monkeypatch module constants to small values. Verify two individually valid
argument fragments exceed the cumulative UTF-8 bound, and a 65th distinct tool
index raises `ProviderError`. Assert no final `done` event is observed.

- [ ] **Step 4: Implement terminal tracking and bounded accumulation**

Set `terminated = False` before `aiter_lines`. On `[DONE]`, set it true and
break. After the response context, raise `ProviderError("OpenAI-compatible
stream ended without [DONE]")` unless true, before emitting tool calls, usage,
or `done`.

When a new tool index appears, call `require_count(len(tool_acc), ...)`.
Replace raw argument concatenation with `append_bounded(...,
MAX_TOOL_ARGUMENTS_BYTES, "tool call arguments")`.

- [ ] **Step 5: Run OpenAI tests and quality**

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/providers/test_openai_compat.py tests/providers/test_stream_limits.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/providers/openai_compat.py tests/providers/test_openai_compat.py
git commit -m "security: require complete OpenAI streams"
```

---

### Task 3: Ollama Terminal, Reasoning, and Tool Safety

**Files:**
- Modify: `src/korvid/providers/ollama.py`
- Modify: `tests/providers/test_ollama.py`

**Interfaces:**
- Consumes: Task 1 limits and helpers.
- Preserves: `OllamaProvider.complete(...) -> AsyncIterator[dict[str, Any]]`.
- Produces: `ProviderError` on missing `done: true`, reasoning overflow, argument overflow, or tool-call exhaustion.

- [ ] **Step 1: Add terminal tests**

Verify EOF after `done: false` raises `ProviderError("Ollama stream ended
without done: true")`. Build a stream with a valid terminal chunk followed by
content/tool data and assert the later data is not emitted or remembered.

- [ ] **Step 2: Verify terminal tests RED**

Expected: EOF currently emits `done`, and post-terminal data is currently
processed.

- [ ] **Step 3: Add hidden-buffer tests**

Monkeypatch limits to small values and verify:

- cumulative multibyte `message.thinking` exceeds the reasoning byte cap;
- serialized arguments exceed the per-call byte cap;
- more than the maximum cumulative native tool calls raises;
- limit failures do not update `_thinking_by_call_id` and do not emit `done`.

- [ ] **Step 4: Implement fail-fast parsing**

Track `terminated = False`. Parse stream errors first. If `chunk.get("done") is
True`, capture usage, set `terminated`, and break before processing `message`.
Otherwise append reasoning with `append_bounded`, yield content, and collect
tools with bounded count and serialized argument bytes.

After the response context, reject unless terminated. Only then remember
reasoning and emit accumulated calls, usage, and `done`.

- [ ] **Step 5: Run Ollama tests and quality**

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/providers/test_ollama.py tests/providers/test_stream_limits.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/providers/ollama.py tests/providers/test_ollama.py
git commit -m "security: require complete Ollama streams"
```

---

### Task 4: Bound Connection Probe and Deliver

**Files:**
- Modify: `src/korvid/providers/configurator.py`
- Modify: `tests/providers/test_configurator.py`
- Verify: all files changed by Tasks 1-3.

**Interfaces:**
- Consumes: `MAX_PROBE_TEXT_BYTES`, `append_bounded`.
- Preserves: `ProviderConfigurator.test(settings: AgentSettings) -> str`.
- Produces: `ProviderError` on probe response overflow and always closes the provider.

- [ ] **Step 1: Add probe overflow test**

Use `ScriptedProvider` with two text deltas that exceed a monkeypatched small
probe limit cumulatively. Assert `ProviderError`, and assert the scripted
provider's close flag/counter shows `aclose()` ran.

- [ ] **Step 2: Verify RED**

Expected: configurator returns the concatenated text instead of raising.

- [ ] **Step 3: Implement bounded concatenation**

Replace:

```python
text += str(ev.get("text", ""))
```

with `append_bounded` using `MAX_PROBE_TEXT_BYTES` and label `"provider
connection test response"`. Keep it inside the existing `try/finally`.

- [ ] **Step 4: Run targeted provider tests**

```bash
PYTHONPATH=src /Users/hwang-inhwan/workspace/kube/.venv/bin/pytest \
  -p no:tach tests/providers/test_stream_limits.py \
  tests/providers/test_openai_compat.py tests/providers/test_ollama.py \
  tests/providers/test_configurator.py -q
```

- [ ] **Step 5: Run full quality gate**

Use the root virtualenv and run Ruff check/format-check, `mypy src`, full
pytest, Tach, `git diff --check`, and:

```bash
test "$(git hash-object uv.lock)" = "$(git rev-parse HEAD:uv.lock)"
```

- [ ] **Step 6: Commit configurator change**

```bash
git add src/korvid/providers/configurator.py tests/providers/test_configurator.py
git commit -m "security: bound provider connection probes"
```

- [ ] **Step 7: Independent review and PR**

Review `origin/main...HEAD` for terminal ordering, buffer accounting,
cancellation, and error leakage. Fix credible findings with RED/GREEN commits.
Push and open:

```text
security: reject truncated provider streams
```

Include `Closes #336`, complete Copilot review rounds, resolve every addressed
thread, and verify all required checks are `SUCCESS`. Do not merge.
