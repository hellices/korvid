"""Deterministic churn pacing seam for the replay and live harness tests.

The harness never pauses or reshapes the workload for the input probe: the
published input percentile is only truthful if every cursor sample lands on a
stream that is still moving, so a run whose churn finishes first fails by name
instead of padding the metric with idle key presses.

That contract is exactly what makes a "compress the whole schedule into zero
wall time" test configuration invalid. Such a run drains its entire schedule
before the probe has taken a single sample, so it cannot measure input latency
under load - no matter how many events it schedules.

`sample_paced_schedule` gives a test a schedule that outlives its probe without
introducing any wall-clock dependency, using the `ReplayOptions.monotonic_fn` /
`ReplayOptions.async_sleep` seams the harness already documents for tests:

* the clock is virtual and only advances inside the injected sleep, so no test
  waits on real time;
* each scheduled-event sleep waits for a permit that a *completed cursor
  sample* releases, so the update stream keeps producing events for the whole
  probe - the interleave is decided by observed sample completions, never by
  a timing race;
* the release settles before the next sample starts, so the virtual clock is
  never advanced *during* a measurement and a virtual-clock latency assertion
  still reads exactly zero;
* once the probe has taken all of its samples the schedule runs unimpeded, so
  churn completion and the final render pass are never held up.

Pacing lives here, in the test seam. Production code must not gate the
workload on the probe: doing so would sample a frozen update stream and make
the reported figure meaningless.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.performance import replay as replay_module
from tests.performance.replay import ReplayOptions

#: Event-loop turns `SamplePacedSchedule.release` gives the schedule to consume
#: a permit. Bounded on purpose: a schedule with nothing left to sleep on must
#: not turn the metric-contract failure into a hang.
_SETTLE_TURNS = 100


class SamplePacedSchedule:
    """Virtual clock that releases one scheduled event per cursor sample.

    Args:
        pairs: The `down`/`up` cursor round trips the probe will perform. The
            schedule releases one event per *sample* (two per pair) and then
            free-runs, so a profile with more events than `2 * pairs` is still
            churning when the last sample is taken.
    """

    def __init__(self, *, pairs: int) -> None:
        self._pairs = pairs
        self._remaining_samples = 2 * pairs
        self._gate = asyncio.Event()
        self._advanced = asyncio.Event()
        self._free_running = False
        self._virtual_time = 0.0
        #: Every delay the schedule was asked to sleep, in order. Pacing never
        #: alters these values (the caller computes them before sleeping), so
        #: tests that assert on the schedule's timing can read them directly.
        self.delays: list[float] = []

    def monotonic(self) -> float:
        """Virtual clock reading; advances only inside `sleep`."""
        return self._virtual_time

    async def sleep(self, delay: float) -> None:
        """Advance the virtual clock by *delay*, one cursor sample at a time."""
        if not self._free_running:
            await self._gate.wait()
            self._gate.clear()
        self.delays.append(delay)
        self._virtual_time += delay
        self._advanced.set()
        await asyncio.sleep(0)

    async def release(self) -> None:
        """Let one more scheduled event through; free-run once the probe ends.

        Returns once the schedule has actually advanced (or after a bounded
        number of event-loop turns, so a schedule that already ran out of
        events fails the run by name instead of hanging).
        """
        self._remaining_samples -= 1
        if self._remaining_samples <= 0:
            self._free_running = True
        self._advanced.clear()
        self._gate.set()
        for _ in range(_SETTLE_TURNS):
            if self._advanced.is_set():
                return
            await asyncio.sleep(0)

    def options(self, **overrides: Any) -> ReplayOptions:
        """`ReplayOptions` wired to this schedule, with *overrides* applied."""
        return ReplayOptions(
            time_scale=1,
            monotonic_fn=self.monotonic,
            async_sleep=self.sleep,
            input_sample_pairs=self._pairs,
            **overrides,
        )


def sample_paced_schedule(
    monkeypatch: pytest.MonkeyPatch, *, pairs: int = 1
) -> SamplePacedSchedule:
    """Pace the scheduled-event stream on completed cursor samples.

    Patches the single cursor measurement both harnesses share, so replay and
    live runs are paced by the same observed signal.

    Args:
        monkeypatch: The test's patching fixture.
        pairs: Cursor sample pairs the probe will take. Keep the profile's
            event count comfortably above `2 * pairs`, otherwise the schedule
            legitimately finishes mid-probe and the run fails the metric
            contract.

    Returns:
        The schedule, whose `options` method builds the matching
        `ReplayOptions`.
    """
    schedule = SamplePacedSchedule(pairs=pairs)
    original = replay_module.measure_cursor_input

    async def paced_measure(*args: Any, **kwargs: Any) -> float:
        elapsed = await original(*args, **kwargs)
        await schedule.release()
        return float(elapsed)

    monkeypatch.setattr(replay_module, "measure_cursor_input", paced_measure)
    return schedule
