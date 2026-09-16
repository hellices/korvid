# Client-owned Test Endpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate Windows Proactor socket leaks from every real local HTTP/TLS test endpoint while preserving tests that intentionally simulate a remote disconnect.

**Architecture:** A shared test-only endpoint helper owns listeners, accepted sockets, handler threads, transport-drain barriers, and teardown ordering. Served endpoints use HTTP/1.1 and remain open until the client closes. Intentional-disconnect endpoints half-close their write side so the client observes EOF, but retain the peer socket until the client closes, avoiding the Proactor `shutdown()`-after-peer-close failure.

**Tech Stack:** Python 3.11+, `asyncio`, `http.server`, `socketserver`, `ssl`, pytest.

## Global Constraints

- Work in `.worktrees/issue-390-windows-socket-cleanup` on `fix/390-windows-socket-cleanup`.
- Test-fixture code only; production behavior must not change.
- Do not filter warnings, call `gc.collect()`, retry failed tests, sleep for correctness, increase existing UI waits, skip Windows, or switch Windows away from Proactor.
- Never wrap a listening socket with TLS. Wrap each accepted socket and explicitly own handshake failure.
- Every endpoint shutdown, thread join, connection wait, and raw socket read is event-driven and bounded.
- A test body or release callback error remains the primary exception; endpoint cleanup must still close every listener and accepted socket.
- Intentional disconnects keep their observable EOF/retry semantics.
- Use strict TDD and commit each task separately with the required Copilot co-author trailer.
- Do not modify dependencies or `uv.lock`; use `UV_FROZEN=1` / `uv run --frozen`.
- Do not merge. Update the existing draft PR only after all tasks and reviews pass.

---

### Task 1: Build the shared endpoint lifecycle

**Files:**
- Create: `tests/local_endpoint.py`
- Create: `tests/test_local_endpoint.py`

**Interfaces:**
- Produces: `LocalEndpoint(url: str, port: int)` with `path(suffix: str) -> str`.
- Produces: `KeepAliveHandler(BaseHTTPRequestHandler)` with HTTP/1.1 and literal logging suppression.
- Produces: `served_endpoint(handler, *, tls=None, release_clients=None, settle_seconds=5.0)`.
- Produces: `disconnecting_endpoint(handler, *, reason, tls=None, settle_seconds=5.0)`.
- Produces: `drain_transport_closures()`.

- [ ] **Step 1: Write RED served-endpoint lifecycle tests**

Add tests that prove:

```python
async def test_served_endpoint_leaves_the_connection_open_for_the_client() -> None:
    async with served_endpoint(JsonHandler) as endpoint:
        client = await asyncio.to_thread(open_keepalive_request, endpoint.port)
        try:
            assert await asyncio.to_thread(peer_state, client) == "open"
        finally:
            client.close()


async def test_release_failure_still_closes_listener_and_connections() -> None:
    async def fail_release() -> None:
        raise RuntimeError("release failed")

    with pytest.raises(RuntimeError, match="release failed"):
        async with served_endpoint(JsonHandler, release_clients=fail_release) as endpoint:
            client = await asyncio.to_thread(open_keepalive_request, endpoint.port)
    assert client.fileno() == -1
```

