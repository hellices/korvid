# Aggressive Test Suite Reduction Design

## Goal

Reduce korvid's test-maintenance surface substantially while preserving the
tests that prevent user-visible, security, compatibility, and data-integrity
regressions.

The starting point is approximately 153,000 test lines and 7,100 Python test
functions. This work targets a net reduction of at least 20,000 test lines.
The target is a floor, not a quota: cleanup continues beyond it while
low-value tests remain. Parameterizing, moving, or compressing unchanged
assertions does not count as reduction.

This design supersedes the preservation rule in the earlier test-suite
maintainability design that retained every currently asserted behavior. That
rule prevented meaningful deletion because many asserted details are not
valuable product contracts.

## Value Test

A test is retained only when its failure would identify at least one of:

- a user-visible behavior regression;
- a security invariant violation;
- data loss, corruption, or an invalid external side effect;
- a public Python, CLI, Kubernetes, MCP, provider, or serialized-data contract
  regression;
- a supported-platform or optional-dependency compatibility regression;
- a previously observed production defect that is not covered at a stronger
  boundary;
- a material performance regression measured without wall-clock flakiness.

A test is deleted when it primarily freezes:

- private attributes, intermediate state, helper calls, or call order;
- constructor wiring or fake-object implementation details;
- exact widget trees, CSS selectors, copy, markup topology, or presentation;
- exhaustive permutations that traverse the same behavioral branch;
- mock interactions when a retained boundary test observes the result;
- implementation-specific exceptions that are not part of a public contract;
- documentation prose, historical plans, or duplicated policy wording;
- a fake, fixture, parser, builder, or test helper rather than product behavior;
- behavior already covered at a more realistic boundary.

Ambiguous cases default to deletion unless the retained value can be stated as
an observable defect in the test name or a short review note.

## Reduction Strategy

The suite is audited by behavioral domain rather than by file size alone.
Each domain pass inventories its public behaviors, selects the smallest set of
tests that covers them, and deletes lower-value duplicates and implementation
assertions.

The passes proceed in this order:

1. documentation, repository-policy, build-wiring, and composition-root tests;
2. UI rendering, controller wiring, fake-signature, and exact-message tests;
3. core, Kubernetes, tools, agent, provider, MCP, and observability unit tests;
4. performance and evaluation tests.

The first two passes have the highest expected maintenance coupling. The later
passes receive the same value test but preserve protocol, security, state
transition, and boundary coverage.

For a behavior covered at multiple levels, retain one primary test at the
strongest practical boundary. Keep a lower-level test only when it isolates a
distinct algorithmic branch, security decision, or failure mode that the
boundary test cannot diagnose reliably.

## Domain Rules

### Documentation and Repository Policy

Preserve working links, build success, executable examples, asset existence,
release integrity, and security-sensitive automation. Delete exact prose,
formatting, section ordering, CSS, and historical-plan assertions unless the
literal text is itself an executable interface.

### UI

Preserve complete user actions and their visible results, approval-gate
keystroke ownership, destructive-action safeguards, navigation, and recovery
from external failures. Delete direct checks of private reactive state, exact
widget composition, implementation messages, worker invocation shape, and
fake call history when a pilot-level behavior test covers the same path.

### Core and Adapters

Preserve state-machine transitions, validation boundaries, redaction,
fail-closed auditing, serialization, Kubernetes API translation, and public
error semantics. Delete getter/setter trivialities, dataclass construction
echoes, mock delegation, and input permutations that do not select distinct
branches.

### Performance and Evaluations

Preserve benchmark thresholds tied to a documented product objective and
grader cases that distinguish genuinely different scoring semantics. Delete
benchmarks that merely execute code, duplicate functional tests, synthetic
scenario variants with identical decisions, and assertions about harness
internals.

## Safety and Verification

Production code is not changed as part of test deletion. A production defect
discovered during the audit is recorded separately rather than making product
changes to justify retaining a test.

Before deleting the last test for a critical behavior, perform a focused
mutation check: temporarily violate the behavior in production or fixture
input and confirm the retained test fails for the intended reason. Mutation
checks are mandatory for the approval gate, audit fail-closed behavior,
redaction, kubectl validation, release integrity, and public serialization
contracts.

Each domain is an independent commit and must pass:

1. collection of the affected tests, to catch accidental disappearance of
   retained coverage;
2. targeted pytest for the affected domain;
3. ruff check and format check for changed test files;
4. `tach check` when imports change;
5. a full `make check` after all domain passes.

The final report records deleted files, net lines, collected-test reduction,
retained critical contracts, mutation evidence, and full-gate results. The
work is not complete if it reaches the line target by relocating assertions,
adding replacement scaffolding of comparable size, or weakening a security
invariant.

## Commit and Stop Rules

Commits are split by domain so a questionable deletion can be reverted without
restoring unrelated tests. A domain pass stops when every remaining test in
that domain satisfies the value test; it does not stop merely because the
global 20,000-line floor has been reached.

If the full gate exposes a product regression, restore only the coverage that
identified a retained-value behavior. Do not broadly restore deleted suites.
If a deleted test was the sole guard for an observable contract, replace it
with the smallest boundary-level regression test and document why that
contract merits preservation.
