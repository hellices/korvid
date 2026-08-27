# TUI-free operation-journey runner + capability-based approval

## Problem

Three prior slices in this project built the pieces this one assembles:

1. PR #321 gave `python -m korvid.evals` a stable, versioned machine
   protocol for **read-only diagnostic scenarios** — but explicitly
   documented (`docs/evals/protocol.md`) that it "does not cover, and must
   not be read as replacing" the stateful *operation* journeys (scale and
   restart flows gated behind the real approval dialog) that
   `tests/evals/operation_app.py`/`operation_campaign.py` exercise.
2. PR #322 gave the operation harness a first-class `PromptGrind` override,
   eliminating monkeypatching of `build_profile`, but did not touch how
   approval decisions are produced or how the harness is composed.
3. PR #323 extracted `ApprovalDecision`/`run_approved_write` out of
   `korvid.ui.write_coordinator` into pure, Textual-free modules under
   `korvid.tools` — but left approval *decision-making* itself entirely
   inside `agent_ui_controller.py`'s Textual dialog flow, and gated the one
   existing string tag (`decision_source == "tui_keystroke"`) with a check
   that is **forgeable**: nothing stops a second caller from constructing an
   `ApprovalDecision` with that exact string and never touching a dialog.

An independent review of PR #323 blocked the next slice on exactly that:
*"source strings are forgeable and `run_approved_write` accepts no
decision; operation eval must use a composition-root approval
policy/capability, with production exclusively binding Textual."*

Today, `tests/evals/operation_app.py` is the **only** way to run an
operation journey end to end, and it is Textual-mandatory: it builds a real
`KorvidApp`, drives it with `Pilot`, and presses keys against a rendered
`ConfirmScreen`. `korvid-prompt-lab` cannot use it without importing
`tests/` and monkeypatching Textual internals — the exact AgentPanel race
this whole project exists to eliminate.

## Goal (this slice)

1. Replace the forgeable string gate with a **capability/policy**
   architecture: trust is established by *which concrete `ApprovalPolicy`
   object a composition root bound*, not by a string compared at the point
   of use.
2. Build a public, TUI-free operation-journey runner in `src/korvid/evals`
   that reuses the exact same production write path (`run_approved_write`,
   real `AuditLog`, `StatefulFakeWriteOps`) a Textual-driven run uses, with
   no imports from `tests/`, `korvid.ui`, or `korvid.core`, and no
   `tach.toml` change.
3. Publish a versioned, deterministic JSON contract for operation runs —
   `EVAL_PROTOCOL_VERSION`, case-pack identity, prompt identity, checkpoints,
   audit provenance — as an **additive** extension of the existing
   external-optimizer protocol (no bump; nothing already published moves
   or changes meaning).
4. Do all of this without weakening `SAFETY_CONTRACT`, without widening the
   armed tool surface, and without changing `run_approved_write`'s already-
   reviewed signature.

## Non-goals

- No live-provider/campaign-scale mode (that stays in
  `tests/evals/operation_campaign.py`).
- No `approval_rerequest_turns` support: no bundled fixture uses it, and
  the scripted policy's one-shot-per-request model has nothing to
  re-request against. Documented as an explicit deferred item.
- No change to `korvid.ui`'s public behavior or its existing test suite
  (every `tests/ui/` assertion must keep passing unmodified).
- No coordinator/`ViewState` extraction beyond what PR #323 already did.
- No delete/resize/edit operation support: no bundled fixture exercises
  them, and `edit-unsupported.yaml` specifically expects "edit" to be an
  *unsupported* action, not a wired one.

## Architecture

### 1. `ApprovalPolicy` capability (`korvid/tools/approval.py`)

```python
@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    title: str
    operation: str
    require_name: str | None = None
    preview: str | None = None
    managed_note: str | None = None
    impact_lines: tuple[str, ...] | None = None


class ApprovalPolicy(ABC):
    @abstractmethod
    async def decide(self, request: ApprovalRequest) -> ApprovalDecision: ...
```

`ApprovalDecision` and `ApprovalOutcome` are PR #323's existing types,
unchanged. A concrete policy's `decide()` is free to look at `request` or
ignore it entirely — a scripted policy authored against a fixture ignores
it and returns its next pre-authored outcome.

**Why this satisfies "do not treat a string as authorization":** nothing
downstream of `decide()` ever compares `decision.decision_source` to decide
whether to trust the result. Trust is established once, at composition
time, by which concrete class the composition root instantiated —
verified by `isinstance` in a composition test, not by string comparison
at the point a write is authorized. `decision_source` remains on
`ApprovalDecision` as a durable audit/debugging fact (which policy produced
this decision), never as a gate.

`SCRIPTED_POLICY_SOURCE: Final = "scripted_policy"` is a new, distinct
constant — never equal to `TUI_KEYSTROKE_SOURCE` — tagging every decision
`ScriptedApprovalPolicy` returns.

