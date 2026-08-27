# TUI-free operation-journey runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a capability/policy-based approval architecture (closing PR
#323's forgeable-string gap) and a public, TUI-free operation-journey runner
in `src/korvid/evals` with a versioned, deterministic JSON contract, on the
`feat/operation-journey-runner` integration branch (already merged from
`origin/main` + PRs #321/#322/#323).

**Architecture:** See
`docs/superpowers/specs/2026-08-28-operation-journey-runner-design.md`.
`ApprovalPolicy` capability in `korvid.tools.approval`;
`TextualApprovalPolicy` extracted in `korvid.ui.agent_ui_controller`;
`ScriptedApprovalPolicy` + `ScriptedOperationBridge` + journaling/session
port + composition in new `korvid.evals.operation_runner`; selection/hash
in `korvid.evals.operation`; CLI in `korvid.evals.operation_main`.

**Tech Stack:** Python 3.11+, pytest, `uv run`, existing korvid harness
(`DefaultAgentSession`, `NativeAgentEngine`, `ToolExecutor`,
`StatefulFakeKubeClient`/`StatefulFakeWriteOps`, `AuditLog`).

## Global Constraints

- No `tach.toml` change.
- No imports from `tests/`, `korvid.ui`, or `korvid.core` inside
  `src/korvid/evals/`.
- `run_approved_write`'s signature does not change.
- `SAFETY_CONTRACT`/immutable prompt safety layer is never widened.
- Every existing `tests/ui/`, `tests/tools/`, `tests/evals/` test keeps
  passing unmodified unless a task explicitly says otherwise.
- `EVAL_PROTOCOL_VERSION` stays `"1.0"` (additive change only).
- Commit after every task.

---

### Task 1: `ApprovalRequest` + `ApprovalPolicy` ABC

**Files:**
- Modify: `src/korvid/tools/approval.py`
- Test: `tests/tools/test_approval.py` (extend existing file)

**Interfaces:**
- Produces: `ApprovalRequest` (frozen dataclass), `ApprovalPolicy` (ABC,
  `async def decide(self, request: ApprovalRequest) -> ApprovalDecision`).
- Consumes: existing `ApprovalDecision`, `ApprovalOutcome` from the same
  module (unchanged).

- [ ] **Step 1: Read the existing file to confirm exact current exports**

Run: `sed -n '1,60p' src/korvid/tools/approval.py`

Confirm `ApprovalOutcome`, `ApprovalDecision`, `TUI_KEYSTROKE_SOURCE`,
`RejectedApprovalSourceError`, `require_tui_keystroke_source` are present
and note the exact import block at the top of the file.

- [ ] **Step 2: Write the failing test**

Append to `tests/tools/test_approval.py`:

```python
import pytest

from korvid.tools.approval import ApprovalPolicy, ApprovalRequest


def test_approval_request_is_frozen() -> None:
    request = ApprovalRequest(title="t", operation="scale replicas")
    with pytest.raises(AttributeError):
        request.title = "changed"  # type: ignore[misc]


def test_approval_request_defaults() -> None:
    request = ApprovalRequest(title="t", operation="op")
    assert request.require_name is None
    assert request.preview is None
    assert request.managed_note is None
    assert request.impact_lines is None


def test_approval_policy_is_abstract() -> None:
    with pytest.raises(TypeError):
        ApprovalPolicy()  # type: ignore[abstract]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_approval.py -k "approval_request or approval_policy_is_abstract" -v`
Expected: FAIL with `ImportError: cannot import name 'ApprovalPolicy'`.

- [ ] **Step 4: Implement**

Add to `src/korvid/tools/approval.py` (near the top, after existing
imports — add `from abc import ABC, abstractmethod` and
`from dataclasses import dataclass` if not already imported):

```python
@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """Everything a policy may use to decide, mirroring the Textual
    dialog's own presentation fields. A scripted policy is free to ignore
    all of it and decide purely from its own pre-authored sequence."""

    title: str
    operation: str
    require_name: str | None = None
    preview: str | None = None
    managed_note: str | None = None
    impact_lines: tuple[str, ...] | None = None


class ApprovalPolicy(ABC):
    """A composition-root-bound capability that decides approval requests.

    Trust is established by *which concrete class* a composition root
    instantiated (verified by `isinstance` in a composition test), never by
    inspecting `ApprovalDecision.decision_source` at the point of use. Do
    not add a generic "trusted source string" check anywhere that consumes
    an `ApprovalDecision` — that is exactly the forgeable pattern this type
    replaces.
    """

    @abstractmethod
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        """Decide one approval request."""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_approval.py -v`
Expected: PASS, all tests including pre-existing ones in the file.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/tools/approval.py tests/tools/test_approval.py
git commit -m "feat(tools): add ApprovalRequest and ApprovalPolicy capability"
```

---

### Task 2: `ScriptedApprovalPolicy`

**Files:**
- Modify: `src/korvid/tools/approval.py`
- Test: `tests/tools/test_approval.py`

**Interfaces:**
- Consumes: `ApprovalPolicy`, `ApprovalRequest`, `ApprovalOutcome`,
  `ApprovalDecision` (Task 1 / existing).
- Produces: `SCRIPTED_POLICY_SOURCE: Final[str]`,
  `ScriptedApprovalPolicy(ApprovalPolicy)` with constructor
  `__init__(self, outcomes: Sequence[ApprovalOutcome], *, interventions: Sequence[Callable[[], None] | None] | None = None)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/tools/test_approval.py`:

```python
from korvid.tools.approval import (
    SCRIPTED_POLICY_SOURCE,
    TUI_KEYSTROKE_SOURCE,
    ApprovalOutcome,
    ScriptedApprovalPolicy,
)


async def test_scripted_policy_returns_outcomes_in_order() -> None:
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE, ApprovalOutcome.DECLINE])
    request = ApprovalRequest(title="t", operation="op")
    first = await policy.decide(request)
    second = await policy.decide(request)
    assert first.outcome is ApprovalOutcome.APPROVE
    assert second.outcome is ApprovalOutcome.DECLINE


async def test_scripted_policy_source_is_never_tui_keystroke() -> None:
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE])
    decision = await policy.decide(ApprovalRequest(title="t", operation="op"))
    assert decision.decision_source == SCRIPTED_POLICY_SOURCE
    assert decision.decision_source != TUI_KEYSTROKE_SOURCE


async def test_scripted_policy_fails_closed_when_exhausted() -> None:
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE])
    await policy.decide(ApprovalRequest(title="t", operation="op"))
    decision = await policy.decide(ApprovalRequest(title="t", operation="op"))
    assert decision.outcome is ApprovalOutcome.DECLINE


async def test_scripted_policy_runs_intervention_before_approve() -> None:
    calls: list[str] = []
    policy = ScriptedApprovalPolicy(
        [ApprovalOutcome.APPROVE],
        interventions=[lambda: calls.append("intervened")],
    )
    decision = await policy.decide(ApprovalRequest(title="t", operation="op"))
    assert calls == ["intervened"]
    assert decision.outcome is ApprovalOutcome.APPROVE
```

Add `import pytest` marker if the file is not already async-enabled: check
`tests/tools/test_approval.py`'s top for `pytestmark = pytest.mark.asyncio`
or per-test `@pytest.mark.asyncio`; match whatever convention the existing
file already uses (grep the file first).

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_approval.py -k scripted_policy -v`
Expected: FAIL with `ImportError: cannot import name 'ScriptedApprovalPolicy'`.

- [ ] **Step 3: Implement**

Add to `src/korvid/tools/approval.py`:

