# Operation Prompt Grind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thread the existing `korvid.evals.harness.PromptGrind` typed
prompt override through the operation-journey harness
(`tests/evals/operation_app.py`, `tests/evals/operation_campaign.py`) so an
external caller can grind an operation run's tier pack / eval overlay
without monkeypatching anything, and every run publishes an explicit
prompt-identity fingerprint.

**Architecture:** No new types. `run_operation_journey` gains a
`grind: PromptGrind = NO_GRIND` keyword parameter forwarded to the
`build_eval_harness` call it already makes; `OperationRun` gains a
`prompt: dict[str, Any]` field from the existing `prompt_fingerprint`
helper. `operation_campaign.py`'s CLI gains `--tier-pack-file` /
`--prompt-overlay-file` flags (reusing `__main__.py`'s
`_read_prompt_file`), threads the resulting `PromptGrind` through every
repetition, and publishes it in both the per-run record and the
campaign-level `meta.prompts`.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, mypy strict, ruff,
tach. No new dependencies.

## Global Constraints

- Reuse `korvid.evals.harness.PromptGrind`/`NO_GRIND` — do not add a new
  prompt-override type.
- No module attribute rebinding / monkeypatching anywhere in the change.
- No change to `src/korvid/evals/harness.py`, the write/approval path,
  `SAFETY_CONTRACT` text, the armed-tool surface, or the operation-journey
  YAML fixture schema (`OPERATION_SCHEMA_VERSION` stays `2`).
- Default behavior (grind omitted) must be unchanged — every existing test
  in `tests/evals/test_operation_journeys.py` and
  `tests/evals/test_operation_campaign.py` must keep passing unmodified.
- `uv.lock` must stay untouched — use `--frozen` / `UV_FROZEN=1` for every
  `uv run`.

---

### Task 1: `run_operation_journey` accepts a typed prompt grind

**Files:**
- Modify: `tests/evals/operation_app.py`
  - imports around line 60-62 (add `NO_GRIND`, `PromptGrind` from
    `korvid.evals.harness`; add `from korvid.evals.__main__ import
    prompt_fingerprint`)
  - `OperationRun` dataclass, currently:
    ```python
    class OperationRun:
        """One complete journey run: what happened, and how it graded."""

        journey_id: str
        answer: str
        grade: OperationGrade
        journal: tuple[dict[str, Any], ...]
        audit: tuple[dict[str, Any], ...]
        wall_time_s: float
    ```
  - `run_operation_journey` signature and body (currently lines ~1223-1301
    and the return statement at ~1368-1375)
- Test: `tests/evals/test_operation_prompt_grind.py` (new file)

**Interfaces:**
- Consumes: `korvid.evals.harness.PromptGrind`, `NO_GRIND`,
  `build_eval_harness` (already imported); `korvid.evals.__main__
  .prompt_fingerprint(policy, *, grind=NO_GRIND) -> dict[str, Any]`
  (returns a dict with keys `pack`, `overlays`, `source`, `sha256`).
- Produces: `run_operation_journey(journey, *, audit_path, provider_factory,
  model_tier=None, approval_timeout_seconds=5.0, turn_timeout=20.0,
  grind: PromptGrind = NO_GRIND) -> OperationRun`, where `OperationRun` now
  also has a `prompt: dict[str, Any]` field. Later tasks (the campaign CLI)
  read `run.prompt`.

- [ ] **Step 1: Write the failing tests**

Create `tests/evals/test_operation_prompt_grind.py`:

```python
"""Typed prompt-grind override for the stateful operation harness.

`run_operation_journey` composes through `build_eval_harness` exactly like
the read-only scenario/journey harnesses — this module proves the grind
travels through unchanged: the immutable safety contract still composes
first, the default (no grind) run is unaffected, and two grinds run
concurrently never leak into each other.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from korvid.agent.prompt_packs import SAFETY_CONTRACT
from korvid.evals.harness import NO_GRIND, PromptGrind
from korvid.evals.operation import bundled_operations_dir, load_operation_journeys
from korvid.evals.scripted import ScriptedProvider

from .operation_app import OperationRun, run_operation_journey
from .operation_scripts import OPERATION_SCRIPTS

_JOURNEYS = {journey.id: journey for journey in load_operation_journeys(bundled_operations_dir())}
_JOURNEY_ID = "scale-deployment-up"


class _PromptSpy(ScriptedProvider):
    """Records every outbound message list, so a test can inspect the
    exact system message the model was sent."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__(script)
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> Any:
        self.calls.append([dict(message) for message in messages])
        async for event in super().complete(messages, tools, stream=stream):
            yield event


async def _run(tmp_path: Path, *, grind: PromptGrind = NO_GRIND) -> OperationRun:
    return await run_operation_journey(
        _JOURNEYS[_JOURNEY_ID],
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: ScriptedProvider(OPERATION_SCRIPTS[_JOURNEY_ID]),
        grind=grind,
    )


async def _run_with_spy(tmp_path: Path, *, grind: PromptGrind, spy: _PromptSpy) -> OperationRun:
    return await run_operation_journey(
        _JOURNEYS[_JOURNEY_ID],
        audit_path=tmp_path / "audit.jsonl",
        provider_factory=lambda: spy,
        grind=grind,
    )


async def test_omitting_the_grind_publishes_the_default_prompt_identity(tmp_path: Path) -> None:
    run = await _run(tmp_path)
    assert run.prompt["source"] == "default"
    assert sorted(run.prompt) == ["overlays", "pack", "sha256", "source"]


async def test_a_tier_pack_grind_changes_the_published_identity(tmp_path: Path) -> None:
    baseline = await _run(tmp_path / "baseline")
    ground = await _run(
        tmp_path / "ground", grind=PromptGrind(tier_pack="Answer in one short sentence.")
    )
    assert ground.prompt["source"] == "override"
    assert ground.prompt["sha256"] != baseline.prompt["sha256"]


async def test_the_safety_contract_still_composes_first_with_an_active_grind(
    tmp_path: Path,
) -> None:
    spy = _PromptSpy(OPERATION_SCRIPTS[_JOURNEY_ID])
    grind = PromptGrind(
        tier_pack="GRIND-TIER-PACK-MARKER", overlay="GRIND-OVERLAY-MARKER"
    )
    await _run_with_spy(tmp_path, grind=grind, spy=spy)
    assert spy.calls, "the scripted provider must have been called at least once"
    system_message = spy.calls[0][0]
    assert system_message["role"] == "system"
    content = system_message["content"]
    assert content.startswith(SAFETY_CONTRACT)
    safety_end = len(SAFETY_CONTRACT)
    assert "GRIND-TIER-PACK-MARKER" in content[safety_end:]
    assert "GRIND-OVERLAY-MARKER" in content[safety_end:]
    assert "GRIND-TIER-PACK-MARKER" not in content[:safety_end]
    assert "GRIND-OVERLAY-MARKER" not in content[:safety_end]


async def test_simultaneous_runs_with_different_grinds_do_not_leak(tmp_path: Path) -> None:
    grind_a = PromptGrind(tier_pack="Tier pack A.")
    grind_b = PromptGrind(tier_pack="Tier pack B.")
    concurrent_a, concurrent_b = await asyncio.gather(
        _run(tmp_path / "concurrent-a", grind=grind_a),
        _run(tmp_path / "concurrent-b", grind=grind_b),
    )
    solo_a = await _run(tmp_path / "solo-a", grind=grind_a)
    solo_b = await _run(tmp_path / "solo-b", grind=grind_b)
    assert concurrent_a.prompt["sha256"] == solo_a.prompt["sha256"]
    assert concurrent_b.prompt["sha256"] == solo_b.prompt["sha256"]
    assert concurrent_a.prompt["sha256"] != concurrent_b.prompt["sha256"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_prompt_grind.py -v`
Expected: FAIL — `TypeError: run_operation_journey() got an unexpected
keyword argument 'grind'` (or similar) on every test, since neither the
parameter nor the `OperationRun.prompt` field exist yet.

