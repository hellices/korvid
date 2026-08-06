# Large-Cluster Performance Qualification Design

**Issue:** #186

**Status:** Approved for planning

## Goal

Establish a reproducible large-cluster benchmark for korvid, validate it against
real load in the dedicated test AKS cluster, and optimize only bottlenecks
demonstrated by the resulting traces.

The work is complete when korvid has a published 1,000-Pod support envelope,
repeatable replay coverage at larger scales, and before/after evidence for every
runtime optimization made under this issue.

## Decision

Use a hybrid, measurement-first qualification program:

1. deterministic replay exercises the real store, watch, app, and table paths at
   1,000, 10,000, and 50,000 objects;
2. a guarded live run creates 1,000 real Pods on the existing
   `aks-korvid-contract-test` cluster;
3. profiling identifies the dominant bottleneck;
4. one bottleneck is optimized at a time with a correctness regression test and
   a deterministic performance comparison;
5. the same live workload is rerun before the support envelope is published.

This is preferred over a live-only benchmark, which is too variable and costly
for regression testing, and over replay-only testing, which cannot validate API
server, network, watch-recovery, or scheduling behavior.

## Scope and sequencing

#186 remains the only selected product issue until its qualification cycle is
finished. The work is split into reviewable deliverables rather than one large
change:

1. **Benchmark foundation:** deterministic profiles, metrics, reports, and
   guarded AKS workload lifecycle.
2. **Baseline qualification:** deterministic results plus the first live
   1,000-Pod run and profile.
3. **Evidence-gated optimization:** a focused TDD change for each material
   bottleneck, beginning with the largest measured contributor.
4. **Requalification:** identical replay and AKS runs, final budgets, support
   envelope, and documentation.

The benchmark foundation may be merged independently. Runtime changes that
overlap PR #197, especially changes in `ui/app.py`, begin only after PR #197 is
merged and the issue worktree is synchronized with the resulting `main`.

## Benchmark architecture

### Workload profiles

Profiles are versioned JSON documents with a schema version, seed, object count,
namespace count, churn rate, burst pattern, failure injections, and duration.
Schema v1, as reviewed in `tests/performance/profile.py`, fixes the initial
state to all Pods already Running and Ready for the chosen object count; varied
initial-state distributions require a future schema version with an explicit
field. A resolved run manifest records the profile plus the korvid SHA, Python,
Textual, OS, CPU, memory, Kubernetes, cluster, and node-pool versions.

The initial profiles are:

| Profile | Objects | Purpose | Gate |
|---|---:|---|---|
| `smoke-1k` | 1,000 | Fast deterministic correctness and report smoke | Normal CI, no wall-clock assertion |
| `steady-10k` | 10,000 | Store/render scaling and sustained churn | Manual or scheduled |
| `burst-50k` | 50,000 | Upper-envelope and burst-backlog measurement | Manual or scheduled |
| `aks-1k` | 1,000 | Deterministic comparison schedule for the live topology | Manual or scheduled |
| `aks-live-1k` | 1,000 Pods | Real API, network, LIST/WATCH, and UI qualification | Protected manual run |

`aks-live-1k` (`tests/performance/profiles/aks-live-1k.json`) encodes the live
sequence below exactly - 1,800 seconds at 20 events/s with three 30-second
bursts at 100 events/s - so the event-to-render, backlog-drain, and RSS-slope
budgets in this document are measurable by a single protected run:

```bash
uv run python -m tests.performance.cli replay-live \
  --profile tests/performance/profiles/aks-live-1k.json \
  --context aks-korvid-contract-test \
  --expected-cluster-id <ARM resource id> --run-id <run> \
  --json live.json --out live.md
```

`aks-1k` keeps the short (30-second) schedule shared with `burst-50k`, so a
live run can be compared against the deterministic 1k/10k/50k baselines on the
same event schedule. `--duration` shortens a live smoke run; every burst and
failure point is re-validated against the shortened duration before the
cluster identity and ownership gates run.

The deterministic generator emits stable names, namespaces, UIDs, resource
versions, and event order from the profile seed. Repeating a profile with the
same seed must produce the same object and event hashes.

### Runtime path

Replay and live runs exercise the same application path:

```text
LIST/WATCH or replay source
  -> WatchManager
  -> ResourceStore
  -> ResourcesUpdated coalescing
  -> KorvidApp render
  -> ResourceTable
```

The benchmark does not replace these components with a simplified table model.
A headless Textual pilot drives the real app and records cursor-response latency
while updates are active.

Instrumentation is observational. Production defaults remain unchanged, and no
benchmark dependency is required at normal runtime. Cross-platform RSS and CPU
sampling uses `psutil` as a development dependency; benchmark reports also
include Python allocation samples so process growth can be separated from
Python heap growth.

### Metrics

Every run emits JSON and Markdown containing:

