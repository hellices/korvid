# Command and Resource Refactoring Plan

> **For agentic workers:** Use subagent-driven-development to implement each
> independently testable workstream in the isolated worktree below.

**Workspace:** `.worktrees/refactor-command-resources-20260906`

**Branch:** `refactor/command-resources-20260906`

**Base:** latest fetched `origin/main`, commit
`e60f0be792f89a3c574f76a5208b281bab4af69d`.

**Goal:** Define built-in commands once and share resource retrieval/watch
machinery without losing discovered resource identity.

**Architecture:** Replace parallel command definitions and resource watch
implementations completely. Retain Textual messages, constructor injection,
and genuinely different presentation projections, not legacy routing or
compatibility fallbacks. Resolve API resources by group/plural and synthetic
views separately, then use one snapshot/watch/poll implementation.

**Tech Stack:** Python 3.11+, Textual, kubernetes_asyncio, pytest, Ruff, mypy, tach.

## Global Constraints

- Do not change agent/provider selection, authentication, runtime, or evaluation
  provider behavior. The eval interaction fixture's call to the shared drill
  API must migrate when that API changes; this is not a provider refactor.
- Do not change dependencies, lockfiles, approval policy, masking, or audit gates.
- Do not edit the original checkout. Preserve its staged and unstaged user work.
- The user explicitly requires a clean replacement. Remove superseded code and
  migrate all callers/tests in the same change. Do not add dual routing,
  duck-typed compatibility, ignored arguments, or old/new execution modes.
- Preserve existing command spelling, aliases, namespace/context handling,
  capability checks, column contents, UID preconditions, and synthetic Helm UX.
- Run validation with `uv run --frozen --no-sync` to avoid rewriting the lock.
- The earlier dirty-checkout audit is not a baseline for this worktree. Recheck
  current main before applying findings; its app is decomposed into controllers.
- Locked environment setup could not fetch a public PyPI artifact. Until it is
  available, run the existing dependency environment read-only with
  `PYTHONPATH="$PWD/src" /Users/hwang-inhwan/workspace/kube/.venv/bin/python -m ...`;
  verify that `korvid.__file__` points to this worktree. Do not rewrite the lock.

## Design Decisions

Three options were considered:

1. Patch missing completion entries and individual resource collisions. This
   leaves parallel definitions and transports, so it does not satisfy the goal.
2. Replace navigation/store/framework contracts wholesale. This needlessly
   expands compatibility and safety risks.
3. **Selected:** centralize definitions and transport, migrate every consumer,
   delete superseded implementations, and retain only domain-specific
   projections. Implementation checkpoints are for verification, not a
   staged compatibility rollout.

### Command contract

`ui/command.py` owns immutable command descriptors, aliases, help rows, argument
completion kinds, and parsers. App-owned operations produce a dedicated
`BuiltinCommand`, not an `UnknownCommand`. Existing navigation/context/sort/quit
messages remain meaningful typed messages. The app binds command operation IDs
to existing handlers; it must not parse the original command text again.

### Resource contract

`ResourceMeta` owns API identity and qualified naming. A shared alias resolver
preserves the existing preferred bare alias while retaining a qualified alias
for every grouped resource. Synthetic metadata is distinguished explicitly.
Source/watch/get/describe/relationship consumers retain the resolved identity
instead of collapsing to a bare plural.

Pod rich and lean models remain distinct projections, but common Pod facts and
specialized model selection are centralized. Specializations use authoritative
discovery groups; unrelated CRDs keep generic presentation.

LIST, WATCH, resourceVersion anchoring, polling fallback, and read telemetry
share one transport. Snapshot rows are marked explicitly as `SNAPSHOT` within
the existing event tuple contract. They upsert rows but do not reset a failed
WATCH's retry budget. Explicit `WatchProgress` values carry live-event and
successful-poll health independently of projected rows; completed streams retain
their health semantics. Helm projections preserve snapshot and progress signals.

## Workstream A: Commands

**Files:** `src/korvid/ui/command.py`, `ui/messages.py`,
`ui/widgets/command_bar.py`, `ui/widgets/namespace_picker.py`,
`tests/ui/test_command.py`, targeted command-bar tests.

**Produces:** typed `BuiltinCommand`; descriptor-derived `command_help`,
`command_words`, and argument-completion lookup. The implementation documents
exact public signatures before app integration.

- [x] Add failing tests for all registered aliases, built-in precedence over CRD
  aliases, command completion, scope/context arguments, and unknown commands.
- [x] Run command tests using the verified dependency environment described above.
- [x] Implement descriptors and derived surfaces; remove legacy duplicate sets.
- [x] Make namespace picker emit `NavigateCommand(view=None, namespace=...)`.
- [x] Run command and widget tests, Ruff, and targeted mypy.

## Workstream B: Resource Transport

**Files:** `src/korvid/k8s/client.py`, a focused transport module if needed,
`src/korvid/core/watch.py`, `tests/k8s/test_client.py`,
`tests/k8s/test_helm.py`, `tests/core/test_watch.py`.

**Consumes:** `ResourceMeta`, `_request_json`, `_make_raw_watch_callable`, existing
summary converters and Helm tracker.

