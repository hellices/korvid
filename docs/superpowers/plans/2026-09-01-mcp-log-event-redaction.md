# MCP Log and Event Producer Redaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redact Kubernetes log and event credentials at the tool producer boundary before MCP-visible size clamping.

**Architecture:** Reuse `korvid.tools.executor._projected` as the single free-text producer projection, extending it to preserve log container and event incarnation metadata. The log and event handlers pass their full rendered text through this helper; `execute_recorded` then applies the existing final cap to already-redacted text.

**Tech Stack:** Python 3.11+, asyncio, pytest, Ruff, mypy strict, Textual-free `tools/` layer

**Status:** Completed. The unchecked steps preserve the original TDD sequence;
all snippets reflect the final reviewed implementation and regressions.

## Global Constraints

- Redact full log/event text before any size clamp.
- Preserve all producer-side `RedactionRecord` entries in `ToolOutcome`.
- Preserve the resolved log container and event incarnation metadata.
- Convert `RedactionError` to `ToolResultBlocked` without including raw cluster text.
- Keep MCP server behavior transport-agnostic; it consumes the safe executor result unchanged.
- Cover representative password, token, and authorization assignments at the MCP boundary.
- Do not add credential heuristics, authentication, OAuth, stdio, or transport identity.
- Do not change log tail limits, event UID scoping, result caps, or unrelated
  error wording. Redaction refusals must use the constant safe message required
  by the fail-closed contract.
- Keep `tools/` free of Textual imports and new third-party dependencies.

---

### Task 1: Redact log and event producer results

**Files:**
- Modify: `src/korvid/tools/executor.py:462-502`
- Modify: `src/korvid/tools/executor.py:1181-1208`
- Modify: `src/korvid/tools/executor.py:1251-1282`
- Test: `tests/tools/test_executor_security.py`
- Test: `tests/mcp/test_server.py:264-298`

**Interfaces:**
- Consumes: `redact_text(text: str, path: str, records: list[RedactionRecord]) -> str`
- Produces: `_projected(text: str, path: str, *, error: bool = False, incarnation: str | None = None, container: str | None = None) -> ToolOutcome`
- Produces: redacted `get_logs` outcomes rooted at `logs`, with `container` preserved
- Produces: redacted `get_events` outcomes rooted at `events`, with `incarnation` preserved
- Produces: MCP responses containing the producer-redacted `ToolOutcome.text`

- [ ] **Step 1: Add failing executor projection tests**

In `tests/tools/test_executor_security.py`, import `RedactionError` beside
`RedactionRecord`, import `LogLine`, and add `FakeEventKube` and `FakeLogKube`
to the existing fake imports. Append:

```python
async def test_get_logs_redacts_full_text_and_preserves_container() -> None:
    class CredentialLogs(FakeLogKube):
        async def stream_logs(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            follow: bool = True,
            tail_lines: int = 200,
        ) -> Any:
            yield LogLine(
                pod=pod,
                container=container,
                text="password=log-password-sentinel",
            )
            yield LogLine(
                pod=pod,
                container=container,
                text="token=log-token-sentinel",
            )
            yield LogLine(
                pod=pod,
                container=container,
                text="Authorization: Bearer log-auth-sentinel",
            )

    outcome = await make_executor(CredentialLogs()).execute_recorded(
        "get_logs",
        {"pod": "web", "namespace": "default"},
    )

    assert "log-password-sentinel" not in outcome.text
    assert "log-token-sentinel" not in outcome.text
    assert "log-auth-sentinel" not in outcome.text
    assert outcome.text.count(MASK_PLACEHOLDER) == 3
    assert outcome.redactions == (
        RedactionRecord(path="logs", reason="authorization-value"),
        RedactionRecord(path="logs", reason="credential-assignment"),
        RedactionRecord(path="logs", reason="credential-assignment"),
    )
    assert outcome.container == "app"


async def test_get_events_redacts_text_and_preserves_incarnation() -> None:
    class CredentialEvents(FakeEventKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            self.event_calls.append(
                {"namespace": namespace, "name": name, "kind": kind, "uid": uid}
            )
            return [
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "count": 3,
                    "message": "token=event-token-sentinel",
                }
            ]

    outcome = await make_executor(CredentialEvents()).execute_recorded(
        "get_events",
        {"kind": "pods", "namespace": "default", "name": "web"},
    )

    assert outcome.text == f"Warning BackOff (3x): token={MASK_PLACEHOLDER}"
    assert outcome.redactions == (
        RedactionRecord(path="events", reason="credential-assignment"),
    )
    assert outcome.incarnation == "abc-123"
```