`ScriptedApprovalPolicy(ApprovalPolicy)`:
- Constructed with an ordered sequence of `ApprovalOutcome` (one per
  expected `decide()` call) and, optionally, a same-length sequence of
  intervention callables (`Callable[[], None] | None`) invoked immediately
  before a step's outcome is returned, to model a fixture's declared
  `dialog_intervention` deterministically — no dialog exists to intervene
  "during", so the intervention runs at the one point that stands in for
  it.
- Fully deterministic: no `asyncio.sleep`, no timers. `decide()` pops the
  next scripted step synchronously (`await` only so the interface matches
  a real dialog wait).
- **Fails closed** when the script is exhausted: an unexpected extra
  `decide()` call (a write the fixture did not anticipate) returns
  `ApprovalOutcome.DECLINE`, never raises, never silently approves. This
  matches every "approval: none" fixture's expectation that an unrequested
  write is refused, not crashed on.

### 2. `TextualApprovalPolicy` (`korvid/ui/agent_ui_controller.py`)

`_await_user_approval`'s existing body (deadline wait, surfaceability
check, `ConfirmScreen` push, cancellation/timeout handling) is extracted
**verbatim** into `TextualApprovalPolicy.decide()`, a new class in the same
module holding a reference to the owning `AgentUiController`. Production
binds exactly one instance in `AgentUiController.__init__`:

```python
self._approval_policy: ApprovalPolicy = TextualApprovalPolicy(self)
```

`_await_user_approval` becomes a thin wrapper: build an `ApprovalRequest`
from its existing parameters, `await self._approval_policy.decide(request)`,
collapse exactly as before via the existing (unmodified)
`_collapse_decision`/`require_tui_keystroke_source` — now applied only to
`TextualApprovalPolicy`'s own output, as an internal self-consistency
assertion (defense-in-depth: if this policy ever produced a decision not
tagged `tui_keystroke`, that is a bug in this class, not a generic gate on
arbitrary callers). Every existing `tests/ui/` assertion about dialog
behavior, key freshness, typed-name gating, dismiss vs. decline, shared
expiry, and cancellation passes unmodified, because the code path they
exercise is unchanged text moved to a new method.

### 3. `run_approved_write` — unchanged

No signature change. Both the Textual and the scripted path obtain a
decision from their own bound `ApprovalPolicy` first, and call
`korvid.tools.write_coordinator.run_approved_write` (already public,
already reviewed) only on `ApprovalOutcome.APPROVE`. This keeps 100% of PR
#323's reviewed code untouched and confines this slice's new risk to new
code.

### 4. `StatefulFakeWriteOps` audit-intent enforcement

`src/korvid/evals/operation_state.py`'s `_observe_audit_intent` today only
*journals* what it reads from the audit file — it never refuses a
mutation. This slice changes it to **fail-closed**: if the audit-intent
probe finds no record matching the pending action/target immediately
before the mutation would run, `StatefulFakeWriteOps` raises
`ApiStatusError` instead of proceeding. `run_approved_write` already writes
the intent record *before* calling the op factory (see its docstring), so
this only rejects a mutation that reaches the fake ops layer through some
path other than the audited one — exactly the gap requirement (5) closes.
UID-conflict enforcement (`_resolve`, 409 on mismatch) already exists and
is unchanged.

### 5. New `korvid.tools.executor.UIBridge` for evals

A new class, `ScriptedOperationBridge(UIBridge)`, lives in
`src/korvid/evals/operation_runner.py`. It implements the write-relevant
methods for real and returns short, honest "not supported in this runner"
strings for the navigation-only methods no operation fixture needs
(`agent_navigate`, `agent_set_filter`, `agent_open_logs`,
`agent_open_describe`, `agent_drill_down`, the write-proposal trio) —
`korvid.tools.executor.UIBridge` already lives outside `korvid.ui`, so this
requires **no `tach.toml` change** (`korvid.evals` already depends on
`korvid.tools`).

`agent_request_write`:
1. Validates `action` is `"scale"` or `"rollout_restart"` (the only two
   bundled-fixture actions); anything else returns
   `"ERROR: unsupported write action ..."` — this is what makes
   `edit-unsupported.yaml` grade correctly with no special-casing.
2. Resolves the resource alias via `korvid.evals.grader`'s existing
   `builtin_aliases()` (already public, already used by the read-only
   harness) — no `korvid.ui` import needed for alias resolution.
3. Applies the fixture's `permission_denials` (the RBAC fixture) via a
   ported, unchanged copy of `_make_check_permission`'s logic.
4. Resolves the target manifest/uid directly from `StatefulFakeKubeClient`
   (no `KorvidApp`/`target_manifest` indirection needed — the fake client
   already exposes `get_object`).
