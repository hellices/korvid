# Plan 4 Slice 1 — Agent Runtime (read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A working conversational agent panel (Ctrl-A) backed by an OpenAI-compatible provider adapter and a read-only agentic tool loop — no write tools, no approval gate yet (next slice).

**Architecture:** `providers/openai_compat.py` implements the existing `LLMProvider` ABC over httpx SSE. `agent/runtime.py` owns the tool-use loop and yields typed `AgentEvent`s. `agent/tools.py` defines 4 read-only tools executed against `KubeClient`. `ui/widgets/agent_panel.py` renders the conversation; `app.py` only wires events to the panel. Module boundaries stay tach-enforced (`providers → agent`, `agent → core/k8s`, `ui → agent`).

**Tech Stack:** Python 3.11+, Textual, httpx (new dep), kubernetes_asyncio, pytest + httpx.MockTransport.

## Global Constraints

- mypy --strict; ruff (line 100, C90 max-complexity 10); tach boundaries as in `tach.toml`
- pytest coverage ≥ 80% (CI enforces `--cov-fail-under=80`)
- No default/bundled provider: with no provider configured the TUI is fully functional and Ctrl-A shows a setup hint (design §6.3)
- Secret payloads must never enter LLM context: `get_resource` masks `data`/`stringData` values for Secrets (design §6.2)
- Every tool result is ingest-capped (~8000 chars) before entering conversation history (design §6.2 tier 1)
- Commit trailer: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

---

### Task 1: CI — run the local pre-commit harness on the server

**Files:**
- Modify: `.github/workflows/ci.yml`

The Makefile/pre-commit harness runs typos, validate-pyproject, ruff-format and the bare-`type: ignore` guard locally, but CI never runs them. Add a `pre-commit` job.

**Interfaces:** none (CI only).

- [ ] **Step 1: Add the job**

Append to `.github/workflows/ci.yml` (same indentation as existing jobs):

```yaml
  pre-commit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9  # v9.0.0
        with:
          enable-cache: true
      - run: uv sync --locked --dev
      - run: uv run pre-commit run --all-files --show-diff-on-failure
```

- [ ] **Step 2: Verify locally**

Run: `uv run pre-commit run --all-files` — expected: all hooks pass.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run pre-commit harness (typos, format, pyproject) in Actions"
```

---

### Task 2: Config — provider settings

**Files:**
- Modify: `src/korvid/core/config.py`
- Test: `tests/core/test_config.py`

**Interfaces:**
- Produces: `KorvidConfig.agent_base_url: str | None`, `agent_model: str | None`, `agent_api_key_env: str | None` (frozen dataclass fields, default None). Existing `agent_enabled`/`agent_provider` semantics unchanged.

Config YAML shape:

```yaml
agent:
  provider: openai-compat
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  api_key_env: OPENAI_API_KEY   # name of env var holding the key; optional (ollama)
```

- [ ] **Step 1: Write failing tests** (append to `tests/core/test_config.py`)

```python
def test_agent_provider_settings_parsed(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent:\n  provider: openai-compat\n  base_url: http://localhost:11434/v1\n"
        "  model: llama3\n  api_key_env: MY_KEY\n"
    )
    cfg = load_config(p)
    assert cfg.agent_provider == "openai-compat"
    assert cfg.agent_base_url == "http://localhost:11434/v1"
    assert cfg.agent_model == "llama3"
    assert cfg.agent_api_key_env == "MY_KEY"


