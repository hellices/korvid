# Write Approval Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract a pure, typed `ApprovalDecision` and a pure fail-closed
intent-audit → mutate → outcome-audit orchestration function into
`src/korvid/tools/`, wire both into the one real production write path
(`WriteCoordinator.run` and `agent_ui_controller.py`'s agent-write approval
step) with zero observable behavior change, and add composition tests
proving the extraction is load-bearing.

**Architecture:** See
`docs/superpowers/specs/2026-08-27-write-approval-port-design.md` for full
rationale. In short: `src/korvid/tools/approval.py` gets a frozen
`ApprovalOutcome`/`ApprovalDecision` plus a fail-closed
`require_tui_keystroke_source` gate; `src/korvid/tools/write_coordinator.py`
gets the moved pure formatting helpers (`WRITE_VERBS`, `gvr_label`,
`write_locus`, `perm_target`) plus a new `run_approved_write` function that
is a byte-identical extraction of `WriteCoordinator._run_write_inner`'s
body, parameterized by two structural `Protocol`s (`AuditRecorder`,
`Notifier`) instead of `self`. `src/korvid/ui/write_coordinator.py`
re-exports the moved names and delegates its reserved-write body to
`run_approved_write`. `src/korvid/ui/agent_ui_controller.py`'s
`_await_user_approval` builds a real `ApprovalDecision` from the
`ConfirmScreen` callback (tagging every real key-driven resolution
`tui_keystroke`, and every timeout `timeout`), passes it through
`require_tui_keystroke_source`, then collapses to the same
`Literal["approved", "declined", "expired"]` string external callers
already depend on.

**Tech Stack:** Python 3.11+, pytest + pytest-asyncio, mypy (strict),
ruff, tach, existing `textual`-based Pilot tests for the UI layer.

## Global Constraints

- No `tach.toml` changes — `korvid.tools` already depends on `korvid.k8s`;
  `korvid.ui` already depends on `korvid.tools`.
- No behavior change to any existing test. `tests/ui/test_write_coordinator.py`
  (89 tests), `tests/ui/test_agent_write.py` (61+ tests),
  `tests/ui/test_agent_ui_controller.py`, `tests/ui/test_approval_timeout.py`,
  and `tests/ui/test_protected_contexts.py` must all pass **unmodified**.
- No audit-log schema change — `AuditLog.append`'s field set is untouched;
  `decision_source` stays purely in-memory on `ApprovalDecision`.
- No new dependency for `korvid.evals`/`korvid.core`/`korvid.ui` beyond what
  `tach.toml` already allows.
- `ApprovalOutcome`/`ApprovalDecision` are frozen (`Enum` + frozen
  `slots=True` dataclass) — no mutation after construction.
- `require_tui_keystroke_source` only ever rejects an `APPROVE` outcome from
  a non-`tui_keystroke` source; decline/dismiss/expire are never rejected
  regardless of source.
- Every new module/test file needs a module docstring in the style already
  used across the repo (see `src/korvid/ui/write_coordinator.py`'s header
  for the house style: a short paragraph on the "why", not just the "what").
- Run `ruff check .`, `ruff format --check .`, `mypy` (project config),
  `tach check`, and `pre-commit run --files <touched files>` after every
  task; run the full `pytest -q` suite once at the end (task 8).

---

### Task 1: `ApprovalOutcome` / `ApprovalDecision` / fail-closed source gate

**Files:**
- Create: `src/korvid/tools/approval.py`
- Test: `tests/tools/test_approval.py`
- Create (if absent): `tests/tools/__init__.py`

**Interfaces:**
- Produces: `ApprovalOutcome` (str `Enum`: `APPROVE`, `DECLINE`, `DISMISS`,
  `EXPIRE`, values `"approve"`/`"decline"`/`"dismiss"`/`"expire"`);
  `ApprovalDecision` (frozen `slots=True` dataclass: `outcome:
  ApprovalOutcome`, `decision_source: str`) with classmethods
  `ApprovalDecision.approved(decision_source: str) -> ApprovalDecision`,
  `.declined(decision_source: str) -> ApprovalDecision`,
  `.dismissed(decision_source: str) -> ApprovalDecision`,
  `.expired(decision_source: str) -> ApprovalDecision`;
  `TUI_KEYSTROKE_SOURCE: Final[str] = "tui_keystroke"`;
  `RejectedApprovalSourceError(RuntimeError)`;
  `require_tui_keystroke_source(decision: ApprovalDecision) ->
  ApprovalDecision` (returns `decision` unchanged, or raises
  `RejectedApprovalSourceError`).

- [ ] **Step 1: Check for `tests/tools/__init__.py`**

Run: `ls tests/tools/__init__.py 2>&1 || echo MISSING`

If `MISSING`, check how a sibling test package does it (e.g.
`tests/ui/__init__.py`) and create an equally empty
`tests/tools/__init__.py` (0 bytes, matching the existing convention — most
repos here use plain empty `__init__.py` files for test packages; confirm
by running `cat tests/ui/__init__.py` first and mirroring it exactly).

- [ ] **Step 2: Write the failing tests**

Create `tests/tools/test_approval.py`:

