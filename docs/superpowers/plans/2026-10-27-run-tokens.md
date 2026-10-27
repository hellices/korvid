# Run Tokens and Late Results (#409 S4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a diagnostic controller apply a read's result only while the run it started under is still current, and drop it after a context switch, a credential replacement, a newer run or a selection change (#488, slice S4 of #409).

**Architecture:** A new pure module `core/run_tokens.py` holds `RunTracker`. A controller gives it three read-only sources: the context epoch, whether a context switch is under way, and the client's session generation. `RunTracker` keeps the controller's own run generation. `next_run()` starts a run and returns its `RunToken`. `apply_if_current()` runs a result's callback only while the token still matches.

**Tech Stack:** Python 3.11+ standard library, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`, section "Run tokens and late results". Where this plan departs from it, the section below says why.
- **Token:** a `RunToken` is `(epoch, session generation, run generation)`. The epoch comes from the context-switch coordinator, the session generation from `KubeClient`, and the run generation from each diagnostic controller.
- **Run generation:** incremented when the controller reruns, or when the user changes the selection it explains.
- **Rule:** a result is applied only while its token still matches the current one; otherwise it is dropped, as write flows do today.
- **Layers:** `core` imports no Textual and no optional extra; `core` may import `k8s`.
- **Code rules:** no `assert` in `src/`; every `pytest.raises` takes `match=`; maximum complexity 10; Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **A switch in progress also makes a token stale.** The epoch changes only once a switch is applied. The workspace flows' `ContextGuard.crossed()` already treats a switch that is tearing down or retargeting as crossed, and `RunTracker` does the same.
- **A result with no token is never applied.** `Observation.token` is optional, so a read that recorded none cannot be checked, and is dropped.
- **No controller uses it yet.** The first consumer wires `ContextGuard.epoch`, `ContextGuard.switching` and `KubeClient.session_generation` into it. The tests use those sources' contracts, and one uses a real `KubeClient`'s session generation.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/core/run_tokens.py` | create | `RunTracker`: the run generation, the current token, and the late-result check |
| `tests/core/test_run_tokens.py` | create | runs, staleness for each cause, and applying or dropping a result |
| `tests/test_optional_extras.py` | modify | the new module imports without extras |

---

### Task 1: `RunTracker`

**Files:**
- Create: `src/korvid/core/run_tokens.py`
- Create: `tests/core/test_run_tokens.py`
- Modify: `tests/test_optional_extras.py`

**Interfaces:**
- Consumes: `korvid.core.capability.RunToken(epoch, session_generation, run_generation)` from S1, and `KubeClient.session_generation` from S2.
- Produces in `korvid.core.run_tokens`: `RunTracker(*, epoch: Callable[[], int], session_generation: Callable[[], int], switching: Callable[[], bool] = ...)` with `next_run() -> RunToken`, `is_current(token: RunToken | None) -> bool` and `apply_if_current(token: RunToken | None, apply: Callable[[], object]) -> bool`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_run_tokens.py`:

```python
"""Late diagnostic results are dropped by their run token (#409, slice S4)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from kubernetes_asyncio import client as k8s_client

from korvid.core.capability import RunToken
from korvid.core.run_tokens import RunTracker
from korvid.k8s import client as client_module
from korvid.k8s.client import KubeClient


class Cluster:
    """The context and session a controller reads through, changed by the tests."""

    def __init__(self) -> None:
        self.epoch = 3
        self.session_generation = 7
        self.switching = False

    def tracker(self) -> RunTracker:
        return RunTracker(
            epoch=lambda: self.epoch,
            session_generation=lambda: self.session_generation,
            switching=lambda: self.switching,
        )


def test_a_run_s_token_is_the_context_session_and_run_it_started_under() -> None:
    cluster = Cluster()
    runs = cluster.tracker()

    assert runs.next_run() == RunToken(3, 7, 1)
    assert runs.next_run() == RunToken(3, 7, 2)


def test_a_result_of_the_current_run_is_applied() -> None:
    runs = Cluster().tracker()
    token = runs.next_run()
    applied: list[str] = []

    assert runs.apply_if_current(token, lambda: applied.append("result"))
    assert applied == ["result"]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda cluster, runs: setattr(cluster, "epoch", 4), id="context-switch"),
        pytest.param(
            lambda cluster, runs: setattr(cluster, "switching", True), id="switch-under-way"
        ),
        pytest.param(
            lambda cluster, runs: setattr(cluster, "session_generation", 8),
            id="credential-replacement",
        ),
        pytest.param(lambda cluster, runs: runs.next_run(), id="newer-run-or-selection"),
    ],
)
def test_a_result_started_before_a_change_is_dropped(change: Any) -> None:
    cluster = Cluster()
    runs = cluster.tracker()
    token = runs.next_run()
    applied: list[str] = []

    change(cluster, runs)

    assert not runs.is_current(token)
    assert not runs.apply_if_current(token, lambda: applied.append("late"))
    assert applied == []


def test_a_result_with_no_token_is_never_applied() -> None:
    runs = Cluster().tracker()
    runs.next_run()
    applied: list[str] = []

    assert not runs.apply_if_current(None, lambda: applied.append("untracked"))
    assert applied == []


def test_no_result_is_current_before_a_run_starts() -> None:
    runs = Cluster().tracker()

    assert not runs.is_current(RunToken(3, 7, 0))


@pytest.fixture
def offline_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    class Api:
        def __init__(self, configuration: Any) -> None:
            self.configuration = configuration

        async def close(self) -> None:
            return None

    monkeypatch.setattr(client_module, "load_refreshable_kube_config", AsyncMock())
    monkeypatch.setattr(k8s_client, "ApiClient", Api)
    monkeypatch.setattr(k8s_client, "CoreV1Api", lambda api: api)
    monkeypatch.setattr(k8s_client.Configuration, "set_default", lambda configuration: None)


@pytest.mark.usefixtures("offline_kubeconfig")
async def test_reloading_the_kubeconfig_drops_a_result_read_before_it() -> None:
    kube = KubeClient()
    await kube.connect("ctx-a")
    runs = RunTracker(epoch=lambda: 0, session_generation=lambda: kube.session_generation)
    token = runs.next_run()

    await kube.switch_context(None)

    assert not runs.apply_if_current(token, lambda: None)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/core/test_run_tokens.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'korvid.core.run_tokens'`.

- [ ] **Step 3: Write `RunTracker`**

Create `src/korvid/core/run_tokens.py`:

```python
"""Drop a diagnostic's late results by the run token they were read under (#409, S4).