def test_agent_settings_default_none(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("namespace: default\n")
    cfg = load_config(p)
    assert cfg.agent_base_url is None
    assert cfg.agent_model is None
    assert cfg.agent_api_key_env is None
```

- [ ] **Step 2: Run** `uv run pytest tests/core/test_config.py -q` — expect FAIL (unknown attribute).

- [ ] **Step 3: Implement** — add three fields to `KorvidConfig` and read them in `load_config` from `agent_raw` (`base_url`, `model`, `api_key_env`; coerce non-str to None via `_opt_str` helper: `return value if isinstance(value, str) and value else None`).

- [ ] **Step 4: Run tests** — PASS; run `make check`.

- [ ] **Step 5: Commit** `feat(config): agent provider settings (base_url/model/api_key_env)`

---

### Task 3: Agent events module

**Files:**
- Create: `src/korvid/agent/events.py`
- Test: `tests/agent/test_events.py` (create `tests/agent/__init__.py` empty)

**Interfaces:**
- Produces (all frozen dataclasses):
  - `TextDelta(text: str)`
  - `ToolCallStarted(call_id: str, name: str, arguments: str)`
  - `ToolCallFinished(call_id: str, name: str, ok: bool, summary: str)`
  - `TurnComplete(input_tokens: int, output_tokens: int, estimated: bool)`
  - `AgentError(message: str)`
  - `AgentEvent = TextDelta | ToolCallStarted | ToolCallFinished | TurnComplete | AgentError` (type alias)

- [ ] **Step 1: Test**

```python
from korvid.agent.events import AgentError, TextDelta, ToolCallFinished, TurnComplete


def test_events_are_frozen_and_typed() -> None:
    d = TextDelta(text="hi")
    assert d.text == "hi"
    t = TurnComplete(input_tokens=10, output_tokens=5, estimated=True)
    assert t.estimated is True
    f = ToolCallFinished(call_id="c1", name="get_logs", ok=False, summary="boom")
    assert not f.ok
    assert AgentError(message="x").message == "x"
```

- [ ] **Step 2: Run to fail**, **Step 3: implement the dataclasses exactly as the interface above** (module docstring: "Typed events yielded by AgentRuntime to the UI (design §6.1 panel contents)"), **Step 4: pass + make check**, **Step 5: commit** `feat(agent): typed AgentEvent stream dataclasses`

---

### Task 4: OpenAI-compatible provider adapter

**Files:**
- Create: `src/korvid/providers/openai_compat.py`
- Modify: `pyproject.toml` (add `"httpx>=0.27"` to `[project] dependencies`)
- Test: `tests/providers/test_openai_compat.py` (create `tests/providers/__init__.py`)

**Interfaces:**
- Consumes: `korvid.agent.provider.LLMProvider` ABC.
- Produces: `OpenAICompatProvider(base_url: str, model: str, api_key: str | None = None, client: httpx.AsyncClient | None = None)`.
  - `name` property → `f"{model}"`
  - `complete(messages, tools, stream=True)` async-generator yielding provider event dicts:
    - `{"type": "text_delta", "text": str}`
    - `{"type": "tool_call", "id": str, "name": str, "arguments": str}` (arguments = complete JSON string, emitted once per call after stream end)
    - `{"type": "usage", "input_tokens": int, "output_tokens": int}` (only when server reports usage)
    - `{"type": "done"}`

Implementation notes (must follow):
- POST `{base_url}/chat/completions` with `{"model": ..., "messages": ..., "stream": true, "stream_options": {"include_usage": true}}`; include `"tools": tools` only when non-empty. `Authorization: Bearer {api_key}` header only when api_key set.
- Parse SSE: iterate `resp.aiter_lines()`; lines starting `data: `; `data: [DONE]` ends. Each JSON chunk: `choices[0].delta.content` → text_delta; `choices[0].delta.tool_calls` is a list of `{index, id?, function: {name?, arguments?}}` fragments — accumulate per index (id and name arrive on the first fragment, arguments concatenate). After `[DONE]`, emit accumulated tool_calls in index order, then usage (from the final chunk's top-level `usage` when present: `prompt_tokens`/`completion_tokens`), then done.
- Non-2xx → raise `ProviderError(f"...")` — define `class ProviderError(Exception)` in this module.
- Own the AsyncClient lazily: create on first call when not injected; `timeout=httpx.Timeout(60.0, connect=10.0)`.

- [ ] **Step 1: Failing tests** using `httpx.MockTransport`:

```python
import json

import httpx
import pytest

from korvid.providers.openai_compat import OpenAICompatProvider, ProviderError


def _sse(*chunks: dict) -> str:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return body + "data: [DONE]\n\n"


def _provider(body: str, status: int = 200, capture: dict | None = None) -> OpenAICompatProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture["json"] = json.loads(request.content)
            capture["headers"] = dict(request.headers)
        return httpx.Response(status, text=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider(
        base_url="http://x/v1", model="m1", api_key="sk-test", client=client
    )


async def test_streams_text_deltas_and_done() -> None:
    body = _sse(
        {"choices": [{"delta": {"content": "Wor"}}]},
        {"choices": [{"delta": {"content": "ld"}}]},
    )
    events = [e async for e in _provider(body).complete([{"role": "user", "content": "hi"}], [])]
    assert {"type": "text_delta", "text": "Wor"} in events
    assert {"type": "text_delta", "text": "ld"} in events
    assert events[-1] == {"type": "done"}


async def test_accumulates_tool_call_fragments() -> None:
    body = _sse(
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c1", "function": {"name": "get_logs", "arguments": "{\"po"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "d\": \"a\"}"}}]}}]},
    )
    events = [e async for e in _provider(body).complete([], [{"type": "function"}])]
    calls = [e for e in events if e["type"] == "tool_call"]
    assert calls == [{"type": "tool_call", "id": "c1", "name": "get_logs",
                      "arguments": '{"pod": "a"}'}]