- process start to interactive app;
- LIST completion to first fully populated table;
- event receipt to reflected table state, p50/p95/p99 and maximum;
- cursor-input acknowledgement, p50/p95/p99 and maximum;
- update backlog depth and time to drain after each burst;
- process CPU, RSS, peak RSS, and post-warm-up RSS slope;
- Python allocation growth by source location;
- logical LIST/WATCH/GET counts, decoded response bytes, watch events,
  reconnects, `410 Gone` relists, throttles, and authorization failures;
- rendered, coalesced, and dropped update counts;
- final store/table digest, which must match the expected workload digest.

Samples use monotonic clocks. Normal unit tests assert metric semantics and
operation counts, never machine-dependent wall-clock thresholds.

### API-load accounting

The benchmark records logical operations at the `KubeClient` boundary and event
payload bytes after decoding. It distinguishes initial LIST, long-lived WATCH,
documented reconnect/re-LIST, user-requested GET, and unexpected GET.

A passive resource view must use one LIST followed by one WATCH for each active
`(kind, scope)` pair. Object count must not increase GET count. Reconnects and
re-LISTs are reported separately rather than hidden inside aggregate request
counts.

## Live AKS qualification

### Fixed target and capacity

The live target is only:

- resource group `rg-korvid-contract-test`;
- cluster `aks-korvid-contract-test`;
- tags `purpose=korvid-contract-testing` and
  `production-use=prohibited`;
- Kubernetes context `aks-korvid-contract-test`.

The cluster currently has a stopped `Standard_D2s_v5` system node and a
zero-node `Standard_D2s_v5` workload pool. Korea Central has 100 available
DSv5-family vCPUs and 116 regional vCPUs.

Create or reuse a user pool named `perftest` with:

- `Standard_D4s_v5`;
- five nodes during the run and zero while idle;
- `maxPods=250`;
- label `korvid.dev/pool=perftest`;
- taint `korvid.dev/performance=true:NoSchedule`.

Five nodes provide room for 1,000 test Pods plus required DaemonSets without
running at the 250-Pod-per-node ceiling. They also provide 20 vCPUs and 80 GiB
of memory while remaining well inside the verified quota.

### Workload

The workload generator creates 20 labelled namespaces named
`korvid-perf-<run>-0` through `korvid-perf-<run>-19`, with 50
Pods in each. Pods use `registry.k8s.io/pause:3.10`, select the `perftest` pool,
tolerate only the performance taint, and request 5 millicores and 16 MiB. Every
namespace and Pod has:

- `app.kubernetes.io/managed-by=korvid-performance`;
- a unique `korvid.dev/performance-run` value;
- the workload profile and seed.

The live sequence is:

1. start the stopped cluster and scale `perftest` to five;
2. run the janitor for stale, labelled performance namespaces;
3. create all 1,000 Pods and require exactly 1,000 Running, Ready Pods before
   measuring, matching schema v1's fixed initial state;
4. capture cold startup and initial LIST/render metrics;
5. run 30 minutes of metadata-only watch churn at 20 events per second;
6. inject three 30-second bursts at 100 events per second during that window;
7. drive filter, sort, namespace switch, split pane, describe, and cursor input;
8. collect final digests, profiles, API counts, and cluster diagnostics;
9. delete only the run-labelled namespaces and verify they are gone;
10. scale `perftest` to zero and stop the cluster in an independent cleanup
    path that runs even after benchmark failure or timeout.

Metadata-only updates create real watch traffic without restarting containers
or changing the workload's resource demand. Churn writes one dedicated,
non-ownership label (`korvid.dev/performance-tick`) on the Pod's own metadata
under a JSON Patch that first `test`s the Pod UID and both ownership labels.
The kubelet-owned `status` subresource is never written: an externally patched
`status.phase` is reconciled back on the next node sync, which would both
corrupt digest parity and violate the metadata-only rule above.

The generator rate and observed API throttling are both recorded; requested
rate is never reported as achieved rate. Reports carry the requested event
count and rate next to the observed event count, churn wall time, achieved
rate, and the count of mutation-side 429 retries, which is accounted
separately from application read-path throttles.

Live churn is driven with explicit bounded concurrency and a bounded per-patch
timeout: a serial driver is capped at one round trip per event and cannot
approach the scheduled rate, which would silently understate the load the
report claims to have applied. Only HTTP 429 is retried, with bounded
target-specific jitter that honors the API server's `Retry-After` hint up to an
explicit delay ceiling and re-issues the identical guarded patch.

### Guardrails

Every mutating command fails closed unless the resource group, cluster name,
test-only tags, context, pool labels, namespace prefix, and run labels all
match. Cleanup lists the exact owned objects before deletion and refuses
unlabelled resources.

The system pool is never targeted. Existing contract-test namespaces are not
shared with the performance workload. The stop-cluster action is independent of
the benchmark job so a timeout cannot leave compute running.

## Failure profiles

Failure behavior is deterministic first and live only when safe:

- `410 Gone` forces re-LIST and validates that deleted objects do not remain;
- throttling validates bounded backoff and request accounting;
- partial RBAC denial validates one reported denial without namespace fan-out;
- unavailable metrics validates responsive resource navigation without the
  metrics poller;