5. Builds an `ApprovalRequest`, applies the fixture's `dialog_intervention`
   (if declared) via the scripted policy's intervention hook immediately
   before the decision is returned, then calls
   `self._policy.decide(request)`.
6. On `DECLINE`/`DISMISS`/`EXPIRE`, returns the same
   `"denied: ..."`/`"not approved: ... expired ..."` strings production
   returns (so `approval_from_result`, ported unchanged, still classifies
   them identically).
7. On `APPROVE`, calls `run_approved_write(...)` with an `op_factory`
   built from `StatefulFakeWriteOps.scale_object`/
   `rollout_restart_with_stamp` (which already self-validate resource-kind
   support, so no separate `SCALABLE`/`RESTARTABLE` table needs to be
   duplicated from `korvid.ui`), an `audit` recorder wrapping the real
   `AuditLog.append` via `asyncio.to_thread`, and a no-op `notify`.

### 6. Journaling parity (`src/korvid/evals/operation_runner.py`)

A new `_OperationJournalingExecutor` wraps a `ToolExecutor` exactly as
`tests/evals/operation_app.py::_JournalingExecutor` does — re-derived (not
imported, since `tests/` is off-limits), preserving every checkpoint event
name and semantics graded by `grade_operation`: `target_resolved`,
`precondition_read`/`postcondition_read`, `write_requested`,
`off_target_read`, `read_without_state`. `_AnswerCapturingSession` (already
Textual-free in the original) is ported verbatim, subclassing the same
`DefaultAgentSession`. `make_audit_intent_probe`, `approval_from_result`,
`_read_audit`, `_journal_audit_records`, `_journal_grader_reads` are ported
unchanged — none of them touch Textual.

### 7. Composition (`run_operation_case`)