```python
"""Pure unit tests for the typed write-approval decision (issue TBD).

`ApprovalDecision` is the shared, UI-independent vocabulary a write
approval resolves to: `approve`/`decline`/`dismiss`/`expire`, plus the
`decision_source` that says how the decision was obtained. The production
Textual app is the only source ever allowed to produce `tui_keystroke`,
and `require_tui_keystroke_source` is the fail-closed gate that enforces
it - checked here without importing `textual` or `korvid.ui` at all.
"""

from __future__ import annotations

import pytest

from korvid.tools.approval import (
    TUI_KEYSTROKE_SOURCE,
    ApprovalDecision,
    ApprovalOutcome,
    RejectedApprovalSourceError,
    require_tui_keystroke_source,
)


def test_outcome_values_are_stable_strings() -> None:
    assert ApprovalOutcome.APPROVE.value == "approve"
    assert ApprovalOutcome.DECLINE.value == "decline"
    assert ApprovalOutcome.DISMISS.value == "dismiss"
    assert ApprovalOutcome.EXPIRE.value == "expire"


def test_decision_is_frozen() -> None:
    decision = ApprovalDecision.approved(TUI_KEYSTROKE_SOURCE)
    with pytest.raises(AttributeError):
        decision.outcome = ApprovalOutcome.DECLINE  # type: ignore[misc]


@pytest.mark.parametrize(
    ("constructor", "outcome"),
    [
        (ApprovalDecision.approved, ApprovalOutcome.APPROVE),
        (ApprovalDecision.declined, ApprovalOutcome.DECLINE),
        (ApprovalDecision.dismissed, ApprovalOutcome.DISMISS),
        (ApprovalDecision.expired, ApprovalOutcome.EXPIRE),
    ],
)
def test_each_constructor_builds_its_outcome(constructor, outcome) -> None:
    decision = constructor("some-source")
    assert decision.outcome is outcome
    assert decision.decision_source == "some-source"


def test_approval_from_the_allowed_source_passes_the_gate() -> None:
    decision = ApprovalDecision.approved(TUI_KEYSTROKE_SOURCE)
    assert require_tui_keystroke_source(decision) is decision


def test_approval_from_any_other_source_is_rejected() -> None:
    decision = ApprovalDecision.approved("mcp")
    with pytest.raises(RejectedApprovalSourceError):
        require_tui_keystroke_source(decision)


@pytest.mark.parametrize(
    "constructor",
    [ApprovalDecision.declined, ApprovalDecision.dismissed, ApprovalDecision.expired],
)
def test_non_approving_outcomes_are_never_rejected_by_source(constructor) -> None:
    decision = constructor("anything-at-all")
    assert require_tui_keystroke_source(decision) is decision
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_approval.py -v`
Expected: collection error / `ModuleNotFoundError: No module named
'korvid.tools.approval'`.

- [ ] **Step 4: Write the implementation**

Create `src/korvid/tools/approval.py`:

```python
"""The typed write-approval decision (issue TBD).

Every cluster mutation korvid performs needs one thing before it may run: a
decision that says whether the user approved it, and - just as
importantly - *how* that decision was obtained. Before this module, an
approval was a bare `bool | None` that collapsed to a two-value string the
instant it left the dialog callback (`agent_ui_controller.py`); nothing
recorded provenance, so nothing could ever assert that an approval reaching
a mutation actually came from a real dialog rather than some other caller.

`ApprovalDecision` is that provenance-carrying value: an `ApprovalOutcome`
(approve/decline/dismiss/expire) plus a `decision_source` string.
`require_tui_keystroke_source` is the fail-closed gate every production
approval passes through before it may authorize a write: only a
`decision_source` of `TUI_KEYSTROKE_SOURCE` may ever carry an `APPROVE`
outcome forward, since that is the only source the running Textual app
(`ConfirmScreen`, gated by its own `FreshKeysInput` key-freshness check) is
ever allowed to produce. Decline, dismiss, and expire carry no mutating
authority, so their source is informational only and is never rejected
here.

This module holds no reference to Textual, `korvid.ui`, or any concrete
approval surface - a decision is built by whichever caller resolved the
approval (today, only the Textual app) and handed to
`require_tui_keystroke_source` and, from there, to
`korvid.tools.write_coordinator.run_approved_write`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class ApprovalOutcome(str, Enum):
    """How a write-approval request was resolved."""

    APPROVE = "approve"
    DECLINE = "decline"
    DISMISS = "dismiss"
    EXPIRE = "expire"


#: The only `decision_source` production code may ever present alongside an
#: `APPROVE` outcome: a real key event resolved by `ConfirmScreen` after it
#: was shown. `ConfirmScreen`'s own `FreshKeysInput` already discards any
#: key event timestamped before the dialog existed, so every decision this
#: source names is guaranteed to be a genuine, post-dialog user keystroke.
TUI_KEYSTROKE_SOURCE: Final[str] = "tui_keystroke"


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """A resolved write-approval request: what happened, and how."""

    outcome: ApprovalOutcome
    decision_source: str

    @classmethod
    def approved(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.APPROVE, decision_source)

    @classmethod
    def declined(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.DECLINE, decision_source)

    @classmethod
    def dismissed(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.DISMISS, decision_source)

    @classmethod
    def expired(cls, decision_source: str) -> ApprovalDecision:
        return cls(ApprovalOutcome.EXPIRE, decision_source)


class RejectedApprovalSourceError(RuntimeError):
    """An `APPROVE` decision arrived from a source production must refuse."""


def require_tui_keystroke_source(decision: ApprovalDecision) -> ApprovalDecision:
    """Fail-closed gate: an `APPROVE` decision must be `tui_keystroke`-sourced.

    Only `APPROVE` is gated - it is the only outcome that can authorize a
    mutation. A decline, dismiss, or expire changes nothing on the cluster
    no matter where it came from, so gating them would add no safety.
    """
    if decision.outcome is ApprovalOutcome.APPROVE and decision.decision_source != (
        TUI_KEYSTROKE_SOURCE
    ):
        raise RejectedApprovalSourceError(
            f"approval source {decision.decision_source!r} is not allowed to "
            "authorize a write"
        )
    return decision
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_approval.py -v`
Expected: all pass.

