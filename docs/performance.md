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

The agent is not part of any number on this page. The performance harness
measures korvid's watch/render path with no agent composed at all, and the
agent's own budgets — iterations, retained history, per-result caps — are
resolved per session by the model router from the capability tier, not from
anything measured here. A workload that needs an agent budget takes it from
`ModelRouter` over the shipped catalog; there are no separate agent
performance knobs to tune.

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

**The event-to-render row measures the same thing here as in the 30-second
table below**, and for the same reason: live churn is metadata-only, so the
recorded interval ends when the table diff completes without writing a cell.
The 527 → 299 ms improvement is a real reduction in per-event diff cost, but
the figure is not a to-screen measurement. The **miss** verdict still holds:
a no-op diff is strictly less work than one that writes cells and repaints, so
299 ms is a lower bound on a rendered-cell workload at the same rate, and a
lower bound already over the 250 ms budget refutes it.

Render passes went *up* 51% (3,640 → 5,493) while latency went *down* 43%.
Before the change each pass was expensive enough that the update coalescer was
swallowing work to keep up; afterwards each pass is cheap enough to run more
often, so events are applied sooner and the backlog stays shallower. That
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
[`tests/performance/profiles/steady-24eps-1k.json`](https://github.com/hellices/korvid/blob/main/tests/performance/profiles/steady-24eps-1k.json)
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
came to be trusted in the first place.

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

## Update-path CPU and memory, before and after

Latency percentiles cannot resolve a change of this size on an unpinned
machine — identical saturated runs varied between 5 ms and 40 ms p95 here. The
same fixed workload delivers the same events and reaches the same final state,
so CPU time is what these numbers are taken from. What is fixed is the
*external* schedule, not the internal work: table-update throughput rises when
an update pass gets cheaper, so the arms do not perform equal amounts of
application work. Both arms in this update-path comparison use the new render
path; the AGE-evaluation counts in the 2×2 section below put the optimised arm
at roughly 1.2× the table-update passes (3.91 M versus 3.19 M evaluations, or
about 3,910 versus 3,190 passes over this 1,000-row view). That makes a CPU-time
ratio a conservative reading of the change rather than a like-for-like one.
Even then, running all the "before" samples and then all the "after" samples is
not enough: a first attempt that way put a 13% swing on one arm and reversed
the sign of the smaller result. The figures below alternate the two arms run by
run, five rounds each. Workload: 1,000 Pods across 20 namespaces at 120 events/s
for 20 s.

| Workload | Metric | Before | After | |
|---|---|---:|---:|---|
| Committed replay profile | CPU time (median) | 13.93 s | 12.90 s | −7.4% |
| Plus Pod creation timestamps | CPU time (median) | 18.80 s | 16.53 s | **−12.1%** |
| Plus Pod creation timestamps | CPU time (best) | 18.42 s | 16.18 s | **−12.2%** |
| Plus Pod creation timestamps | Peak RSS | 127.8 MiB | 127.1 MiB | −0.7 MiB |
| Plus Pod creation timestamps | `tracemalloc` peak | 28.47 MiB | 28.58 MiB | +0.11 MiB |

Every run kept `dropped updates: 0` and a matching final digest. On the
timestamp-bearing workload the two arms' samples do not overlap at all
(18.42–18.91 s against 16.18–16.88 s). On the committed profile they do
overlap, so −7.4% is the weaker claim of the two. Absolute CPU time drifts
between sessions on this machine — an earlier round measured the same change
at −16.5% with both arms roughly 0.5–1.5 s faster — which is why only
same-session, interleaved pairs are quoted.

The figures above were re-taken after review hardening added work to the
per-cell path, and held: −12.3% median against −12.1% as published.

### The two changes do not compose independently

An earlier revision of this section claimed the render-path change and the
update-path change multiply out. That was wrong twice over. The figure quoted
as "both together" came from a run that reverted only the render-path file, so
the update-path change was present in *both* arms and no arm ever had both
changes off. And the two effects are not independent to begin with.

Answering "do these interact" needs every cell of the 2×2 against the same
machine state, which two separate two-arm runs do not provide — a first
attempt at the split below produced two runs that disagreed about the shared
cell (14.64 s against 14.93 s) by more than the effect being claimed. All four
arms are therefore run round-robin *within* each round, and the arms are
differenced within a round before anything is summarised. That does not make
load artifacts impossible: the four arms still run one after another, so a
short spike can land on a single arm. What it bounds is the *time* between the
arms being compared, which is what slow drift needs to separate them. Anything
faster than that survives, which is why the interaction is reported below as
its nine per-round values, together with how often they agree on a sign,
rather than as one number. Each arm starts by restoring every file the matrix
touches to `HEAD`, then reverts its own set to the pre-merge commit
`fb674c5` — without the restore, an arm inherits whatever the previous arm
reverted, which is a sharper version of the same error this section is about.
The empty set is the shipped tree.

**Render path (`resource_table.py`) × update path (`store.py` + `models.py`)**,
9 rounds, timestamp-bearing workload:

| | old render path | new render path |
|---|---:|---:|
| **old update path** | 20.31 s | 18.60 s (−8.4%) |
| **new update path** | 20.14 s (−0.8%) | 16.64 s (**−18.1%**) |

Within-round savings: render path 1.77 s, update path 0.16 s, the pair 3.66 s.
The pair removes **2.04 s more than the two removed separately** — and does so
in **9 rounds out of 9**, so the sign does not depend on which round is
trusted, and no single spike can produce it. The nine per-round interactions,
in the order they were run:

```
+2.79  +2.27  +0.79  +2.85  +2.24  +2.02  +0.10  +2.04  +1.98   (seconds)
```

(The 2.04 s is the median of those nine, not the difference of the three
medians quoted before them; differencing first is the whole point.) Adding or
multiplying the single-change numbers underestimates the pair by roughly a
factor of two.

The mechanism is not Amdahl's law. Removing an additive cost changes the
*share* the remaining work holds, but it cannot turn a 0.16 s saving into a
1.89 s one — the absolute saving would stay put. Something has to make the
per-object work run more often, and it does. Counting `format_age` evaluations
over the same 20 s schedule (two runs), against the row rebuilds in the same
runs:

| Arm | AGE evaluations | | row rebuilds |
|---|---:|---|---:|
| neither | 1.09 M / 1.03 M | | 3,538 |
| new render path only | 3.19 M / 3.17 M | **~3×** | 3,543 |
| new update path only | 1.20 M / 1.22 M | | 3,538 |
| both | 3.91 M / 3.83 M | **~3.6×** | 3,543 |

AGE is evaluated for every row on every table-update pass — it feeds the stamp
that decides whether the row can be reused, so it runs *before* the memo can
spare anything, and 99.7% of those rows are then reused unchanged. Row rebuilds
track the events, so they are flat across all four arms. The quantity that
triples is exactly the quantity the update-path change makes cheap.

Table-update passes are not a fixed quantity of the workload: the cheaper a
pass gets, the more of them the run completes before the schedule ends. The
render-path change roughly triples them, so the same optimisation has about
three times as many opportunities to pay. That explains the direction but only
a minority of the size: tripling the 0.16-second isolated saving predicts
roughly 0.48 seconds, well below the 1.89-second saving with the new render
path. Nothing measured here separates the remainder, so the residual is left
unexplained rather than narrated.

Two consequences worth stating. The arms do not perform equal work: the
fully-optimised arm completes roughly 3.5× the table-update passes of the
unoptimised one while using less CPU, so a percentage of CPU time understates
the total update work completed. This counter does not establish that the
display itself refreshed more often. And every event still lands, in every arm:
the digests match and no arm dropped an update.

**Inside the update path**, the same treatment separates the AGE memo from the
settled row order (9 rounds, own session):

| | old row order | new row order |
|---|---:|---:|
| **old AGE handling** | 18.41 s | 18.00 s (−2.2%) |
| **new AGE handling** | 16.18 s (−12.1%) | 15.73 s (**−14.6%**) |

Within-round savings: memo 2.11 s, row order 0.42 s, the pair 2.77 s. The nine
per-round interactions:

```
+0.91  −0.20  +0.06  +0.68  −0.42  +0.23  +0.50  +0.16  −0.49   (seconds)
```

A median of +0.16 s, positive in only 6 rounds of 9, spanning −0.49 to
+0.91 s. These two are additive as far as this measurement can tell; the memo
carries most of the update path, and the row order is a real but small
addition. The contrast with the render pairing is the point: one interaction
is unanimous and worth 2 s, the other changes sign round to round.

Absolute times are not comparable between the two tables — they were measured
in different sessions on a shared machine, and the second ran under heavier
load. Only the arms within one table, and the within-round differences, are.
The headline table further up came from a third session, which is why the same
change reads −12.3% there; it keeps its own measurement rather than borrowing
any of these.

Two redundancies were removed, both of which repeated per-object work that
nothing had invalidated:

- `ResourceStore.get()` re-ordered the whole bucket on every read, although a
  repaint re-reads it constantly and a `MODIFIED` event replaces a value under
  an unchanged key — which cannot reorder anything. The order is now settled
  once per key-set change; objects are still re-read from the bucket, so a
  replaced value is never stale.
- `format_age` re-parsed every creation timestamp on every repaint, although
  "5m" is the correct answer for a whole minute. Each answer is now remembered
  with the window it is valid for, checked against both ends so a pane
  repainting with an earlier clock reading still gets the right string.

The age memo dominates the new retained allocation: 1,000 rows cost ~197 KiB.
It is capped both by entry count (20,000) and by key length, because `created`
is unvalidated API-server input and `datetime.fromisoformat` accepts an
arbitrarily long fractional-second field — a count-only cap would bound
nothing. The settled order adds one pointer list per bucket (~8 KiB at 1,000
objects; the key strings are shared with the bucket itself). Peak RSS moved
less than the ±1 MiB run-to-run spread at 128 MiB, so it is not a measurable
trade.

**The committed replay profile understates the render path.** Its synthetic
Pods carry no `created`, so `format_age` short-circuits and the AGE cell costs
nothing — which is why the same change is worth 7.4% against that profile and
12.1% once the timestamps a real cluster always sends are present. The
timestamps also account for the workload's own cost: adding them raised
baseline CPU from 13.93 s to 18.80 s. Benchmark numbers taken against the
committed profile are therefore a floor for anything AGE-dependent.

## Corrected live 1,000-pod smoke result

Run `i279-20260813-1620` exercised the same checked-in
`steady-24eps-1k` profile against the dedicated AKS cluster: 1,000 Running,
Ready Pods across 20 namespaces, 720 guarded metadata mutations in 30 seconds,
and 50 cursor samples after churn dispatch began. This run predates the
watch-receipt gate described below, so its cursor result is preliminary
dispatch-gated evidence, not a live budget verdict.

| Metric | Budget | Result | |
|---|---:|---:|---|
| Dropped updates | 0 | 0 | pass |
| Final digest mismatch | 0 | 0 | pass |
| LIST to 1,000-row table | ≤ 2 s | 144 ms | pass |
| Process start to interactive | ≤ 10 s | 1.55 s | pass |
| Peak RSS | ≤ 512 MiB | 236 MiB | pass |
| Achieved churn | 24 ev/s | 24.06 ev/s (720/720) | pass |
| Cursor-input p95 | ≤ 100 ms | 7 ms (n=50), dispatch-gated | preliminary |
| Event-to-render p95 | ≤ 250 ms @ 20 ev/s | **n/a for this workload** | not measured |

**Event-to-render was not measured by this run, and its 250 ms budget is
neither passed nor missed by it.** The live churn driver is metadata-only by
design: it patches one label, `korvid.dev/performance-tick`, and nothing
else. No Pod column renders labels — they feed the client-side `-l` filter —
so the in-place table diff finds no changed cell for such an event, writes
nothing, and requests no repaint. The harness nevertheless calls
`record_render()` when the resource-update handler returns, so the 32 ms
figure this run recorded is the interval from **watch-event receipt to
no-op table-diff completion**. That is a real number about the message path,
but it is not a rendered-frame measurement and is not compared to the render
budget here. (Almost every tick is a no-op: AGE is recomputed on each update,
so a tick that carries a Pod across a minute, hour or day boundary does write a
cell. Those are a small, unpredictable minority, which is the other reason the
figure is published under the weaker name.) A no-op diff is strictly less work
than one that writes cells and
requests a repaint, so 32 ms is a *lower bound* on what a rendered-cell
workload would record at the same rate — a lower bound can refute a budget,
never establish it. Measuring event-to-render live needs a churn workload that
changes a rendered cell (phase, ready, restarts) and observes it on the table;
that workload does not exist yet.

The deterministic replay above *does* rewrite rendered cells, so its
event-to-render numbers remain event-to-rendered-cell measurements.

The artifacts encode this distinction rather than leaving it to prose. Every
report carries `latency.update_latency_kind`. A deterministic replay reports
`event_to_render`, populates `latency.event_to_render`, leaves
`latency.watch_to_diff_completion` null, and prints "Event to render p95" in
the Markdown. A metadata-only live run reports `watch_to_diff_completion`,
publishes its samples under `latency.watch_to_diff_completion`, leaves
`latency.event_to_render` **null**, and prints "Watch receipt to diff
completion p95" — the string "Event to render" never appears in such a report.
`latency.event_to_render` becoming nullable is why the JSON `schema_version`
is `2`.

The current harness requires activity at the *application*, not merely at the
mutation driver: it opens its own watch and does not take the first cursor
sample until an owned `MODIFIED` event arrives. Run `i279-20260813-1620`
predates that gate and began sampling after mutation dispatch, which occurs
before the `PATCH` is awaited. Its 7 ms result therefore cannot establish that
the table was already receiving churn. A repeat under the current gate is
required before the live cursor budget can be called passed.

The driver completed all 720 requested mutations in 29.92 seconds
(24.06 events/s), with no mutation throttles. A separate run with `cProfile`
enabled measured 116 ms cursor-input p95 and 99.4% peak CPU; those figures are
diagnostic overhead, not acceptance results (its 134 ms watch-receipt-to-diff-
completion p95 carries the same metadata-only caveat as above).

## Known limits

**The corrected live cursor budget remains unqualified.** The 30-second run
establishes cluster topology, achieved churn, digest, drops, startup, and memory,
but its cursor samples were dispatch-gated. It must be repeated with the current
watch-receipt gate. The 31-minute endurance run also has not been repeated with
the corrected input probe and current render path.

**Event-to-render is unqualified live.** The live churn workload is
metadata-only and changes no rendered cell, so the 250 ms budget has no live
result behind it — only the deterministic replay exercises rendered-cell
updates. A live churn driver that mutates a rendered field is required before
that budget can be called passed or missed against a real API server.

**The replay workload has no creation timestamps.** `initial_pods` builds
`PodSummary` without `created`, so every AGE cell renders "-" and the whole
age path is skipped. Measured above, that alone accounts for 26% of the
update-path CPU on this workload, so any AGE-dependent figure taken from the
committed profile is a floor rather than a result. Populating `created` would
change every published baseline, so it is deliberately left as follow-up work
rather than folded into an unrelated change.

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
