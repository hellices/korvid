# The Operation-Journey External-Optimizer Machine Protocol

`python -m korvid.evals.operation_main --json report.json` writes a
machine-readable artifact for the *write*-lifecycle counterpart to
[the scenario protocol](protocol.md): stateful operation journeys (scale
and restart flows gated behind a real approval decision), run end to end
and entirely TUI-free, over the same production write path
(`run_approved_write`, a real `AuditLog`, `StatefulFakeWriteOps`) a
Textual-driven run uses.

This page documents that contract for an external prompt optimizer (such
as `korvid-prompt-lab`) that needs write/approval coverage it cannot get
from the read-only scenario/journey protocol. It is deliberately narrower
than "every key the JSON happens to contain": anything described here is
load-bearing; anything else may still be present but is not a promise.

## Versioning

```json
{"meta": {"protocol_version": "1.0", "...": "..."}}
```

`meta.protocol_version` is the same published constant the scenario
protocol uses (`korvid.evals.__main__.EVAL_PROTOCOL_VERSION`) — there is
one protocol version across both entry points, not two independently
numbered contracts. A field's meaning changing, or a documented field
being removed, is a **breaking** change and bumps it for both CLIs at
once.

## Selecting an exact case pack

```sh
uv run python -m korvid.evals.operation_main \
  --operation-id restart-deployment --operation-id scale-deployment-up \
  --json report.json
```

`--operation-id` is repeatable and selects an exact, repeatable subset of
`src/korvid/evals/operations/*.yaml` by id — no fixture files are copied
into a separate directory to narrow a run. Selection is fail-closed,
mirroring the scenario protocol's `--scenario-id` exactly:

- An id `--operations` does not contain is refused.
- A repeated id is refused (a duplicate would silently double one run).
- An explicit empty id is refused.
- Omitting `--operation-id` entirely runs every bundled fixture —
  unchanged default behavior.

A bad selection exits **before any provider call**, so a caller can trust
that a nonzero exit before "running ..." lines appear on stderr means the
selection itself was invalid, not that a run failed partway through.

## Case-pack identity

```json
{
  "meta": {
    "operation_case_pack": {
      "operation_ids": ["restart-deployment", "scale-deployment-up"],
      "count": 2,
      "sha256": "…64 hex chars…"
    }
  }
}
```

`meta.operation_case_pack` is always published (there is no legacy
direct-call path for this CLI the way `run_payload(..., scenarios=None)`
exists for the scenario protocol — `operation_main.main()` always loads
and passes the exact journeys it selected). `operation_ids` is sorted, so
the identity does not depend on selection order or filesystem enumeration
order. `sha256` is a digest of the *loaded scenario definitions
themselves* — goal, target, approval script, required checkpoints,
pre/postcondition assertions, and cluster fixture content — using the same
type-preserving canonical encoding
`korvid.evals.scenario.case_pack_identity` uses (a mapping key must be a
string; a `datetime`/`date` value is never conflated with an
equal-looking string; an unsupported value type is rejected rather than
silently stringified). It is identical for two runs that loaded the same
fixture content from different paths or checkouts, and it changes
whenever any selected journey's content does — never a function of a
file's path or mtime.

## Per-operation results

```json
{
  "operations": [
    {
      "journey_id": "scale-deployment-up",
      "runs": [
        {
          "answer": "Scaled checkout-a in shop-a; a fresh read confirms it is now 3 replicas.",
          "grade": {
            "journey_id": "scale-deployment-up",
            "safe": true,
            "hard_failures": [],
            "checkpoints": ["goal_received", "target_resolved", "…"],
            "missing_checkpoints": [],
            "outcome": "completed",
            "truthful": true,
            "completion": true,
            "verification": true,
            "request_match": true,
            "efficiency": 1.0,
            "quality": 1.0,
            "scored_assertions": [],
            "provisional_assertions": ["…"],
            "tool_calls": 2,
            "iterations": 2
          },
          "journal": ["…summarized ActionJournal events, in order…"],
          "audit": ["…the persisted AuditLog lines this run wrote…"],
          "decisions": [{"outcome": "approve", "decision_source": "scripted_policy"}],
          "wall_time_s": 0.031,
          "prompt": {"pack": "low-korvid-operator", "overlays": [], "source": "default", "sha256": "…"}
        }
      ]
    }
  ]
}
```

One entry in `operations[]` per selected journey, in the order it ran;
today each carries exactly one `runs[]` entry (no repetitions yet — a
future extension may add more without breaking this shape, since an
array of length 1 and an array of length *n* are both already this same
type).

- **`grade`** is `OperationGrade`, unchanged: the pass/fail safety gate
  (`safe`, `hard_failures`), the ordered lifecycle checkpoints actually
  recorded versus required (`checkpoints`/`missing_checkpoints`), the
  classifier's terminal `outcome` (`completed`/`rejected`/`failed`/
  `verification_unknown`/`in_progress`/`accepted`/`ambiguous`/`unknown`),
  and the completion/verification/efficiency/quality scoring
  [operation journeys](operations.md) already documents.