- [ ] **Step 6: Static checks on the new files**

Run:
```
UV_FROZEN=1 uv run ruff check src/korvid/tools/approval.py tests/tools/test_approval.py
UV_FROZEN=1 uv run ruff format --check src/korvid/tools/approval.py tests/tools/test_approval.py
UV_FROZEN=1 uv run mypy src/korvid/tools/approval.py tests/tools/test_approval.py
UV_FROZEN=1 uv run tach check
```
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/tools/approval.py tests/tools/test_approval.py tests/tools/__init__.py
git commit -m "feat(tools): add typed ApprovalDecision and fail-closed source gate"
```

---

### Task 2: Pure `run_approved_write` orchestration + moved formatting helpers

**Files:**
- Create: `src/korvid/tools/write_coordinator.py`
- Test: `tests/tools/test_write_coordinator.py`
- Read (do not modify yet): `src/korvid/ui/write_coordinator.py:77-107` (the
  `WRITE_VERBS` dict, `gvr_label`, `write_locus`, `canonical_meta_kind`) and
  `:336-341` (`perm_target` staticmethod) and `:707-776` (`_run_write`/
  `_run_write_inner`) — this task copies their exact logic, Task 3 makes
  `korvid/ui/write_coordinator.py` delegate to it.

**Interfaces:**
- Consumes: nothing from Task 1 directly (this module has no approval
  concerns) — it is independent of `korvid.tools.approval`.
- Produces: `WRITE_VERBS: dict[str, tuple[str, str]]` (same contents as
  today's `korvid.ui.write_coordinator.WRITE_VERBS`); `gvr_label(meta:
  ResourceMeta) -> str`; `write_locus(ns: str | None) -> str`;
  `perm_target(action: str, meta: ResourceMeta) -> tuple[str, str]`;
  `Severity = Literal["information", "warning", "error"]`; `Notifier`
  (Protocol: `__call__(self, message: str, *, severity: Severity) ->
  None`); `AuditRecorder` (Protocol: `async def __call__(self, action: str,
  meta: ResourceMeta, namespace: str | None, name: str, detail: str,
  outcome: str) -> None`); `async def run_approved_write(action: str, meta:
  ResourceMeta, namespace: str | None, name: str, op_factory: Callable[[],
  Awaitable[None]], detail: str, *, audit: AuditRecorder, notify: Notifier)
  -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_write_coordinator.py`:

```python
"""Pure unit tests for the fail-closed write orchestration (issue TBD).

`run_approved_write` is a byte-identical extraction of what was
`WriteCoordinator._run_write_inner`'s body: intent audit (fail-closed) ->
mutation -> outcome audit, with `AuditRecorder`/`Notifier` standing in for
`self.audit_write`/`self._ui.notify`. It holds no Textual or `ViewState`
reference at all - exercised here with plain async fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.tools.write_coordinator import (
    WRITE_VERBS,
    gvr_label,
    perm_target,
    run_approved_write,
    write_locus,
)

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))


class _Recorder:
    """The approved mutation, plus a record of when its factory was called."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.built = 0

    def factory(self) -> Awaitable[None]:
        self.built += 1
        return self._run()

    async def _run(self) -> None:
        if self.error is not None:
            raise self.error


class _AuditSpy:
    """Records outcomes in call order; can be made to fail on demand."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.outcomes: list[str] = []

    async def __call__(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        if self.fails:
            raise OSError("audit sink unavailable")
        self.outcomes.append(outcome)


class _NotifySpy:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def __call__(self, message: str, *, severity: str) -> None:
        self.messages.append((message, severity))


async def test_audit_runs_before_the_mutation_is_ever_built() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder()

    async def audit_then_check(*args: Any, **kwargs: Any) -> None:
        await audit(*args, **kwargs)
        assert rec.built == 0, "the factory must not exist before intent is audited"

    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "",
        audit=audit_then_check, notify=notify,
    )
    assert outcome == "done"
    assert rec.built == 1
    assert audit.outcomes == ["intent", "success"]


async def test_failed_intent_audit_blocks_the_mutation() -> None:
    audit = _AuditSpy(fails=True)
    notify = _NotifySpy()
    rec = _Recorder()
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "",
        audit=audit, notify=notify,
    )
    assert outcome == "blocked: audit log unavailable"
    assert rec.built == 0
    assert ("delete pods/web-1 blocked: audit log unavailable", "error") in notify.messages


async def test_forbidden_mutation_keeps_the_rbac_message_contract() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=ApiStatusError(403, "Forbidden"))
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "",
        audit=audit, notify=notify,
    )
    assert outcome == "failed: missing permission: delete pods"
    assert audit.outcomes[0] == "intent"
    assert audit.outcomes[1].startswith("error:")


async def test_conflicting_mutation_explains_the_uid_precondition() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=ApiStatusError(409, "Conflict"))
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "",
        audit=audit, notify=notify,
    )
    assert outcome == (
        "failed: conflict: the target changed since it was approved - refresh and retry"
    )


async def test_unexpected_mutation_failure_still_audits_the_outcome() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=RuntimeError("boom"))
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "",
        audit=audit, notify=notify,
    )
    assert outcome == "failed: boom"
    assert audit.outcomes == ["intent", "error: boom"]


async def test_outcome_audit_failure_warns_but_keeps_the_executed_write() -> None:
    calls: list[str] = []

    async def flaky(
        action: str, meta: ResourceMeta, namespace: str | None, name: str,
        detail: str, outcome: str,
    ) -> None:
        calls.append(outcome)
        if outcome != "intent":
            raise OSError("disk full")

    notify = _NotifySpy()
    rec = _Recorder()
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "",
        audit=flaky, notify=notify,
    )
    assert outcome == "done"
    assert rec.built == 1
    assert ("Audit log write failed (operation already executed)", "warning") in notify.messages


async def test_cancelled_mutation_propagates_without_being_reported_as_failed() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_approved_write(
            "delete", _PODS_META, "default", "web-1", rec.factory, "",
            audit=audit, notify=notify,
        )
    assert audit.outcomes == ["intent"], "no outcome audit or notify for a cancelled mutation"
    assert notify.messages == []


def test_perm_target_matches_the_write_verbs_table() -> None:
    assert perm_target("delete", _PODS_META) == ("delete", "pods")
    assert WRITE_VERBS["delete"] == ("delete", "")


def test_gvr_label_and_write_locus_are_pure_string_helpers() -> None:
    assert gvr_label(_PODS_META) == "pods"
    assert write_locus("default") == " in default"
    assert write_locus(None) == " (cluster-scoped)"
```

(The exact `write_locus` return strings must match the real
implementation — check `src/korvid/ui/write_coordinator.py:107-112` for the
literal current strings before pasting this test; adjust the two asserted
literals to match exactly if they differ from the placeholders above.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_write_coordinator.py -v`
Expected: `ModuleNotFoundError: No module named
'korvid.tools.write_coordinator'`.

- [ ] **Step 3: Write the implementation**

Create `src/korvid/tools/write_coordinator.py`. Copy the *exact* current
bodies of `WRITE_VERBS`, `gvr_label`, `write_locus`, and the `perm_target`
staticmethod's logic (now a free function) from
`src/korvid/ui/write_coordinator.py` (lines 77-107 and 336-341), and the
exact current body of `_run_write_inner` (lines 723-776), rewritten to take
`audit`/`notify` as parameters instead of `self.audit_write`/
`self._ui.notify`:

```python
"""The fail-closed write orchestration, decoupled from Textual (issue TBD).

