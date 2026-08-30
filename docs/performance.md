# Performance and scale envelope

What korvid has actually been measured doing, on what hardware, against which
cluster. Every number here comes from a recorded run whose manifest pins the
cluster, the workload profile, and the korvid commit. Nothing here is
extrapolated: a scale that is not listed has not been measured.

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

The agent is not part of any number on this page: the harness measures the
watch/render path with no agent composed at all, and an agent's budgets —
iterations, retained history, per-result caps — are resolved per session by
`ModelRouter` from the capability tier, not from anything measured here.

A 31-minute session survives its own credentials. Exec-plugin credentials
(`kubelogin`, and any other `client.authentication.k8s.io` provider) expire
mid-session, so korvid refreshes them just before expiry and propagates the
new token to every client, including the websocket clients behind exec, logs
and port-forward. Without that, a watch established at connect time dies with
HTTP 401 partway through — first seen ~22 minutes into a 30-minute run. A
session that still drops with a 401 after a long idle period is a bug worth
reporting, not expected behavior.

## Live 1,000-pod results

**Run `i186-20260806-195353`**, measured before and after the render-path work
described below.

| Metric | Budget | Baseline | Optimized | |
|---|---:|---:|---:|---|
| Dropped updates | 0 | 0 | 0 | pass |
| Final digest mismatch | 0 | 0 | 0 | pass |
| LIST to 1,000-row table | ≤ 2 s | 1.12 s | 1.19 s | pass |
| Process start to interactive | ≤ 10 s | 2.75 s | 2.88 s | pass |
| Peak RSS | ≤ 512 MiB | 276 MiB | 271 MiB | pass |
| Post-warm-up RSS slope | ≤ 1 MiB/min | 1.06 MiB/min | 0.53 MiB/min | fixed |
| Event-to-render p95 | ≤ 250 ms @ 20 ev/s | 527 ms @ 24 ev/s | 299 ms @ 24 ev/s | **diagnostic** |
| Cursor-input p95 | ≤ 100 ms | 2,311 ms | 2,447 ms | **invalid** |

**The 2,311 ms and 2,447 ms cursor figures are not measurements of korvid.**
Both were taken with Textual's `Pilot.press()`, whose two CPU-idle waits each
run to their one-second ceiling on a saturated client: they are the test
driver's quiescence heuristic plus a ~2 s constant, not user-visible input
latency. They are retained only to invalidate them; do not compare them to any
budget or to any corrected figure.