async def test_reports_usage_when_present() -> None:
    body = _sse(
        {"choices": [{"delta": {"content": "x"}}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
    )
    events = [e async for e in _provider(body).complete([], [])]
    assert {"type": "usage", "input_tokens": 12, "output_tokens": 3} in events


async def test_sends_auth_header_and_tools() -> None:
    cap: dict = {}
    body = _sse({"choices": [{"delta": {"content": "x"}}]})
    tools = [{"type": "function", "function": {"name": "t"}}]
    _ = [e async for e in _provider(body, capture=cap).complete([{"role": "user", "content": "q"}], tools)]
    assert cap["headers"]["authorization"] == "Bearer sk-test"
    assert cap["json"]["tools"] == tools
    assert cap["json"]["stream"] is True


async def test_non_2xx_raises_provider_error() -> None:
    with pytest.raises(ProviderError):
        _ = [e async for e in _provider("nope", status=401).complete([], [])]
```

- [ ] **Step 2: Run to fail** (`uv run pytest tests/providers -q`), **Step 3: implement**, **Step 4: pass + make check** (deptry must see httpx used; run `uv sync` after pyproject edit), **Step 5: commit** `feat(providers): OpenAI-compatible streaming adapter (httpx SSE)`

---

### Task 5: Read-only agent tools

**Files:**
- Create: `src/korvid/agent/tools.py`
- Modify: `src/korvid/k8s/client.py` (add `list_objects`)
- Test: `tests/agent/test_tools.py`, extend `tests/k8s/test_client.py`

**Interfaces:**
- Consumes: `KubeClient.get_object(meta, namespace, name) -> dict`, `stream_logs(...) -> AsyncIterator[LogLine]`, `list_events_for(namespace, name) -> list[dict]`, `ResourceMeta`.
- Produces:
  - `KubeClient.list_objects(meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]` — LIST-only (reuses the path logic of watch_objects' LIST phase; wrap ApiException→ApiStatusError like `_request_json` already does).
  - `READ_TOOLS: list[dict[str, Any]]` — OpenAI function-tool JSON schema for 4 tools:
    - `list_resources(kind: str, namespace?: str)`
    - `get_resource(kind: str, name: str, namespace?: str)`
    - `get_logs(pod: str, namespace: str, container?: str, tail_lines?: int (default 100, max 500))`
    - `get_events(namespace: str, name: str)`
  - `MAX_RESULT_CHARS = 8000`
  - `class ToolExecutor:`
    - `__init__(self, kube: KubeClient, aliases: Mapping[str, ResourceMeta])`
    - `async def execute(self, name: str, arguments: dict[str, Any]) -> str` — returns plain-text result; never raises: catches Exception and returns `f"ERROR: {exc}"`; result truncated to `MAX_RESULT_CHARS` with suffix `"\n… [truncated — narrow the query]"`.

Behavior details:
- `list_resources`: resolve `aliases[kind]` (unknown kind → `"ERROR: unknown kind …"`); format one line per object: `f"{ns}/{name}  {status}  age={age}"` (GenericSummary fields).
- `get_resource`: `get_object` then YAML-dump. **Masking**: when `manifest.get("kind") == "Secret"`, replace every value in `data`/`stringData` with `"***MASKED***"` before dump. Also strip `metadata.managedFields`.
- `get_logs`: collect `stream_logs(namespace, pod, container, follow=False, tail_lines=n)` lines into text. `container` optional — pass through (client already handles container=None? check: stream_logs signature requires container; if the tool gets no container, call get_object on the pod to pick the first container name).
- `get_events`: format each event dict as `f"{type} {reason} ({count}x): {message}"` — inspect `list_events_for` return shape in client and match it.

- [ ] **Step 1: Failing tests** (representative — write all):

```python
# tests/agent/test_tools.py
from typing import Any

import pytest

from korvid.agent.tools import MAX_RESULT_CHARS, READ_TOOLS, ToolExecutor
from korvid.k8s.discovery import PODS_META


class FakeKube:
    def __init__(self) -> None:
        self.manifest: dict[str, Any] = {"kind": "Pod", "metadata": {"name": "a"}}

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        return self.manifest


def make_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META})


