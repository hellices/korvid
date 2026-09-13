# Ambient Pulse / Problems design

## Decision and scope

Implements #389 for v0.5.0. The maintainer approved this direction and requested
implementation, a pull request, and the full review loop on 2026-09-13.
Review of the implementation happens on the PR; no merge is authorized.
#388 remains the subsequent Action Palette change. #390 is investigated
separately on Windows; #387 remains the release tracker.

Pulse is a compact, continuously updated attention summary in the ordinary
workspace, with an explicitly opened detail screen. It does not turn the
workspace into a scrolling event log or automatically open notifications.
Pods remain the default view. `:pulse` and `:problems` open the detail screen;
the summary names that command. The command catalogue remains the source for
help and completion, including later Palette integration.

## Two kinds of evidence, not one health score

- Current problems come from bounded fresh Pod and Deployment snapshots.
- Recent Warning Events accept previously unknown reasons, including events
  concerning custom resources. They are observations, not proof of an active
  incident or a guessed root cause.
- Coverage is explicit per source: loading, complete, partial, forbidden,
  unavailable, failed, capped, or stale. No result means only "no matched
  problems in the observed sources", never "cluster healthy".
- Events are supplemental best-effort information. Initial snapshots and
  periodic reconciliation find problems that predate the session and do not
  emit fresh events. Event expiry, acknowledgement, and buffer eviction do
  not resolve a current problem.

## Data collection and bounds

Pulse adds no watches. The existing timeline Warning stream is shared through
an injected callback without replacing the timeline producer. That existing
stream remains context-wide; namespace filtering in Pulse is presentation,
not a claim that its transport became namespace-scoped. The detail screen
discloses both the snapshot scope and this inherited live-feed scope.

New background snapshot requests use the selected workspace namespace, or
all namespaces only when that is the selected scope. The first snapshot runs
after mounting without delaying startup. Reconciliation runs every 15 seconds
and on explicit refresh or scope change. Only one refresh can run at a time;
overlapping triggers coalesce. Reads are suspended during context teardown.
In-flight old-context/scope results cannot publish into the new view.
An exception or cancellation during pre-retarget teardown restores Pulse's
prior suspension state and preserves the unchanged frame's findings, Warning
evidence, and loss counters. The aborted work's read and navigation generations
remain invalidated. Once retargeting starts, only a successfully applied target
or fallback resumes it; a disconnected session stays suspended across retries.
Suspension releases refresh ownership before awaiting worker cancellation, so
a cancelled-before-start worker cannot wedge retries and a late old worker
cannot release its replacement's ownership. The collector still serializes
requests if a teardown abort leaves an old worker settling.
Registered snapshot source keys are distinct from dynamic live-watch and
retention-loss coverage, so a missing reader cannot overwrite those signals.

Each source permits at most two pages of 100 objects, 256 KiB decoded response
bytes per page, and a five-second total deadline. There are three initial
sources and at most one request in flight: at most six requests / 1.5 MiB of
response data per refresh, excluding the pre-existing Warning watch. Bodies
are streamed with a byte cap before JSON parsing and are always closed. Encoded
input is bounded to the same per-page budget. Per-request HTTP decompression is
disabled without changing the shared session; gzip decoding has a bounded output
and rejects incomplete or trailing streams. Authentication refresh, cookies,
configured TLS, and explicit proxy settings remain in effect. Payload budgets do
not claim an exact bound on Python-object or network read-ahead overhead.
Automatic redirects and disconnect retries are disabled for Pulse, so the
request budget counts actual API sends rather than just logical page calls.
A short-lived non-owning session borrows the configured connector/cookies;
ordinary shared-session retry/decompression settings remain unchanged.
The send budget uses public transport hooks rather than version-dependent private
retry controls: reserve before headers are written, at a boundary that closes
and releases acquired connections on rejection. Do not raise from connector
creation/reuse trace hooks: supported older clients lack cleanup there. Tests
count actual API sends and verify shared-pool capacity and ordinary reads after
rejection. API send bounds do not claim a bound on underlying connection attempts.
HTTP errors, malformed payloads, repeated continuation tokens, timeouts, and
truncation are visible; none becomes a successful empty snapshot.
Valid earlier pages remain usable if a later page fails. Partial coverage keeps
the failure detail, accepts observed findings/recoveries and Warning history,
and never clears unobserved identities.