- [ ] **Step 2: Add failing clamp-order and fail-closed tests**

Append:

```python
async def test_get_logs_redacts_before_the_final_result_cap() -> None:
    visible_prefix = MAX_RESULT_CHARS - len(executor_module._TRUNCATION_SUFFIX)
    padding = visible_prefix - len(" to")

    class LongCredentialLogs(FakeLogKube):
        async def stream_logs(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            follow: bool = True,
            tail_lines: int = 200,
        ) -> Any:
            text = "x" * padding + " token=1234 trailing-diagnostics " + "y" * 100
            yield LogLine(pod=pod, container=container, text=text)

    outcome = await make_executor(LongCredentialLogs()).execute_recorded(
        "get_logs",
        {"pod": "web", "namespace": "default"},
    )

    assert len(outcome.text) == MAX_RESULT_CHARS
    assert outcome.text.endswith(executor_module._TRUNCATION_SUFFIX)
    assert "1234" not in outcome.text
    assert "trailing-diagnostics" not in outcome.text
    assert outcome.redactions == (
        RedactionRecord(path="logs", reason="credential-assignment"),
    )


async def test_log_redaction_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_text(
        text: str,
        path: str,
        records: list[RedactionRecord],
    ) -> str:
        raise RedactionError(f"unsafe text shape: {text}")

    monkeypatch.setattr(executor_module, "redact_text", reject_text)

    with pytest.raises(ToolResultBlocked, match="could not redact the result") as caught:
        await make_executor(FakeLogKube()).execute_recorded(
            "get_logs",
            {"pod": "web", "namespace": "default"},
        )

    assert "line-1" not in str(caught.value)


async def test_event_redaction_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_text(
        text: str,
        path: str,
        records: list[RedactionRecord],
    ) -> str:
        raise RedactionError(f"unsafe text shape: {text}")

    monkeypatch.setattr(executor_module, "redact_text", reject_text)

    with pytest.raises(ToolResultBlocked, match="could not redact the result") as caught:
        await make_executor(FakeEventKube()).execute_recorded(
            "get_events",
            {"kind": "pods", "namespace": "default", "name": "web"},
        )

    assert "restarting" not in str(caught.value)
```

- [ ] **Step 3: Replace the MCP leak characterization with failing safety tests**

Replace `test_mcp_text_results_carry_only_tool_shaping` in
`tests/mcp/test_server.py` with:

```python
async def test_mcp_log_results_are_redacted_by_the_producer() -> None:
    class LoggingKube:
        async def get_object(
            self,
            meta: Any,
            namespace: str | None,
            name: str,
        ) -> dict[str, Any]:
            return {"spec": {"containers": [{"name": "main"}]}}

        async def stream_logs(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            follow: bool,
            tail_lines: int,
        ) -> AsyncIterator[LogLine]:
            yield LogLine(
                pod=pod,
                container=container,
                text="password=mcp-password-sentinel",
            )
            yield LogLine(
                pod=pod,
                container=container,
                text="token=mcp-token-sentinel",
            )

    executor = ToolExecutor(LoggingKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_logs",
        {"pod": "api-0", "namespace": "prod"},
    )

    assert "mcp-password-sentinel" not in content[0].text
    assert "mcp-token-sentinel" not in content[0].text
    assert content[0].text.count(MASK_PLACEHOLDER) == 2


async def test_mcp_event_results_are_redacted_by_the_producer() -> None:
    class EventKube:
        async def get_object(
            self,
            meta: Any,
            namespace: str | None,
            name: str,
        ) -> dict[str, Any]:
            return {"kind": "Pod", "metadata": {"name": name, "uid": "pod-uid"}}

        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            return [
                {
                    "type": "Warning",
                    "reason": "Failed",
                    "count": 1,
                    "message": "Authorization: Bearer mcp-auth-sentinel",
                }
            ]

    executor = ToolExecutor(EventKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # read-only test double
    server = make_server(executor)

    content = await server.call_tool(
        "get_events",
        {"kind": "pods", "name": "api-0", "namespace": "prod"},
    )

    assert "mcp-auth-sentinel" not in content[0].text
    assert MASK_PLACEHOLDER in content[0].text
    assert "Warning Failed (1x)" in content[0].text
```