def test_read_tools_schema_names() -> None:
    names = [t["function"]["name"] for t in READ_TOOLS]
    assert names == ["list_resources", "get_resource", "get_logs", "get_events"]


async def test_get_resource_masks_secret_data() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {"name": "s", "managedFields": [{"x": 1}]},
        "data": {"password": "aGVsbG8="},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )
    assert "aGVsbG8=" not in out
    assert "***MASKED***" in out
    assert "managedFields" not in out


async def test_unknown_tool_and_kind_return_error_text() -> None:
    ex = make_executor(FakeKube())
    assert (await ex.execute("nope", {})).startswith("ERROR:")
    assert (await ex.execute("list_resources", {"kind": "wat"})).startswith("ERROR:")


async def test_result_is_capped() -> None:
    kube = FakeKube()
    kube.manifest = {"kind": "Pod", "metadata": {"name": "a"}, "blob": "x" * 20000}
    out = await make_executor(kube).execute("get_resource", {"kind": "pods", "name": "a"})
    assert len(out) <= MAX_RESULT_CHARS + 50
    assert "[truncated" in out


async def test_executor_never_raises() -> None:
    class Boom:
        async def get_object(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("kaput")

    out = await make_executor(Boom()).execute("get_resource", {"kind": "pods", "name": "a"})
    assert out.startswith("ERROR:")
```

Plus a `list_objects` unit test in `tests/k8s/test_client.py` following that file's existing `_request_json` stubbing pattern (LIST returns items → GenericSummary list; namespaced vs cluster path).

- [ ] **Step 2: fail**, **Step 3: implement**, **Step 4: pass + make check**, **Step 5: commit** `feat(agent): read-only tools with ingest caps and Secret masking`

---

### Task 6: AgentRuntime — the tool-use loop

**Files:**
- Create: `src/korvid/agent/runtime.py`
- Test: `tests/agent/test_runtime.py`

**Interfaces:**
- Consumes: `LLMProvider.complete`, `ToolExecutor.execute`, `READ_TOOLS`, events from Task 3.
- Produces:
  - `SYSTEM_PROMPT: str` (concise: "You are korvid's Kubernetes diagnostic agent… use tools to inspect, cite evidence, never guess resource state.")
  - `class AgentRuntime:`
    - `__init__(self, provider: LLMProvider, executor: ToolExecutor, *, max_iterations: int = 15)`
    - `run_turn(self, user_text: str, screen_context: str) -> AsyncIterator[AgentEvent]` (async generator)
    - `@property def total_tokens(self) -> tuple[int, int]` — cumulative (input, output)

Loop contract:
- History (`self._messages`) persists across turns. Message 0 is the system prompt. Each `run_turn` appends `{"role": "user", "content": f"[screen] {screen_context}\n\n{user_text}"}`.
- Per iteration: consume `provider.complete(self._messages, READ_TOOLS)`; yield `TextDelta` for text_delta events; collect tool_call events; accumulate usage events into totals (estimated=False when usage seen this turn, else estimate `len(text)//4` for output and 0 input, estimated=True).
- After `done`: append assistant message (content + `tool_calls` list in OpenAI format when any). If no tool calls → yield `TurnComplete(...)`, return.
- For each tool call: yield `ToolCallStarted(call_id, name, arguments)`; `result = await executor.execute(name, json.loads(arguments or "{}"))` (json errors → result = "ERROR: bad arguments"); yield `ToolCallFinished(call_id, name, ok=not result.startswith("ERROR:"), summary=result[:120])`; append `{"role": "tool", "tool_call_id": call_id, "content": result}`. Loop again.
- Iteration cap reached → yield `AgentError("iteration limit reached (15) — refine the question")` then `TurnComplete`.
- Provider exception → yield `AgentError(str(exc))` then return (history keeps the user message; no partial assistant message appended).

- [ ] **Step 1: Failing tests** with a scripted fake provider:

```python
import json
from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.events import (
    AgentError, TextDelta, ToolCallFinished, ToolCallStarted, TurnComplete,
)
from korvid.agent.runtime import AgentRuntime


class ScriptedProvider:
    """Each call to complete() pops the next scripted event list."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = turns
        self.calls: list[list[dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "scripted"

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool = True
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append([dict(m) for m in messages])
        for ev in self.turns.pop(0):
            yield ev


class EchoExecutor:
    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return f"result-of-{name}"


async def collect(runtime: AgentRuntime, text: str) -> list[Any]:
    return [e async for e in runtime.run_turn(text, "view=pods ns=default")]


async def test_text_only_turn() -> None:
    p = ScriptedProvider([[{"type": "text_delta", "text": "hi"}, {"type": "done"}]])
    events = await collect(AgentRuntime(p, EchoExecutor()), "hello")
    assert events[0] == TextDelta(text="hi")
    assert isinstance(events[-1], TurnComplete)
    # system + user message present
    assert p.calls[0][0]["role"] == "system"
    assert "view=pods" in p.calls[0][1]["content"]


async def test_tool_call_roundtrip() -> None:
    p = ScriptedProvider([
        [{"type": "tool_call", "id": "c1", "name": "get_logs", "arguments": "{}"},
         {"type": "done"}],
        [{"type": "text_delta", "text": "done"}, {"type": "done"}],
    ])
    events = await collect(AgentRuntime(p, EchoExecutor()), "logs?")
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["ToolCallStarted", "ToolCallFinished", "TextDelta", "TurnComplete"]
    # second provider call saw the tool result message
    roles = [m["role"] for m in p.calls[1]]
    assert roles[-1] == "tool"
    assert p.calls[1][-1]["content"] == "result-of-get_logs"


async def test_iteration_cap() -> None:
    turn = [{"type": "tool_call", "id": "c", "name": "t", "arguments": "{}"}, {"type": "done"}]
    p = ScriptedProvider([list(turn) for _ in range(20)])
    events = await collect(AgentRuntime(p, EchoExecutor(), max_iterations=3), "loop")
    errs = [e for e in events if isinstance(e, AgentError)]
    assert errs and "iteration limit" in errs[0].message
    assert len(p.calls) == 3


async def test_provider_error_surfaces() -> None:
    class BadProvider(ScriptedProvider):
        async def complete(self, messages, tools, *, stream=True):  # type: ignore[override]
            raise RuntimeError("api down")
            yield  # pragma: no cover

    events = await collect(AgentRuntime(BadProvider([]), EchoExecutor()), "x")
    assert isinstance(events[0], AgentError)


async def test_history_persists_across_turns() -> None:
    p = ScriptedProvider([
        [{"type": "text_delta", "text": "a"}, {"type": "done"}],
        [{"type": "text_delta", "text": "b"}, {"type": "done"}],
    ])
    rt = AgentRuntime(p, EchoExecutor())
    await collect(rt, "first")
    await collect(rt, "second")
    contents = [m.get("content", "") for m in p.calls[1]]
    assert any("first" in c for c in contents)
    assert any(m["role"] == "assistant" for m in p.calls[1])


async def test_usage_accumulates() -> None:
    p = ScriptedProvider([[
        {"type": "text_delta", "text": "x"},
        {"type": "usage", "input_tokens": 100, "output_tokens": 7},
        {"type": "done"},
    ]])
    rt = AgentRuntime(p, EchoExecutor())
    events = await collect(rt, "q")
    tc = [e for e in events if isinstance(e, TurnComplete)][0]
    assert (tc.input_tokens, tc.output_tokens, tc.estimated) == (100, 7, False)
    assert rt.total_tokens == (100, 7)
```

- [ ] **Step 2: fail**, **Step 3: implement** (keep `run_turn` under C90 10 — split helpers `_consume_stream`, `_dispatch_tools`), **Step 4: pass + make check**, **Step 5: commit** `feat(agent): agentic tool-use loop with typed event stream`

---

### Task 7: Provider registry

**Files:**
- Create: `src/korvid/providers/registry.py`
- Test: `tests/providers/test_registry.py`

**Interfaces:**
- Consumes: `KorvidConfig` (Task 2 fields), `OpenAICompatProvider`.
- Produces: `create_provider(config: KorvidConfig) -> LLMProvider | None`
  - Returns None when `not config.agent_enabled` or provider name unknown or `agent_model`/`agent_base_url` missing (log a warning for misconfiguration, silent None when simply unset).
  - `"openai-compat"` (accept aliases `"openai"`, `"ollama"`, `"azure"`, `"vllm"`) → `OpenAICompatProvider(base_url=..., model=..., api_key=os.environ.get(config.agent_api_key_env) if set else None)`.

- [ ] **Step 1: Failing tests**

```python
import pytest

from korvid.core.config import KorvidConfig
from korvid.providers.openai_compat import OpenAICompatProvider
from korvid.providers.registry import create_provider


def test_none_when_agent_disabled() -> None:
    assert create_provider(KorvidConfig()) is None


def test_openai_compat_created(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K", "sk-1")
    cfg = KorvidConfig(
        agent_enabled=True, agent_provider="openai-compat",
        agent_base_url="http://x/v1", agent_model="m", agent_api_key_env="K",
    )
    p = create_provider(cfg)
    assert isinstance(p, OpenAICompatProvider)


def test_none_when_model_missing() -> None:
    cfg = KorvidConfig(agent_enabled=True, agent_provider="openai-compat",
                       agent_base_url="http://x/v1")
    assert create_provider(cfg) is None
```

- [ ] **Steps 2-4: fail → implement → pass + make check**, **Step 5: commit** `feat(providers): config-driven provider registry`

---

### Task 8: AgentPanel widget

**Files:**
- Create: `src/korvid/ui/widgets/agent_panel.py`
- Test: `tests/ui/test_agent_panel.py`

**Interfaces:**
- Consumes: `AgentEvent` types (Task 3).
- Produces: `class AgentPanel(Vertical)`:
  - Message: `class PromptSubmitted(Message): text: str` (inner class, posted when the input is submitted non-empty; input then cleared)
  - `set_header(model: str, input_tokens: int, output_tokens: int, estimated: bool)` → header Static shows `f"⚡ {model} · ↑{fmt(in)} ↓{fmt(out)} tok"` (`fmt` = thousands as `12.3k`; prefix `~` when estimated)
  - `show_setup_hint()` → conversation shows config example, input disabled
  - `apply_event(event: AgentEvent)` — TextDelta appends to the current assistant block; ToolCallStarted writes `🔧 name(args…) …`; ToolCallFinished rewrites intent as `🔧 name ✓`/`✗ summary`; AgentError writes red error line; TurnComplete re-enables input (disabled while a turn is running) and updates header
  - `begin_turn(user_text: str)` — echoes the user line, disables input
  - Layout: header Static (`id="agent-header"`), `RichLog(id="agent-log", wrap=True)`, `Input(id="agent-input", placeholder="Ask about the cluster…")`. Panel styles: `width: 40%; dock: right; border-left: solid $accent;` via DEFAULT_CSS. Hidden by default (`display: none` handled by the app).

Testing approach: mount inside a bare `App` test harness (follow `tests/ui/test_log_pane.py` patterns — build a minimal App subclass in the test file that composes the panel), drive with `run_test()`/Pilot:

```python
async def test_prompt_submitted_posted_and_input_cleared() -> None: ...
async def test_text_deltas_accumulate_in_log() -> None: ...
async def test_tool_call_lines_rendered() -> None: ...
async def test_setup_hint_disables_input() -> None: ...
async def test_input_disabled_during_turn_reenabled_on_complete() -> None: ...
```

Each with concrete asserts on the widgets (query `RichLog.lines` text like `_richlog_text` in test_log_pane.py; `Input.disabled`).

- [ ] **Steps: failing tests → implement → pass + make check → commit** `feat(ui): agent panel widget (streaming text, tool-call log, input)`

---

### Task 9: App wiring — Ctrl-A, screen context, turn task

**Files:**
- Modify: `src/korvid/ui/app.py`, `src/korvid/__main__.py`
- Test: `tests/ui/test_agent_wiring.py`

**Interfaces:**
- Consumes: `AgentRuntime` (Task 6), `AgentPanel` (Task 8), `create_provider`+`ToolExecutor` (composition in `__main__`).
- Produces:
  - `KorvidApp.__init__` gains keyword `agent_runtime: AgentRuntime | None = None` and `agent_model_name: str | None = None`.
  - Binding `Binding("ctrl+a", "toggle_agent", "AI")` — always registered; action shows the panel; when `agent_runtime is None` the panel shows the setup hint (design §6.3 item 3).
  - `action_toggle_agent()` toggles `AgentPanel.display`; focus moves to the panel input when opened, back to the table when closed.
  - `on_agent_panel_prompt_submitted(msg)` → `panel.begin_turn(msg.text)`; spawn task `self._run_agent_turn(msg.text)` (store handle in `self._agent_task`; ignore submit while a turn is running).
  - `_run_agent_turn`: builds `screen_context = f"view={kind} scope={scope} selected={name or '-'} filter={active_filter or '-'}"` from current app state; `async for ev in runtime.run_turn(...)` → `panel.apply_event(ev)`; wrap in try/except → `panel.apply_event(AgentError(str(exc)))`.
  - `__main__._run()`: after aliases built — `provider = create_provider(config)`; when provider: `runtime = AgentRuntime(provider, ToolExecutor(kube, aliases))`; pass to app.
- **Keep app.py small**: only wiring lives here (~80 lines); all rendering in the panel, all loop logic in the runtime.

Tests (reuse the fake-runtime approach — a stub with scripted `run_turn` async generator):

```python
async def test_ctrl_a_toggles_panel_display() -> None: ...
async def test_no_runtime_shows_setup_hint() -> None: ...
async def test_prompt_drives_runtime_and_renders_reply() -> None: ...
async def test_screen_context_includes_current_view() -> None: ...  # assert stub received "view=pods"
async def test_second_submit_ignored_while_turn_running() -> None: ...
```

- [ ] **Steps: failing tests → implement → pass + make check → commit** `feat(ui): Ctrl-A agent panel wiring with screen-context injection`

---

### Task 10: Docs + smoke + PR

**Files:**
- Modify: `README.md` (agent section: config example, Ctrl-A, read-only slice note)

- [ ] **Step 1: README** — add "AI agent (preview)" section with the YAML example from Task 2 and the "no provider → TUI fully functional" note.
- [ ] **Step 2: Full harness** — `make check` + full pytest; coverage ≥80%.
- [ ] **Step 3: Real smoke** — run korvid against the real cluster (pty script pattern): Ctrl-A opens panel; without provider config → setup hint visible. (LLM round-trip smoke optional — only if an endpoint is available.)
- [ ] **Step 4: Commit + push + PR** — `gh pr create` (base main), then request Copilot review; iterate on review rounds until no actionable inline comments remain.
