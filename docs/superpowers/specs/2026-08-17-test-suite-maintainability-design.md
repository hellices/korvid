# Test Suite Maintainability Design

## Goal

Reduce test-suite latency, flakiness, and maintenance cost without weakening
korvid's security, write-approval, audit, release-supply-chain, or compatibility
guarantees.

The baseline on Python 3.13 is 5,652 passed, 21 skipped in 13 minutes 8 seconds.
The suite contains 112,788 lines for 54,795 lines of product code. Size alone is
not the problem: exact duplicate test bodies account for only 18 tests. The
largest avoidable costs are fixed-duration UI waits, prose-level documentation
contracts, repeated scenario bodies, and oversized test modules.

## Preservation Rules

The work must preserve:

- agent write approval and user-keystroke confirmation coverage;
- audit fail-closed behavior;
- masking and redaction coverage;
- kubectl validation and sensitive-read coverage;
- optional-extra import boundaries;
- release ordering, credential isolation, artifact integrity, and immutable tag
  guarantees;
- supported Python and operating-system CI coverage;
- all product behavior currently asserted by retained tests.

Test count is not a success metric. A test is removed only when another retained
test covers the same behavior or when it asserts prose rather than a product or
operational invariant.

## Phase 1: Replace Fixed UI Waits

Numeric `pilot.pause(...)` calls are replaced when the test is waiting for an
observable condition. Tests use `tests.ui.waits.until` and focused predicates
such as screen type, widget visibility, row count, worker completion, or
recorded calls.

A numeric wait remains only when elapsed time is itself the behavior under test,
such as debounce, polling cadence, or delayed cleanup. Such waits are kept local
and documented by the test's behavior, not by a generic settling comment.

Duplicate polling helpers are removed in favor of the shared helper. The
migration starts with the highest fixed-wait contributors and proceeds in
independently verified batches.

## Phase 2: Narrow Documentation and Workflow Contracts

Release and security tests continue to parse workflow and configuration
structure. They assert semantic properties: job ordering, permissions,
credential boundaries, pinned dependencies, required commands, links, and
version consistency.

Tests that require exact explanatory sentences in `README.md`, `AGENTS.md`, or
release documentation are removed or replaced with structural assertions. A
small number of explicit prohibition checks may remain where the text itself is
an executable agent policy, but they should check the prohibited action rather
than freeze a preferred sentence.

## Phase 3: Consolidate Repeated Scenarios

Exact duplicate bodies and adjacent tests that differ only in input and expected
result are combined with `pytest.mark.parametrize`. Each parameter receives a
descriptive case ID. Distinct failure messages, security boundaries, and
behavioral branches remain separately visible.

Consolidation must not hide setup differences or combine tests merely because
their final assertion has the same shape.

## Phase 4: Split Oversized Test Modules

The largest test modules are split by public behavior:

- tool execution: read execution, write approval, recorded execution,
  redaction, and manifest handling;
- agent runtime: turn lifecycle, tool calls, interruption, protocol validation,
  and compatibility adapters.

Shared fakes and builders move to non-test support modules in the same package.
The split is a file-organization change only: test bodies and collection
semantics remain unchanged except for consolidations completed in Phase 3.

## Validation

Each phase is validated independently with:

1. targeted pytest runs for touched modules;
2. ruff check and format on touched files;
3. mypy when helpers or imports change;
4. tach when imports cross package boundaries;
5. the full `make check` gate after all phases.

Before and after measurements record:

- collected, passed, skipped, and failed tests;
- full-suite wall-clock time on the same Python 3.13 environment;
- numeric `pilot.pause(...)` call count and encoded duration;
- exact duplicate-body count.

The final suite must have zero failures, preserve the 80% coverage gate, reduce
non-semantic fixed waits substantially, and contain no known exact duplicate
test scenario.

## Rollout and Risk Control

Changes are committed by phase so regressions can be isolated. No product code
is changed solely to make test cleanup easier. If replacing a wait exposes an
unobservable state transition, the test keeps its existing behavior until a
separate product-facing observability design is approved.