**Event-to-render is a no-op-diff interval here, so its row is diagnostic
rather than a verdict.** Live churn is metadata-only, so the recorded interval
ends when the table diff completes without writing a cell. The 527 → 299 ms
improvement is a real reduction in per-event diff cost, but it cannot pass or
fail the qualification budget, which is ≤ 250 ms at 20 ev/s over churn that
changes a **rendered cell**. This run measured a no-op diff at 24 ev/s, above
the 20 ev/s the budget names, and a bound taken at a higher rate is not a bound
at a lower one. The contract therefore stays unqualified live (see
[Known limits](#known-limits)) — neither passed nor missed by this run.

**What made it faster.** A 31-minute CPU profile of the baseline showed the
cost was per-row-per-frame work on 1,000 rows: the table diff read every
surviving row back out of the `DataTable` to decide whether it had changed
(7.35 M `get_row` calls answering a question the writer already knew),
`format_age` re-parsed the same ~1,000 timestamps every frame, and
`phase_style` recomputed a small closed set of styles 7.3 M times. All three
are now memoised. The one thing deliberately *not* cached is the `Text` object
returned for a phase cell: `DataTable` takes ownership of it and mutates it, so
a shared instance would corrupt unrelated rows. Render passes rose 51%
(3,640 → 5,493) while latency fell 43% — cheaper passes run more often, so the
backlog stays shallower (42 → 39) and achieved churn only reaches its 24.0 ev/s
target after the change.

**Run `i279-20260813-1620`** exercised the same checked-in `steady-24eps-1k`
profile against the same cluster and passed every acceptance row it measured:
LIST to table 144 ms, start to interactive 1.55 s, peak RSS 236 MiB, all 720
requested mutations delivered in 29.92 s (24.06 ev/s), digest matched, nothing
dropped. Its 7 ms cursor p95 is **preliminary, not a budget verdict**: sampling
began at mutation dispatch, before the `PATCH` was awaited, so it cannot show
the table was already receiving churn. The current harness opens its own watch
and waits for an owned `MODIFIED` event before the first sample; the live
cursor budget stays unqualified until the run is repeated under that gate.

## Environment and methodology

- **Live cluster:** a dedicated AKS cluster, Kubernetes 1.35.6, 5 ×
  `Standard_D4s_v5`, for both live runs above.
- **Live client** for run `i186-20260806-195353`: macOS arm64, Python 3.12,
  10 cores, running the 31-minute schedule.
- **Short workload**, used by run `i279-20260813-1620` and the deterministic
  replay: the committed profile
  [`tests/performance/profiles/steady-24eps-1k.json`](https://github.com/hellices/korvid/blob/main/tests/performance/profiles/steady-24eps-1k.json)
  — 1,000 Pods across 20 namespaces, 30 seconds of burst-free churn at 24
  events/s.
- **Replay and CPU-comparison runs:** an unpinned local developer machine, so
  only same-session, interleaved arms are ever compared with each other.

The deterministic replay drives the real app, watch manager, store and table,
but not the API server, the watch decoder or a real terminal, so it never
replaces the live tables above. It *does* rewrite rendered cells, so its
event-to-render figures are real event-to-rendered-cell measurements — and its
corrected cursor probe reports single-digit milliseconds where the
`Pilot.press` artifact reported ~2.2 s. **No point estimate from it is
published here**: successive runs on an unpinned machine differ, and quoting
one run's percentile as *the* number is how the withdrawn figures above came to
be trusted in the first place.

**Every cursor figure on this page is a state acknowledgement**, not a terminal
paint: the interval from injecting one key event into the running app to the
`ResourceTable` cursor row being observed on its new index, over 25 `down`/`up`
round trips. `cProfile` runs are diagnostic artifacts only — instrumentation
alone moves the cursor figure by an order of magnitude — and each report
records which latency it measured in `latency.update_latency_kind`, leaving the
other field null, which is why the metrics JSON is at `schema_version` 2.

Reproduce any of it with the benchmark command in
[the qualification design](dev/specs/2026-08-06-large-cluster-performance-qualification-design.md):

```bash
python -m tests.performance.cli replay \
  --profile tests/performance/profiles/steady-24eps-1k.json \
  --json <artifact-dir>/steady-24eps-1k.json \
  --out <artifact-dir>/steady-24eps-1k.md
```

## Update-path CPU and memory

Latency percentiles cannot resolve a change of this size on an unpinned
machine, so the render- and update-path work was measured in CPU time over a
fixed 20-second, 120 ev/s schedule, with the two arms alternated run by run and
differenced within each round.

| Workload | Metric | Before | After | |
|---|---|---:|---:|---|
| Committed replay profile | CPU time (median) | 13.93 s | 12.90 s | −7.4% |
| Plus Pod creation timestamps | CPU time (median) | 18.80 s | 16.53 s | **−12.1%** |
| Plus Pod creation timestamps | Peak RSS | 127.8 MiB | 127.1 MiB | −0.7 MiB |

Every run kept `dropped updates: 0` and a matching final digest. The two
changes do **not** compose independently: run round-robin as a 2×2, the render
and update paths together remove about 2 s more than they do separately, in 9
rounds out of 9. The render-path change roughly triples how often the
table-update pass runs, giving the update-path memo roughly three times as
many chances to pay — that explains the *direction* of the interaction but
only a minority of its size: tripling the update path's own 0.16 s isolated
saving predicts about 0.48 s, well short of the measured 1.89 s gain, and
nothing measured here separates the remainder, so it stays unexplained rather
than narrated. The two arms do not perform equal work either: the
fully-optimised arm completes roughly 3.5× the table-update passes of the
unoptimised one, so a percentage of CPU time understates the total work
completed. The committed profile understates all of it — its synthetic Pods
carry no `created`, so the AGE path is skipped entirely — which is why the
same change is worth 7.4% there and 12.1% once real timestamps are present.
Absolute times drift between sessions on a shared machine, so only arms
measured within one session are comparable.

## Known limits

- **The live cursor budget is unqualified.** Run `i279`'s samples were
  dispatch-gated and must be repeated under the current watch-receipt gate; the
  31-minute endurance run has not been repeated with the corrected probe and
  the current render path either.
- **Event-to-render is unqualified live.** The live churn driver is
  metadata-only and changes no rendered cell, so only the deterministic replay
  exercises the 250 ms contract. A live driver that mutates a rendered field is
  required before it can be called passed or missed.
- **The replay workload has no creation timestamps.** `initial_pods` builds
  `PodSummary` without `created`, so every AGE cell renders "-" — worth 26% of
  update-path CPU on this workload, and the reason any AGE-dependent figure
  from the committed profile is a floor rather than a result.
- **UI-at-scale interaction timings are not yet trustworthy.** Filter,
  split-pane and multi-log key sequences still use `Pilot.press()`-style
  keystroke timing rather than the direct-injection probe, so their recorded
  values remain invalid upper bounds.
- **Burst drain is unmeasured live.** The 3-second post-burst drain budget is
  exercised in replay only; the live profile contains no burst.
- **Memory is not a limit at this size.** End-of-run allocation snapshots show
  no unbounded growth attributable to korvid; the 0.53 MiB/min slope over 31
  minutes against a 271 MiB peak is drift, not a leak.

## Raw artifacts

Each run emits a summary, a metrics JSON, a `cProfile` dump, a `tracemalloc`
snapshot, and the seed manifest that reproduces the exact workload. Those stay
out of the product source history:
[issue #186](https://github.com/hellices/korvid/issues/186) carries the
`i186` render-path run's summary and profiling tables; the raw artifacts
themselves stay out of the repository and are available on request. The
issue does not carry the update-path 2×2 interaction's per-round values or
allocation accounting — that detail was cut from this page for length, not
relocated, and no other destination holds it.