A diagnostic reads in the background, and by the time a read finishes the
user may have switched context, korvid may have reconnected with other
credentials, the diagnostic may have rerun, or the user may have selected
something else. Each read records the `RunToken` it started under, and the
controller applies its result only while that token is still the current
one. There is no cache of read results to invalidate.
"""

from __future__ import annotations

from collections.abc import Callable

from korvid.core.capability import RunToken


def _never() -> bool:
    return False


class RunTracker:
    """One diagnostic controller's runs, and which of their results may be applied.

    The controller supplies three read-only sources: the context-switch
    coordinator's epoch, whether a switch is under way, and `KubeClient`'s
    session generation. It calls `next_run()` when it reruns, and when the
    user changes the selection it explains.
    """

    def __init__(
        self,
        *,
        epoch: Callable[[], int],
        session_generation: Callable[[], int],
        switching: Callable[[], bool] = _never,
    ) -> None:
        self._epoch = epoch
        self._session_generation = session_generation
        self._switching = switching
        self._run_generation = 0

    def next_run(self) -> RunToken:
        """Start a run, making every earlier run's results stale; its token."""
        self._run_generation += 1
        return self._current()

    def is_current(self, token: RunToken | None) -> bool:
        """Whether a result read under `token` may still be applied.

        It may not once the context, the session or the run has changed, nor
        while a context switch is under way, nor without a token at all.
        """
        if token is None or self._run_generation == 0 or self._switching():
            return False
        return token == self._current()

    def apply_if_current(self, token: RunToken | None, apply: Callable[[], object]) -> bool:
        """Call `apply` only if `token` is current; whether it was called."""
        if not self.is_current(token):
            return False
        apply()
        return True

    def _current(self) -> RunToken:
        return RunToken(self._epoch(), self._session_generation(), self._run_generation)
```

In `tests/test_optional_extras.py`, add `"korvid.core.run_tokens"` after `"korvid.k8s.evidence_requests"` in the parametrize list of `test_base_import_does_not_require_optional_extras`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_run_tokens.py "tests/test_optional_extras.py::test_base_import_does_not_require_optional_extras" -q`
Expected: all pass.

Run: `uv run --frozen ruff check src/korvid/core tests/core/test_run_tokens.py && uv run --frozen mypy src/korvid/core tests/core/test_run_tokens.py && uv run --frozen tach check`
Expected: no findings, and `All modules validated!`.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/run_tokens.py tests/core/test_run_tokens.py tests/test_optional_extras.py
git commit -m "core: drop late diagnostic results by run token"
```

### Task 2: Run the full gate

- [ ] **Step 1: Run every check that `make check` runs**

```bash
uv run --frozen python scripts/check_source_size.py
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy
uv run --frozen tach check
uv run --frozen pytest -x -q
```

Expected: every command exits 0.

- [ ] **Step 2: Check #488's acceptance criteria against tests**

| Criterion | Tests |
| --- | --- |
| Dropped after a context switch | `test_a_result_started_before_a_change_is_dropped[context-switch]` and `[switch-under-way]` |
| Dropped after a credential replacement | `test_a_result_started_before_a_change_is_dropped[credential-replacement]`, `test_reloading_the_kubeconfig_drops_a_result_read_before_it` |
| Dropped after a newer run or a selection change | `test_a_result_started_before_a_change_is_dropped[newer-run-or-selection]` |
| Applied otherwise | `test_a_result_of_the_current_run_is_applied` |