- [ ] **Step 3: Add the imports**

In `tests/evals/operation_app.py`, change:

```python
from korvid.evals.harness import EVAL_CLUSTER, build_eval_harness, resolve_eval_policy
```

to:

```python
from korvid.evals.__main__ import prompt_fingerprint
from korvid.evals.harness import (
    EVAL_CLUSTER,
    NO_GRIND,
    PromptGrind,
    build_eval_harness,
    resolve_eval_policy,
)
```

- [ ] **Step 4: Add the `prompt` field to `OperationRun`**

Change:

```python
class OperationRun:
    """One complete journey run: what happened, and how it graded."""

    journey_id: str
    answer: str
    grade: OperationGrade
    journal: tuple[dict[str, Any], ...]
    audit: tuple[dict[str, Any], ...]
    wall_time_s: float
```

to:

```python
class OperationRun:
    """One complete journey run: what happened, and how it graded."""

    journey_id: str
    answer: str
    grade: OperationGrade
    journal: tuple[dict[str, Any], ...]
    audit: tuple[dict[str, Any], ...]
    wall_time_s: float
    prompt: dict[str, Any]
```

- [ ] **Step 5: Thread `grind` through `run_operation_journey`**

In the signature, change:

```python
async def run_operation_journey(
    journey: OperationJourney,
    *,
    audit_path: Path,
    provider_factory: Callable[[], Any],
    model_tier: str | None = None,
    approval_timeout_seconds: float = 5.0,
    turn_timeout: float = 20.0,
) -> OperationRun:
```

to:

```python
async def run_operation_journey(
    journey: OperationJourney,
    *,
    audit_path: Path,
    provider_factory: Callable[[], Any],
    model_tier: str | None = None,
    approval_timeout_seconds: float = 5.0,
    turn_timeout: float = 20.0,
    grind: PromptGrind = NO_GRIND,
) -> OperationRun:
```

Add one line to the docstring's `Args:` section, directly after the
`model_tier` entry:

```
        grind: The eval-only prompt levers (tier pack replacement, eval
            overlay) — identical to the read-only scenario/journey
            harness's grind. Composed after the immutable safety contract;
            never widens the armed-tool surface. Published in the
            returned `OperationRun.prompt`.
```

Where the harness is built:

```python
    harness = build_eval_harness(
        provider=provider,
        execution=executor,
        bridge=agent_ui_proxy,
        policy=policy,
        cluster=EVAL_CLUSTER,
    )
```

becomes:

```python
    harness = build_eval_harness(
        provider=provider,
        execution=executor,
        bridge=agent_ui_proxy,
        policy=policy,
        cluster=EVAL_CLUSTER,
        grind=grind,
    )
```

- [ ] **Step 6: Compute and return the prompt identity**

Directly before the final `return OperationRun(...)` statement, add:

```python
    prompt = prompt_fingerprint(harness.policy, grind=grind)
```

Then change the return statement from:

```python
    return OperationRun(
        journey_id=journey.id,
        answer=answer,
        grade=grade,
        journal=tuple(journal.payload()),
        audit=audit,
        wall_time_s=time.monotonic() - started,
    )
```

to:

```python
    return OperationRun(
        journey_id=journey.id,
        answer=answer,
        grade=grade,
        journal=tuple(journal.payload()),
        audit=audit,
        wall_time_s=time.monotonic() - started,
        prompt=prompt,
    )
```

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_prompt_grind.py -v`
Expected: PASS (4 tests). If the concurrency test is slow, that is
expected — two full scripted journeys run end to end.

- [ ] **Step 8: Run the full existing operation test suite for regressions**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_journeys.py tests/evals/test_operation_bridge_parity.py -q`
Expected: PASS, same count as before this task (the new `prompt` field is
additive; no existing assertion reads `OperationRun`'s field set
exhaustively — confirm this by checking there is no `dataclasses.fields`
or `asdict` equality assertion against `OperationRun` in either file before
relying on this).

