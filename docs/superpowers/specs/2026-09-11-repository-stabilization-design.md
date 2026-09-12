# Repository Stabilization Design

**Date:** 2026-09-11
**Status:** Approved in maintainer discussion; amended during PR #385 review

## Purpose

Stabilize the repository before starting another product feature by doing four
bounded pieces of work:

1. retire open issues whose premises no longer describe the product;
2. finish the structural decomposition of `src/korvid/ui/app.py` and prevent
   large modules from silently growing again;
3. turn the intermittent Windows documentation-harness failure into an
   evidence-producing, bounded failure before selecting a runtime fix; and
4. harden pull-request CI, default-branch rules, and the boundary around the
   self-hosted runner pool.

The work is deliberately separate from Kubernetes and agent product fixes.
Issues such as #343 and #340 will be handled in later sessions so their test,
review, and rollback boundaries remain small.

## Verified baseline

The design is based on the following state observed on 2026-09-11:

- `main` and `origin/main` both point to
  `01233c5c7603b6e37db55e76aa45ee5043ac77f2`; the checkout was clean.
- The pre-existing stash beginning `codex: preserve
  chore/require-maintainer-approval` remains untouched.
- `src/korvid/ui/app.py` is 2,821 physical lines. It was 2,620 lines immediately
  after PR #314, and its current `KorvidApp.__init__` spans 541 lines.
- The same file contains the Textual app, declarative bindings/CSS, the complete
  controller graph, and roughly 700 lines of `App*` boundary adapters.
- Main CI run 34577805068 passed all three Linux test legs with 10,684 tests and
  92.07% coverage, but failed two Windows Node-harness tests.
- One failed harness did not emit its first synchronous write in ten seconds.
  The other emitted every scenario and `stage=complete` with zero active
  resources, handles, and requests, but did not reach `before-exit` before the
  same deadline. Subsequent Python and Node probes succeeded.
- Pull-request jobs in `ci.yml` and `codeql.yml` execute untrusted source on the
  custom `korvid-runners` pool. Repository-visible configuration cannot prove
  the pool's pod, service-account, network, node, or host-path isolation.
- The default-branch ruleset does not require `security`,
  `dependency-review`, or CodeQL, and does not require the branch to be current
  with `main`.
- Actions are SHA-pinned in the workflows, but repository setting
  `sha_pinning_required` is false.
- CodeQL alert #16 points at an intentionally world-writable decoy file in a
  test; production creation uses a private temporary file and mode `0600`.

## Scope and non-goals

### In scope

- Source-only refactoring that preserves `KorvidApp` behavior and its security
  invariants.
- A fast, platform-independent source-size gate in pre-commit and CI.
- Bounded Windows process diagnostics and deterministic CI seed reporting.
- CI runner selection, timeouts, and correction of the experimental `ty` job.
- Repository ruleset and Actions-policy changes described below.
- Closing #195 and #307 with explanatory comments.

### Out of scope

- No Kubernetes client, resource accounting, drain, polling, observability, or
  agent lifecycle behavior is changed.
- No retry, skip, or longer timeout is used to hide Windows harness failures.
- No runner infrastructure is provisioned or modified from this repository.
- No dependency is added and `uv.lock` remains byte-identical.
- PR #385 is the authorized delivery path. It is reviewed and updated in place;
  no branch is merged, auto-merged, or approved by the agent.
- No existing stash or unrelated worktree is removed.

## Issue disposition

### Close #195

`Define a versioned analyzer, view, and read-tool extension API` combines three
extension surfaces with different readiness levels. Its analyzer premise is
still unvalidated, and the versioned provider API used as its precedent has
since been replaced by data-driven special flows. Closing it is a
`not planned` decision, not a claim that plugin work was implemented. A future
tool or panel extension must start from a concrete consumer and receive its own
narrow issue.

### Close #307

`Reframe the small profile as a bounded Kubernetes operator...` is no longer an
actionable umbrella:

- PR #312 delivered its stateful operation-journey foundation;
- `small` and `full` were retired in favor of `low` and `high` model tiers; and
- the remaining phase-specific tool-surface work is product behavior that must
  be justified and scoped independently.

The close comment will name PR #312 and state explicitly that dynamic
phase-specific low-tier tool exposure was not implemented. A later product
session may file a new issue after rechecking the current tier contracts.

### Keep the remaining issues

- #371 is active because the latest `main` CI reproduced it.
- #347 is a deliberate security activation dossier, not an implementation task.
- #337-#345 each still describes a concrete correctness or reliability gap.

No other issue will be closed merely because it is in the backlog.

## `app.py` architecture

### Target responsibilities

`src/korvid/ui/app.py` will retain only the Textual application shell:

- widget composition and mounted-widget lookup;
- Textual lifecycle and message translation;
- action entry points that delegate to controllers;
- table/status rendering that directly owns mounted widgets; and
- session state that Textual itself owns.

The following responsibilities move out:

1. `app_bindings.py` owns `APP_BINDINGS`, handler-key help, and app CSS.
2. `app_surfaces.py` owns `_RelationshipLister` and every `App*` adapter that
   implements a controller boundary over the live Textual app.
3. `app_runtime.py` owns only the typed input/runtime records and the typed
   one-use references needed to describe the session-scoped UI controller
   graph. It does not construct that graph.

`app_surfaces.py` and `app_runtime.py` may refer to `KorvidApp` only under
`TYPE_CHECKING`; neither imports `app.py` at runtime. Controllers continue to
depend on named ABCs and protocols and do not import the app.

### Runtime assembly

`app_runtime.py` defines two internal dataclasses:

- `AppRuntimeInputs`: external collaborators and feature switches already
  supplied by `src/korvid/__main__.py`;
- `AppRuntime`: the constructed controllers, surfaces, and shared UI state that
  the Textual shell delegates to.

`KorvidApp.__init__` keeps its existing keyword interface. It validates the
approval timeout, stores the small set of fields owned by the shell, and packs
the remaining arguments into an immutable `AppRuntimeInputs` record. It does
not call controller constructors. A one-time `bind_runtime(AppRuntime)` method
injects the completed graph and rejects a second bind.

`src/korvid/__main__.py` creates the Textual shell, constructs every controller
against that real shell, resolves the existing cycles with the typed one-use
references, and then binds the completed `AppRuntime`. This makes the file the
actual and structurally enforced composition root rather than merely the
caller of a second root in `ui/`.

Direct repository tests use `tests/app_factory.py`, which constructs the chosen
`KorvidApp` subclass and invokes the production root's assembly entry point.
This keeps test setup explicit and prevents an unbound shell from becoming a
quiet alternate construction path. An AST contract rejects controller-graph
constructor calls in `app.py` and `app_runtime.py`, requires those calls in
`__main__.py`, and rejects direct `KorvidApp(...)` calls elsewhere under
`tests/`.

Moving the graph does not justify growing another monolith. Pure records,
adapters, and lifecycle helpers used by the root move to
`src/korvid/composition_support.py`; that module may define behavior-neutral
adapters but may not construct the UI controller graph. The existing
`src/korvid/__main__.py` 1,773-line ratchet remains unchanged.

No controller workflow is rewritten during this move. Any behavior change
discovered while extracting is split into a separate failing test and commit.

### Size targets

After extraction:

- `src/korvid/ui/app.py` must contain at most 1,500 physical lines;
- `KorvidApp.__init__` must span at most 160 physical lines;
- each new extracted module must remain below the repository default of 1,200
  lines.

These are upper bounds, not targets to fill.

## Source-size ratchet

Add `scripts/check_source_size.py`, implemented using only the Python standard
library. It checks every tracked Python module under `src/korvid` on every run.

Policy:

- the default maximum is 1,200 physical lines;
- `ui/app.py` has an explicit post-refactor maximum of 1,500 lines plus the
  160-line constructor maximum;
