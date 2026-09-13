# Pulse / Problems

Pulse keeps a small attention summary in the ordinary workspace. Pods remain
the default view, and the context/namespace/protected-mode status stays visible.
The summary updates without opening popups, moving the cursor, or changing
the resource table.

Type **`:pulse`** or **`:problems`** to inspect the evidence. Both commands
appear in the existing command completion and `?` help; no optional Agent,
MCP, Prometheus, or Loki installation is required.

## Read the three sections

| Section | Meaning |
|---|---|
| Current problems | Deterministic findings from bounded, fresh Pod and Deployment snapshots |
| Recent warnings | Warning Events from the last 15 minutes, including unfamiliar reasons and custom-resource references; not proof of a current incident |
| Coverage / observation status | What was observed, when, and which reads or retention limits leave gaps |

Initial current-state checks inspect Pod phase, scheduling/readiness conditions,
and current regular/init/ephemeral container states. They exclude successful
completed Pods, ordinary container creation/initialization, and historical
restarts alone. Deployment checks report explicit `ReplicaFailure=True` or
`Progressing=False` only for the current observed generation. A failing
Deployment is visible even when no Pod was created.

These checks report observed evidence, not a guessed root cause or a complete
Kubernetes analyzer catalogue. **“No matched problems” is not “cluster healthy.”**
Inspect coverage before drawing a conclusion. Loading, partial, forbidden,
unavailable, failed, capped, and stale sources remain distinct. Evidence ages
to stale after 30 seconds without a fresh successful observation; timestamps
and previous failure details remain available.

Complete fresh resource snapshots can remove recovered current findings.
Incomplete reads do not resolve unobserved findings, and malformed resource
status or a missing/empty/non-string UID cannot clear prior evidence. Such
objects can still contribute fresh findings, with partial assessment coverage.
Older snapshots cannot roll back newer
observations. If a later page fails, valid earlier pages still contribute
findings, observed recoveries, and Warning history; partial coverage retains
the failure explanation without clearing unobserved findings.
Current-finding retention loss is disclosed, never presented as
recovery. Old Warning Events can
remain after recovery, and reading, expiring, or evicting an Event never
resolves a current finding. Event updates are deduplicated by Event UID and
keep the cumulative count rather than adding it repeatedly. The event-time
window includes both endpoints: from 15 minutes before the model's effective
observation time through that time. Future timestamps are omitted with a
coverage gap, without a clock-skew allowance or clamping them to the present;
they cannot evict valid recent evidence. Missing, invalid, timezone-naive,
or UTC-overflowing timestamps likewise produce a gap rather than invented
freshness, without preventing later valid Events from being ingested.
Missing, empty, or non-string Event types also leave an explicit coverage gap,
while valid non-Warning records are ignored. Epoch and namespace filters apply
before reporting these gaps, so a different scope cannot pollute this view.

## Keyboard workflow

- **Up/Down:** select a row; its full evidence, timestamp, and UID appear below.
- **Tab:** reach the scrollable evidence area when a message is longer than the preview.
- **Enter:** verify the selected live UID and use the normal resource-navigation path.
- **r:** apply the latest buffered rows and request fresh snapshot reads.
- **Esc / q:** return to the workspace.

Live updates only show **“New data available”** while detail is open. They do
not reorder the rows beneath the cursor. `r` preserves the same row identity
where possible; if it disappeared, selection returns to a non-actionable
section header, not another resource. Reads requested by `r` finish in the
background; another new-data indicator appears if their result differs.

A missing UID, altered/truncated identity, unsupported resource kind, deleted
or recreated object, or changed context/scope cannot silently navigate by name.
Returning to the original scope or resource view does not revive an older
pending navigation; generation guards remain active across asynchronous waits.
Pulse does not open over approval dialogs, type into them, or satisfy them.
No Pulse operation writes to the cluster.

## Collection and retention budgets

Snapshot reads start after mount, every 15 seconds, on scope change, and on
explicit refresh. They use the selected namespace, or all namespaces only
when the workspace selects that scope. Overlapping refresh requests coalesce.
Context teardown cancels and awaits both snapshot and navigation identity reads
before retargeting the shared client; old epoch/scope results and errors cannot
publish into the new frame.
If teardown aborts before the client is retargeted, an active Pulse resumes
without clearing the unchanged frame's findings, Warning evidence, or loss
counters. An already-suspended Pulse stays suspended, including after a failed
target and fallback connection. Only a successfully applied context starts a
new observation frame. A missing snapshot reader marks only registered snapshot
sources unavailable; live-watch denials and retention-loss coverage remain
independent.