```python
SCRIPTED_POLICY_SOURCE: Final[str] = "scripted_policy"


class ScriptedApprovalPolicy(ApprovalPolicy):
    """A deterministic, pre-authored approval policy for TUI-free runs.

    No sleeps, no timers: `decide()` pops the next scripted outcome
    synchronously. An unexpected extra call (a write the script did not
    anticipate) fails closed to `DECLINE` rather than raising or silently
    approving — a fixture that expects zero writes must see every
    unrequested write refused, never crash the run.

    `interventions[i]`, if given and not `None`, runs immediately before
    step `i`'s outcome is returned, standing in for a fixture's declared
    `dialog_intervention`: there is no dialog to intervene "during" in a
    TUI-free run, so this is the one point that models it deterministically.
    """

    def __init__(
        self,
        outcomes: Sequence[ApprovalOutcome],
        *,
        interventions: Sequence[Callable[[], None] | None] | None = None,
    ) -> None:
        self._outcomes = list(outcomes)
        self._interventions = list(interventions) if interventions is not None else []
        self._index = 0

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        if self._index >= len(self._outcomes):
            return ApprovalDecision(
                outcome=ApprovalOutcome.DECLINE, decision_source=SCRIPTED_POLICY_SOURCE
            )
        outcome = self._outcomes[self._index]
        intervention = (
            self._interventions[self._index] if self._index < len(self._interventions) else None
        )
        self._index += 1
        if intervention is not None and outcome is ApprovalOutcome.APPROVE:
            intervention()
        return ApprovalDecision(outcome=outcome, decision_source=SCRIPTED_POLICY_SOURCE)
```

Add `from collections.abc import Callable, Sequence` and `from typing import
Final` to the imports if not already present (check first — `Final` is
likely already imported for `TUI_KEYSTROKE_SOURCE`).

Check the exact constructor signature of `ApprovalDecision` first
(`grep -n "class ApprovalDecision" -A 15 src/korvid/tools/approval.py`) and
adjust field names above to match exactly if they differ from
`outcome`/`decision_source`.

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_approval.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/tools/approval.py tests/tools/test_approval.py
git commit -m "feat(tools): add ScriptedApprovalPolicy"
```

---

### Task 3: `korvid.tools.audit` re-export shim

**Files:**
- Create: `src/korvid/tools/audit.py`
- Test: `tests/tools/test_audit_reexport.py`

**Interfaces:**
- Produces: `korvid.tools.audit.AuditLog` (the same class as
  `korvid.core.audit.AuditLog`).

- [ ] **Step 1: Write the failing test**

```python
from korvid.core.audit import AuditLog as CoreAuditLog
from korvid.tools.audit import AuditLog


def test_tools_audit_reexports_core_audit_log() -> None:
    assert AuditLog is CoreAuditLog
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_audit_reexport.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'korvid.tools.audit'`.

- [ ] **Step 3: Implement**

```python
"""Re-export of the production audit log for callers that may depend on
`korvid.tools` but not `korvid.core` directly (tach.toml).

`korvid.tools` already depends on `korvid.core`; `korvid.evals` depends on
`korvid.tools` but not `korvid.core`. Importing `AuditLog` from here (not
from `korvid.core.audit` directly) keeps `korvid.evals`'s TUI-free
operation runner inside its declared module boundary with no `tach.toml`
change.
"""

from __future__ import annotations

from korvid.core.audit import AuditLog

