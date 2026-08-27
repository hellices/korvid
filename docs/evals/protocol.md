# The External-Optimizer Machine Protocol

`python -m korvid.evals --json report.json` writes a machine-readable
artifact meant to be *parsed*, not just archived — by scoreboard tooling,
regression dashboards, and (the case this page is written for) external
prompt optimizers that run korvid's own eval harness as their scoring
function and need a stable shape to read results back from.

This page documents that contract: the fields an external tool may rely
on, the version they are pinned against, and the exact selection and
identity guarantees the harness makes so a prompt optimizer can reproduce
one measurement run after run. It is deliberately narrower than "every key
the JSON happens to contain" — anything described here is load-bearing;
anything else may still be present but is not a promise.

## Versioning

```json
{"meta": {"protocol_version": "1.0", "...": "..."}}
```

`meta.protocol_version` is published on every artifact, unconditionally.
It is an independent literal (`korvid.evals.__main__.EVAL_PROTOCOL_VERSION`)
— it does not track korvid's own `pyproject.toml` package version, so an
external optimizer's pin against the contract survives an unrelated korvid
release that touches nothing this page describes.

- A field's meaning changing, or a documented field being removed, is a
  **breaking** change and bumps `protocol_version`.
- A new, additive field (present alongside everything already documented)
  is not a breaking change and does not require a bump. `meta.case_pack`
  (below) is one such field: an artifact that never selects a scenario
  subset omits it, matching every artifact written before this contract
  existed.

An external optimizer should refuse to parse an artifact whose
`protocol_version` it does not recognize, rather than guess at a shape it
was never tested against.

## Exact scenario selection

By default `python -m korvid.evals` runs every scenario in `--scenarios DIR`
(the bundled pack unless overridden) — unchanged from before this
contract. `--scenario-id ID` (repeatable) narrows a run to an exact,
named set without copying fixture files into a scratch directory:

```sh
uv run python -m korvid.evals \
  --scenario-id oom-killed --scenario-id image-pull-typo \
  --json report.json
```

Selection is **fail-closed**: an optimizer that names the wrong thing
finds out immediately, not by silently scoring a different case pack.

| Selection | Result |
|---|---|
| flag omitted entirely | every scenario in `--scenarios` runs, unchanged |
| one or more known ids | exactly those scenarios run, regardless of the order named |
| an id `--scenarios` does not contain | `SystemExit`, before any provider call |
| the same id named twice | `SystemExit` |
| a blank/whitespace-only id | `SystemExit` |

The selected set is always ordered by scenario id, so naming the same ids
in a different order on two invocations produces the same run order and
the same `meta.case_pack` identity below.

## Deterministic case-pack identity

```json
{
  "meta": {
    "case_pack": {
      "scenario_ids": ["image-pull-typo", "oom-killed"],
      "count": 2,
      "sha256": "3f9c2b2c…"
    }
  }
}
```

Every artifact that names a `scenarios=` set (which the CLI always does)
publishes `meta.case_pack`:

- `scenario_ids` — every scenario id that ran, sorted, independent of
  selection order or filesystem enumeration order.
- `count` — `len(scenario_ids)`, for a cheap sanity check without parsing
  the array.
- `sha256` — a digest of the **exact loaded scenario definitions**:
  question, starting interaction, grading assertions (`must_mention`,
  `must_not_mention`, `expected_evidence`), and cluster fixtures (objects,
  events, log tails, forbidden reads) for every selected scenario, in id
  order.

`sha256` is content-derived, not path- or mtime-derived: the same fixture
text loaded from a different directory, a renamed file, or a fresh
checkout with a different mtime always produces the same digest, and any
change to what a scenario actually asserts or simulates — even one that
leaves its id and file name alone — changes it. Two runs reporting the
same `sha256` measured the identical case pack; two reporting a different
one did not, whatever their `scenario_ids` say.

This is what lets an external optimizer trust a scoreboard comparison
across two runs it did not orchestrate back-to-back: pin the expected
`sha256` once, and a later run that silently drifted (a fixture edited
upstream, a stale bundled pack, a typo'd `--scenarios` path) is caught
before its score is compared against anything.

## Fields that remain present

Everything the JSON artifact published before this contract still
appears, unconditionally:

- `meta.policy` — provider, model, tier, route source, prompt pack id, and
  composed overlay ids.
- `meta.limits` — every budget (`max_iterations`, `max_history_chars`,
  `max_result_chars`, `max_tool_calls_per_iteration`,
  `allow_parallel_tool_calls`, `strict_history_budget`) the run was bound
  by.
- `meta.capabilities` — the merged capability facts and their provenance.
- `meta.catalog_version` — which model catalog resolved the tier.
- `meta.prompts` — the prompt fingerprint: `pack`, composed `overlays`,
  `source` (`default` or `override`), and a `sha256` over the fully
  composed system prompt plus the transmitted tool schemas. This is
  unaffected by scenario selection — it identifies the *prompt*, not the
  case pack.
- `meta.tools` — the exact armed tool names, their count, and any names
  dropped by `--without-tool`.
- `meta.serving` — the probed serving environment (engine name/version,
  loaded-model digest and context length, warm-up outcome), present
  whenever `--warmup`/the serving probe ran; omitted, not defaulted, when
  it did not.
- `scenarios[]` — one entry per scenario that ran: `scenario`, `root_cause`,
  `successes`, `evidence_hits`, `interaction` (the exact starting
  workspace), `max_tool_calls`, and the per-repetition `runs[]` (answer,
  grade, citations, token counts, outcome, failure class, tool-call
  counts).

An external optimizer that only reads `meta.prompts.sha256` (to confirm
which prompt produced a score) and `meta.case_pack.sha256` (to confirm
which case pack it was measured against) has, between the two, everything
needed to make a scoreboard comparison meaningful without re-deriving
either from the model's raw output.

## Non-goals

This protocol does not, and must not, weaken any existing safety or
validation behavior to make external orchestration easier: an unknown
`--without-tool` name is still refused (it would silently claim to reduce
a surface it did not touch), the eval environment is still read-only, and
a `--tier-pack-file`/`--prompt-overlay-file` grind is still layered after
korvid's immutable safety contract, never in place of it. See
[evaluation methodology](methodology.md) for how the numbers this artifact
carries are meant to be interpreted.

**This protocol covers diagnostic scenarios only.** It does not cover, and
must not be read as replacing, the stateful *operation* journeys (scale
and restart flows gated behind the real approval dialog) that
`tests.evals.operation_campaign`/`operation_app.py` exercise — an external
optimizer that needs write/approval coverage cannot get it from
`python -m korvid.evals` today. That harness has no TUI-free public
equivalent yet, and building one is not a matter of extending this JSON
shape: it runs into an enforced module boundary and a deliberately
Textual-only approval gate. See [operation journeys: external-optimizer
machine protocol status](operations.md#external-optimizer-machine-protocol-not-yet-available)
for exactly what blocks it and what a future extension would require.