- [ ] **Step 4: Run all new tests and verify RED**

Run:

```bash
UV_FROZEN=1 uv run pytest \
  -p no:tach tests/tools/test_executor_security.py tests/mcp/test_server.py \
  -k 'get_logs_redacts_full_text or get_events_redacts_text or get_logs_redacts_before or log_redaction_failure or event_redaction_failure or mcp_log_results_are_redacted or mcp_event_results_are_redacted' -q
```

Expected: all seven tests fail because `_get_logs` and `_get_events` return raw
text without calling `redact_text`.

- [ ] **Step 5: Extend `_projected` to preserve producer metadata**

Change the helper signature and outcome construction to:

```python
def _projected(
    text: str,
    path: str,
    *,
    error: bool = False,
    incarnation: str | None = None,
    container: str | None = None,
) -> ToolOutcome:
    """A rendered free-text result, masked before it leaves its producer."""
    records: list[RedactionRecord] = []
    return ToolOutcome(
        text=redact_text(text, path, records),
        redactions=tuple(records),
        error=error,
        incarnation=incarnation,
        container=container,
    )
```

Retain the existing explanatory docstring content about producer-side
projection and full-length evidence; update only its opening sentence so it
applies to Kubernetes and external reads.

- [ ] **Step 6: Route logs and events through `_projected`**

Replace the `_get_logs` return with:

```python
        return _projected(
            "\n".join(lines),
            "logs",
            container=container or None,
        )
```

Replace the no-events return with:

```python
            return _projected("(no events)", "events", incarnation=uid)
```

Replace the final `_get_events` return with:

```python
        return _projected("\n".join(parts), "events", incarnation=uid)
```

- [ ] **Step 7: Run affected tests and static checks GREEN**

Run:

```bash
UV_FROZEN=1 uv run pytest \
  -p no:tach tests/tools/test_executor_core.py tests/tools/test_executor_security.py \
  tests/mcp/test_server.py tests/agent/test_outbound.py -q
UV_FROZEN=1 uv run ruff check \
  src/korvid/tools/executor.py tests/tools/test_executor_security.py \
  tests/mcp/test_server.py
UV_FROZEN=1 uv run ruff format --check \
  src/korvid/tools/executor.py tests/tools/test_executor_security.py \
  tests/mcp/test_server.py
UV_FROZEN=1 uv run mypy src/korvid/tools/executor.py
```

Expected: affected tests, Ruff, format check, and mypy pass.

- [ ] **Step 8: Commit producer and MCP behavior**

```bash
git add src/korvid/tools/executor.py tests/tools/test_executor_security.py \
  tests/mcp/test_server.py
git commit -m "fix: redact Kubernetes log and event results" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Verify issue #330 and prepare review

**Files:**
- Modify: none
- Test: repository-wide quality gates

**Interfaces:**
- Consumes: producer projection and MCP regressions from Task 1
- Produces: verified issue #330 branch ready for code review

- [ ] **Step 1: Run repository gates**

Run:

```bash
UV_FROZEN=1 make check
UV_FROZEN=1 uv run ruff format --check src tests
```

Expected: Ruff, mypy, pytest, Tach, and format checks pass. If the local
unsupported Python 3.14 deep-JSON test is nondeterministic again, document its
exact output, verify the affected suite independently, and rely on CI only
after supported Python 3.11–3.13 jobs pass.

- [ ] **Step 2: Verify branch scope and lockfile integrity**

Run:

```bash
git status --short
git diff --check origin/main...HEAD
test "$(git hash-object uv.lock)" = "$(git rev-parse HEAD:uv.lock)"
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
```

Expected: clean worktree, unchanged lockfile, and changes limited to issue #330
design, plan, executor implementation, and tests.

- [ ] **Step 3: Run task and whole-branch reviews**

Use the repository review workflow:

1. Review Task 1 for spec compliance and code quality.
2. Fix every Critical or Important finding with a failing regression first.
3. Run a final whole-branch review.
4. Create a PR only when explicitly instructed; never merge it automatically.