- existing modules already above 1,200 lines are grandfathered at no more than
  their 2026-09-11 baseline and therefore may shrink but may not grow;
- every grandfathered entry contains a non-empty rationale;
- a new exception requires an explicit policy edit visible in review; and
- missing files, malformed policy entries, duplicate entries, and unreadable
  UTF-8 fail closed.

The initial grandfathered modules are `src/korvid/__main__.py`,
`src/korvid/core/config.py`, `src/korvid/k8s/client.py`,
`src/korvid/tools/executor.py`, `src/korvid/tools/registry.py`,
`src/korvid/ui/agent_ui_controller.py`, `src/korvid/ui/workspace_controller.py`,
and `src/korvid/ui/widgets/resource_table.py`. Their exact caps are recorded
from `origin/main` immediately before the gate is introduced.

The checker is wired as a local pre-commit hook with `pass_filenames: false`,
and `make check` runs it explicitly. CI already runs both paths. Unit tests
exercise boundary acceptance, one-line overflow, invalid exceptions, the
constructor rule, and the real repository policy. `docs/dev/quality-gates.md`
documents how to respond: split responsibility, reduce the module, or justify
a narrowly reviewed exception; never bypass the hook.

## Windows harness investigation

### Current hypothesis status

There is not yet enough evidence to call this a Node assertion defect, a V8
startup defect, or Windows runner starvation. The two failure shapes share only
the process/scheduling boundary. Therefore this change gathers evidence and
does not pretend to fix an unproven cause.

### Process runner

Replace the test helper's `subprocess.run` wrapper with a bounded `Popen`
lifecycle:

1. start Node with stdin closed and stdout/stderr directed to the existing
   temporary files;
2. wait for the existing ten-second deadline;
3. on timeout, capture the child PID, `poll()` state, elapsed monotonic time,
   and a bounded `psutil` process snapshot before termination;
4. terminate the child under a second short bound, escalating to kill only if
   necessary; after a successful `kill()` wait until the child is confirmed
   reaped, or record a type-only `reap=error` and continue without claiming
   confirmation if that final wait raises `OSError`. If `kill()` itself fails,
   perform one final bounded wait and report `reap=timed-out` or the concrete
   wait error instead of hanging the entire test process;
5. read bounded head/tail diagnostics and run the existing independently
   bounded Python, Node-version, and loader probes; and
6. re-raise the original `TimeoutExpired` with no retry.

The process snapshot contains only status, CPU times, thread count, and memory
size. It never records command-line arguments, environment values, open file
names, network endpoints, or other potentially sensitive runner state.

### JavaScript milestones

A tiny CommonJS preload writes a synchronous `node-started` milestone before
the ESM harness is loaded. `harness_lifecycle.mjs` adds elapsed monotonic
milliseconds to subsequent milestones. This distinguishes:

- Node/V8 never reached the preload;
- preload ran but ESM imports did not finish;
- harness scenarios stalled; and
- harness called `finish()` but Node did not deliver `beforeExit`/`exit`.

Real JavaScript assertions and the ten-second bound remain unchanged.

### CI evidence

The Windows job prints and passes an explicit pytest-randomly seed derived from
the workflow run ID. The full-suite command remains a single non-retried run.
Job-level timeout prevents a genuinely wedged suite from consuming a runner
indefinitely.

#371 remains open until a Windows recurrence supplies the new evidence and a
single root-cause hypothesis can be tested. If the branch does not receive an
explicitly authorized pull request, Windows validation is reported as pending;
local macOS success is not presented as Windows proof.

## CI and runner hardening

### Untrusted pull-request code

For every job in `ci.yml` and `codeql.yml` that checks out and executes pull
request source:

- `pull_request` uses a GitHub-hosted runner (`ubuntu-latest` or the existing
  `windows-latest`);
- trusted `push`/scheduled runs may continue to use `korvid-runners` where they
  do today; and
- release, AKS contract, and relock workflows keep their existing trusted-only
  triggers and runner choices.