Recent Warning storage is independently bounded to 100 entries / 128 KiB and
a 15-minute event-time window, inclusive at both endpoints against the model's
effective observation clock. Future timestamps have no skew allowance and are
omitted before retention/version updates rather than clamped to the present.
Missing, invalid, timezone-naive, UTC-overflowing, and future timestamps expose
coverage gaps without aborting ingestion of subsequent valid Events.
Missing, empty, or non-string Event types are likewise unassessable observations
and leave coverage gaps. Valid non-Warning records remain excluded, and epoch
and namespace guards run before reporting any event gap.
Repeated updates of one Event UID replace the
same entry and use the cumulative Event count rather than adding it again.
Unknown reasons are not filtered out by a catalogue. Capacity loss is visible.
Current findings have a separate per-rule-source retention cap of 200 entries /
256 KiB serialized sanitized item bytes, initially at most 400 entries / 512 KiB
across two sources. Per-source loss coverage and cumulative discard counters
persist until reset; eviction is not recovery. Stale snapshots and malformed
unassessable resources cannot clear newer or previously established findings.
An observed object needs a non-empty string UID before it can clear retained
evidence; an unverified identity may still contribute a fresh finding but
makes assessment incomplete.
Rendering is coalesced to at most four updates per second. Event ingestion
does not trigger network reads, table reordering, focus changes, or popups.

## Architecture and extension boundary

- `k8s/pulse.py`: stdlib-oriented source descriptors, bounded page contract,
  `PulseReader` ABC, and bounded Kubernetes response decoding.
- `core/pulse.py`: immutable target/item/coverage/snapshot records and bounded
  current/recent state. The current result keeps the existing `Finding`
  contract instead of introducing a second diagnostic schema.
- `PulseRule.can_clear(resource)`: generic per-resource assessment seam; built-in
  rules preserve prior evidence for incomplete status without adding source-specific
  schema branches to the model. Existing evaluate-only rules remain compatible.
- `core/pulse_rules.py`: pure `PulseRule` implementations for current Pod and
  Deployment facts. Rules cannot perform I/O and declare their source key.
- `core/pulse_collector.py`: per-source paging/deadlines and coverage. Source
  descriptors and rules are constructor-injected, not discovered plugins.
- `ui/pulse_controller.py`: refresh lifecycle, epoch/scope invalidation,
  coalesced presentation, detail opening, and safe navigation.
- `ui/widgets/pulse.py`: one-line summary and compact keyboard-first detail.
- `__main__.py`: all construction and dependency wiring.

New variants normally add only scenario fixtures. New rules using existing
sources add a rule, fixtures, and explicit registration, not UI branches.
New evidence sources must independently satisfy the collection contract.
Public runtime plugins, a YAML expression language, LLM diagnosis, incident
persistence, observability integrations, and new write paths are excluded.

## Current rules

Pod rules inspect current phase, scheduling/Ready conditions, and current
container states. Successful completed Pods and normal container creation
are not failures merely because of old restarts or historical termination.
Failure messages report observed fields rather than infer a root cause.
Deployment rules report explicit ReplicaFailure=True or Progressing=False;
stale observedGeneration is not treated as a current failure. A Deployment
can produce a problem even when no Pod exists.

## UI and navigation

The summary occupies one line and does not displace context/protected-mode
status. Detail separates current problems, recent warnings, and coverage,
with deterministic order. It works at 80x24 and 120x40. Refresh never steals
focus or moves the selected target to a different resource. New detail data
is signalled while the operator chooses when to refresh the displayed rows.
Escape restores the prior workspace. Approval dialogs cannot be opened,
replaced, confirmed, or focused by Pulse updates.

Targets retain API group/kind/namespace/name/UID plus the context epoch.
Unknown kind, absent UID, deletion, recreation, and a crossed context/scope
are non-navigable with an explanation. Navigation goes through the existing
workspace route and checks the expected UID before focusing a row, including
after awaited transitions. No display text is parsed into an action.
Monotonic navigation generations invalidate pending UID reads, workspace lock
acquisition, transitions, and row waits even after a scope/kind round-trip.
Capture the pane identifier and requested-navigation counter before the UID read
and preserve that origin through the callback and lock-time check. A newer command
is already an invalidation while its watch teardown still exposes the old scope.
Cluster-authored text uses existing redaction/control-character handling
before storage and is rendered without markup.

## Verification

An automatically discovered YAML scenario pack is test data, not executable
runtime configuration. Fixtures contain input, fixed observation time,
expected and forbidden findings, and coverage expectations. There is no
scenario count ceiling or second central list. Tests cover unfamiliar reasons,
existing failures without new events, recovery with old Warnings, normal
rollouts/completed Pods, event bursts, missing evidence, caps, malformed data,
RBAC denial, reconnect/stale inputs, UID recreation, and context changes.

Collector tests prove count, decoded-byte, page, deadline, cancellation, and
concurrency bounds. UI pilots prove the narrow/wide layouts, keyboard
discovery/navigation, cursor preservation, and approval-focus isolation.
The workflow comparison records startup-to-problem keystrokes and uses
condition-driven readiness rather than wall-clock assertions. A synthetic
pilot is not represented as a live-cluster usability/performance study.

The full local gate and required remote checks must pass. PR review reads
all comments, including suppressed findings, applies credible fixes with
TDD, replies and resolves each addressed thread, and stops after two
consecutive advisory-only rounds without blockers. The maintainer merges.