- slow API responses validate bounded input latency and visible backlog;
- slow log streams validate that resource watch/render progress is independent
  of log consumption.

The first live qualification covers normal LIST/WATCH plus real throttling if it
occurs naturally. Synthetic failure injection stays in replay until the normal
live path is stable, avoiding unnecessary API-server disruption.

## Evidence-gated optimization

The baseline is profiled before runtime code changes. A candidate is material
when it either:

- misses a hard budget;
- contributes at least 25% of sampled CPU time during the affected phase;
- causes unbounded growth with object count, churn rate, or duration; or
- creates unexpected per-object API requests.

The largest material contributor is addressed first. Each optimization must:

1. add a correctness regression test that fails on the original behavior;
2. add or extend a deterministic benchmark comparison;
3. preserve cursor, viewport, filtering, sorting, split-pane, and recovery
   behavior;
4. improve its target metric by at least 20% over the median of three
   deterministic runs;
5. introduce no greater than 10% regression in another published performance
   metric;
6. pass the full repository gate;
7. improve or preserve the same target in the repeated live run.

Likely hot paths such as full-store sorting, full-row table diffing, render
message cadence, and hierarchy refresh are hypotheses only. They are not
changed unless the profile identifies them.

If the first baseline meets every hard budget and no material contributor is
found, no speculative optimization is made. The measured support envelope is
still published.

## Initial hard budgets

These budgets define usable behavior for the live 1,000-Pod profile:

| Metric | Budget |
|---|---:|
| Dropped updates | 0 |
| Final store/table digest mismatch | 0 |
| Unexpected per-object GETs for passive view | 0 |
| LIST completion to 1,000-row table | <= 2 seconds |
| Process start to interactive 1,000-row table | <= 10 seconds |
| Event-to-render p95 at 20 events/s | <= 250 ms |
| Cursor-input p95 during steady churn | <= 100 ms |
| Backlog drain after a 100 events/s burst | <= 3 seconds |
| Post-warm-up RSS slope over 30 minutes | <= 1 MiB/minute |
| Peak RSS at 1,000 Pods | <= 512 MiB |
| Failed UI-at-scale scenarios | 0 |

`drive_ui_scenarios` records a key sequence that never reached its target state
as `ScenarioResult(ok=False)` rather than raising, so `replay-live` folds those
outcomes into its exit status: a run that produced no UI-at-scale evidence fails
instead of reporting success.

The final document records both observed values and budgets. A budget may be
changed only with measured rationale in review; baseline updates never silently
overwrite historical results. The 10,000- and 50,000-object replay profiles
define measured upper envelopes, not claims that the same live budgets apply.

## Results and documentation

Main contains:

- workload schemas and profiles;
- benchmark command and tests;
- reproducibility methodology;
- compact baseline and optimized summaries;
- the supported scale envelope and known limits.

Large raw JSON samples, process traces, allocation snapshots, and cluster
diagnostics are committed to a separate `benchmark-results` branch. Issue #186
links the immutable artifact commit and summarizes the run. Raw artifacts are
not added to the product source history.

Reports never imply validation beyond the exact profile, cluster, and commit
recorded in the run manifest.

## Test strategy

- Schema tests reject unknown versions, invalid rates, impossible namespace
  distributions, and unsafe live targets.
- Generator tests prove deterministic hashes and exact event counts.
- Metric tests use a fake monotonic clock and fixed process samples.
- Replay tests exercise 1,000 objects without timing assertions and verify
  final digest, zero drops, bounded API operations, and report shape.
- Textual tests drive cursor input during controlled churn and verify
  acknowledgement accounting.
- AKS lifecycle tests prove guard failures and label-scoped cleanup with fakes.
- Protected live execution verifies the 1,000-Pod workload and records timing
  rather than asserting cloud wall-clock behavior inside the normal PR suite.

## Acceptance criteria

- The same seed produces identical object and event hashes.
- Replay runs at 1,000, 10,000, and 50,000 objects emit complete JSON and
  Markdown reports.
- The protected AKS run has 1,000 simultaneously visible Pods on five
  performance nodes and completes the 30-minute churn profile.
- Passive viewing shows bounded LIST/WATCH behavior with no per-object GET
  fan-out.
- Reports include latency distributions, CPU/RSS, memory slope, backlog,
  reconnects, throttles, dropped updates, and final digests.
- Every material bottleneck is either optimized with before/after evidence or
  documented with a reason it is not safe or valuable to change.
- The final live rerun satisfies the hard budgets or publishes the missed budget
  as an explicit unsupported limit.
- All benchmark namespaces are removed, `perftest` is at zero nodes, and the
  test cluster is stopped after each live run.

## Non-goals

- Maintaining a permanently running large AKS cluster.
- Claiming production-scale support beyond the tested profiles.
- Adding wall-clock assertions to normal unit tests.
- Optimizing suspected hot paths before profiling.
- Running destructive failure injection against the AKS control plane.
- Starting another product issue before #186 reaches its documented outcome.