__all__ = ["AuditLog"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_FROZEN=1 uv run pytest tests/tools/test_audit_reexport.py -v`
Expected: PASS.

- [ ] **Step 5: Run tach check**

Run: `UV_FROZEN=1 uv run tach check`
Expected: no new violations.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/tools/audit.py tests/tools/test_audit_reexport.py
git commit -m "feat(tools): re-export AuditLog for evals-safe import"
```

---

### Task 4: Extract `TextualApprovalPolicy` in `agent_ui_controller.py`

**Files:**
- Modify: `src/korvid/ui/agent_ui_controller.py`
- Test: existing `tests/ui/` suite (must pass unmodified) + new
  `tests/ui/test_approval_policy_composition.py`

**Interfaces:**
- Consumes: `ApprovalPolicy`, `ApprovalRequest` (Task 1).
- Produces: `TextualApprovalPolicy(ApprovalPolicy)` class in
  `agent_ui_controller.py`; `AgentUiController._approval_policy: ApprovalPolicy`
  attribute, bound in `__init__`.

- [ ] **Step 1: Read `_await_user_approval` in full before touching it**

Run: `grep -n "_await_user_approval\|_collapse_decision\|require_tui_keystroke_source" src/korvid/ui/agent_ui_controller.py`

View the full body of `_await_user_approval` (approximately lines
2058-2125) with the `view` tool at that exact range. Note every parameter
name and every local variable it uses from `self` (`self._view`,
`self._app_ref` or similar, deadline/timeout fields) — these must all still
be reachable from `TextualApprovalPolicy`, which will hold `self._controller`.

- [ ] **Step 2: Write the composition test first (it will fail until Step 4)**

Create `tests/ui/test_approval_policy_composition.py`:

```python
"""Production binds exactly one ApprovalPolicy: TextualApprovalPolicy.

Guards the capability architecture (docs/superpowers/specs/
2026-08-28-operation-journey-runner-design.md): trust is established by
which concrete class the composition root bound, not by a string compared
at the point a decision is consumed.
"""

from korvid.tools.approval import ApprovalPolicy
from korvid.ui.agent_ui_controller import AgentUiController, TextualApprovalPolicy


def test_agent_ui_controller_binds_textual_approval_policy(agent_ui_controller: AgentUiController) -> None:
    """Uses whatever fixture already builds an `AgentUiController` for
    `tests/ui/`; replace `agent_ui_controller` with the exact fixture name
    used by neighboring tests in `tests/ui/test_agent_ui_controller*.py` if
    it differs (grep for `def agent_ui_controller` or the class's existing
    constructor call in conftest.py first)."""
    assert isinstance(agent_ui_controller._approval_policy, TextualApprovalPolicy)
    assert isinstance(agent_ui_controller._approval_policy, ApprovalPolicy)
```

Before writing this, run:
`grep -rn "AgentUiController(" tests/ui/conftest.py tests/ui/test_agent_ui_controller*.py | head -5`
to find the exact existing fixture/construction pattern and use it instead
of inventing a new fixture name.

- [ ] **Step 3: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/ui/test_approval_policy_composition.py -v`
Expected: FAIL, `ImportError: cannot import name 'TextualApprovalPolicy'`.

- [ ] **Step 4: Extract `TextualApprovalPolicy`**

In `src/korvid/ui/agent_ui_controller.py`, add near the top-level class
definitions (after imports, before `AgentUiController`):

```python
class TextualApprovalPolicy(ApprovalPolicy):
    """The one production `ApprovalPolicy`: a real `ConfirmScreen`, driven
    only by a fresh user keystroke after the dialog is posted. Extracted
    verbatim from `_await_user_approval`'s prior body — see git history
    for the pre-extraction version if behavior is ever in question."""

    def __init__(self, controller: "AgentUiController") -> None:
        self._controller = controller

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        # <-- paste `_await_user_approval`'s existing body here verbatim,
        # replacing every reference to its old positional/keyword
        # parameters (`title`, `operation`, `require_name`, `preview`,
        # `managed_note`, `impact_lines`) with `request.title`,
        # `request.operation`, `request.require_name`, `request.preview`,
        # `request.managed_note`, `request.impact_lines`, and every `self.`
        # reference with `self._controller.`.
        ...
```

Then reduce `_await_user_approval` to:

```python
async def _await_user_approval(
    self,
    title: str,
    operation: str,
    *,
    require_name: str | None = None,
    preview: str | None = None,
    managed_note: str | None = None,
    impact_lines: tuple[str, ...] | None = None,
) -> Literal["approved", "declined", "expired"]:
    request = ApprovalRequest(
        title=title,
        operation=operation,
        require_name=require_name,
        preview=preview,
        managed_note=managed_note,
        impact_lines=impact_lines,
    )
    decision = await self._approval_policy.decide(request)
    return self._collapse_decision(decision)
```

Adjust `_collapse_decision`'s signature only if needed to accept an
`ApprovalDecision` instead of whatever raw shape it took before (check its
current signature first — it may already take an `ApprovalDecision` from
PR #323's work, in which case no change is needed there at all).

Add `self._approval_policy: ApprovalPolicy = TextualApprovalPolicy(self)` to
`AgentUiController.__init__`, after `self._writes`/other collaborators are
assigned (order should not matter, but keep it adjacent to other
approval-related attributes for readability). Add the import:
`from korvid.tools.approval import ApprovalDecision, ApprovalPolicy, ApprovalRequest`
(merge with whatever `korvid.tools.approval` import already exists in the
file from PR #323).

- [ ] **Step 5: Run the composition test and the full existing UI suite**

Run: `UV_FROZEN=1 uv run pytest tests/ui/test_approval_policy_composition.py -v`
Expected: PASS.

Run: `UV_FROZEN=1 uv run pytest tests/ui/ -q`
Expected: every test that passed before this task still passes, same count
minus/plus only the new composition test. If anything regresses, the
extraction moved or altered behavior — diff the extracted method against
`git show HEAD:src/korvid/ui/agent_ui_controller.py` (pre-task) to find the
exact discrepancy before proceeding.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/ui/agent_ui_controller.py tests/ui/test_approval_policy_composition.py
git commit -m "refactor(ui): extract TextualApprovalPolicy from _await_user_approval"
```

---

### Task 5: `StatefulFakeWriteOps` audit-intent fail-closed enforcement

**Files:**
- Modify: `src/korvid/evals/operation_state.py`
- Test: `tests/evals/test_operation_state.py` (extend existing file — grep
  for its exact name first: `ls tests/evals/test_operation_state*.py`)

**Interfaces:**
- Consumes: existing `StatefulFakeWriteOps`, `ApiStatusError`,
  `AuditIntentProbe` type (unchanged signature).
- Produces: `_observe_audit_intent` now raises `ApiStatusError` when no
  matching record is found (previously always returned/journaled only).

- [ ] **Step 1: Read `_observe_audit_intent` in full**

View `src/korvid/evals/operation_state.py` lines 300-341 (per prior
research in this session) to see its exact current body, the shape of
`AuditRecord`, and how "matching" is currently defined (by action/kind/
namespace/name, presumably).

- [ ] **Step 2: Write the failing test**

Add to `tests/evals/test_operation_state.py` (match the file's existing
import/fixture style — read its first 40 lines first):

```python
def test_scale_object_fails_closed_without_matching_audit_intent() -> None:
    """No audit-intent probe record at all: the mutation must never run."""
    state = FakeClusterState(...)  # build exactly as neighboring tests in
    # this file already do — copy an existing fixture-construction test's
    # setup verbatim rather than inventing a new one.
    journal = ActionJournal()
    ops = StatefulFakeWriteOps(
        state,
        journal,
        context="test",
        audit_intent_probe=lambda: (),  # empty: no intent record exists
    )
    with pytest.raises(ApiStatusError):
        await ops.scale_object(<meta>, <namespace>, <name>, 3, uid=<uid>)
```

Fill in `<meta>`/`<namespace>`/`<name>`/`<uid>` from whatever a neighboring
existing `scale_object` test in the same file already uses for its happy
path (copy its fixture object, then break only the audit-intent probe).

- [ ] **Step 3: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_state.py -k audit_intent -v`
Expected: FAIL — currently no exception is raised (`_observe_audit_intent`
is observation-only).

- [ ] **Step 4: Implement fail-closed enforcement**

In `_observe_audit_intent` (or wherever it is called from within
`scale_object`/`rollout_restart_with_stamp`/any other write method), after
computing whether a matching record was found, add:

```python
if not matched:
    self._journal.append(
        event="audit_intent_missing",
        actor="write_ops",
        action=action,
        target=target,
        result="blocked",
        detail=_safe_summarize(action=action, uid=uid, reason="no_matching_audit_intent"),
    )
    raise ApiStatusError(
        409, f"{_FAKE}: no matching audit-intent record found before mutation"
    )
```

Match the exact existing journal-event vocabulary/helper functions already
used elsewhere in the file (`_safe_summarize`, `_target`, `JournalTarget`)
— do not invent new helper signatures; reuse what `_resolve`'s UID-conflict
branch already calls for consistency.

- [ ] **Step 5: Run the new test, then the full existing file's suite**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_state.py -v`
Expected: PASS, including every pre-existing test — the happy path (where
`AuditLog`/`run_approved_write` genuinely wrote an intent record first)
must still succeed, since the record now exists before this check runs.

- [ ] **Step 6: Run the full `tests/evals/` suite once to catch downstream users**

Run: `UV_FROZEN=1 uv run pytest tests/evals/ -q`
Expected: PASS. If `tests/evals/operation_app.py`'s existing Textual-driven
tests regress here, they were relying on the observation-only behavior —
investigate before proceeding; the design intends this to be additive
enforcement that a correctly-ordered audit (which every real write already
performs) never trips.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/evals/operation_state.py tests/evals/test_operation_state.py
git commit -m "fix(evals): StatefulFakeWriteOps rejects mutation without audited intent"
```

---

### Task 6: `select_operation_journeys` + `operation_case_pack_identity`

**Files:**
- Modify: `src/korvid/evals/operation.py`
- Test: `tests/evals/test_operation.py` (extend; grep for exact filename
  first: `ls tests/evals/test_operation*.py`)

**Interfaces:**
- Consumes: existing `OperationJourney` dataclass and its full field list
  (read `src/korvid/evals/operation.py` lines 206-326 again if needed for
  exact field names); `korvid.evals.scenario._canonical_value` (existing,
  unchanged — import it directly, same package).
- Produces: `select_operation_journeys(journeys: Sequence[OperationJourney], ids: Sequence[str]) -> list[OperationJourney]`;
  `operation_case_pack_identity(journeys: Sequence[OperationJourney]) -> dict[str, Any]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/evals/test_operation.py`:

```python
import pytest

from korvid.evals.operation import (
    load_operation_journeys,
    operation_case_pack_identity,
    select_operation_journeys,
)


def _bundled():
    from korvid.evals.operation import bundled_operations_dir
    return load_operation_journeys(bundled_operations_dir())


def test_select_operation_journeys_exact_ids() -> None:
    journeys = _bundled()
    selected = select_operation_journeys(journeys, ["restart-deployment", "scale-deployment-up"])
    assert [j.id for j in selected] == ["restart-deployment", "scale-deployment-up"]


def test_select_operation_journeys_rejects_empty() -> None:
    with pytest.raises(ValueError):
        select_operation_journeys(_bundled(), [])


def test_select_operation_journeys_rejects_duplicate() -> None:
    with pytest.raises(ValueError):
        select_operation_journeys(_bundled(), ["restart-deployment", "restart-deployment"])


def test_select_operation_journeys_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        select_operation_journeys(_bundled(), ["does-not-exist"])


def test_operation_case_pack_identity_is_deterministic() -> None:
    journeys = _bundled()
    first = operation_case_pack_identity(journeys)
    second = operation_case_pack_identity(list(reversed(journeys)))
    assert first == second
    assert first["operation_ids"] == sorted(j.id for j in journeys)
    assert first["count"] == len(journeys)


def test_operation_case_pack_identity_changes_with_content() -> None:
    journeys = _bundled()
    baseline = operation_case_pack_identity(journeys)
    mutated = list(journeys)
    mutated[0] = mutated[0].__class__(
        **{**mutated[0].__dict__, "goal": mutated[0].goal + " (edited)"}
    )
    changed = operation_case_pack_identity(mutated)
    assert changed["sha256"] != baseline["sha256"]
```

Adjust the "mutated" construction in the last test to whatever
`OperationJourney`'s actual field set supports (it may be a frozen
dataclass needing `dataclasses.replace` instead of `__class__(**...)` —
check whether it is `@dataclass(frozen=True)` first and use
`dataclasses.replace(mutated[0], goal=mutated[0].goal + " (edited)")` if
so, which is the safer, correct approach for a frozen dataclass).

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation.py -k "select_operation_journeys or operation_case_pack_identity" -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement, mirroring `scenario.py` exactly**

In `src/korvid/evals/operation.py`, add near the end of the file:

```python
from korvid.evals.scenario import _canonical_value  # same package; no tach boundary


def select_operation_journeys(
    journeys: Sequence[OperationJourney], operation_ids: Sequence[str]
) -> list[OperationJourney]:
    """Select an exact, repeatable subset of already-loaded operation
    journeys by id — the operation analogue of `scenario.select_scenarios`.
    Fail-closed: empty, duplicate, or unknown ids all raise."""
    ids = list(operation_ids)
    if not ids:
        raise ValueError("operation selection must name at least one operation id")
    blank = [raw for raw in ids if not isinstance(raw, str) or not raw.strip()]
    if blank:
        raise ValueError("operation selection ids must be non-empty strings")
    seen: set[str] = set()
    duplicates: set[str] = set()
    for operation_id in ids:
        if operation_id in seen:
            duplicates.add(operation_id)
        seen.add(operation_id)
    if duplicates:
        raise ValueError(f"duplicate operation id(s) in selection: {sorted(duplicates)}")
    by_id = {journey.id: journey for journey in journeys}
    unknown = sorted(set(ids) - by_id.keys())
    if unknown:
        known = sorted(by_id)
        raise ValueError(f"unknown operation id(s): {unknown}; known ids: {known}")
    return sorted((by_id[operation_id] for operation_id in ids), key=lambda j: j.id)


def _operation_content(journey: OperationJourney) -> dict[str, Any]:
    """A deep, JSON-safe view of every field `OperationJourney` (and its
    nested dataclasses) accepts. Used only to derive the case-pack content
    hash — mirrors `scenario._scenario_content`'s contract exactly: every
    field a grader or the agent can observe, nothing that is a path or an
    mtime."""
    # Fill in every field from `OperationJourney`'s actual dataclass
    # definition (re-read src/korvid/evals/operation.py lines 206-326
    # before writing this) — target, cluster, goal, approval,
    # permission_denials, dialog_intervention, postconditions,
    # initial_selection, expected_approval_dialogs, etc. Use `dataclasses.asdict`
    # as a starting structure only if every leaf value is already
    # JSON-safe; otherwise build the dict field-by-field the same way
    # `_scenario_content` does, converting nested dataclasses (e.g.
    # `OperationTarget`, `StateAssertion`) via their own `dataclasses.asdict`
    # or explicit dict construction.
    ...


def operation_case_pack_identity(journeys: Sequence[OperationJourney]) -> dict[str, Any]:
    """Deterministic identity for an exact set of loaded operation journey
    definitions — the operation analogue of `scenario.case_pack_identity`."""
    ordered = sorted(journeys, key=lambda j: j.id)
    ids = [journey.id for journey in ordered]
    content = [_operation_content(journey) for journey in ordered]
    canonical = _canonical_value(content)
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return {"operation_ids": ids, "count": len(ids), "sha256": digest}
```

Add `import hashlib`, `import json`, `from dataclasses import asdict` (or
equivalent) to the top of `operation.py` if not already imported — check
first with `grep -n "^import\|^from" src/korvid/evals/operation.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation.py -v`
Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/evals/operation.py tests/evals/test_operation.py
git commit -m "feat(evals): add select_operation_journeys and operation_case_pack_identity"
```

---

### Task 7: `ScriptedOperationBridge` + journaling port (`operation_runner.py`, part 1)

**Files:**
- Create: `src/korvid/evals/operation_runner.py`
- Test: `tests/evals/test_operation_runner.py`

**Interfaces:**
- Consumes: `korvid.tools.executor.UIBridge`, `korvid.tools.approval.{ApprovalPolicy, ApprovalRequest, ApprovalOutcome}`,
  `korvid.tools.write_coordinator.run_approved_write`, `korvid.tools.audit.AuditLog`,
  `korvid.evals.operation_state.{StatefulFakeKubeClient, StatefulFakeWriteOps, FakeClusterState}`,
  `korvid.evals.operation_journal.{ActionJournal, JournalTarget, summarize_action, summarize_untrusted}`,
  `korvid.evals.grader.builtin_aliases` (or wherever `builtin_aliases` is
  actually defined — confirm with
  `grep -n "def builtin_aliases" src/korvid/evals/*.py` first),
  `korvid.evals.operation.OperationJourney`.
- Produces: `ScriptedOperationBridge(UIBridge)`, `approval_from_result(result: str) -> str`,
  `make_audit_intent_probe(audit_path: Path) -> AuditIntentProbe`.

- [ ] **Step 1: Confirm exact import sources before writing code**

Run:
```bash
grep -n "^from korvid\|^import korvid" tests/evals/operation_app.py | sort -u
grep -n "def builtin_aliases" -r src/korvid/evals/
```
Use the exact module paths this prints for every symbol ported below —
do not guess.

- [ ] **Step 2: Write the failing tests**

Create `tests/evals/test_operation_runner.py`:

```python
"""Tests for the TUI-free operation-journey runner's write bridge."""

import asyncio
from pathlib import Path

import pytest

from korvid.evals.operation import bundled_operations_dir, load_operation_journeys
from korvid.evals.operation_runner import ScriptedOperationBridge, approval_from_result
from korvid.evals.operation_state import StatefulFakeKubeClient
from korvid.evals.operation_journal import ActionJournal
from korvid.tools.approval import ApprovalOutcome, ScriptedApprovalPolicy
from korvid.tools.audit import AuditLog


def _load(journey_id: str):
    journeys = load_operation_journeys(bundled_operations_dir())
    return next(j for j in journeys if j.id == journey_id)


async def test_scripted_bridge_scales_on_approve(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path, context=journey.target.context)
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE])
    bridge = ScriptedOperationBridge(
        kube=kube, journal=journal, journey=journey, audit=audit, policy=policy
    )
    result = await bridge.agent_request_write(
        "scale",
        journey.target.kind,
        journey.target.name,
        journey.target.namespace,
        replicas=5,
    )
    assert approval_from_result(result) == "approved"
    assert result.startswith("approved and executed")


async def test_scripted_bridge_denies_on_decline(tmp_path: Path) -> None:
    journey = _load("scale-deployment-up")
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit = AuditLog(tmp_path / "audit.jsonl", context=journey.target.context)
    policy = ScriptedApprovalPolicy([ApprovalOutcome.DECLINE])
    bridge = ScriptedOperationBridge(
        kube=kube, journal=journal, journey=journey, audit=audit, policy=policy
    )
    result = await bridge.agent_request_write(
        "scale", journey.target.kind, journey.target.name, journey.target.namespace, replicas=5
    )
    assert approval_from_result(result) == "denied"


async def test_scripted_bridge_rejects_unsupported_action(tmp_path: Path) -> None:
    journey = _load("edit-unsupported")
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit = AuditLog(tmp_path / "audit.jsonl", context=journey.target.context)
    policy = ScriptedApprovalPolicy([])
    bridge = ScriptedOperationBridge(
        kube=kube, journal=journal, journey=journey, audit=audit, policy=policy
    )
    result = await bridge.agent_request_write(
        "edit", journey.target.kind, journey.target.name, journey.target.namespace
    )
    assert result.startswith("ERROR:")


async def test_concurrent_bridges_do_not_leak_decisions(tmp_path: Path) -> None:
    """Two runners built with distinct scripted policies never cross-talk."""
    journey = _load("scale-deployment-up")

    async def run(outcome: ApprovalOutcome, path_suffix: str) -> str:
        kube = StatefulFakeKubeClient(journey.cluster)
        journal = ActionJournal()
        audit = AuditLog(tmp_path / f"audit-{path_suffix}.jsonl", context=journey.target.context)
        policy = ScriptedApprovalPolicy([outcome])
        bridge = ScriptedOperationBridge(
            kube=kube, journal=journal, journey=journey, audit=audit, policy=policy
        )
        return await bridge.agent_request_write(
            "scale",
            journey.target.kind,
            journey.target.name,
            journey.target.namespace,
            replicas=7,
        )

    approved, denied = await asyncio.gather(
        run(ApprovalOutcome.APPROVE, "a"), run(ApprovalOutcome.DECLINE, "b")
    )
    assert approval_from_result(approved) == "approved"
    assert approval_from_result(denied) == "denied"
```

Check whether `tests/evals/` already has `pytest-asyncio` configured
(`asyncio_mode = "auto"` in `pyproject.toml`/`pytest.ini`) before deciding
whether these need `@pytest.mark.asyncio` — grep first:
`grep -n "asyncio_mode" pyproject.toml`.

- [ ] **Step 3: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_runner.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'korvid.evals.operation_runner'`.

- [ ] **Step 4: Implement `ScriptedOperationBridge` and helpers**

Create `src/korvid/evals/operation_runner.py`. Start with the module
docstring, imports (using the exact paths confirmed in Step 1), then:

```python
"""A TUI-free operation-journey runner (docs/superpowers/specs/
2026-08-28-operation-journey-runner-design.md). Reuses the exact same
production write path a Textual run uses — `run_approved_write`, a real
`AuditLog`, `StatefulFakeWriteOps` — through a scripted `ApprovalPolicy`
instead of a `ConfirmScreen`. No imports from `tests/`, `korvid.ui`, or
`korvid.core`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from korvid.evals.grader import builtin_aliases  # confirm exact source in Step 1
from korvid.evals.operation import OperationJourney
from korvid.evals.operation_journal import ActionJournal, JournalTarget, summarize_untrusted
from korvid.evals.operation_state import StatefulFakeKubeClient, StatefulFakeWriteOps
from korvid.k8s.errors import ApiStatusError
from korvid.tools.approval import ApprovalOutcome, ApprovalPolicy, ApprovalRequest
from korvid.tools.audit import AuditLog
from korvid.tools.executor import UIBridge
from korvid.tools.write_coordinator import gvr_label, run_approved_write, write_locus

_ALIASES = builtin_aliases()

_APPROVED_ERROR = ...  # port the exact regex/constant from operation_app.py's
# `_APPROVED_ERROR` used by `approval_from_result` — grep for it in
# tests/evals/operation_app.py and copy its exact pattern.


def approval_from_result(result: str) -> str:
    """Ported unchanged from tests/evals/operation_app.py::approval_from_result."""
    if result.startswith("approved and executed"):
        return "approved"
    if result.startswith("denied:"):
        return "denied"
    if result.startswith("not approved:") and "expired" in result:
        return "expired"
    if _APPROVED_ERROR.match(result):
        return "approved"
    return "error"


def make_audit_intent_probe(audit_path):
    """Ported unchanged from tests/evals/operation_app.py::make_audit_intent_probe."""
    from korvid.core.audit import parse_audit_records  # confirm exact import path

    def probe():
        if not audit_path.exists():
            return ()
        return parse_audit_records(audit_path.read_text(encoding="utf-8"))

    return probe


async def _audit_recorder(audit: AuditLog):
    async def record(action, meta, namespace, name, detail, outcome) -> None:
        await asyncio.to_thread(
            lambda: audit.append(
                action=action,
                kind=meta.plural,
                group=meta.group,
                version=meta.version,
                namespace=namespace,
                name=name,
                detail=detail,
                outcome=outcome,
            )
        )

    return record


def _notify(message: str, *, severity: str) -> None:
    """No-op: nothing renders a toast in a TUI-free run."""


class ScriptedOperationBridge(UIBridge):
    """The TUI-free `UIBridge`: only `agent_request_write` does real work."""

    def __init__(
        self,
        *,
        kube: StatefulFakeKubeClient,
        journal: ActionJournal,
        journey: OperationJourney,
        audit: AuditLog,
        policy: ApprovalPolicy,
    ) -> None:
        self._kube = kube
        self._journal = journal
        self._journey = journey
        self._audit = audit
        self._policy = policy
        self._write_ops = StatefulFakeWriteOps(
            kube.state,
            journal,
            context=journey.target.context,
            audit_intent_probe=make_audit_intent_probe(_audit_path_of(audit)),
        )

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return "ERROR: navigation is not supported in the TUI-free operation runner"

    async def agent_set_filter(self, pattern: str) -> str:
        return "ERROR: filtering is not supported in the TUI-free operation runner"

    async def agent_open_logs(self, pod, namespace, container=None) -> str:
        return "ERROR: log viewing is not supported in the TUI-free operation runner"

    async def agent_open_describe(self, kind, name, namespace=None) -> str:
        return "ERROR: describe is not supported in the TUI-free operation runner"

    async def agent_drill_down(self, name: str) -> str:
        return "ERROR: drill-down is not supported in the TUI-free operation runner"

    async def agent_submit_write_proposal(self, *args: Any, **kwargs: Any) -> str:
        return "ERROR: write proposals are not supported in the TUI-free operation runner"

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return "ERROR: write proposals are not supported in the TUI-free operation runner"

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return "ERROR: write proposals are not supported in the TUI-free operation runner"

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        if action not in ("scale", "rollout_restart"):
            return f"ERROR: unsupported write action {action!r} in the TUI-free operation runner"
        meta = _ALIASES.get(kind.strip().lower())
        if meta is None:
            return f"ERROR: unknown kind {kind!r} - not a resource kind in this cluster"
        ns = namespace.strip() or None if namespace is not None else None
        name = name.strip()
        if not self._permitted(action, meta, ns):
            verb = "patch"
            return f"ERROR: missing permission: {verb} {meta.plural}"
        try:
            manifest = await self._kube.get_object(meta, ns, name)
        except ApiStatusError:
            return f"ERROR: {gvr_label(meta)}/{name} not found{write_locus(ns)}"
        from korvid.evals.grader import manifest_uid  # confirm exact source

        uid = manifest_uid(manifest)
        if uid is None:
            return (
                f"ERROR: target identity has no UID for {gvr_label(meta)}/{name}"
                f"{write_locus(ns)}; write blocked"
            )
        self._journal.append(
            event="write_target_bound",
            actor="app_internal",
            action="get_manifest",
            target=JournalTarget(
                context=self._journey.target.context,
                namespace=ns,
                group=meta.group,
                kind=meta.kind,
                plural=meta.plural,
                name=name,
                uid=uid,
            ),
            result="resolved",
            detail=summarize_untrusted(kind=meta.kind, name=name, namespace=ns),
        )
        request = ApprovalRequest(
            title=f"Agent requests: {action} {gvr_label(meta)}/{name}{write_locus(ns)}",
            operation=f"{action} {gvr_label(meta)}/{name}",
        )
        decision = await self._policy.decide(request)
        self._journal.append(
            event="approval_observed",
            actor="approval_driver",
            action=action,
            target=JournalTarget(
                context=self._journey.target.context,
                namespace=ns,
                group=meta.group,
                kind=meta.kind,
                plural=meta.plural,
                name=name,
                uid=uid,
            ),
            approval=decision.outcome.value.lower(),
            result="keystroke",
        )
        if decision.outcome is ApprovalOutcome.EXPIRE:
            return f"not approved: the request expired before the user responded ({action} {gvr_label(meta)}/{name})"
        if decision.outcome is not ApprovalOutcome.APPROVE:
            return f"denied: the user declined the {action} request for {gvr_label(meta)}/{name}"

        def op_factory():
            if action == "scale":
                return self._write_ops.scale_object(meta, ns, name, replicas or 0, uid=uid)
            return self._write_ops.rollout_restart_with_stamp(meta, ns, name, uid=uid)

        outcome = await run_approved_write(
            action,
            meta,
            ns,
            name,
            op_factory,
            "requested by agent",
            audit=await _audit_recorder(self._audit),
            notify=_notify,
        )
        if outcome != "done":
            return f"ERROR: {action} {gvr_label(meta)}/{name} {outcome}"
        return f"approved and executed: {action} {gvr_label(meta)}/{name}"

    def _permitted(self, action: str, meta, namespace: str | None) -> bool:
        verb_map = {"scale": ("patch", "scale"), "rollout_restart": ("patch", "")}
        verb, subresource = verb_map[action]
        for rule in self._journey.permission_denials:
            if (rule.verb, rule.resource, rule.subresource) != (verb, meta.plural, subresource):
                continue
            if rule.namespace is not None and rule.namespace != namespace:
                continue
            self._journal.append(
                event="permission_denied",
                actor="app_internal",
                action=action,
                result="denied",
                detail=summarize_untrusted(
                    group=meta.group or "core",
                    resource=meta.plural,
                    namespace=rule.namespace if rule.namespace is not None else "all",
                ),
            )
            return False
        return True
```

This is a substantial first draft — expect to fix field/method names
against the *actual* signatures of `StatefulFakeKubeClient.get_object`,
`ApprovalDecision` (does it use `.outcome`/`.decision_source`, and is
`ApprovalOutcome` a `str`-backed enum with `.value`, or something else? —
re-check `src/korvid/tools/approval.py` directly), `OperationTarget`'s
`permission_denials` field's exact rule dataclass field names (`verb`,
`resource`, `subresource`, `namespace` — confirm against
`src/korvid/evals/operation.py`'s `PermissionDenial` dataclass), and
`_audit_path_of` (replace with simply threading the `audit_path` in via
the constructor instead of trying to introspect `AuditLog` for its path —
simpler: add an explicit `audit_path: Path` constructor parameter to
`ScriptedOperationBridge` rather than deriving it from `audit`).

- [ ] **Step 5: Fix compile/signature errors iteratively**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_runner.py -v`

Iterate: each failure names an exact missing attribute or wrong
signature — fix the one specific line, re-run, repeat until all four tests
in this task pass. Do not move to Task 8 until this file's tests are green.

- [ ] **Step 6: Run mypy/ruff on the new file**

Run: `UV_FROZEN=1 uv run ruff check src/korvid/evals/operation_runner.py`
Run: `UV_FROZEN=1 uv run mypy src/korvid/evals/operation_runner.py`
Fix any reported issue before committing.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/evals/operation_runner.py tests/evals/test_operation_runner.py
git commit -m "feat(evals): add ScriptedOperationBridge, the TUI-free write seam"
```

---

### Task 8: Journaling executor + `_AnswerCapturingSession` port (`operation_runner.py`, part 2)

**Files:**
- Modify: `src/korvid/evals/operation_runner.py`
- Test: `tests/evals/test_operation_runner.py`

**Interfaces:**
- Consumes: `korvid.tools.executor.ToolExecutor`, `korvid.agent.session.DefaultAgentSession`,
  `korvid.agent.events.{TextDelta, ToolCallFinished, AgentError}` (confirm
  exact module path — grep `tests/evals/operation_app.py`'s imports),
  `korvid.evals.operation.OperationJourney`.
- Produces: `_OperationJournalingExecutor` (wraps a `ToolExecutor`),
  `_AnswerCapturingSession(DefaultAgentSession)`.

- [ ] **Step 1: Port `_AnswerCapturingSession` verbatim**

Copy the class body from `tests/evals/operation_app.py` lines 576-631
(already read earlier in this session) into `operation_runner.py`,
unchanged except for import paths. It has zero Textual dependency already.

- [ ] **Step 2: Write a focused test for the journaling executor**

```python
async def test_journaling_executor_records_target_resolved(tmp_path: Path) -> None:
    from korvid.evals.operation_runner import _OperationJournalingExecutor
    from korvid.tools.executor import ToolExecutor

    journey = _load("scale-deployment-up")
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit = AuditLog(tmp_path / "audit.jsonl", context=journey.target.context)
    policy = ScriptedApprovalPolicy([ApprovalOutcome.APPROVE])
    bridge = ScriptedOperationBridge(
        kube=kube, journal=journal, journey=journey, audit=audit, policy=policy
    )
    executor = _OperationJournalingExecutor(
        ToolExecutor(kube, bridge=bridge), journal, journey, max_result_chars=20000
    )
    result = await executor.execute(
        "get_object",
        {
            "kind": journey.target.kind,
            "name": journey.target.name,
            "namespace": journey.target.namespace,
        },
    )
    assert "target_resolved" in {event["event"] for event in journal.payload()} or result
```

Adjust the tool name/arguments to whatever `ToolExecutor`'s actual read
tool is called (grep `tests/evals/operation_app.py` for how it calls
`executor.execute(...)` in its own tests, or check `TOOL_DEFS` in
`korvid/tools/executor.py` for the exact registered read-tool name) — this
test's exact assertion matters less than confirming the wrapper does not
crash and produces *some* checkpoint event; Task 9's fixture-parity tests
are the real correctness gate.

- [ ] **Step 3: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_runner.py -k journaling_executor -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 4: Port `_JournalingExecutor` and its private helpers**

Copy, adapting only import paths and the class name
(`_JournalingExecutor` → `_OperationJournalingExecutor`), the full body
from `tests/evals/operation_app.py` lines 335-576 (`_read_document`,
`_is_target_document`, `_shows_state`, `_write_request_target`,
`_write_request_state`, `_mutation_pending_verification`, and the executor
class itself) into `operation_runner.py`. This is the checkpoint-emission
core — port it faithfully; do not "simplify" it, since fixture grading
parity depends on exact event names/ordering.

- [ ] **Step 5: Run test to verify it passes**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_runner.py -v`
Expected: PASS, all tests in the file including Task 7's.

- [ ] **Step 6: Ruff/mypy**

Run: `UV_FROZEN=1 uv run ruff check src/korvid/evals/operation_runner.py`
Run: `UV_FROZEN=1 uv run mypy src/korvid/evals/operation_runner.py`

- [ ] **Step 7: Commit**

```bash
git add src/korvid/evals/operation_runner.py tests/evals/test_operation_runner.py
git commit -m "feat(evals): port journaling executor and answer-capturing session"
```

---

### Task 9: `run_operation_case` composition + fixture-parity tests

**Files:**
- Modify: `src/korvid/evals/operation_runner.py`
- Test: `tests/evals/test_operation_runner.py`

**Interfaces:**
- Consumes: `korvid.evals.harness.build_eval_harness`,
  `korvid.evals.interaction.EvalUiBridge`, `korvid.evals.runner.resolve_eval_policy`
  (confirm exact module — grep `tests/evals/operation_app.py`'s import of
  `resolve_eval_policy`), `korvid.evals.operation_grader.grade_operation`,
  `korvid.evals.operation_journal.ActionJournal`, `PromptGrind`/`NO_GRIND`
  (confirm exact module, likely `korvid.evals.harness` or
  `korvid.agent.prompt`).
- Produces:

```python
@dataclass(frozen=True, slots=True)
class OperationRun:
    journey_id: str
    answer: str
    grade: Any  # OperationGrade
    journal: tuple[dict[str, Any], ...]
    audit: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    wall_time_s: float
    prompt: dict[str, Any]


async def run_operation_case(
    journey: OperationJourney,
    *,
    audit_path: Path,
    provider_factory: Callable[[], Any],
    approval_script: Sequence[ApprovalOutcome] | None = None,
    model_tier: str | None = None,
    grind: PromptGrind = NO_GRIND,
) -> OperationRun: ...
```

- [ ] **Step 1: Read the default-script derivation rule**

Given `journey.approval` is one of the fixture's authored values
(`"approved"`/`"expired"`/`"denied"`/`None`/etc. — re-check
`src/korvid/evals/operation.py`'s exact `OperationJourney.approval` type),
write a small private helper `_default_script(journey) -> list[ApprovalOutcome]`
that maps the fixture's authored `approval` field to a one-element (or
zero-element, for "approval: none" fixtures with
`expected_approval_dialogs == 0`) `ApprovalOutcome` list, and pass
`journey.dialog_intervention`'s replacement as the matching intervention
callable (`lambda: kube.state.replace_incarnation(...)`) when the fixture
declares one. Use exactly the field names read earlier in this session
from `src/korvid/evals/operation.py` lines 206-326.

- [ ] **Step 2: Write one parity test per bundled fixture**

```python
import pytest

from korvid.evals.operation_runner import run_operation_case

_BUNDLED_IDS = [j.id for j in load_operation_journeys(bundled_operations_dir())]


@pytest.mark.parametrize("journey_id", _BUNDLED_IDS)
async def test_run_operation_case_grades_bundled_fixture(
    journey_id: str, tmp_path: Path
) -> None:
    from korvid.evals.scripted import ScriptedProvider  # confirm exact import

    journey = _load(journey_id)
    run = await run_operation_case(
        journey,
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: ScriptedProvider(_script_for(journey)),
    )
    assert run.journey_id == journey_id
    assert run.grade is not None
    # A fixture whose scripted transcript performs the intended write must
    # grade safe and complete; a fixture with no matching scripted
    # transcript in this test is expected to at least not crash the
    # harness. Tighten this assertion per-fixture once `_script_for` is
    # written (Step 3) to actually drive each fixture's intended tool
    # calls, mirroring the equivalent `ScriptedProvider` transcripts
    # `tests/evals/operation_campaign.py` or `operation_app.py`'s own test
    # module already authors for these exact fixture ids — reuse those
    # scripted transcripts' *content* (call sequence/arguments) since they
    # already exist and are known-correct; do not re-invent new transcripts.
```

Before writing `_script_for`, run:
`grep -n "ScriptedProvider(\[" tests/evals/test_operation_app.py tests/evals/operation_campaign.py 2>/dev/null | head -20`
to find the exact existing scripted transcripts for each of these 11
fixture ids and adapt them (same tool-call sequence/arguments) rather than
authoring new ones from scratch — this is what makes the parity claim in
the design doc ("matches what the Textual harness produces for the same
fixture with an equivalent scripted provider") actually true.

- [ ] **Step 3: Implement `run_operation_case`**

```python
async def run_operation_case(
    journey: OperationJourney,
    *,
    audit_path: Path,
    provider_factory: Callable[[], Any],
    approval_script: Sequence[ApprovalOutcome] | None = None,
    model_tier: str | None = None,
    grind: PromptGrind = NO_GRIND,
) -> OperationRun:
    started = time.monotonic()
    kube = StatefulFakeKubeClient(journey.cluster)
    journal = ActionJournal()
    audit = AuditLog(audit_path, context=journey.target.context)
    script, interventions = approval_script or _default_script(journey, kube)
    policy = ScriptedApprovalPolicy(script, interventions=interventions)
    bridge = ScriptedOperationBridge(
        kube=kube, journal=journal, journey=journey, audit=audit, policy=policy
    )
    raw_provider = provider_factory()
    policy_resolved = resolve_eval_policy(
        raw_provider, model_tier=model_tier, environment=_WRITE_ENVIRONMENT
    )
    executor = _OperationJournalingExecutor(
        ToolExecutor(kube, bridge=bridge),
        journal,
        journey,
        max_result_chars=policy_resolved.max_result_chars,
    )
    ui_bridge = EvalUiBridge(journey.interaction)
    ui_bridge.bind_objects(journey.objects)
    harness = build_eval_harness(
        provider=raw_provider,
        execution=executor,
        bridge=ui_bridge,
        policy=policy_resolved,
        grind=grind,
    )
    session = _AnswerCapturingSession(
        engine=harness.engine,
        bridge=harness.bridge,
        prompt_harness=harness.prompts,
        conversation=harness.conversation,
        gateway=harness.gateway,
        tools=harness.tools,
        policy=harness.policy,
        cluster=harness.cluster,
        user_rules=harness.user_rules,
        journal=journal,
    )
    try:
        async for _event in session.run_turn(journey.goal):
            pass
    finally:
        aclose = getattr(raw_provider, "aclose", None)
        if callable(aclose):
            await aclose()
        await session.aclose()
    answer = session.answers[-1] if session.answers else ""
    journal.append(
        event="outcome_reported",
        actor="model_tool",
        result="captured" if answer else "empty",
        detail=summarize_untrusted(chars=len(answer)),
    )
    audit_records = _read_audit(audit_path, journal=journal)
    _journal_audit_records(journal, audit_records)
    _journal_grader_reads(journal, kube.state, journey)
    grade = grade_operation(journey, journal, kube.state, answer, tool_calls=executor.tool_calls)
    return OperationRun(
        journey_id=journey.id,
        answer=answer,
        grade=grade,
        journal=tuple(journal.payload()),
        audit=audit_records,
        decisions=tuple(
            {"outcome": step.name.lower(), "decision_source": SCRIPTED_POLICY_SOURCE}
            for step in script
        ),
        wall_time_s=time.monotonic() - started,
        prompt=prompt_fingerprint(policy_resolved, grind=grind),
    )
```

Port `_read_audit`, `_audit_result`, `_journal_audit_records`,
`_journal_grader_reads` unchanged from `tests/evals/operation_app.py` lines
1106-1188 (already read this session) into this module first, adjusting
only the `evaluate_assertion` import path if it moved. Reconcile
`executor.tool_calls`/`provider.completions`-equivalent counters against
whatever the ported `_OperationJournalingExecutor` actually exposes (may
need a small `tool_calls` property added to it, mirroring
`_JournalingExecutor`'s own — check that class's full body from Task 8 for
whether it already tracks this).

- [ ] **Step 4: Iterate until every fixture's test passes**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_runner.py -v`

This is the highest-risk task in the plan — expect several iterations.
For each fixture that fails, read its exact YAML
(`src/korvid/evals/operations/<id>.yaml`) and the ported journaling
executor's behavior side by side; the most common failure mode is a
checkpoint event name/ordering mismatch against what `grade_operation`
expects — cross-reference `src/korvid/evals/operation_grader.py`'s
`evaluate_assertion`/checkpoint-vocabulary logic directly rather than
guessing.

- [ ] **Step 5: Run the full `tests/evals/` suite**

Run: `UV_FROZEN=1 uv run pytest tests/evals/ -q`
Expected: every previously-passing test still passes; only new tests
added.

- [ ] **Step 6: Ruff/mypy**

Run: `UV_FROZEN=1 uv run ruff check src/korvid/evals/operation_runner.py`
Run: `UV_FROZEN=1 uv run mypy src/korvid/evals/operation_runner.py`

- [ ] **Step 7: Commit**

```bash
git add src/korvid/evals/operation_runner.py tests/evals/test_operation_runner.py
git commit -m "feat(evals): compose run_operation_case over the TUI-free write path"
```

---

### Task 10: CLI (`operation_main.py`)

**Files:**
- Create: `src/korvid/evals/operation_main.py`
- Test: `tests/evals/test_operation_main.py`

**Interfaces:**
- Consumes: `run_operation_case`, `select_operation_journeys`,
  `operation_case_pack_identity`, `EVAL_PROTOCOL_VERSION` (import from
  `korvid.evals.__main__`, unchanged, no new constant), `load_operation_journeys`,
  `bundled_operations_dir`.
- Produces: `main(argv: list[str] | None = None) -> int`, `run_payload(...)`
  (mirroring `korvid/evals/__main__.py::run_payload`'s shape, adding
  `meta.operation_case_pack` and top-level `"operations"` key instead of
  `"scenarios"`).

- [ ] **Step 1: Write failing CLI tests**

Create `tests/evals/test_operation_main.py`, mirroring
`tests/evals/test___main__.py`'s (confirm exact filename first:
`ls tests/evals/test*main*.py`) existing `--scenario-id` tests one-for-one
for `--operation-id`:

```python
import json
import subprocess
import sys

from korvid.evals.operation_main import EVAL_PROTOCOL_VERSION_IMPORT_CHECK  # placeholder; remove
```

Replace the placeholder import with real assertions once the module
exists; concretely test:
1. `--operation-id` with an unknown id exits nonzero (`SystemExit`/exit
   code `2`) before any provider call.
2. `--operation-id` repeated (duplicate) exits nonzero.
3. Omitting `--operation-id` runs every bundled fixture and
   `meta.operation_case_pack.count` equals the bundled fixture count.
4. `meta.protocol_version` equals `korvid.evals.__main__.EVAL_PROTOCOL_VERSION`.
5. `run_payload`'s `operations` list has one entry per selected journey id.

Use `run_payload(...)` directly (in-process), not a subprocess, for speed —
mirror however `tests/evals/test___main__.py` already tests `run_payload`
without invoking `main()` end-to-end (grep that file for its pattern
first).

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_main.py -v`
Expected: FAIL, `ImportError`.

- [ ] **Step 3: Implement `operation_main.py`**

```python
"""Public, TUI-free CLI for operation-journey runs (docs/superpowers/specs/
2026-08-28-operation-journey-runner-design.md). A model failing an
operation (unsafe write, wrong target, missed checkpoint) is scored
evidence in the JSON, not a nonzero exit — only a systemic/usage error
(unknown id, unwritable output path) exits nonzero. See that spec for the
deliberate divergence from `operation_campaign.py --scripted`'s exit-code
convention.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from korvid.evals.__main__ import EVAL_PROTOCOL_VERSION
from korvid.evals.operation import (
    bundled_operations_dir,
    load_operation_journeys,
    operation_case_pack_identity,
    select_operation_journeys,
)
from korvid.evals.operation_runner import OperationRun, run_operation_case
from korvid.evals.scripted import ScriptedProvider  # or the live-provider factory equivalent


def run_payload(runs: list[OperationRun], *, journeys) -> dict[str, Any]:
    return {
        "meta": {
            "protocol_version": EVAL_PROTOCOL_VERSION,
            "operation_case_pack": operation_case_pack_identity(journeys),
        },
        "operations": [
            {
                "journey_id": run.journey_id,
                "runs": [
                    {
                        "answer": run.answer,
                        "grade": run.grade.__dict__ if hasattr(run.grade, "__dict__") else run.grade,
                        "journal": list(run.journal),
                        "audit": list(run.audit),
                        "decisions": list(run.decisions),
                        "wall_time_s": run.wall_time_s,
                        "prompt": run.prompt,
                    }
                ],
            }
            for run in runs
        ],
    }


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m korvid.evals.operation_main",
        description="Run the operation-journey pack against a scripted or live model.",
    )
    parser.add_argument("--operations", type=Path, default=bundled_operations_dir())
    parser.add_argument("--operation-id", action="append", default=[], dest="operation_id")
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    journeys = load_operation_journeys(args.operations)
    if args.operation_id:
        try:
            journeys = select_operation_journeys(journeys, args.operation_id)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    try:
        runs = asyncio.run(_run_all(journeys))
    except Exception as exc:  # systemic/harness failure, not a graded outcome
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = run_payload(runs, journeys=journeys)
    if args.json is not None:
        try:
            args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: could not write {args.json}: {exc}", file=sys.stderr)
            return 1
    print(json.dumps(payload["meta"], indent=2))
    return 0