`run_approved_write` is the shared audit -> mutate -> audit sequence every
cluster mutation passes through once it is approved: a fail-closed intent
audit (if the record cannot persist, the mutation is never attempted), the
mutation itself, and an outcome audit that never un-does a mutation that
already ran. It is a direct extraction of what was
`korvid.ui.write_coordinator.WriteCoordinator._run_write_inner` - the only
change is that `audit`/`notify` arrive as structural ports instead of
`self.audit_write`/`self._ui.notify`, so nothing here needs Textual,
`ViewState`, or a running `KorvidApp` to run.

The write-approval decision that must precede this call - and the
synchronous in-flight reservation, and the `ViewState`/context-epoch
revalidation - stay outside this function on purpose: approval and
revalidation genuinely need the running app's dialog and view model
(`korvid.ui.write_coordinator.WriteCoordinator`), while everything here is
generic to any approved write, from any caller.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError

logger = logging.getLogger(__name__)

#: action -> (verb, subresource) for the SubjectAccessReview pre-check.
WRITE_VERBS: dict[str, tuple[str, str]] = {
    "delete": ("delete", ""),
    "scale": ("patch", "scale"),
    "rollout_restart": ("patch", ""),
    "debug": ("patch", "ephemeralcontainers"),
    "edit": ("update", ""),
    "resize": ("patch", "resize"),
    "install": ("create", ""),
    "approve": ("update", ""),
    # Operator uninstall deletes the Subscription (then its CSV); the
    # pre-check and 403 messages therefore speak in delete terms.
    "uninstall": ("delete", ""),
    # Cordon/uncordon patch node.spec.unschedulable; the drain pre-check
    # covers its cordon step (evictions are per-namespace pod
    # subresource creations that surface individually during execution).
    "cordon": ("patch", ""),
    "uncordon": ("patch", ""),
    "drain": ("patch", ""),
    # Node shell creates a privileged debug pod in the shell namespace
    # (kubectl debug node/, issue #46); the pre-check runs against pods.
    "node-shell": ("create", ""),
}


def gvr_label(meta: ResourceMeta) -> str:
    """<copy the exact current body from ui/write_coordinator.py>"""
    ...


def write_locus(ns: str | None) -> str:
    """<copy the exact current body from ui/write_coordinator.py>"""
    ...


def perm_target(action: str, meta: ResourceMeta) -> tuple[str, str]:
    """(verb, resource[/subresource]) as shown in permission messages."""
    verb, subresource = WRITE_VERBS[action]
    target = f"{meta.plural}/{subresource}" if subresource else meta.plural
    return verb, target


Severity = Literal["information", "warning", "error"]


class Notifier(Protocol):
    def __call__(self, message: str, *, severity: Severity) -> None: ...