**Produces:** shared snapshot/watch/poll implementation and
`KubeClient.watch_resources(meta, namespace)` returning an explicitly closable
`AsyncGenerator[WatchEvent[PodSummary | GenericSummary], None]`. Retire public
Pod/generic/Helm watch entrypoints after migrating all callers, including
performance and contract adapters. Resource-specific projections are private
implementation details of the single entrypoint.

- [x] Add failing tests that exercise shared transport for namespaced and
  cluster-wide Pods, generic resources, and Helm selectors.
- [x] Add a regression with `max_retries=2`, a snapshot row on every connection,
  and a failing WATCH; require exactly two attempts and an error report.
- [x] Run the new cases and establish RED.
- [x] Consolidate LIST/WATCH/polling; retain resourceVersion, 403/405/410 behavior,
  cancellation, custom columns, telemetry, and rich/lean outputs.
- [x] Propagate `SNAPSHOT` markers through Helm aggregation and adjust event
  contract assertions rather than adding a hidden compatibility fallback.
- [x] Run client/Helm/watch tests and targeted Ruff/mypy.

## Workstream C: Resource Identity and Model Projections

**Files:** `src/korvid/k8s/discovery.py`, `k8s/models.py`, `k8s/relations.py`,
`tests/k8s/test_discovery.py`, `tests/k8s/test_models.py`,
`tests/k8s/test_relations.py`.

- [x] Add collision tests: two groups sharing a plural, native versus foreign
  ReplicaSet/Pod, synthetic Helm versus Flux HelmRelease, and missing TypeMeta.
- [x] Run these tests to establish RED.
- [x] Add shared identity/qualified-alias resolution helpers.
- [x] Dispatch specialized summaries by authoritative group/kind and reuse Pod
  status facts across rich/lean projections.
- [x] Make drill relations identity-aware; preserve native rollout and Helm
  history behavior while refusing foreign-group lookalikes.
- [x] Run discovery/models/relations tests and targeted Ruff/mypy.

## Workstream D: Integration and Documentation

**Files:** `src/korvid/__main__.py`, `ui/app.py`, `ui/command_router.py`,
`ui/workspace_controller.py`, `ui/agent_ui_controller.py`,
`ui/relationship_controller.py`, `ui/widgets/resource_table.py`,
`tests/test_main_wiring.py`, targeted navigation/describe/relationship tests,
`docs/tui.md`, `docs/dev/ui-controllers.md`.

- [x] Replace duplicate completion seeds and unknown-command built-in parsing
  with descriptor-derived words and typed message dispatch.
- [x] Stop suppressing discovered HelmRelease collisions; seed synthetic aliases
  deliberately and preserve all qualified API aliases.
- [x] Wire `watch_resources` once; dispatch synthetic manifest reads only for
  synthetic metadata, never for a matching plural alone.
- [x] Preserve identity in agent describe, relationship lookups, drill-down,
  table selection, and store/watch bucket keys.
- [x] Verify same-name resources can occupy independent buckets and that
  namespace picker preserves the current resource view.
- [x] Update contributor/user docs for command definitions and qualified views.

## Final Verification

- [x] Run related targeted test files together per runner.
- [x] Run Ruff lint and `ruff format --check` on
  touched Python files.
- [x] Run strict mypy on changed Python files and tach using the verified
  dependency environment described above.
- [x] Review the aggregate diff for scope, resource identity, snapshot health,
  approval boundaries, and removed duplicate registrations.
- [x] Confirm no changes to agent/provider implementations or evaluation
  provider selection, dependency manifests, lockfiles, or gate configuration.
- [x] Report completed changes, actual verification results, and unrelated
  pre-existing failures without claiming a clean full suite.

## Execution Results

- Implemented command, transport, identity, model, presentation, and integration
  workstreams. Superseded command routing and watch methods were removed.
- Original checkout restored to its pre-task staged/unstaged state. All refactor
  changes live in the isolated worktree based on `e60f0be7`.
- RED regression tests covered command routing/completion, resource collisions,
  summary dispatch, snapshot retry accounting, and raw snapshot retention.
- Final combined targeted regression run: **1037 passed in 254.87 seconds**.
- All **39 changed Python files** passed Ruff lint/format checks and targeted
  strict mypy; tach passed.
- Independent command, identity/presentation, and whole-refactor reviews passed.
  The final reviewer found no significant issues and additionally confirmed
  27 resource-routing tests and 25 watch/poll tests.
- At the implementation handoff, the branch and worktree were retained without
  committing or publishing. The subsequent PR review work is maintainer-requested;
  merge remains a separate maintainer decision.
- Provider connection/evaluation behavior is unchanged. The eval interaction
  fixture only migrated its call to the shared resource drill API.
- Dependency files remain byte-identical to the main baseline. Tests used the
  existing dependency environment with this worktree's source on `PYTHONPATH`;
  the public-PyPI download failure did not require changing dependencies.

### Pre-PR validation

- Full `make check`: **8726 passed, 22 skipped** on Python 3.13.12; full strict
  mypy checked 489 files, and Ruff plus tach passed.
- Existing pre-commit checks and deptry passed.
- The isolated environment was restored from hash-pinned locked requirements.
  Two versions unavailable on the corporate mirror retained their installed
  versions locally: `regex==2026.7.19` and
  `types-regex==2026.7.19.20260720`. CI must verify the exact unchanged lockfile,
  including `regex==2026.9.3` and `types-regex==2026.8.31.20260901`.