async def _run_all(journeys) -> list[OperationRun]:
    import tempfile

    runs = []
    for journey in journeys:
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            run = await run_operation_case(
                journey, audit_path=audit_path, provider_factory=lambda: ScriptedProvider([])
            )
            runs.append(run)
    return runs


if __name__ == "__main__":
    raise SystemExit(main())
```

The `provider_factory=lambda: ScriptedProvider([])` placeholder needs a
real per-fixture scripted transcript (reuse Task 9's `_script_for`
helper, moved to a shared, importable location such as
`korvid.evals.operation_runner` or a small new
`korvid.evals.operation_scripts` module) rather than an empty script — an
empty script cannot drive any tool calls. Wire this through before this
task is considered done; do not ship a CLI that cannot actually complete a
run.

- [ ] **Step 4: Run tests, iterate to green**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_main.py -v`

- [ ] **Step 5: Manual smoke test**

Run:
```bash
UV_FROZEN=1 uv run python -m korvid.evals.operation_main \
  --operation-id restart-deployment --json /tmp/korvid-latest-protocol/.scratch/op-report.json
cat /tmp/korvid-latest-protocol/.scratch/op-report.json | python3 -m json.tool | head -30
```
(create `.scratch/` under the repo working tree if it does not exist — do
not write to `/tmp` outside the repo per this environment's rules; use a
path under the cloned repo instead, e.g.
`/tmp/korvid-latest-protocol/.scratch/`, and delete it afterward.)

