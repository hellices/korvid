# Typed Prompt Override for Operation Evaluation

## Goal

Give the stateful operation-journey harness (`tests/evals/operation_app.py`,
`tests/evals/operation_campaign.py`) the same first-class, typed prompt-lever
API the read-only scenario and conversational-journey harnesses already have
(`korvid.evals.harness.PromptGrind`), so an external caller such as
korvid-prompt-lab can grind an operation run's tier pack or add an eval
overlay without monkeypatching any module attribute. This is a composition
slice only: no TUI-free operation runner, no approval-coordinator extraction.
Both are out of scope and remain tracked as future work.

## Context

`korvid.evals.harness.build_eval_harness` already accepts a `grind:
PromptGrind` parameter and composes it through the real `PromptHarness` —
layer 3 (tier pack) may be replaced and layer 5 gains an `eval-overlay`
overlay, always *after* `PromptHarness` composes the immutable
`SAFETY_CONTRACT` first. `korvid.evals.__main__` (scenario CLI) and
`korvid.evals.journey_runner`/`journeys_cli` (conversational journeys)
already thread a `PromptGrind` through this exact path and publish an
explicit `prompt_fingerprint` (`pack`, `overlays`, `source`, `sha256`) in
their JSON output.

The operation harness never adopted this. `run_operation_journey` calls
`build_eval_harness` without a `grind` argument at all — there is no
parameter to pass one through — and `operation_campaign.py`'s `main()` calls
`prompt_fingerprint(policy)` with no grind, so its published fingerprint is
always the unground default regardless of what a caller might want measured.
An external caller that needs to grind an operation run's prompt today has
no supported way to do it, which is exactly the pressure that leads to
monkeypatching internals instead.

## Design

Reuse `korvid.evals.harness.PromptGrind`/`NO_GRIND` as-is. It is already the
production-shared typed prompt lever, it already guarantees tier-pack- and
overlay-only replacement after the immutable safety layer, and it already
carries no path to widen the armed-tool surface (that is a separate,
environment-driven axis `PromptGrind` never touches). No new type is
introduced.

1. **`tests/evals/operation_app.py`**: `run_operation_journey` gains a
   keyword-only `grind: PromptGrind = NO_GRIND` parameter, forwarded to the
   `build_eval_harness(..., grind=grind)` call it already makes — the
   composition graph itself does not change, only the previously-implicit
   `NO_GRIND` becomes caller-supplied. `OperationRun` gains a `prompt:
   dict[str, Any]` field, populated once via the existing
   `korvid.evals.__main__.prompt_fingerprint(harness.policy, grind=grind)`
   helper, so every run publishes an explicit, self-describing prompt
   identity whether or not a grind was supplied.
2. **`tests/evals/operation_campaign.py`**: gains `--tier-pack-file` and
   `--prompt-overlay-file` CLI flags, behaviorally identical to the scenario
   CLI's flags — the same `_read_prompt_file` fail-closed file-reading
   helper is imported and reused, not duplicated. `main()` builds one
   `PromptGrind` per invocation (before any provider/network work, so a bad
   file exits fast) and threads it through every repetition of every
   journey. Each per-run JSON record gains the run's own `"prompt"` field
   (from `OperationRun.prompt`); the existing campaign-level
   `meta.prompts` fingerprint is fixed to pass `grind=grind` so it actually
   reflects what the campaign measured instead of always reporting the
   default.
3. No monkeypatching, no module attribute rebinding anywhere in this
   change: the override travels exclusively as a constructor/function
   argument, exactly as the read-only harnesses already do it.

## Non-goals

- A public, TUI-free operation runner or any new approval-coordinator
  abstraction (tracked separately).
- Any change to the write/approval path, `SAFETY_CONTRACT` text, armed-tool
  surface, or operation-journey YAML fixture schema.
- Any change to `src/korvid/evals/harness.py` — `PromptGrind` already has
  everything this slice needs.
- CLI flags on any interface other than `tests.evals.operation_campaign`
  (the harness's only source-checkout campaign entry point today).

## Testing (TDD)

New `tests/evals/test_operation_prompt_grind.py`:

- Omitting `grind` leaves `OperationRun.prompt["source"] == "default"` and
  every existing assertion in `test_operation_journeys.py` keeps passing
  unmodified — the additive field changes nothing else.
- A supplied `tier_pack`/`overlay` grind changes `OperationRun.prompt`'s
  `sha256`/`source == "override"`.
- The model's actually-sent system message (captured with a
  `ScriptedProvider` spy subclass) always starts with `SAFETY_CONTRACT`,
  with the grind's tier-pack/overlay text appearing only after it — proving
  the override cannot precede or replace the immutable layer at the
  operation-runner boundary itself (the generic `PromptHarness`-level
  guarantee is already covered by `tests/evals/test_harness.py`; this proves
  the operation composition path wires it through unchanged).
- Two `run_operation_journey` calls with two different grinds, executed
  concurrently via `asyncio.gather`, each produce the exact same
  `prompt["sha256"]` as an equivalent solo/sequential run with the same
  grind — proving no cross-run leakage through shared mutable state.

`tests/evals/test_operation_campaign.py` gains CLI-flag tests mirroring
`tests/evals/test_cli.py`'s scenario-CLI grind tests: flags default to
unset, accept paths, a scripted campaign run with both flags populates
`meta.prompts.source == "override"` and every run record's `"prompt"`
field, and an invalid (missing/non-UTF-8) file exits with the correct flag
name in the message.

## Verification

Targeted: `pytest tests/evals/ -q`. Static: `mypy`, `ruff check`/`ruff
format --check`, `tach check`, `pre-commit run` on touched files. Full
suite as feasible (`pytest -q`, ~19 minutes locally). `uv.lock` must stay
untouched (frozen runs only, per `AGENTS.md`).

## Docs

`docs/evals/operations.md` gains a subsection describing
`--tier-pack-file`/`--prompt-overlay-file`, the per-run `prompt` field, and
the safety/no-leak guarantees — the supported mechanism that removes any
need for an external caller to monkeypatch composition internals.