- **`journal`** is the same summarized, payload-free `ActionJournal`
  record the Textual harness produces — actor, event, target, result, and
  a closed-vocabulary `detail` string; never raw tool arguments, raw tool
  results, or raw user turns.
- **`audit`** is the exact persisted lines this run's real `AuditLog`
  wrote, read back after the run.
- **`decisions`** is new: one entry per approval decision the run's
  `ApprovalPolicy` actually made, in order — `outcome` is
  `"approve"`/`"decline"`/`"dismiss"`/`"expire"`, and `decision_source`
  names *which* policy produced it (`"scripted_policy"` for this CLI,
  always — this runner never binds `"tui_keystroke"`, the source only a
  real post-dialog user keystroke may ever carry; see
  [the approval capability](#the-approval-capability-not-a-string) below).
  Empty for a journey whose fixture never reaches a dialog
  (`expected_approval_dialogs: 0`).
- **`wall_time_s`** is this run's own wall-clock duration.
- **`prompt`** is this run's prompt-grind identity (`pack`, `overlays`,
  `source`, `sha256`) — identical in shape and derivation to
  `meta.prompts`, published per-run so a reader never has to assume every
  run in one artifact shared the same grind.

## Run-level metadata

`meta` also carries the same `policy`, `limits`, `capabilities`,
`catalog_version`, `prompts`, and `tools` blocks
[the scenario protocol](protocol.md) documents, resolved once against a
**write-armed** environment (`readonly=False` — this is the one
substantive difference from the scenario protocol's meta: `meta.tools.armed`
includes `scale_resource`/`rollout_restart` for this CLI, because arming
them is the entire point of an operation run). `meta.serving`, when
captured, is the identical block the scenario protocol publishes.

## Grinding the prompt

`--tier-pack-file` and `--prompt-overlay-file` are the same eval-only
prompt levers the scenario CLI accepts, composed after korvid's immutable
safety contract exactly as there — a grind can change how the model is
instructed to operate, never what it is permitted to do, and it can never
widen the armed tool surface (writes are always armed for this CLI
regardless of tier or grind, exactly as `operation_campaign.py` already
behaves). Omitting both flags reproduces the shipped prompt exactly, and
`source` in the published `prompts` fingerprint reports `"default"`
rather than `"override"`.

## The approval capability, not a string

Every mutation this runner performs passes through the identical
production seam a Textual run does: `run_approved_write`, the real
fail-closed `AuditLog`, and `StatefulFakeWriteOps`'s UID/audit-intent
revalidation. The only thing that differs from a Textual run is *which*
`ApprovalPolicy` decides — `ScriptedOperationBridge` binds an explicit,
composition-root-injected `ScriptedApprovalPolicy` (deterministic,
no sleeps, pops one pre-authored `ApprovalOutcome` per write) instead of
the real `ConfirmScreen` a running `KorvidApp` binds
(`TextualApprovalPolicy`, gated so only an actual post-dialog fresh user
keystroke may ever carry an `approve` outcome forward). A bare string is
never treated as authorization at any point in this path: the write
coordinator inspects a typed `ApprovalDecision` (`outcome` +
`decision_source`), and production's own gate,
`require_tui_keystroke_source`, rejects any `approve` decision whose
`decision_source` is not `"tui_keystroke"` — a fail-closed check this
runner's own `decisions[]` field lets an external caller audit directly,
run over run.

## Exit codes

- **`0`** whenever every requested operation ran to a graded result — a
  model *failing* an operation (an unsafe write, the wrong target, a
  missed checkpoint) is scored evidence in the JSON, not a nonzero exit.
- **`1`** for a systemic/harness error: a provider could not be
  constructed, an operation could not run to a graded result at all, or
  the result artifact could not be written.
- **`2`** for a usage/argument error, including a rejected `--operation-id`
  selection.

This is a deliberate divergence from `operation_campaign.py --scripted`
mode's convention (a CI regression gate for korvid's own suite, which
does exit `1` on an unsafe or incomplete run): this CLI is a scoring
function for an external optimizer that needs every requested run's
result, safe or not, back as data — exactly the same philosophy
`python -m korvid.evals` already applies to scenario grading.

## What this protocol does not weaken

This protocol does not, and must not, weaken any existing safety or
validation behavior to make external orchestration easier: the write
coordinator, the fail-closed audit-intent probe, and the UID-revalidation
path are the identical production code a Textual run exercises; a
`--tier-pack-file`/`--prompt-overlay-file` grind is still layered after
korvid's immutable safety contract, never in place of it; and
`decision_source` provenance is still checked at the one gate that may
ever authorize a mutation. See [operation journeys](operations.md) for
the full safety-boundary and grading documentation this CLI's runs
inherit unchanged.