- [ ] **Step 9: Typecheck and lint the touched files**

Run: `UV_FROZEN=1 uv run mypy tests/evals/operation_app.py tests/evals/test_operation_prompt_grind.py`
Expected: clean (0 errors).

Run: `UV_FROZEN=1 uv run ruff check tests/evals/operation_app.py tests/evals/test_operation_prompt_grind.py && UV_FROZEN=1 uv run ruff format --check tests/evals/operation_app.py tests/evals/test_operation_prompt_grind.py`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add tests/evals/operation_app.py tests/evals/test_operation_prompt_grind.py
git commit -m "feat(evals): typed prompt grind for operation journeys"
```

---

### Task 2: `operation_campaign.py` CLI flags and per-run/campaign identity

**Files:**
- Modify: `tests/evals/operation_campaign.py`
  - imports (currently lines 30-49)
  - `_parse_args` (currently lines 61-83)
  - `_record` (currently lines 214-258)
  - `_run` (currently lines 339-411, both the success and the exception
    branches)
  - `main` (currently lines 458-541)
- Test: `tests/evals/test_operation_campaign.py` (append new tests)

**Interfaces:**
- Consumes: `run_operation_journey(..., grind: PromptGrind = NO_GRIND)` and
  `OperationRun.prompt: dict[str, Any]` from Task 1;
  `korvid.evals.__main__._read_prompt_file(path: Path | None, flag: str) ->
  str | None` (raises `SystemExit` on a missing/empty/non-UTF-8 file);
  `korvid.evals.__main__.prompt_fingerprint(policy, *, grind=NO_GRIND)`
  (already imported).
- Produces: `_prompt_grind(args: argparse.Namespace) -> PromptGrind`; every
  campaign JSON run record now has a `"prompt"` key; `main()`'s top-level
  `meta.prompts` reflects the CLI's `--tier-pack-file`/
  `--prompt-overlay-file` flags.

- [ ] **Step 1: Write the failing tests**

Append to `tests/evals/test_operation_campaign.py` (add `PromptGrind` to
the existing import from `korvid.evals.harness`... there is currently no
such import in this file; add a new import line, and add `_prompt_grind`
to the existing `from .operation_campaign import (...)` import):

```python
from korvid.evals.harness import PromptGrind
```

and change:

```python
from .operation_campaign import _korvid_revision, _record, _seeds, approval_timeout_for, main
```

to:

```python
from .operation_campaign import (
    _korvid_revision,
    _prompt_grind,
    _record,
    _seeds,
    approval_timeout_for,
    main,
)
```

Then append at the end of the file:

```python
# --- prompt grinding flags --------------------------------------------------


def test_prompt_grind_flags_default_to_unset() -> None:
    args = operation_campaign._parse_args([])
    assert args.tier_pack_file is None
    assert args.prompt_overlay_file is None


def test_prompt_grind_flags_accept_paths() -> None:
    args = operation_campaign._parse_args(
        ["--tier-pack-file", "a.md", "--prompt-overlay-file", "b.md"]
    )
    assert args.tier_pack_file == Path("a.md")
    assert args.prompt_overlay_file == Path("b.md")


def test_prompt_grind_reads_both_layers(tmp_path: Path) -> None:
    pack = tmp_path / "pack.md"
    pack.write_text("Answer in one sentence.", encoding="utf-8")
    overlay = tmp_path / "overlay.md"
    overlay.write_text("Always name the namespace.", encoding="utf-8")
    grind = _prompt_grind(
        operation_campaign._parse_args(
            ["--tier-pack-file", str(pack), "--prompt-overlay-file", str(overlay)]
        )
    )
    assert grind == PromptGrind(
        tier_pack="Answer in one sentence.", overlay="Always name the namespace."
    )


def test_prompt_grind_file_with_invalid_utf8_exits_cleanly(tmp_path: Path) -> None:
    bad = tmp_path / "prompt.md"
    bad.write_bytes(b"\xff\xfe not utf-8")
    args = operation_campaign._parse_args(["--tier-pack-file", str(bad)])
    with pytest.raises(SystemExit, match="--tier-pack-file"):
        _prompt_grind(args)


