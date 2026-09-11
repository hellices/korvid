# Kubernetes Reliability Implementation Plan

> **For agentic workers:** Use `superpowers:subagent-driven-development` for
> independent tasks and TDD for every correction. The coordinator owns the
> LIST/discovery/watch transport changes and integrates the remaining tasks.

**Goal:** Resolve #343, #340, #341, #345, #338, #337, and #339, then open a PR
and complete the repository's review loop without merging.

**Architecture:** Keep existing whole-collection reads for callers that require
complete snapshots; give the bounded tool path its own lazy, paged read boundary.
Bound owned asynchronous cleanup independently of Kubernetes cleanup. Preserve
partial observability evidence, prefer API versions by resource identity, and
correct Kubernetes accounting/admission/polling at their existing source functions.

**Tech Stack:** Python 3.11+, asyncio, kubernetes_asyncio, pytest, Textual.

## Global Constraints

- No lockfile or dependency changes; use frozen/offline dependency operations.
- Keep layer-boundary interfaces abstract and dependencies constructor-injected.
- Preserve approval, redaction, fail-closed audit, and namespace boundaries.
- Add failing regression tests before each implementation change.
- Run targeted checks during implementation and `make check` before pushing.
- Do not merge, enable auto-merge, or approve this PR.

## Task 1 — Bounded LIST (#343)

**Files:** `src/korvid/k8s/reads.py`, `src/korvid/k8s/client.py`,
`src/korvid/tools/executor.py`, read-boundary evaluation adapters, and their tests.

**Interface:** Add `ReadOps.iter_objects(meta, namespace)` returning an explicitly
closable async generator of `GenericSummary`. Request at most 100 objects per
page, follow continuation tokens only on demand, and retain only the current page.

- [x] Add multi-page, error, early-close, and large-collection regression tests.
- [x] Prove the current implementation renders the entire collection despite
  `MAX_RESULT_CHARS`, using a render-call counter rather than timing assertions.
- [x] Implement paging, per-page telemetry, and cooperative page transitions.
- [x] Render incrementally, close the iterator on every exit, and reserve space
  for the explicit truncation suffix within `MAX_RESULT_CHARS`.
- [x] Update concrete read implementations and namespace-guarded fakes together.
- [x] Run `uv run pytest -p no:tach tests/tools/test_list_resources.py tests/k8s/test_client.py tests/evals/test_live_journey.py`.

## Task 2 — Bounded Teardown (#340)

**Files:** `src/korvid/__main__.py`, `src/korvid/mcp/server.py`,
`tests/test_agent_cleanup.py`, related wiring, recovery, and MCP tests.

- [x] Reproduce a stuck provider, replacement close tasks, and cancellation-resistant MCP shutdown with event-driven tests.
- [x] Put every owned close task in run state; drain under explicit deadlines.
- [x] Ensure provider failures cannot prevent `kube.close()` and consume/log task exceptions.
- [x] Define and test the terminal policy for non-cooperative MCP tasks; never add an unbounded final await.
- [x] Bound runner finalization, including executor threads and async generators, with subprocess regression tests.
- [x] Run `uv run pytest -p no:tach tests/test_agent_cleanup.py tests/test_main_wiring.py tests/test_main_recovery.py tests/mcp/test_server.py`.

## Task 3 — Incomplete Observability (#341)

**Files:** `src/korvid/obs/{connector,prometheus,loki}.py`, `tests/obs/`, `tests/tools/test_observability_tools.py`.

- [x] Reproduce mixed-validity and all-malformed backend responses for both connectors.
- [x] Preserve valid rows while explicitly recording omissions; never show a complete empty result after dropping data.
- [x] Cover tool rendering as well as parser result metadata.
- [x] Run `uv run pytest -p no:tach tests/obs tests/tools/test_observability_tools.py`.

## Task 4 — All Served API Versions (#345)

**Files:** `src/korvid/k8s/client.py`, `tests/k8s/test_discovery.py`,
`tests/k8s/test_discovery_versions.py`.

- [x] Reproduce kinds split between a preferred version and another served version.
- [x] Fetch each valid advertised group/version once with bounded concurrency.
- [x] Deduplicate group/resource identities in preferred-version order and isolate broken versions.
- [x] Test malformed advertisements and aliases, bootstrap deadlines, preferred-version failure, and concurrency bounds.
- [x] Run `uv run pytest -p no:tach tests/k8s/test_discovery.py tests/k8s/test_discovery_versions.py`.

## Task 5 — PDB Admission (#338)

**Files:** `src/korvid/k8s/drain.py`, `tests/k8s/test_drain.py`.

- [x] Test unready Pods with healthy counts below, equal to, and above the desired budget.
- [x] Apply default/explicit `IfHealthyBudget` and `AlwaysAllow` semantics while keeping stale and multiple-match checks fail-safe.
- [x] Run `uv run pytest -p no:tach tests/k8s/test_drain.py`.

## Task 6 — RuntimeClass Overhead (#337)

**Files:** `src/korvid/k8s/models.py`, `tests/k8s/test_models.py`,
`tests/k8s/test_pod_overhead.py`.

- [x] Test CPU/memory overhead for requests, nonzero limits, overhead-only resources, pod-level precedence, and restartable init containers.
- [x] Add overhead after selecting effective requests/limits using Kubernetes PodRequests/PodLimits rules.
- [x] Preserve fractional quantities through aggregation and overhead, rounding final memory totals as Kubernetes `Quantity.Value()` does.
- [x] Run `uv run pytest -p no:tach tests/k8s/test_models.py tests/k8s/test_pod_overhead.py`.

## Task 7 — Poll Snapshot Diffs (#339)

**Files:** `src/korvid/k8s/client.py`, `tests/k8s/test_client.py`.

- [x] Test unchanged, changed, added, deleted, and replaced object snapshots.
- [x] Compare projected summaries rather than volatile transport metadata; emit no event for unchanged rows.
- [x] Preserve initial snapshot and poll/watch fallback behavior.
- [x] Run `uv run pytest -p no:tach tests/k8s/test_client.py tests/k8s/test_helm.py tests/core/test_watch.py`.

## Integration Evidence

- [x] Run touched-file ruff checks, formatting, pre-commit, and `uv run tach check`.
- [x] Run `make check` with coverage: **10,955 passed, 25 skipped; 92.35% coverage**.
- [x] Run deptry and the strict documentation build.
- [x] Complete independent task and integrated reviews, including encoded-response and subprocess lifecycle regressions.

Local validation used `UV_NO_SYNC=1 PYTEST_ADDOPTS=--cov make check` with
Python 3.14 and an existing development environment. Locked artifacts were
unavailable on this network; dependencies and `uv.lock` remain unchanged.
The PR's required CI validates the frozen Python 3.11–3.13 and Windows environments.

## Pull Request Review Procedure

- Push and open a PR linking all seven issues.
- Read every review comment, including suppressed findings; fix credible issues with RED/GREEN tests.
- Run the full gate before each review-fix commit, reply to each thread, resolve it, and re-request review.
- Stop after two consecutive advisory-only rounds; verify every required check is successful and hand back without merging.