class AuditRecorder(Protocol):
    async def __call__(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None: ...


async def run_approved_write(
    action: str,
    meta: ResourceMeta,
    namespace: str | None,
    name: str,
    op_factory: Callable[[], Awaitable[None]],
    detail: str,
    *,
    audit: AuditRecorder,
    notify: Notifier,
) -> str:
    """Fail-closed intent audit -> mutation -> outcome audit.

    Returns a short outcome string for callers that report back: 'done',
    'blocked: ...', or 'failed: ...'.
    """
    kind = meta.plural
    try:
        await audit(action, meta, namespace, name, detail, "intent")
    except Exception as exc:
        # Factory was never called - no coroutine to leak.
        logger.exception("audit intent record failed; write blocked: %s", exc)
        notify(f"{action} {kind}/{name} blocked: audit log unavailable", severity="error")
        return "blocked: audit log unavailable"
    try:
        await op_factory()
    except ApiStatusError as exc:
        with contextlib.suppress(Exception):
            await audit(action, meta, namespace, name, detail, f"error: {exc}")
        if exc.status == 403:
            verb, target = perm_target(action, meta)
            message = f"missing permission: {verb} {target}"
        elif exc.status == 409:
            message = "conflict: the target changed since it was approved - refresh and retry"
        else:
            message = str(exc)
        notify(f"{action} {kind}/{name} failed: {message}", severity="error")
        return f"failed: {message}"
    except Exception as exc:
        with contextlib.suppress(Exception):
            await audit(action, meta, namespace, name, detail, f"error: {exc}")
        notify(f"{action} {kind}/{name} failed: {exc}", severity="error")
        return f"failed: {exc}"
    try:
        await audit(action, meta, namespace, name, detail, "success")
    except Exception:
        logger.exception("audit outcome record failed after successful write")
        notify("Audit log write failed (operation already executed)", severity="warning")
    notify(f"{action} {kind}/{name}: done", severity="information")
    return "done"
```

Fill in `gvr_label`/`write_locus` with the exact current bodies (open
`src/korvid/ui/write_coordinator.py` and copy lines 101-112 verbatim,
adapting only `def write_locus(ns: str | None) -> str:`'s call site inside
the `WriteCoordinator.write_locus` staticmethod is *not* touched in this
task — only the free function is copied here).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_write_coordinator.py -v`
Expected: all pass. Fix the two `write_locus` literal assertions in the
test if they do not match the real current strings.

- [ ] **Step 5: Static checks**

Run:
```
UV_FROZEN=1 uv run ruff check src/korvid/tools/write_coordinator.py tests/tools/test_write_coordinator.py
UV_FROZEN=1 uv run ruff format --check src/korvid/tools/write_coordinator.py tests/tools/test_write_coordinator.py
UV_FROZEN=1 uv run mypy src/korvid/tools/write_coordinator.py tests/tools/test_write_coordinator.py
UV_FROZEN=1 uv run tach check
```
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/tools/write_coordinator.py tests/tools/test_write_coordinator.py
git commit -m "feat(tools): extract pure fail-closed write orchestration"
```

---

### Task 3: Delegate `ui/write_coordinator.py` to the pure module

**Files:**
- Modify: `src/korvid/ui/write_coordinator.py:1-112` (imports, `WRITE_VERBS`,
  `gvr_label`, `write_locus` become re-exports), `:336-341` (`perm_target`
  staticmethod delegates), `:707-776` (`_run_write`/`_run_write_inner`
  merge into one delegating method)
- Test: no new test file — run the existing
  `tests/ui/test_write_coordinator.py` unmodified as the regression gate.

**Interfaces:**
- Consumes: `korvid.tools.write_coordinator.{WRITE_VERBS, gvr_label,
  write_locus, perm_target, run_approved_write}` from Task 2.
- Produces: `WriteCoordinator.run`/`._run_write`'s external behavior is
  unchanged (still returns `'done'`/`'blocked: ...'`/`'failed: ...'`, still
  calls `self._ui.notify`/`self._ui.progress`/`self.audit_write` with the
  same arguments in the same order).

- [ ] **Step 1: Replace the module-level definitions with re-exports**

In `src/korvid/ui/write_coordinator.py`, replace the `WRITE_VERBS` dict
literal and the `gvr_label`/`write_locus` function bodies (keep
`canonical_meta_kind` as-is — it is not part of this extraction) with:

```python
from korvid.tools.write_coordinator import (
    WRITE_VERBS,
    gvr_label,
    perm_target,
    run_approved_write,
    write_locus,
)
```

placed in the existing import block (alongside the other `korvid.*`
imports, alphabetically ordered per the existing style — check
`ruff check` catches import order automatically). Delete the old
`WRITE_VERBS = {...}` dict literal and the old `def gvr_label(...)`/
`def write_locus(...)` module-level function bodies entirely (they now
come from the import).

- [ ] **Step 2: Delegate the `perm_target` staticmethod**

Replace:

```python
    @staticmethod
    def perm_target(action: str, meta: ResourceMeta) -> tuple[str, str]:
        """(verb, resource[/subresource]) as shown in permission messages."""
        verb, subresource = WRITE_VERBS[action]
        target = f"{meta.plural}/{subresource}" if subresource else meta.plural
        return verb, target
```

with:

```python
    @staticmethod
    def perm_target(action: str, meta: ResourceMeta) -> tuple[str, str]:
        """(verb, resource[/subresource]) as shown in permission messages."""
        return perm_target(action, meta)
```

- [ ] **Step 3: Merge `_run_write`/`_run_write_inner` into one delegating method**

Replace both methods (currently lines 707-776):

```python
    async def _run_write(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str,
    ) -> str:
        """The reserved body: the whole span publishes an in-flight progress
        label (issue #143) — between approval and the outcome toast there was
        previously no visible state at all. The audit -> mutate -> audit
        sequence itself is `korvid.tools.write_coordinator.
        run_approved_write` (issue TBD) - a pure extraction so the same
        ordering is available to any future non-Textual caller."""
        kind = meta.plural
        with self._ui.progress(f"{action} {kind}/{name}"):
            return await run_approved_write(
                action,
                meta,
                namespace,
                name,
                op_factory,
                detail,
                audit=self.audit_write,
                notify=self._ui.notify,
            )
```

Remove the old `_run_write_inner` method entirely (its logic now lives in
`run_approved_write`). `run()` already calls `self._run_write(...)` and
needs no change.

- [ ] **Step 4: Run the full existing write-coordinator test suite**

Run: `UV_FROZEN=1 uv run pytest tests/ui/test_write_coordinator.py -v`
Expected: all ~89 tests pass, unmodified.

- [ ] **Step 5: Run the broader UI write-related regression suites**

Run:
```
UV_FROZEN=1 uv run pytest tests/ui/test_agent_write.py tests/ui/test_resize_flow.py tests/ui/test_dryrun_preview.py tests/ui/test_proposal_controller.py tests/ui/test_resource_write_controller.py tests/ui/test_operator_uninstall.py -v
```
Expected: all pass, unmodified.

- [ ] **Step 6: Static checks**

Run:
```
UV_FROZEN=1 uv run ruff check src/korvid/ui/write_coordinator.py
UV_FROZEN=1 uv run ruff format --check src/korvid/ui/write_coordinator.py
UV_FROZEN=1 uv run mypy src/korvid/ui/write_coordinator.py
UV_FROZEN=1 uv run tach check
```
Expected: all clean.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/ui/write_coordinator.py
git commit -m "refactor(ui): delegate write orchestration to the pure tools module"
```

---

### Task 4: Wire `ApprovalDecision` into `agent_ui_controller.py`'s approval step

**Files:**
- Modify: `src/korvid/ui/agent_ui_controller.py` (imports near the top;
  `_await_user_approval` around line 2026; add two small module-level
  helper functions near it)
- Test: no new test file in this task — the existing
  `tests/ui/test_agent_write.py`, `tests/ui/test_agent_ui_controller.py`,
  `tests/ui/test_approval_timeout.py`, `tests/ui/test_protected_contexts.py`
  are the regression gate; Task 5 adds new composition-level tests.

**Interfaces:**
- Consumes: `korvid.tools.approval.{ApprovalDecision, ApprovalOutcome,
  TUI_KEYSTROKE_SOURCE, require_tui_keystroke_source,
  RejectedApprovalSourceError}` from Task 1.
- Produces: `_await_user_approval`'s return type/values are **unchanged**
  (`Literal["approved", "declined", "expired"]`); adds two new
  module-level pure functions in `agent_ui_controller.py`:
  `_decision_from_confirm_screen(confirmed: bool | None) ->
  ApprovalDecision` and `_collapse_decision(decision: ApprovalDecision) ->
  Literal["approved", "declined", "expired"]`.

- [ ] **Step 1: Add the import**

In `src/korvid/ui/agent_ui_controller.py`'s import block, add:

```python
from korvid.tools.approval import (
    TUI_KEYSTROKE_SOURCE,
    ApprovalDecision,
    ApprovalOutcome,
    require_tui_keystroke_source,
)
```

(placed alongside the other `korvid.*` imports, matching existing import
ordering — `ruff check --fix` will correct placement if needed).

- [ ] **Step 2: Add the two module-level helper functions**

Add these just above the `AgentUiController` class (or in the same
"approval dialog" section as `_await_user_approval`, whichever the
existing file's organization reads more naturally — check the surrounding
~30 lines before editing to match indentation/section-comment style):

```python
def _decision_from_confirm_screen(confirmed: bool | None) -> ApprovalDecision:
    """Every `ConfirmScreen` resolution - approve, decline, or Esc dismiss -
    is itself gated by `FreshKeysInput`'s post-dialog freshness check (a
    key buffered before the dialog existed can never resolve it), so all
    three outcomes are genuinely `tui_keystroke`-sourced."""
    if confirmed is True:
        return ApprovalDecision.approved(TUI_KEYSTROKE_SOURCE)
    if confirmed is False:
        return ApprovalDecision.declined(TUI_KEYSTROKE_SOURCE)
    return ApprovalDecision.dismissed(TUI_KEYSTROKE_SOURCE)


def _collapse_decision(
    decision: ApprovalDecision,
) -> Literal["approved", "declined", "expired"]:
    """Fail-closed gate (only a `tui_keystroke`-sourced decision may ever
    carry an `APPROVE` outcome forward), then fold to the legacy
    three-value contract external callers already depend on: dismiss reads
    the same as decline, exactly as it did before this decision type
    existed."""
    decision = require_tui_keystroke_source(decision)
    if decision.outcome is ApprovalOutcome.APPROVE:
        return "approved"
    if decision.outcome is ApprovalOutcome.EXPIRE:
        return "expired"
    return "declined"
```

- [ ] **Step 3: Route every return of `_await_user_approval` through them**

Change:

```python
        try:
            await self._ui.push_screen(screen, _done)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            confirmed = await asyncio.wait_for(fut, timeout=remaining)
            return "approved" if confirmed else "declined"
        except asyncio.CancelledError:
            ...
            raise
        except TimeoutError:
            ...
            return "expired"
```

to:

```python
        try:
            await self._ui.push_screen(screen, _done)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            confirmed = await asyncio.wait_for(fut, timeout=remaining)
            return _collapse_decision(_decision_from_confirm_screen(confirmed))
        except asyncio.CancelledError:
            ...
            raise
        except TimeoutError:
            ...
            return _collapse_decision(ApprovalDecision.expired(decision_source="timeout"))
```

Also change the earlier early return (the "never surfaced in time" path):

```python
        if not await self._wait_until_surfaceable(deadline):
            return "expired"
```

to:

```python
        if not await self._wait_until_surfaceable(deadline):
            return _collapse_decision(ApprovalDecision.expired(decision_source="timeout"))
```

(Do not touch the `_done`/`fut`/dialog-push/cancellation-handling logic
itself — only the three `return` statements and the one `raise
TimeoutError` path's eventual return value change.)

- [ ] **Step 4: Run the targeted approval-path regression tests**

Run:
```
UV_FROZEN=1 uv run pytest tests/ui/test_agent_write.py tests/ui/test_agent_ui_controller.py tests/ui/test_approval_timeout.py tests/ui/test_protected_contexts.py tests/ui/test_agent_interrupt.py -v
```
Expected: all pass, unmodified (in particular
`test_agent_write_approval_uses_protected_gate`'s `result_box ==
["approved"]` assertion, and every decline/dismiss/expire assertion in
`test_agent_write.py`).

- [ ] **Step 5: Static checks**

Run:
```
UV_FROZEN=1 uv run ruff check src/korvid/ui/agent_ui_controller.py
UV_FROZEN=1 uv run ruff format --check src/korvid/ui/agent_ui_controller.py
UV_FROZEN=1 uv run mypy src/korvid/ui/agent_ui_controller.py
UV_FROZEN=1 uv run tach check
```
Expected: all clean.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/ui/agent_ui_controller.py
git commit -m "feat(ui): gate agent-write approval on a typed tui_keystroke decision"
```

**If this task's Step 4 reveals any regression that cannot be resolved by
a small, obviously-correct fix** (i.e., the wiring changes real behavior,
not just internal typing), stop, revert this task's changes (`git checkout
-- src/korvid/ui/agent_ui_controller.py`), and report to the user: ship
Tasks 1-3 (the pure modules + the `ui/write_coordinator.py` delegation,
both already fully tested and behavior-preserving) in this PR, and defer
`agent_ui_controller.py` wiring — the single riskiest step — to a follow-up
slice. This is the plan's explicit escape hatch.

---

### Task 5: Composition tests proving the extraction is load-bearing

**Files:**
- Create: `tests/tools/test_write_approval_composition.py`
- Test: this task's deliverable *is* the test file.

**Interfaces:**
- Consumes: `korvid.tools.approval.{ApprovalDecision, ApprovalOutcome,
  TUI_KEYSTROKE_SOURCE, require_tui_keystroke_source,
  RejectedApprovalSourceError}` (Task 1),
  `korvid.tools.write_coordinator.run_approved_write` (Task 2),
  `korvid.ui.agent_ui_controller._decision_from_confirm_screen` and
  `_collapse_decision` (Task 4, private but directly importable for a
  same-repo test — no `__all__` restricts this in the existing codebase;
  confirm with `grep -n "__all__" src/korvid/ui/agent_ui_controller.py`
  before importing, and if one exists, add the two names to it rather than
  importing around it).

- [ ] **Step 1: Write the composition tests**

Create `tests/tools/test_write_approval_composition.py`:

```python
"""Composition tests: the extraction is load-bearing, not inert code.

These prove the four things the write-approval-port slice exists to
guarantee, independent of any single unit test elsewhere:

1. only a `tui_keystroke`-sourced approval may ever authorize a write;
2. `agent_ui_controller`'s own decision-building matches that contract;
3. the fail-closed audit-before-mutation ordering holds for the pure
   orchestrator every production write now runs through;
4. an unavailable audit sink blocks the mutation outright.
"""

from __future__ import annotations

import pytest

from korvid.k8s.discovery import ResourceMeta
from korvid.tools.approval import (
    TUI_KEYSTROKE_SOURCE,
    ApprovalDecision,
    RejectedApprovalSourceError,
    require_tui_keystroke_source,
)
from korvid.tools.write_coordinator import run_approved_write
from korvid.ui.agent_ui_controller import _collapse_decision, _decision_from_confirm_screen

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))


@pytest.mark.parametrize(
    ("confirmed", "expected_outcome_name"),
    [(True, "APPROVE"), (False, "DECLINE"), (None, "DISMISS")],
)
def test_every_real_dialog_resolution_is_tui_keystroke_sourced(
    confirmed, expected_outcome_name
) -> None:
    decision = _decision_from_confirm_screen(confirmed)
    assert decision.decision_source == TUI_KEYSTROKE_SOURCE
    assert decision.outcome.name == expected_outcome_name


def test_collapse_decision_matches_the_legacy_three_value_contract() -> None:
    assert _collapse_decision(_decision_from_confirm_screen(True)) == "approved"
    assert _collapse_decision(_decision_from_confirm_screen(False)) == "declined"
    assert _collapse_decision(_decision_from_confirm_screen(None)) == "declined"
    assert _collapse_decision(ApprovalDecision.expired("timeout")) == "expired"


def test_a_non_tui_keystroke_approval_is_rejected_before_it_ever_reaches_a_write() -> None:
    """A future non-Textual caller (an eval runner, an MCP tool) that ever
    builds an APPROVE decision without a real dialog resolution must be
    refused here - before `run_approved_write` is even called, since an
    unauthorized decision must never reach the mutation step at all."""
    forged = ApprovalDecision.approved("scripted-test-double")
    with pytest.raises(RejectedApprovalSourceError):
        require_tui_keystroke_source(forged)


async def test_run_approved_write_never_mutates_before_the_intent_audit_lands() -> None:
    calls: list[str] = []

    async def audit(action, meta, namespace, name, detail, outcome) -> None:
        calls.append(f"audit:{outcome}")

    def notify(message: str, *, severity: str) -> None:
        calls.append(f"notify:{severity}")

    async def op_factory() -> None:
        calls.append("mutate")

    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", op_factory, "",
        audit=audit, notify=notify,
    )
    assert outcome == "done"
    assert calls == ["audit:intent", "mutate", "audit:success", "notify:information"]


