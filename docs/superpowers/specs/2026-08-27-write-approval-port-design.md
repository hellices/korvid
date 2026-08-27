# Write approval port: typed `ApprovalDecision` + pure write orchestration

## Problem

The write security perimeter (`src/korvid/ui/write_coordinator.py`) is one
class, `WriteCoordinator(WriteGate)`, that mixes three genuinely different
concerns in one 1000+ line module:

1. **Approval-decision handling** — today expressed only as an untyped
   `bool | None` (`True` approve / `False` decline / `None` dismiss) that
   collapses to a two-value `Literal["approved", "declined"]` (plus a
   separate `"expired"` for timeout) the instant it leaves the dialog
   callback (`agent_ui_controller.py::_await_user_approval`). Dismiss and
   decline are indistinguishable from that point on, and nothing records
   *how* an approval was obtained — there is no way to assert, in code, that
   an approval reaching the mutation step actually came from a real
   `ConfirmScreen` key event rather than some other future caller (an eval
   runner, an MCP tool, a scripted test double).
2. **The fail-closed audit → mutate → audit orchestration**
   (`_run_write`/`_run_write_inner`) — already almost entirely free of
   Textual/`ViewState` coupling; it only touches `self._ui.notify` and
   `self._ui.progress` (both narrow, already-abstract calls) and
   `self.audit_write` (itself free of UI concerns).
3. **UI/`ViewState`-specific revalidation** (`context_intact`,
   `identity_intact`, `scale_identity_intact`) and the `ConfirmScreen`
   construction/dialog push machinery — genuinely coupled to the running
   Textual app and its pane/view model.

Only (3) needs to stay next to Textual. (1) and (2) are conceptually pure —
generic to *any* write, not to the TUI — but they currently live only in
`korvid.ui`, so nothing outside the running Textual app (a future TUI-free
operation runner, an eval harness, a unit test) can exercise the same
ordering guarantees without importing Textual or monkeypatching module
internals.

