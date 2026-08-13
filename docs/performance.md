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
| Cursor-input p95 | ≤ 100 ms | 2,311 ms | 2,447 ms | **invalid** |

**The 2,311 ms and 2,447 ms cursor figures are not measurements of korvid.**
Both were taken with Textual's `Pilot.press()`, which performs two CPU-idle
waits, each bounded at one second. Under this workload the client is
CPU-saturated, so both waits run to their one-second ceiling regardless of how
fast the cursor actually moved: the numbers are the test driver's quiescence
heuristic plus a ~2 s constant, not user-visible input latency. They are
retained here only to invalidate them; do not compare them to any budget or to
any corrected figure.

**The corrected metric** — everything reported as cursor-input latency from
this point on — is the interval from injecting one key event into the running
app to the `ResourceTable` cursor row being observed on its new index. It is a
state acknowledgement, not a terminal paint: it excludes the emulator's own
draw, and it excludes every driver idle heuristic. It is emitted as
`latency.input` in the metrics JSON and as "Input latency p95 (key injection to
cursor-row acknowledgement)" in the markdown report. The probe takes
`--input-sample-pairs` `down`/`up` round trips (25 pairs — 50 samples — by
default, each pair returning the cursor to its original row), so the reported
percentile is a percentile rather than a point observation.

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

## Corrected deterministic 1,000-pod replay

The corrected probe has been exercised against a local, deterministic replay of
the 1,000-pod / 24-events-per-second workload committed as
[`tests/performance/profiles/steady-24eps-1k.json`](../tests/performance/profiles/steady-24eps-1k.json)
(1,000 Pods across 20 namespaces, 30 seconds of burst-free churn at 24
events/s). That replay exercises the real app, watch manager, store and table,
but not the API server, the watch decoder, or a real terminal, so it can never
replace the live table above.

Reproduce it with:

```bash
python -m tests.performance.cli replay \
  --profile tests/performance/profiles/steady-24eps-1k.json \
  --json <artifact-dir>/steady-24eps-1k.json \
  --out <artifact-dir>/steady-24eps-1k.md
```

What that replay establishes is qualitative and holds across runs: the digest
matches, no update is dropped, and the corrected cursor probe now reports a
figure in the single-digit-millisecond range instead of the ~2.2 s
`Pilot.press` artifact it replaced. **No point estimate from it is published
here.** Successive local runs on an unpinned developer machine differ, and
quoting one run's percentile as *the* number is how the withdrawn figures above
came to be trusted in the first place. The authoritative numbers are the live
ones, and the live cursor-input result has not been re-measured with the
corrected probe yet (see "Known limits").

Profiled runs are not comparable to unprofiled ones: `cProfile` instruments
every Python call and materially changes compositor cost, which alone moves the
measured cursor figure by an order of magnitude. Profiles are diagnostic
artifacts only, never the acceptance environment for the 100 ms input budget,
and before/after profiles must use identical instrumentation to be comparable.

Two production changes drive the difference, both in the in-place table diff: an
unchanged cursor is no longer re-seated after every watch tick (a
`move_cursor()` to the same coordinate is repaint work), and a batch of cell
updates requests at most one repaint, and none at all when no changed row
intersects the painted viewport. Off-screen rows still update their data
immediately — `get_row`, sorting, filtering and the final digest see the new
value — and the row repaints as soon as it scrolls into view.

## Known limits

**The live cursor-input result is unmeasured, not slow.** The only live figures
ever taken (2,311 ms / 2,447 ms) came from the invalid `Pilot.press` probe
described above and have been withdrawn. The corrected probe has a
deterministic replay result only; no live cursor number is estimated from it.
Until the live run is repeated, korvid makes **no claim** that the 100 ms
cursor-input budget is met against a real cluster.

**Event-to-render p95 misses its budget live, but the budget and the
measurement do not line up.** The budget is written at 20 events/s; the live
profile runs at 24. The optimized 299 ms is a miss at the higher rate and has
not been re-measured at 20, nor re-measured live since the render-path work
above.

**UI-at-scale interaction timings are not yet trustworthy.** Filter, split-pane
and multi-log key sequences still use `Pilot.press()`-style keystroke timing,
not the direct driver injection plus state-acknowledgement probe used for the
cursor metric above. The cursor-harness fix did not validate those scenario
timings. Their recorded values remain invalid upper bounds until those
scenarios are migrated to the same direct driver/state acknowledgement method.

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