Also test TLS refused-handshake client ownership, off-loop `shutdown()`, HTTP/1.0 handler rejection, and no leaked server/handler/reaper thread.

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest -p no:tach tests/test_local_endpoint.py -q
```

Expected: collection fails because `tests.local_endpoint` does not exist.

- [ ] **Step 3: Implement served mode**

Implement a `ThreadingHTTPServer` subclass that:

- uses `daemon_threads = True`;
- tracks accepted sockets under a `threading.Condition`;
- wraps accepted sockets per connection for TLS 1.2+;
- on failed TLS handshake, hands the accepted socket to a bounded client-EOF holder;
- deregisters sockets from `close_request`;
- can force-close remaining sockets during exceptional teardown.

`served_endpoint` validates `handler.protocol_version == "HTTP/1.1"`, starts the accept thread, then tears down in this order:

1. await `release_clients`;
2. await `drain_transport_closures`;
3. wait for tracked connections to empty;
4. force-close any remainder;
5. run `shutdown`, bounded thread join, and `server_close` in nested `finally` blocks;
6. on a clean body/release path, fail with peer details if ownership did not settle.

- [ ] **Step 4: Implement intentional-disconnect mode**

Override `shutdown_request` for `disconnecting_endpoint`:

```python
request.shutdown(socket.SHUT_WR)
start_holder(request)  # read until client EOF, then close/deregister
```

This preserves client-visible EOF/short-response behavior while keeping the peer alive until the client releases its transport. Require a non-empty `reason` argument and expose it in assertion diagnostics.

- [ ] **Step 5: Verify GREEN and static checks**

```bash
uv run --frozen pytest -p no:tach tests/test_local_endpoint.py -q
uv run --frozen ruff check tests/local_endpoint.py tests/test_local_endpoint.py
uv run --frozen ruff format --check tests/local_endpoint.py tests/test_local_endpoint.py
uv run --frozen mypy tests/local_endpoint.py tests/test_local_endpoint.py
```

- [ ] **Step 6: Commit**

```bash
git add tests/local_endpoint.py tests/test_local_endpoint.py
git commit -m "test: centralize client-owned local endpoints (#390)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Prevent endpoint lifecycle drift

**Files:**
- Create: `tests/test_local_endpoint_conventions.py`

**Interfaces:**
- Consumes: `tests/local_endpoint.py`.
- Produces: repository contract forbidding new direct HTTP server construction and listening-socket TLS wrapping outside the helper.

- [ ] **Step 1: Write RED AST contract**

Walk tracked `tests/**/*.py` and report:

- calls constructing `HTTPServer` or `ThreadingHTTPServer`;
- assignments calling `wrap_socket(server.socket, ...)`;
- direct `serve_forever` threads.

Allow only `tests/local_endpoint.py`; exclude fixture source snippets embedded inside the contract's own test strings by parsing dedicated temporary modules rather than scanning itself as text.

- [ ] **Step 2: Run RED**

```bash
uv run --frozen pytest -p no:tach tests/test_local_endpoint_conventions.py -q
```

Expected: failures name the six existing hand-written endpoint modules.

- [ ] **Step 3: Keep the contract RED until Tasks 3–5 migrate every endpoint**

Do not weaken the contract with an allowlist. Commit the contract together with Task 1 only after the migration branch can keep commits buildable, or stage it for the final migration commit.

---

### Task 3: Migrate LiteLLM endpoints

**Files:**
- Modify: `tests/providers/test_litellm_ca_bundle.py`
- Modify: `tests/providers/test_litellm_synthetic_controls.py`
- Modify: `tests/providers/litellm_clients.py`
- Test: `tests/test_local_endpoint.py`
- Test: both provider modules above

**Interfaces:**
- CA bundle consumes `served_endpoint(..., tls=(cert, key), release_clients=drop_cached_clients)`.
- Synthetic controls consume `served_endpoint(..., release_clients=drop_cached_clients)`.
- `drop_cached_clients()` consumes shared `drain_transport_closures()`.

- [ ] **Step 1: Port the CA-bundle behavior tests to the shared helper**

Keep the direct served/rejected ordering tests, ownership probe, cache isolation, TLS 1.2 regression, and unexpected-handshake cleanup. Delete duplicated server/reaper/teardown classes only after equivalent Task 1 tests are green.

- [ ] **Step 2: Port synthetic controls**

Make `_Chat` inherit `KeepAliveHandler`; change `_chat_endpoint` call sites to `async with served_endpoint(...)`. Preserve `_unreachable_endpoint`.

- [ ] **Step 3: Verify**

```bash
uv run --frozen pytest -p no:tach \
  tests/providers/test_litellm_ca_bundle.py \
  tests/providers/test_litellm_synthetic_controls.py \
  tests/providers/test_litellm_clients.py -q
uv run --frozen pytest -p no:tach tests/providers -q
```

- [ ] **Step 4: Commit**