async def test_run_approved_write_blocks_the_mutation_when_the_audit_sink_is_gone() -> None:
    mutated = False

    async def audit(action, meta, namespace, name, detail, outcome) -> None:
        raise RuntimeError("audit log not configured")

    def notify(message: str, *, severity: str) -> None:
        pass

    async def op_factory() -> None:
        nonlocal mutated
        mutated = True

    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", op_factory, "",
        audit=audit, notify=notify,
    )
    assert outcome == "blocked: audit log unavailable"
    assert mutated is False
```

- [ ] **Step 2: Run the new tests**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_write_approval_composition.py -v`
Expected: all pass (this task adds no new production code — every
assertion targets Tasks 1/2/4's already-implemented behavior, so a failure
here means one of those tasks needs a fix, not this test).

- [ ] **Step 3: Static checks**

Run:
```
UV_FROZEN=1 uv run ruff check tests/tools/test_write_approval_composition.py
UV_FROZEN=1 uv run ruff format --check tests/tools/test_write_approval_composition.py
UV_FROZEN=1 uv run mypy tests/tools/test_write_approval_composition.py
```
Expected: all clean.

- [ ] **Step 4: Commit**

```bash
git add tests/tools/test_write_approval_composition.py
git commit -m "test: add write-approval-port composition tests"
```

