# Final Review Fixes — phase1-foundation

## Fix 1: Lock LLMProvider override shape with concrete test subclass

**Files changed:** `tests/agent/test_provider.py`

**What changed:** Added `_ConcreteProvider` — a minimal concrete subclass of `LLMProvider` with:
- `name` property returning `"test-provider"`
- `async def complete(...)` implemented as an async generator yielding one `{"type": "text", ...}` event

Added `test_concrete_provider_yields_event` that instantiates `_ConcreteProvider` and consumes one item via `async for`.

**Why:** The base class declared `complete` as a sync method returning `AsyncIterator`. Without a concrete subclass, mypy --strict never validated the override seam. Now mypy checks that the async-generator override satisfies the abstract signature.

**Test adaptations:** None — new test only.

---

## Fix 2: Clear stale namespace bucket when a watch (re)starts

**Files changed:**
- `src/korvid/core/store.py` — added `ResourceStore.clear(kind, namespace)`
- `src/korvid/core/watch.py` — call `self._store.clear(kind, namespace)` in `WatchManager.start()` before creating the asyncio task
- `tests/core/test_store.py` — added 3 new tests
- `tests/core/test_watch.py` — added 1 new test

**What changed (store.py):** New synchronous method `clear` removes the bucket for `(kind, namespace)` and notifies all subscribers (same error-isolation pattern as `apply_event`).

**What changed (watch.py):** In `start()`, after the early-return guard (`if key in self._tasks`), `clear` is called synchronously before `asyncio.create_task`. This means: every fresh watch starts from an empty bucket, so pods that disappeared between watch sessions are never left as phantoms. The clear does NOT happen on internal reconnects inside `_run()` — only on an externally initiated `start()`.

**New store tests:**
- `test_clear_empties_bucket`: adds two pods, clears, asserts `get` returns `[]`
- `test_clear_notifies_subscribers`: asserts subscriber receives `kind` on clear
- `test_clear_other_namespace_unaffected`: clearing an absent key does not disturb other buckets

**New watch test:**
- `test_start_clears_stale_store_data`: seeds store with `"stale"` pod, calls `start()`, asserts store is empty synchronously (before async task runs) and contains only `"fresh"` after the task processes its event.

**Test adaptations:** No existing tests conflicted. `test_start_is_idempotent` still passes because the second `start()` returns early before reaching `clear()`. `test_start_feeds_store` still passes because the cleared-then-refilled data equals the expected result. `test_stream_end_reconnects` still passes because `clear` is only called from `start()`, not from the internal reconnect loop.

---

## Commands & Output Summary

```
make check
  ruff check src/ tests/         → All checks passed!
  mypy --strict (38 source files) → Success: no issues found
  pytest -x -q (43 tests)        → 43 passed in 4.22s
  tach check                     → ✅ All modules validated!

uv run pytest tests/agent/ tests/core/ tests/ui/ -v  (run ×2)
  → 39 passed (both runs, no flakiness)
```
