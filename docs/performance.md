# Performance and scale envelope

What korvid has actually been measured doing, on what hardware, against which
cluster. Every number here comes from a recorded run whose manifest pins the
cluster, the workload profile, and the korvid commit. Nothing here is
extrapolated: a scale that is not listed has not been measured.

Reproduce any of it with the benchmark command described in
[the qualification design](dev/specs/2026-08-06-large-cluster-performance-qualification-design.md).

## Supported envelope

| | Measured |
|---|---|
| Objects in one view | 1,000 pods across 20 namespaces |
| Sustained churn | 24 watch events/second for 31 minutes |
| Session length | 31 minutes unattended, no reconnect, no relist |
| Correctness | 43,200/43,200 events applied, final digest matches, 0 dropped updates |

Replay-only profiles reach 10,000 and 50,000 objects. Those establish that the
store and diff paths do not fall over at that size; they are **not** a claim
that the live budgets below hold there, because they do not exercise the API
server, the watch decoder, or a real terminal.

## Live 1,000-pod result

Run `i186-20260806-195353` against a dedicated AKS cluster (Kubernetes 1.35.6,
5 × `Standard_D4s_v5`), driven from macOS arm64 / Python 3.12 / 10 cores.
Measured before and after the render-path work described below.

| Metric | Budget | Baseline | Optimized | |
|---|---:|---:|---:|---|
| Dropped updates | 0 | 0 | 0 | pass |
| Final digest mismatch | 0 | 0 | 0 | pass |
| LIST to 1,000-row table | ≤ 2 s | 1.12 s | 1.19 s | pass |
| Process start to interactive | ≤ 10 s | 2.75 s | 2.88 s | pass |
| Peak RSS | ≤ 512 MiB | 276 MiB | 271 MiB | pass |
| Post-warm-up RSS slope | ≤ 1 MiB/min | 1.06 MiB/min | 0.53 MiB/min | fixed |
| Event-to-render p95 | ≤ 250 ms @ 20 ev/s | 527 ms @ 24 ev/s | 299 ms @ 24 ev/s | **miss** |
| Cursor-input p95 | ≤ 100 ms | 2,311 ms | 2,447 ms | **miss** |

Supporting numbers: event-to-render p50 266 → 156 ms, p99 659 → 356 ms, max
1,628 → 714 ms; max backlog depth 42 → 39; achieved churn 23.18 → 23.996 ev/s
against a 24.0 target.

Render passes went *up* 51% (3,640 → 5,493) while latency went *down* 43%.
Before the change each pass was expensive enough that the update coalescer was
swallowing work to keep up; afterwards each pass is cheap enough to run more
often, so events reach the screen sooner and the backlog stays shallower. That
is also why achieved churn only reaches its 24.0 ev/s target after the change —
the event driver was previously being back-pressured by the renderer.

### What made it faster

A 31-minute CPU profile of the baseline run (1,877 s of samples, 2.5 B calls)
showed the cost was per-row-per-frame work on 1,000 rows, in three places:

- the in-place table diff read every surviving row back out of the `DataTable`
  to decide whether it had changed — 7.35 M `get_row` calls and 102 M cell
  comparisons per run, answering a question the writer already knew;
- `format_age` re-parsed the same ~1,000 timestamp strings every frame, pulling
  `dateutil` into the hot loop for 96 s combined;
- `phase_style` recomputed a small closed set of styles 7.3 M times.

All three are now memoised. The one thing deliberately *not* cached is the
`Text` object returned for a phase cell: `DataTable` takes ownership of it and
mutates it, so a shared instance would corrupt unrelated rows.

## Known limits

**Cursor input is the binding constraint, not rendering.** Input
acknowledgement p95 sits above 2 s at 1,000 objects under full churn, more than
20× its budget, and the render-path work above did not move it. The client is
CPU-saturated (peak ~99.7%) at this size, so the render win buys headroom
rather than removing the ceiling. Treat 1,000 objects under sustained churn as
the point where interaction becomes visibly sluggish.

**Event-to-render p95 misses its budget, but the budget and the measurement do
not line up.** The budget is written at 20 events/s; the live profile runs at
24. The optimized 299 ms is a miss at the higher rate and has not been
re-measured at 20.

**UI-at-scale interaction timings are not yet trustworthy.** Filter, split-pane
and multi-log key sequences took seconds, not milliseconds, in both runs — but
both runs predate the harness fix that makes those scenarios wait for the
target UI state instead of for the keystroke to return. The recorded values are
upper bounds taken while the app was CPU-saturated, not clean measurements, and
they need re-running on the fixed harness before they mean anything.

**Burst drain is unmeasured live.** The 3-second post-burst drain budget is
exercised in replay only; the live profile contains no burst.

**Memory is not a limit at this size.** End-of-run allocation snapshots show no
unbounded growth attributable to korvid — the retained set is dominated by
transient watch-decode buffers and rich text fragments. The 0.53 MiB/min slope
over 31 minutes against a 271 MiB peak is drift, not a leak.

## Long sessions against AKS

Exec-plugin credentials (`kubelogin`, and any other
`client.authentication.k8s.io` provider) expire mid-session. korvid refreshes
them just before expiry and propagates the new token to every client, including
the websocket clients used for exec, logs, and port-forward. Without this a
watch established at connect time dies with HTTP 401 partway through — first
observed at ~22 minutes into a 30-minute run. If you see a session drop with
401 after a long idle period, that is a bug worth reporting, not expected
behavior.

## Raw artifacts

Each run emits a summary, a metrics JSON, a `cProfile` dump, a `tracemalloc`
snapshot, and the seed manifest that reproduces the exact workload. Those are
kept out of the product source history; issue #186 carries the run summaries
and links the artifacts.