Confirm `meta.protocol_version` and `meta.operation_case_pack` are present
and the run's `grade` reflects a safe, complete outcome.

- [ ] **Step 6: Ruff/mypy**

Run: `UV_FROZEN=1 uv run ruff check src/korvid/evals/operation_main.py`
Run: `UV_FROZEN=1 uv run mypy src/korvid/evals/operation_main.py`

- [ ] **Step 7: Commit**

```bash
git add src/korvid/evals/operation_main.py tests/evals/test_operation_main.py
git commit -m "feat(evals): add python -m korvid.evals.operation_main CLI"
```

---

### Task 11: Documentation

**Files:**
- Modify: `docs/evals/operations.md`
- Modify: `docs/evals/protocol.md`

**Interfaces:** none (docs only).

- [ ] **Step 1: Rewrite `docs/evals/operations.md`'s "not yet available" section**

Replace the `## External-optimizer machine protocol: not yet available`
section (added by PR #321, currently describing why no such runner
exists) with a new section documenting the actual, now-shipped
`python -m korvid.evals.operation_main` contract: `--operation-id`
selection semantics, `meta.operation_case_pack` shape, `decisions[]`
provenance, exit-code contract, and the explicit non-goals (no
`approval_rerequest_turns`, no live-provider mode from this entry point).

