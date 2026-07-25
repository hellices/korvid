# Plan 4 Slice 3 — Agent Drives the TUI (UI-Control Tools)

**Status: IN PROGRESS** — branch `agent-ui-drive-slice3`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent act like GitHub Copilot CLI / OpenClaw agents inside korvid: instead of only answering in the chat panel, the agent **drives the TUI directly** — switching resource views, applying filters, opening the log pane, and opening describe — while narrating its reasoning. This implements design spec §4.1 ("UI Bus"), §5 ("agent-drive mode"), and §6 tool table row "UI control".

**Why:** Today the agent is a read-only Q&A proxy (an "MCP in a panel"). The design spec's core differentiator is an agent whose actions land in the same TUI the user works in, using the same command bus as keystrokes.

**Architecture:** tach layers forbid `agent → ui`. So:
- `korvid.agent` defines a `UIBridge` **Protocol** (structural, no ui import) plus `UI_TOOLS` OpenAI tool schemas. `ToolExecutor` gains an optional bridge and dispatches the four UI tools to it.
- `korvid.ui.app.KorvidApp` implements the bridge methods by reusing the *exact same* handlers user keystrokes hit (`on_navigate_command`, `on_filter_command`, `_open_log_pane`, `DescribeScreen`) and returning a short confirmation string the model can read ("switched to pods in prod — 12 rows visible").
- `korvid.__main__` (composition root) late-binds: the executor is created before the app exists, so it holds a small mutable proxy whose target is set to the app right after construction.

**Safety (spec §6):** UI-control tools are **ungated** — they change only what is on screen, never cluster state. Every UI action is still visible in the agent panel's tool-call log, and the affected change is announced via `notify` with an "agent" marker. Any user keystroke takes priority naturally (Textual message queue; agent never grabs focus).

## Global Constraints

- tach layers: `korvid.agent` depends only on `core`, `k8s`. `UIBridge` must be a `typing.Protocol` in `agent`, satisfied structurally by `KorvidApp`.
- ruff S101 (no assert in src/), mypy --strict, `make check` before every commit.
- pytest needs `-p no:tach`.
- Commit trailer: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
- UI tool results feed conversation history — cap via existing `cap_result`.
- Tool names/args (exact): `navigate(view, namespace?)`, `set_filter(pattern)` (empty clears), `open_logs(pod, namespace, container?)`, `open_describe(kind, name, namespace?)`.

---

### Task 1: UIBridge protocol + UI tool schemas + executor dispatch (agent layer)

**Files:** modify `src/korvid/agent/tools.py`; test `tests/agent/test_tools.py`.

- `UIBridge(Protocol)`: four async methods, each returns `str` (confirmation or "ERROR: …").
- `UI_TOOLS: list[dict]` — OpenAI function schemas for the four tools.
- `ToolExecutor(kube, aliases, ui=None)`; `_dispatch` routes the four names to `self._ui`; when `ui is None` returns `ERROR: UI control unavailable`.
- All results (including bridge errors) flow through `cap_result`.

- [ ] Red: tests for dispatch-to-bridge, missing-bridge error, cap applied
- [ ] Green: implement
- [ ] `make check`

### Task 2: AgentRuntime accepts a tools list (agent layer)

**Files:** modify `src/korvid/agent/runtime.py`; test `tests/agent/test_runtime.py`.

- `AgentRuntime(provider, executor, *, tools=None, …)` — defaults to `READ_TOOLS`; composition root passes `READ_TOOLS + UI_TOOLS`.
- `SYSTEM_PROMPT` extended: "You can drive the korvid TUI … prefer showing evidence on screen (navigate/open_logs/open_describe/set_filter) while you narrate."

- [ ] Red: test that provider.complete receives the injected tools list
- [ ] Green: implement
- [ ] `make check`

### Task 3: KorvidApp implements the bridge (ui layer)

**Files:** modify `src/korvid/ui/app.py`; test `tests/ui/test_agent_ui_drive.py`.

Methods (async, return `str`):
- `agent_navigate(view, namespace=None)` — validate view against `self.aliases`; reuse `on_navigate_command`; confirmation includes view/scope/row count.
- `agent_set_filter(pattern)` — reuse `on_filter_command`/`on_clear_filter`; sync FilterBar display.
- `agent_open_logs(pod, namespace, container=None)` — reuse `_open_log_pane`; requires stream_logs.
- `agent_open_describe(kind, name, namespace=None)` — fetch manifest via `self._get_manifest`, push `DescribeScreen`.
- Each action notifies with an `agent` marker (e.g. `notify("agent: switched view to pods")`, severity=information) and never steals focus from the agent input.
- Errors return `"ERROR: …"` strings — never raise (executor contract).

- [ ] Red: pilot tests — navigate switches table, filter applies, describe pushes screen, invalid view returns ERROR
- [ ] Green: implement
- [ ] `make check`

### Task 4: composition-root wiring (late-bound proxy)

**Files:** modify `src/korvid/__main__.py`; test `tests/test_main_wiring.py`.

- `_UIBridgeProxy` with `target: UIBridge | None`; each method forwards or returns `ERROR: UI not ready`.
- Both initial wiring and `rebuild_agent` pass `ToolExecutor(kube, aliases, ui=proxy)` and `tools=READ_TOOLS + UI_TOOLS`; `proxy.target = app` set after `KorvidApp(...)`.

- [ ] Red: wiring test — runtime built by `_build_agent_wiring` exposes UI tools and its executor reaches the proxy
- [ ] Green: implement
- [ ] `make check`

### Task 5: agent panel UX polish for drive mode

**Files:** modify `src/korvid/ui/widgets/agent_panel.py`; test `tests/ui/test_agent_panel.py`.

- UI tools render with a distinct marker (`🖥` instead of `🔧`) so users see screen actions vs cluster reads at a glance.

- [ ] Red / Green / `make check`

### Task 6: verify, PR, review loop

- [ ] Full `make check`
- [ ] Live smoke against AKS (agent prompt: "show me failing pods and open the logs of the worst one")
- [ ] PR + Copilot review loop until 0 unresolved; squash merge