This removes repository reliance on unverifiable self-hosted isolation for
untrusted code. The existing `all_external_contributors` approval policy
remains defense in depth, not the primary isolation boundary.

A workflow-contract test fails if an ordinary pull-request execution path is
changed back to an unconditional custom runner.

### Bounded jobs

Add explicit job timeouts sized above measured healthy durations:

- Linux matrix and Windows full test: 45 minutes;
- pre-commit and CodeQL: 20 minutes;
- security and experimental `ty`: 15 minutes;
- dependency review and change classification: 10 minutes.

These bounds do not replace command-level diagnostics or cancel healthy
parallel work.

### Useful experimental typing

The `ty-experimental` job first synchronizes the locked project with all
supported extras, then runs `ty` inside that environment. Failure remains
non-blocking through `continue-on-error`; the shell-level `|| true` is removed
so the job accurately records the step result. No `ty` version is added to the
project lock in this change.

The Python 3.11/3.12/3.13 and Windows compatibility matrix remains intact.
The existing docs-only classification remains intact. Reducing supported legs
would violate the documented compatibility contract and is not an efficiency
optimization available to this work.

## Repository settings

After source changes are locally verified, perform these explicit external
updates:

1. Dismiss CodeQL alert #16 as `used in tests`, with a comment naming the
   deliberate permissive-file decoy and the production `0600` path.
2. Update the default-branch ruleset to require `security`,
   `dependency-review`, and CodeQL analysis in addition to the current required
   checks.
3. Enable strict required-status-check evaluation against current `main`.
4. Add a CodeQL code-scanning rule that blocks new high-or-critical security
   alerts after the existing false positive is dismissed.
5. Enable repository-level SHA pinning enforcement for Actions while leaving
   the current action allowlist policy otherwise unchanged.

The approving-review count remains zero. This repository currently has one
maintainer, agents may not approve their own work, and setting the count to one
would make maintainer-authored changes impossible to land without adding a real
second reviewer. Thread resolution, stale-review dismissal, and the ban on
ruleset bypass remain enabled.

Settings are read back after mutation and compared with the intended payload.
No merge method, auto-merge, release rule, or branch-deletion setting changes.

## Validation

Implementation follows red-green-refactor cycles. The final evidence must
include:

- unit tests for source-size policy and the Windows process runner;
- targeted UI wiring and adapter tests after each extraction;
- workflow contract tests for runner selection, timeouts, required job names,
  and the `ty` invocation;
- Ruff and formatting on touched Python files;
- mypy over `src` and `tests`;
- `tach check` after import movement;
- `make check` with all tests passing;
- `uv.lock` byte identity against `origin/main`;
- clean `git diff --check`; and
- read-back evidence for every GitHub issue, alert, Actions, and ruleset update.

Local development uses the worktree's proxy-resolved `.venv` through
`UV_NO_SYNC=1 uv run ...`; this avoids an implicit sync or re-lock while still
using uv's configured corporate package-feed proxy. `uv.lock` remains
byte-identical to `origin/main`. CI on the exact pushed commit is the
cross-platform evidence.

## Delivery and later sessions

This session produces small commits in this order:

1. approved design and implementation plan;
2. `app.py` extraction with behavior preserved;
3. source-size ratchet and documentation;
4. Windows evidence runner;
5. CI and workflow hardening;
6. issue and repository-setting disposition with read-back evidence;
7. bounded kill-failure cleanup after review; and
8. restoration of root-owned UI runtime assembly after review.

PR #385 is updated and re-reviewed until every credible finding is addressed
and every required check is successful. The maintainer alone decides whether
to merge it.

Suggested later product sessions are:

1. #343 alone: bounded Kubernetes LIST paging and memory behavior;
2. #340 alone: provider/MCP teardown ownership and deadlines;
3. #341 and #345 as separate integration/discovery correctness sessions; and
4. #337, #338, and #339 as independently testable Kubernetes semantics work,
   not one bulk patch.