def test_a_ground_campaign_publishes_the_override_in_meta_and_every_run(
    tmp_path: Path,
) -> None:
    pack = tmp_path / "pack.md"
    pack.write_text("Answer in one short sentence.", encoding="utf-8")
    payload_path = tmp_path / "operations.json"
    code = main(
        [
            "--only",
            "scale-deployment-up",
            "--scripted",
            "--reps",
            "1",
            "--tier-pack-file",
            str(pack),
            "--json",
            str(payload_path),
            "--artifacts",
            str(tmp_path / "artifacts"),
        ]
    )
    assert code == 0
    payload = json.loads(payload_path.read_text())
    assert payload["meta"]["prompts"]["source"] == "override"
    run = payload["runs"][0]
    assert run["prompt"]["source"] == "override"
    assert run["prompt"]["sha256"] == payload["meta"]["prompts"]["sha256"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_campaign.py -k prompt_grind -v`
Expected: FAIL — `--tier-pack-file`/`--prompt-overlay-file` are unrecognized
arguments, and `_prompt_grind` does not exist in `operation_campaign`.

- [ ] **Step 3: Add the imports**

In `tests/evals/operation_campaign.py`, change:

```python
from korvid.evals.__main__ import (
    PROBE_TIMEOUT_SECONDS,
    capture_serving,
    httpx_fetch,
    prompt_fingerprint,
    provider_factory_from_env,
    warn_if_unpinned,
)
from korvid.evals.harness import resolve_eval_policy
```

to:

```python
from korvid.evals.__main__ import (
    PROBE_TIMEOUT_SECONDS,
    _read_prompt_file,
    capture_serving,
    httpx_fetch,
    prompt_fingerprint,
    provider_factory_from_env,
    warn_if_unpinned,
)
from korvid.evals.harness import NO_GRIND, PromptGrind, resolve_eval_policy
```

- [ ] **Step 4: Add the CLI flags**

In `_parse_args`, directly after the `--approval-timeout` argument and
before `--json`:

```python
    parser.add_argument("--approval-timeout", type=float, default=5.0)
    parser.add_argument(
        "--tier-pack-file",
        type=Path,
        default=None,
        help=(
            "replace the tier's operating pack with this file's contents "
            "for every operation journey run. Eval-only prompt grinding: "
            "layered after korvid's immutable safety contract and can "
            "never widen it. Recorded in the result JSON"
        ),
    )
    parser.add_argument(
        "--prompt-overlay-file",
        type=Path,
        default=None,
        help=(
            "layer this file's contents on top of the tier pack as an "
            "eval overlay, published as 'eval-overlay'"
        ),
    )
    parser.add_argument("--json", type=Path)
```

(only the two new `add_argument` calls are new; `--approval-timeout` and
`--json` already exist and anchor the insertion point.)

- [ ] **Step 5: Add the `_prompt_grind` helper**

Directly after `_validated_campaign_inputs` and before `async def _run`,
add:

```python
def _prompt_grind(args: argparse.Namespace) -> PromptGrind:
    """The eval-only prompt levers, read from the CLI's file flags.

    Identical in behavior to `korvid.evals.__main__._prompt_grind` — the
    same fail-closed file reader, reused rather than duplicated.
    """
    return PromptGrind(
        tier_pack=_read_prompt_file(args.tier_pack_file, "--tier-pack-file"),
        overlay=_read_prompt_file(args.prompt_overlay_file, "--prompt-overlay-file"),
    )
```

- [ ] **Step 6: Thread the grind through `_run`**

Change the `_run` signature from:

```python
async def _run(
    args: argparse.Namespace,
    pairs: list[tuple[OperationJourney, GenerationRecord | None]],
    *,
    run_id: str,
    run_dir: Path,
    live_provider_factory: Callable[[], Any] | None = None,
) -> list[dict[str, Any]]:
```

to:

```python
async def _run(
    args: argparse.Namespace,
    pairs: list[tuple[OperationJourney, GenerationRecord | None]],
    *,
    run_id: str,
    run_dir: Path,
    live_provider_factory: Callable[[], Any] | None = None,
    grind: PromptGrind = NO_GRIND,
) -> list[dict[str, Any]]:
```

Inside the loop, change the `run_operation_journey` call from:

```python
                run = await run_operation_journey(
                    instance,
                    audit_path=audit_path,
                    provider_factory=_provider_factory(
                        template_id, args.scripted, live_provider_factory
                    ),
                    model_tier=args.model_tier,
                    approval_timeout_seconds=approval_timeout_for(instance, args.approval_timeout),
                )
```

to:

```python
                run = await run_operation_journey(
                    instance,
                    audit_path=audit_path,
                    provider_factory=_provider_factory(
                        template_id, args.scripted, live_provider_factory
                    ),
                    model_tier=args.model_tier,
                    approval_timeout_seconds=approval_timeout_for(instance, args.approval_timeout),
                    grind=grind,
                )
```

In the `except Exception as exc:` branch's stub `records.append({...})`
dict literal, add a `"prompt": None,` entry (placed next to the other
per-run fields, e.g. directly after `"wall_time_s": 0.0,`) so every record
in the campaign's JSON output — success or error — has the same key set.

- [ ] **Step 7: Publish the per-run prompt identity in `_record`**

In `_record`, add a `"prompt": run.prompt,` entry to the returned dict,
placed directly after `"wall_time_s": run.wall_time_s,`:

```python
        "wall_time_s": run.wall_time_s,
        "prompt": run.prompt,
```

- [ ] **Step 8: Thread the grind through `main`**

In `main`, change:

```python
    try:
        seeds, pairs, live_provider_factory = _validated_campaign_inputs(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
```

to add a grind-parsing step right after it (before the artifact-path/
revision work, so a bad file exits fast, same ordering discipline as
`korvid.evals.__main__.main`):

```python
    try:
        seeds, pairs, live_provider_factory = _validated_campaign_inputs(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        grind = _prompt_grind(args)
    except SystemExit as exc:
        return _exit_code(exc)
```

Change the `_run(...)` call from:

```python
    records = asyncio.run(
        _run(
            args,
            pairs,
            run_id=run_id,
            run_dir=run_dir,
            live_provider_factory=live_provider_factory,
        )
    )
```

to:

```python
    records = asyncio.run(
        _run(
            args,
            pairs,
            run_id=run_id,
            run_dir=run_dir,
            live_provider_factory=live_provider_factory,
            grind=grind,
        )
    )
```

Change:

```python
            "prompts": prompt_fingerprint(policy),
```

to:

```python
            "prompts": prompt_fingerprint(policy, grind=grind),
```

- [ ] **Step 9: Run the new tests to verify they pass**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_campaign.py -k prompt_grind -v`
Expected: PASS (5 tests).

- [ ] **Step 10: Run the full existing campaign test suite for regressions**

Run: `UV_FROZEN=1 uv run pytest tests/evals/test_operation_campaign.py -q`
Expected: PASS, same count as before this task plus the 5 new tests.
(`test_a_scripted_campaign_writes_a_provenance_stamped_artifact`'s
`set(meta["prompts"]) == {"pack", "overlays", "source", "sha256"}`
assertion must still pass unmodified — the key set inside `meta.prompts`
does not change, only its values become grind-aware when a grind is
supplied, and the new `"prompt"` key is added to `runs[i]`, a dict the
existing test does not assert the full key set of.)

- [ ] **Step 11: Typecheck and lint the touched files**

Run: `UV_FROZEN=1 uv run mypy tests/evals/operation_campaign.py tests/evals/test_operation_campaign.py`
Expected: clean.

Run: `UV_FROZEN=1 uv run ruff check tests/evals/operation_campaign.py tests/evals/test_operation_campaign.py && UV_FROZEN=1 uv run ruff format --check tests/evals/operation_campaign.py tests/evals/test_operation_campaign.py`
Expected: clean.

- [ ] **Step 12: Commit**

```bash
git add tests/evals/operation_campaign.py tests/evals/test_operation_campaign.py
git commit -m "feat(evals): operation campaign CLI prompt-grind flags"
```

---

### Task 3: Documentation and full verification

**Files:**
- Modify: `docs/evals/operations.md`

**Interfaces:**
- Consumes: nothing new (prose only).
- Produces: nothing consumed by later tasks — this is the final task.

- [ ] **Step 1: Add the docs subsection**

In `docs/evals/operations.md`, directly after the "## Running the pack"
section's existing prose paragraph (the one ending "...instead of the
default so it does not idle.") and before "## Metamorphic generation",
insert:

```markdown
## Grinding the operation prompt

`run_operation_journey` and `tests.evals.operation_campaign` accept the
same typed `korvid.evals.harness.PromptGrind` the read-only scenario and
conversational-journey harnesses already use — no separate type, no
monkeypatching. `--tier-pack-file` replaces the tier's operating pack text
for every journey the campaign runs; `--prompt-overlay-file` layers
additional text on top as an `eval-overlay`. Both compose *after*
`PromptHarness`'s immutable safety contract, exactly as they do for
scenarios and journeys — a grind can never precede or replace it, and it
never widens the armed-tool surface, which is controlled entirely by the
policy environment.

Every run publishes its own prompt identity as a `"prompt"` field
(`pack`, `overlays`, `source`, `sha256`), and the campaign-level
`meta.prompts` reflects the same override. `source` is `"default"` when
the grind reproduces korvid's own shipped wording byte for byte, or
`"override"` otherwise — omitting both flags is unaffected: every field
this section describes is additive to what the campaign already
published.
```

- [ ] **Step 2: Run the docs-link/anchor validation tests**

Run: `UV_FROZEN=1 uv run pytest tests/test_docs_links.py tests/test_docs_agent_contracts.py -q`
Expected: PASS (no new cross-links were added, so this should be
unaffected; run it to confirm the new prose did not break an existing
anchor).

- [ ] **Step 3: Commit the docs change**

```bash
git add docs/evals/operations.md
git commit -m "docs(evals): document the operation prompt grind"
```

- [ ] **Step 4: Run the full targeted eval suite**

Run: `UV_FROZEN=1 uv run pytest tests/evals/ -q`
Expected: PASS, with a passed count equal to the pre-change count plus the
9 new tests added across Tasks 1 and 2 (4 in
`test_operation_prompt_grind.py`, 5 in `test_operation_campaign.py`).

- [ ] **Step 5: Run static checks**

Run: `UV_FROZEN=1 uv run mypy` (no path argument — full strict run)
Expected: clean.

Run: `UV_FROZEN=1 uv run ruff check src/ tests/ && UV_FROZEN=1 uv run ruff format --check src/ tests/`
Expected: clean.

Run: `UV_FROZEN=1 uv run tach check`
Expected: clean (no module boundary changed).

- [ ] **Step 6: Run the full test suite**

Run: `UV_FROZEN=1 uv run pytest -q`
Expected: PASS, full green (this run takes roughly 19 minutes based on
prior full-suite timing in this repository).

- [ ] **Step 7: Confirm `uv.lock` is untouched**

Run: `git status --short uv.lock`
Expected: no output (clean).

- [ ] **Step 8: Run pre-commit on every touched file**

Run:
```bash
UV_FROZEN=1 uv run pre-commit run --files \
  tests/evals/operation_app.py \
  tests/evals/operation_campaign.py \
  tests/evals/test_operation_prompt_grind.py \
  tests/evals/test_operation_campaign.py \
  docs/evals/operations.md \
  docs/superpowers/specs/2026-08-27-operation-prompt-grind-design.md \
  docs/superpowers/plans/2026-08-27-operation-prompt-grind.md
```
Expected: every hook passes.