---

### Task 6: Documentation

**Files:**
- Modify: `docs/evals/` — check whether any existing doc references
  `WriteCoordinator`'s internals in a way this refactor affects (run `grep
  -rln "_run_write_inner\|WRITE_VERBS" docs/` first); if none do, this task
  is only the design/plan docs already written (no further doc changes
  needed).
- Create (if a docs entry point for internal architecture exists, e.g. an
  `AGENTS.md`/`docs/architecture.md` "write perimeter" section): add a short
  note. Check first: `grep -rln "write perimeter\|WriteCoordinator" docs/
  AGENTS.md 2>/dev/null`.

- [ ] **Step 1: Check for existing references that need updating**

Run: `grep -rln "_run_write_inner\|WRITE_VERBS\|write perimeter" docs/ AGENTS.md 2>/dev/null`

- [ ] **Step 2: Update any matches found to reflect the new module split**

If `AGENTS.md` or a docs file describes the write perimeter as "one class"
or references `_run_write_inner` by name, add one sentence noting the pure
audit/mutate orchestration now lives in `korvid.tools.write_coordinator`
and the approval decision type in `korvid.tools.approval`, without
otherwise rewriting the surrounding prose.

- [ ] **Step 3: Commit (only if Step 2 made changes)**

```bash
git add <changed docs files>
git commit -m "docs: note the write-approval-port split"
```

---

### Task 7: Full verification

- [ ] **Step 1: Full targeted suite**

Run:
```
UV_FROZEN=1 uv run pytest tests/tools/ tests/ui/ -q
```
Expected: all pass, 0 failures.

- [ ] **Step 2: Full repository test suite**

Run: `UV_FROZEN=1 uv run pytest -q` (expect ~19 minutes based on prior
slices in this project; use a long `initial_wait`/poll with `read_bash`
rather than a short timeout).
Expected: same pass/skip counts as the pre-existing baseline (9754 passed,
22 skipped as of the last full run in this project), plus this task's new
tests.

- [ ] **Step 3: Full static checks**

Run:
```
UV_FROZEN=1 uv run ruff check .
UV_FROZEN=1 uv run ruff format --check .
UV_FROZEN=1 uv run mypy
UV_FROZEN=1 uv run tach check
```
Expected: clean on every file this slice touched (pre-existing, unrelated
formatting drift on files this slice does not touch is not this slice's
responsibility to fix).

- [ ] **Step 4: `pre-commit` on touched files**

Run: `UV_FROZEN=1 uv run pre-commit run --files <every file touched by
this slice, listed explicitly>`
Expected: all hooks pass.

- [ ] **Step 5: Confirm `uv.lock` is untouched**

Run: `git status --short uv.lock`
Expected: no output (clean). If drift appears, run `git checkout origin/main
-- uv.lock` before continuing.

---

### Task 8: Push and open the PR

- [ ] **Step 1: Push the branch**

Run: `git push -u origin feat/write-approval-port`

- [ ] **Step 2: Open the PR**

Write a PR body to a scratch file inside the repo (e.g.
`.pr-body-write-approval-port.md`), summarizing: the goal, the design
decisions (especially the three explicit non-goals — no TUI-free runner,
no `proposal_controller.py` change, no audit-schema change — and why), the
exact new public API (`ApprovalDecision`/`ApprovalOutcome`/
`require_tui_keystroke_source`/`run_approved_write`), and test evidence.
Run:
```
gh pr create --repo hellices/korvid --base main --head feat/write-approval-port \
  --title "Extract a typed write-approval decision and pure write orchestration" \
  --body-file .pr-body-write-approval-port.md
rm .pr-body-write-approval-port.md
```

- [ ] **Step 3: Poll CI to green**

Run `sleep 300 && gh pr checks <PR number> --repo hellices/korvid` in a
loop (300-400s increments) until every check reports success or the run
is clearly stuck/failed; investigate and fix any failure before
re-polling.

- [ ] **Step 4: Report back**

Report to the user: PR URL, head commit SHA, the design decisions from the
spec (in particular the three explicit non-goals and their rationale), the
exact new public API surface, test results (targeted + full suite +
static checks), CI status, and confirmation the PR was **not** merged (per
repo branch policy).