```bash
git add tests/local_endpoint.py tests/test_local_endpoint.py \
  tests/providers/test_litellm_ca_bundle.py \
  tests/providers/test_litellm_synthetic_controls.py \
  tests/providers/litellm_clients.py
git commit -m "test: share client-owned LiteLLM endpoints (#390)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Migrate private-CA HTTP clients

**Files:**
- Modify: `tests/providers/test_net.py`
- Modify: `tests/providers/test_models_dev.py`
- Modify: `tests/providers/test_endpoint_discovery.py`

- [ ] **Step 1: Convert handlers to `KeepAliveHandler`**

Every successful response must have `Content-Length`; retain existing content type, etag, request capture, and assertions.

- [ ] **Step 2: Replace synchronous endpoint context managers**

Use:

```python
async with served_endpoint(Handler, tls=(cert_pem, key_pem)) as endpoint:
    url = endpoint.path("/api.json")
```

The existing async clients close before endpoint context exit.

- [ ] **Step 3: Verify**

```bash
uv run --frozen pytest -p no:tach \
  tests/providers/test_net.py \
  tests/providers/test_models_dev.py \
  tests/providers/test_endpoint_discovery.py -q
```

- [ ] **Step 4: Commit**

```bash
git add tests/providers/test_net.py tests/providers/test_models_dev.py \
  tests/providers/test_endpoint_discovery.py
git commit -m "test: share client-owned private-CA endpoints (#390)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Preserve intentional transport disconnects

**Files:**
- Modify: `tests/k8s/test_pulse_transport.py`
- Add/complete: `tests/test_local_endpoint_conventions.py`
- Modify: `AGENTS.md`

- [ ] **Step 1: Add disconnect-mode RED tests**

In `tests/test_local_endpoint.py`, prove that a handler returning no response or `Connection: close` causes the client to observe EOF, while the server retains the peer until client teardown and leaves no tracked socket/thread.

- [ ] **Step 2: Migrate Pulse**

Replace `serve_http` internals with `disconnecting_endpoint` and an explicit reason:

```python
async with disconnecting_endpoint(
    Handler,
    reason="the SDK disconnect/retry budget is the subject",
) as endpoint:
    yield endpoint.url
```

Keep every response byte, `close_connection`, retry/redirect assertion, and client context order unchanged.

- [ ] **Step 3: Make the convention contract GREEN**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/test_local_endpoint.py \
  tests/test_local_endpoint_conventions.py \
  tests/k8s/test_pulse_transport.py -q
```

- [ ] **Step 4: Document the test invariant**

Add one Testing Gotcha to `AGENTS.md`: local HTTP/TLS servers use `tests/local_endpoint.py`; served endpoints are client-owned; intentional disconnects require `disconnecting_endpoint(reason=...)`; never TLS-wrap a listening socket.

- [ ] **Step 5: Commit**

```bash
git add tests/local_endpoint.py tests/test_local_endpoint.py \
  tests/test_local_endpoint_conventions.py tests/k8s/test_pulse_transport.py \
  AGENTS.md
git commit -m "test: make intentional disconnect ownership explicit (#390)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Verify and update draft PR

**Files:**
- Modify only files required by failures caused by Tasks 1–5.

- [ ] **Step 1: Run focused endpoint and provider suites**

```bash
uv run --frozen pytest -p no:tach \
  tests/test_local_endpoint.py \
  tests/test_local_endpoint_conventions.py \
  tests/providers \
  tests/k8s/test_pulse_transport.py -q
```

- [ ] **Step 2: Run complete frozen gate**

```bash
UV_FROZEN=1 make check
```

Expected: all tests, strict mypy, Ruff, tach, source-size, deptry, and lock-host checks pass.

- [ ] **Step 3: Request full-range review**

Review from `7ec049b7` to final head, emphasizing client-owned vs intentional-disconnect semantics and test-helper isolation.

- [ ] **Step 4: Update draft #398**

Push only after review passes. Keep draft until exact-head Windows, CodeQL, Linux matrix, and all required checks succeed. Do not rerun a failed Windows job merely to obtain green.