| Budget | Limit |
|---|---|
| Initial snapshot sources | Pods, Deployments, Warning Events |
| Per-source pages / rows | 2 pages of 100 objects; at most 200 objects |
| Per-page encoded / decoded body | 256 KiB each, enforced before JSON parsing |
| Per-source total deadline | 5 seconds, including pagination |
| Concurrent snapshot requests | 1 |
| Maximum per refresh | 6 requests / 1.5 MiB of decoded response data across the initial sources |
| Current retention per rule source | 200 findings / 256 KiB of serialized, sanitized item data |
| Initial current retention total | At most 400 findings / 512 KiB across Pods and Deployments |
| Recent Warning retention | 100 entries / 128 KiB / 15 minutes by event time |
| Presentation cadence | At most 4 updates per second |

Automatic HTTP decompression is disabled for these requests only. The existing
authenticated TLS/proxy connection uses a small read buffer; gzip decoding has
an explicit output ceiling and requires a complete single stream. Unsupported,
truncated, or trailing encodings are failures, not empty successes. These are
payload/retention budgets, not an exact bound on Python object or network-buffer
overhead.

Redirects and disconnects are reported rather than automatically followed or
retried, so a logical page cannot silently consume extra API requests. A
short-lived, non-owning HTTP session borrows the existing configured connector
and cookies for each page; it does not change the ordinary session's retry or
decompression policy. Public transport hooks reserve the first send and reject
replayed headers at a boundary that releases acquired connections, without
relying on private SDK retry switches. This bounds API sends, not underlying
connection attempts. The next scheduled or manual refresh can retry a failed
source within a new explicit budget.

Responses close on success, error, overflow, and cancellation. A cap, malformed
payload, repeated pagination token, timeout, or RBAC/API failure is not an empty
successful snapshot. Retention eviction/refusal and clipped Warning text are
disclosed in coverage. Current-finding retention has independent per-source
budgets, so Pod churn cannot evict Deployment findings. Its discard counters
count eviction/refusal occurrences and remain visible until the scope/context
resets; they do not count unique incidents. The row preview can be shorter than
the retained evidence;
select the row to inspect it.

**Pulse adds no watches.** It shares the pre-existing timeline Warning stream
through an injected callback. That inherited subscription remains
**context-wide**, even in a namespace-scoped workspace; Pulse filters what it
retains/displays, not the watch's network subscription. The live stream is
best-effort supplemental evidence. Its failure is visible separately and does
not disable periodic snapshots or the timeline's other producers. The budgets
above exclude that already-existing watch and explicit ordinary navigation reads.
An Event's namespace scopes the observation; its referenced resource keeps its
own namespace, including an empty namespace for cluster-scoped targets. Pulse
does not invent a namespaced identity for a Node or cluster-scoped custom resource.

## Add scenarios without changing the UI

New scenarios are test fixtures, not executable runtime configuration. Add a
YAML file under `tests/fixtures/pulse/`; the runner discovers `*.yaml`
automatically, with no central scenario list or fixed count limit. For example:

```yaml
name: A previously unknown controller warning
now: "2026-09-13T12:00:00Z"
scope: production
steps:
  - warnings:
      - type: Warning
        reason: NewControllerFailure
        eventTime: "2026-09-13T11:59:50Z"
        metadata: {uid: event-1, namespace: production}
        regarding:
          apiVersion: example.io/v1
          kind: CustomWorkload
          namespace: production
          name: example
          uid: custom-workload-1
    expect:
      current_count: 0
      recent_count: 1
      recent_reasons: [NewControllerFailure]
```

Fixtures can also supply `sources`, their `objects` and coverage `state`, multiple
steps with `after_seconds`, expected/forbidden reasons, rule IDs, resource names,
counts, and redacted text. Run them with:

```bash
uv run pytest -p no:tach tests/core/test_pulse_scenarios.py
```

An unfamiliar Warning reason needs **no new rule**. To explain a new current
condition using an existing source, add a pure `PulseRule`, fixtures, and explicit
registration in `__main__.py`; the UI consumes the same immutable records and
existing `Finding` evidence contract. A new evidence source additionally needs
its own bounded collection/coverage tests and an updated documented budget.
Rules that accept incomplete status also override `can_clear(resource)`:
returning false keeps previous evidence for that resource rather than treating
unassessable data as recovery. Existing evaluate-only rules remain compatible.
Scenario fixtures can exercise per-source current caps with `max_current_findings`
and `max_current_bytes`, as well as expected `current_dropped` counts.
There is no public Pulse plugin API, YAML expression language, incident database,
or AI-generated finding path in this release.

## Validation limits

The offline pilot journey uses a synthetic failed Deployment with no Pods at
80×24 and 120×40. The attention summary needs zero keystrokes; following
`:pulse`, Enter, Enter uses eight keystrokes. Directly opening the already-known
`:deploy` alias also uses eight. This demonstrates discoverability without
claiming every direct resource shortcut is slower. Tests wait for observable
readiness, not an assumed wall-clock sleep.

Scenario, collector, identity, and approval-focus tests are repeatable offline
evidence. They are **not** a live-cluster usability or performance study, nor
proof that the finite initial current-state rules cover every Kubernetes problem.