Mirrors `run_operation_journey`'s exact composition sequence
(`resolve_eval_policy` → `ToolExecutor` → `build_eval_harness` → manual
session substitution with `_AnswerCapturingSession`), swapping only:
`bridge=` is `EvalUiBridge` (already public, unchanged, built from the
fixture's `interaction`/`objects` exactly as the read-only runners do) for
the `AgentUiBridge` screen-snapshot seam, and `ui=ScriptedOperationBridge`
(this slice's new class, bound with a `ScriptedApprovalPolicy` built from
the fixture's authored `approval`/`dialog_intervention`) for the
`ToolExecutor`'s write seam. No Pilot, no `KorvidApp`, no screen
navigation: the model resolves its target through its own tool calls,
exactly as the existing TUI-free read-only journey runner already does.

### 8. Selection + case-pack identity (`src/korvid/evals/operation.py`)

`select_operation_journeys(journeys, ids)` mirrors PR #321's
`select_scenarios` byte-for-byte in its fail-closed rules (empty → error,
duplicate → error, unknown → error, sorted result). `operation_case_pack_identity(journeys)`
mirrors `case_pack_identity`, reusing the exact same canonical encoder
(`korvid.evals.scenario._canonical_value`) over every field
`OperationJourney`/`OperationTarget`/etc. actually accept — no forbidden-
read field is claimed, matching the PR #321 review correction already
applied to `docs/evals/protocol.md`.

### 9. JSON contract (additive; `EVAL_PROTOCOL_VERSION` unchanged, still `"1.0"`)

```json
{
  "meta": {
    "protocol_version": "1.0",
    "policy": {"...": "..."},
    "limits": {"...": "..."},
    "capabilities": {"...": "..."},
    "catalog_version": "...",
    "prompts": {"pack": "...", "overlays": [], "source": "default", "sha256": "..."},
    "tools": {"...": "..."},
    "operation_case_pack": {
      "operation_ids": ["restart-deployment", "scale-deployment-up"],
      "count": 2,
      "sha256": "..."
    }
  },
  "operations": [
    {
      "journey_id": "restart-deployment",
      "runs": [
        {
          "answer": "...",
          "grade": {"...": "...", "hard_failures": []},
          "journal": [{"event": "...", "actor": "...", "...": "..."}],
          "audit": [{"action": "...", "outcome": "...", "...": "..."}],
          "decisions": [{"outcome": "approve", "decision_source": "scripted_policy"}],
          "wall_time_s": 0.01,
          "prompt": {"pack": "...", "overlays": [], "source": "default", "sha256": "..."}
        }
      ]
    }
  ]
}
```

`meta.operation_case_pack` is the operation analogue of `meta.case_pack`;
`decisions[]` is new, explicit **audit decision provenance** — which
policy produced each approval decision this run made, discoverable without
parsing the journal. `grade.hard_failures` names any checkpoint-graded
hard failure (`off_target_read`, `wrong_target_write`, `uid_conflict`,
`write_without_uid`, `unsupported_write`, `permission_denied` bypass) so a
consumer does not have to re-derive them from the raw journal either — it
lives inside `grade` (the shipped `OperationGrade` field), not as a
sibling of `grade` in the run entry.

### 10. CLI (`src/korvid/evals/operation_main.py`)

```sh
uv run python -m korvid.evals.operation_main \
  --operation-id restart-deployment --operation-id scale-deployment-up \
  --json report.json
```

A new, separate entry point (not folded into `korvid/evals/__main__.py`):
operation runs need a write-armed policy and a different composition than
the read-only scenario runner, and `korvid/evals/operations/` is already a
fixture *directory*, not an importable package, so the module is named
`operation_main.py` rather than `operations/__main__.py`. `--operation-id`
is repeatable and fail-closed exactly like PR #321's `--scenario-id`;
omitting it runs every bundled fixture, unchanged. Exit codes: `2` for a
usage/argument error, `1` for a systemic/harness error (a result artifact
could not be written, a provider could not be constructed), `0` whenever
every requested operation ran to a graded result — **a model failing an
operation (unsafe write, wrong target, missed checkpoint) is scored
evidence in the JSON, not a nonzero exit**, matching `python -m
korvid.evals`'s existing philosophy for scenario grading (requirement 11).
This is a deliberate, documented divergence from
`operation_campaign.py --scripted` mode's convention (which does exit `1`
on an unsafe/incomplete run): that script is a CI regression gate for
korvid's own suite, while this CLI is a scoring function for an external
optimizer that needs every requested run's result, safe or not, back as
data.

## Fixture coverage

All 12 bundled `src/korvid/evals/operations/*.yaml` fixtures are targeted:
straightforward scale/restart fixtures, the two approval-denial/expiry
fixtures, the RBAC/no-op/unsupported "approval: none" fixtures, the
neutral-selection fixture (expected to work identically to the target-
selection fixtures since there is no UI-context bias in a TUI-free run —
`initial_selection` becomes a no-op for this runner, verified empirically
by the test suite), and `scale-same-name-replacement.yaml` via the
intervention hook. The `approval_rerequested` journal event itself is
ported faithfully (one per turn whose index+1 is in a journey's
`approval_rerequest_turns`), but `_default_script` only ever authors one
approval outcome per run; no bundled fixture declares
`approval_rerequest_turns`, so the re-request path is exercised by the
journaling logic alone, not end to end by any bundled test.

## Testing

- `ApprovalRequest`/`ApprovalPolicy`/`ScriptedApprovalPolicy`: unit tests
  for scripted sequencing, script-exhausted fail-closed behavior, the
  intervention hook, and that its `decision_source` is never
  `TUI_KEYSTROKE_SOURCE`.
- `TextualApprovalPolicy` extraction: the full existing `tests/ui/`
  approval/dialog suite passes unmodified; a new composition test asserts
  `isinstance(controller._approval_policy, TextualApprovalPolicy)` in
  production and that no other policy type is ever bound there.
- `StatefulFakeWriteOps` audit-intent enforcement: a test that calls
  `scale_object`/`rollout_restart_with_stamp` with no matching audit-intent
  record present asserts a fail-closed `ApiStatusError`, alongside the
  existing UID-conflict test (unchanged).
- `select_operation_journeys`/`operation_case_pack_identity`: mirrors PR
  #321's scenario-selection/hash test suite (empty/duplicate/unknown
  selection, hash determinism, hash change on content edit, hash
  independence from path/mtime).
- `run_operation_case`: one test per bundled fixture (11), asserting the
  graded outcome matches what `tests/evals/operation_app.py`'s Textual
  harness produces for the same fixture with an equivalent scripted
  provider/approval script, plus a composition test proving the eval path
  binds `ScriptedApprovalPolicy` (never `TextualApprovalPolicy`) and that
  two concurrent `run_operation_case` calls (`asyncio.gather`) cannot leak
  one run's scripted policy/decisions into the other's journal or audit
  file.
- CLI (`operation_main.py`): `--operation-id` selection tests mirroring
  PR #321's `--scenario-id` tests; JSON contract shape tests
  (`protocol_version`, `operation_case_pack`, `decisions`, backward-
  compatible omission path).

## Risks / open questions

- Re-deriving `_JournalingExecutor`'s checkpoint semantics without
  importing it risks subtle drift from the Textual harness's grading. Each
  fixture gets a direct parity assertion against the existing harness'
  behavior to catch this.
- `korvid.evals` cannot import `korvid.core.audit.AuditLog` directly
  (tach: `korvid.evals` does not depend on `korvid.core`). Resolved by a
  one-line re-export, `korvid.tools.audit.AuditLog = korvid.core.audit.AuditLog`,
  since `korvid.tools` already depends on `korvid.core` — no `tach.toml`
  edit, no new cross-boundary import from `korvid.evals`.