This is exactly the shape of gap the two prior slices in this project (the
scenario/journey machine protocol, PR #321; the operation `PromptGrind`
override, PR #322) both ran into and explicitly deferred: a TUI-free
operation runner needs a first-class, typed way to drive an *approved*
write without importing `textual` or monkeypatching `KorvidApp`.

## Goal (this slice)

Extract (1) and (2) into new, pure modules under `src/korvid/tools/` with
**zero behavior change** to the existing Textual app, and wire them into the
one real production call path (`WriteCoordinator.run` +
`agent_ui_controller.py`'s agent-write approval step) so the extraction is
provably load-bearing, not inert parallel code — while leaving (3)
(`context_intact`/`identity_intact`/the `ConfirmScreen` dialog machinery)
exactly where it is, per the review's explicit permission to "leave
UI-specific pieces outside the coordinator."

This slice does **not** build a TUI-free operation runner. It builds the
one piece of first-class, typed, UI-independent API such a runner (or any
other non-Textual caller) would need later: a way to name *how* an approval
was obtained, and a way to run the intent-audit → mutate → outcome-audit
sequence given nothing but that approval and a mutation coroutine factory.

## Non-goals

- No TUI-free operation runner in this PR (deferred, as previously reported
  to the user after PR #322).
- No change to `src/korvid/ui/proposal_controller.py`'s separate
  `:proposals` review flow (`_await_decision`, `Decision =
  Literal["approved", "declined", "dismissed"]`). It already distinguishes
  dismiss from decline for its own purposes and expires proposals through
  `korvid.tools.proposals`'s own TTL sweep rather than the agent-write
  timeout path this slice touches. Folding it into the same typed
  `ApprovalDecision` is a reasonable *future* slice, but doing it here would
  double the surface area under review for no behavior change today, so it
  is explicitly out of scope.
- No `tach.toml` change. `korvid.tools` already depends on `korvid.core`,
  `korvid.k8s`, and `korvid.obs`; `korvid.ui` already depends on
  `korvid.tools`. Both new modules only need `korvid.k8s` (for
  `ResourceMeta`/`ApiStatusError`) — an existing, allowed edge.
- No audit-log schema change. `AuditLog.append`'s field set (`timestamp`,
  `context`, `actor`, `action`, `kind`, `group`, `version`, `namespace`,
  `name`, `detail`, `outcome`) is unchanged. `decision_source` stays an
  in-memory, typed value on `ApprovalDecision`; it is not persisted to the
  JSONL audit record in this slice. Rationale: the audit log is a
  size-rotated, fail-closed, externally-parsed JSONL format
  (`src/korvid/core/audit.py`); adding a field is a real compatibility
  decision (schema version, downstream parser impact) that the review made
  conditional ("if adding ... version deliberately and investigate ...
  breaking external exact parsers"). Nothing in this slice's requirements
  *needs* the source persisted durably — the fail-closed guarantee is that a
  non-`tui_keystroke` approval can never reach the mutation step at all, not
  that the audit trail records the source after the fact. If a later slice
  needs the source on disk, that is a dedicated, deliberately-versioned
  follow-up.
- No change to `korvid.k8s.writes.WriteOps` (already UI-decoupled) or to
  `src/korvid/ui/write_gate.py`'s reservation machinery
  (`ReservedWrite`/`reserve_write`/`reserved`) — it has no UI coupling
  already and is not part of the approval/audit-orchestration split this
  slice makes.

## Design

### New module: `src/korvid/tools/approval.py`

```python
class ApprovalOutcome(str, Enum):
    APPROVE = "approve"
    DECLINE = "decline"
    DISMISS = "dismiss"
    EXPIRE = "expire"

#: The only decision_source production code may present for an APPROVE
#: outcome: a real key event resolved by `ConfirmScreen` after it was
#: shown (its own freshness gate already discards any buffered key from
#: before the dialog existed - see `FreshKeysInput`).
TUI_KEYSTROKE_SOURCE: Final = "tui_keystroke"

@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    outcome: ApprovalOutcome
    decision_source: str

    @classmethod
    def approved(cls, decision_source: str) -> ApprovalDecision: ...
    @classmethod
    def declined(cls, decision_source: str) -> ApprovalDecision: ...
    @classmethod
    def dismissed(cls, decision_source: str) -> ApprovalDecision: ...
    @classmethod
    def expired(cls, decision_source: str) -> ApprovalDecision: ...

class RejectedApprovalSourceError(RuntimeError):
    """Raised when an APPROVE decision did not come from an allowed source."""

def require_tui_keystroke_source(decision: ApprovalDecision) -> ApprovalDecision:
    """Fail-closed gate: an APPROVE decision must carry decision_source ==
    TUI_KEYSTROKE_SOURCE or this raises RejectedApprovalSourceError. Decline,
    dismiss, and expire carry no mutating authority, so their source is
    informational only and is never rejected here - only an outcome that
    would let a write proceed is gated."""
```

`ApprovalOutcome`/`ApprovalDecision` are frozen (an `Enum` and a frozen
`slots=True` dataclass) so a decision, once built, cannot be mutated on its
way from the dialog callback to the write. `require_tui_keystroke_source`
only ever rejects an `APPROVE` from a non-allowed source — decline/
dismiss/expire never authorize a mutation, so gating them would add no
safety and would reject legitimate "the user said no" results.

### New module: `src/korvid/tools/write_coordinator.py`

Moves the pure parts of `WRITE_VERBS`/`gvr_label`/`perm_target`/
`write_locus` here (they only format a `ResourceMeta`/action pair into
strings; `korvid.tools` already depends on `korvid.k8s`), and adds the
extracted intent-audit → mutate → outcome-audit sequence as a free
function:

```python
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
    """Fail-closed intent audit -> mutation -> outcome audit, byte-identical
    to `WriteCoordinator._run_write_inner`'s previous body. Returns 'done' /
    'blocked: ...' / 'failed: ...'."""
```

`AuditRecorder`'s call signature is written to match
`WriteCoordinator.audit_write`'s bound-method signature exactly, so the
production caller passes `self.audit_write` directly with no adapter.
`Notifier` matches the one two-argument shape `_run_write_inner` already
uses (`message`, `severity=`) — not the full `UiSurface.notify` signature
(`title`/`timeout`/`markup` are never used at this call site).

`run_approved_write` does not take a `progress` port: the existing
`with self._ui.progress(...):` span wraps the whole reserved body in
`WriteCoordinator._run_write`, which stays exactly as it is (a two-line
method that opens the progress span and awaits `run_approved_write`, in
`korvid.ui`, next to the concrete `UiSurface` it already holds) — there is
no reason to abstract a Textual-only status-bar span into a `korvid.tools`
port that nothing outside the TUI can meaningfully implement.

`run_approved_write` intentionally does **not** take a `revalidate` guard.
The review named "UID/context revalidation" as part of the shared ordering,
but every current call site (`WriteCoordinator.confirm`/
`confirm_interactive`'s `approval_guard`, and `agent_request_write`'s own
UID snapshot re-check) already revalidates *before* calling `.run(...)` —
`.run(...)`'s reservation is itself the synchronous point after which no
further revalidation gap exists (the write is already reserved by the time
any concurrent switch could observe it). Adding a second revalidation
parameter inside `run_approved_write` would not close a real gap and would
only duplicate a check every caller already makes; the extraction instead
proves the *existing* ordering (approve+revalidate happens, then `.run(...)`
audits before mutating) is unchanged, rather than inventing a new
mid-orchestration recheck.

### `src/korvid/ui/write_coordinator.py` changes

- `WRITE_VERBS`, `gvr_label`, `write_locus`, `perm_target` become
  re-exports of the moved definitions in `korvid.tools.write_coordinator`
  (`from korvid.tools.write_coordinator import ...`), so every existing
  `from korvid.ui.write_coordinator import gvr_label, write_locus` (four
  call sites: `agent_ui_controller.py`, `proposal_controller.py`,
  `resource_write_controller.py`, `debug.py`, plus the test file) keeps
  working unchanged.
- `_run_write`/`_run_write_inner` merge into one small method that opens
  the progress span and delegates to `run_approved_write`, passing
  `audit=self.audit_write` and `notify=self._ui.notify`. `run()`'s external
  contract (return value, notifications, audit calls, reservation timing)
  is unchanged — verified by running the existing `tests/ui/
  test_write_coordinator.py` (89 tests) unmodified.

### `src/korvid/ui/agent_ui_controller.py` changes

`_await_user_approval`'s public return type is pinned by an existing test
(`tests/ui/test_protected_contexts.py::test_agent_write_approval_uses_
protected_gate`, which asserts `result_box == ["approved"]`) and must not
change. The typed decision is built and gated *inside* the function, before
it collapses to the legacy three-value string:

```python
def _decision_from_confirm_screen(confirmed: bool | None) -> ApprovalDecision:
    """Every ConfirmScreen resolution - approve, decline, or Esc dismiss -
    is itself gated by FreshKeysInput's post-dialog freshness check (a
    buffered pre-dialog key can never resolve it), so all three outcomes
    are genuinely tui_keystroke-sourced."""
    if confirmed is True:
        return ApprovalDecision.approved(TUI_KEYSTROKE_SOURCE)
    if confirmed is False:
        return ApprovalDecision.declined(TUI_KEYSTROKE_SOURCE)
    return ApprovalDecision.dismissed(TUI_KEYSTROKE_SOURCE)


def _collapse_decision(
    decision: ApprovalDecision,
) -> Literal["approved", "declined", "expired"]:
    """Fail-closed gate, then fold to the legacy three-value contract external
    callers already depend on: dismiss reads the same as decline, exactly as
    before this slice."""
    decision = require_tui_keystroke_source(decision)
    if decision.outcome is ApprovalOutcome.APPROVE:
        return "approved"
    if decision.outcome is ApprovalOutcome.EXPIRE:
        return "expired"
    return "declined"  # decline or dismiss - unchanged external behavior
```

Both the surfaceability timeout and the `asyncio.wait_for` timeout build
`ApprovalDecision.expired(decision_source="timeout")` instead of a bare
`"expired"` string; the real key-driven path builds its decision from
`_decision_from_confirm_screen`. Every return now passes through
`_collapse_decision`, so `require_tui_keystroke_source` runs on every real
production approval — a fail-closed gate that is provably load-bearing (a
future caller that ever builds an `APPROVE` decision without a genuine
`tui_keystroke` source is rejected here), not inert parallel code, while
every existing external observation of `_await_user_approval` (its return
strings, the notifications it sends, the dialog it pushes, cancellation
handling) is unchanged.

`asyncio.CancelledError` handling is untouched — cancellation re-raises
before any decision is built, exactly as today.

### Composition tests (new): `tests/tools/test_approval.py`,
`tests/tools/test_write_coordinator.py`

New pure unit tests (no Textual, no `KorvidApp`) for:

- `ApprovalDecision`/`ApprovalOutcome` frozen values and the four
  classmethod constructors.
- `require_tui_keystroke_source` accepts an `APPROVE` from
  `tui_keystroke` and rejects one from any other source
  (`RejectedApprovalSourceError`); never rejects decline/dismiss/expire
  regardless of source.
- `run_approved_write`: audit happens strictly before `op_factory` is
  awaited (ordering); an audit failure blocks the mutation and the
  factory is never called; a 403 keeps the exact RBAC message contract; a
  409 keeps the exact conflict-message contract; an unexpected mutation
  failure still records the outcome audit and re-raises nothing extra
  (returns `failed: ...`); a `CancelledError` raised from `op_factory`
  propagates untouched (it is a `BaseException`, never caught by the
  `except Exception` blocks) rather than being reported as a failure.

`tests/ui/test_agent_ui_controller.py` (or a new focused test in the same
directory) gets one additional composition-level test: production's
`agent_request_write` path, driven end-to-end through a real `ConfirmScreen`
via `Pilot`, only ever produces `decision_source == "tui_keystroke"` for an
approval that proceeds to a mutation - proven indirectly, since a
non-`tui_keystroke` source would raise `RejectedApprovalSourceError` and
surface as an unhandled exception in `agent_request_write`, which no
existing approve/decline/dismiss/expire Pilot test observes.

## Testing strategy

1. TDD, module by module: `tools/approval.py` first (pure, no
   dependencies on anything else new), then `tools/write_coordinator.py`
   (depends on nothing but `korvid.k8s`), then the `ui/write_coordinator.py`
   delegation (verified against the full existing
   `tests/ui/test_write_coordinator.py` suite unmodified), then the
   `agent_ui_controller.py` wiring (verified against the full existing
   `tests/ui/test_agent_write.py`, `tests/ui/test_agent_ui_controller.py`,
   `tests/ui/test_approval_timeout.py`, `tests/ui/test_protected_contexts.py`
   suites unmodified), then the new composition tests.
2. Full `tests/ui` suite, then the full repository test suite, then
   `ruff`/`mypy`/`tach check`/`pre-commit` on touched files, mirroring the
   verification depth of the two prior slices (PR #321, PR #322).

## Risk assessment / escape hatch

The riskiest step is wiring `_await_user_approval` to route every return
through `_collapse_decision`/`require_tui_keystroke_source`, since a bug
there would make every agent write in the TUI fail closed (raise instead of
returning a string). This is mitigated by:

- The gate only ever fires for `APPROVE` outcomes, and production always
  builds `APPROVE` from `_decision_from_confirm_screen(True)`, which always
  carries `TUI_KEYSTROKE_SOURCE` — the gate is unreachable-as-a-rejection in
  every existing test and every existing production code path; it only
  guards against a *future* caller.
- The full existing Pilot suite for agent writes (`test_agent_write.py`,
  61+ tests) exercises every one of approve/decline/dismiss/expire/
  cancel/protected-context paths and must pass unmodified for this slice
  to be considered done — any regression is caught immediately, not left
  to be discovered later.

If, once concretely attempted, wiring `_collapse_decision` into
`_await_user_approval` turns out to introduce real behavior risk that the
existing Pilot suite cannot adequately cover, the safe fallback is to ship
only `tools/approval.py` + `tools/write_coordinator.py` (both fully unit
tested) plus the `ui/write_coordinator.py` delegation (which carries no
approval-decision risk at all, only an internal refactor of already-pure
code) in this PR, and defer the `agent_ui_controller.py` wiring to a
follow-up slice. This has not been necessary based on the investigation
above, but is the explicit fallback if implementation reveals otherwise.