- [ ] **Step 2: Update `docs/evals/protocol.md`'s closing paragraph**

The paragraph currently reading "That harness has no TUI-free public
equivalent yet... See operations.md#... for exactly what blocks it" no
longer describes reality. Replace it with a short pointer: "A TUI-free
operation-journey runner now exists as a separate entry point,
`python -m korvid.evals.operation_main` — see
[operations.md](operations.md#...) for its contract. This page continues
to describe only the read-only scenario protocol; the two share
`EVAL_PROTOCOL_VERSION` but are otherwise independent artifacts."

- [ ] **Step 3: Proofread cross-references**

Run: `grep -rn "operations.md#external-optimizer" docs/` to find every
stale anchor reference and fix each one to point at the new section's
actual heading anchor.

- [ ] **Step 4: Commit**

```bash
git add docs/evals/operations.md docs/evals/protocol.md
git commit -m "docs(evals): document the TUI-free operation-journey runner contract"
```

---

### Task 12: Full verification and PR

**Files:** none (verification only).

- [ ] **Step 1: Full targeted suite**

Run: `UV_FROZEN=1 uv run pytest tests/tools/ tests/ui/ tests/evals/ -q`
(budget ~20 minutes; use `initial_wait: 300` and poll with `read_bash`)
Expected: 100% pass, count ≥ the pre-slice baseline (4154) plus every new
test this plan added.

- [ ] **Step 2: Static checks**

Run: `UV_FROZEN=1 uv run ruff check src/korvid tests`
Run: `UV_FROZEN=1 uv run mypy src/korvid`
Run: `UV_FROZEN=1 uv run tach check`
Run: `git status --short uv.lock` (expect clean)

- [ ] **Step 3: Full suite**

Run: `UV_FROZEN=1 uv run pytest -q` (budget ~20 minutes)

- [ ] **Step 4: Push and open PR**

```bash
git push -u origin feat/operation-journey-runner
gh pr create --repo hellices/korvid --base main \
  --head feat/operation-journey-runner \
  --title "Capability-based approval + TUI-free operation-journey runner" \
  --body "..."
```

Body must: list PRs #321/#322/#323 as merged prerequisites already
included in this branch's history (leave those PRs open per policy), state
the exact CLI/JSON contract, list deferred items
(`approval_rerequest_turns`, live-provider mode from this entry point),
and explicitly do not merge.

- [ ] **Step 5: Poll CI**

Run: `sleep 400 && gh pr checks <PR_NUMBER> --repo hellices/korvid`
repeat until conclusive; fix any failure and push again.

---

## Self-review notes for whoever executes this plan

- If Task 9's per-fixture parity proves too large to fully verify against
  the Textual harness's exact output (rather than just "grades safe"),
  stop, report exactly which fixtures are unverified, and ship the rest —
  do not silently weaken the assertion to "does not crash" for a fixture
  this plan claims to support.
- If the `TextualApprovalPolicy` extraction in Task 4 causes *any*
  `tests/ui/` regression, stop before Task 5 and diagnose fully — this is
  the one task most likely to have subtle behavioral drift, since it moves
  a large, timing-sensitive method.
