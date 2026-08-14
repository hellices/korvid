"""Pacing seam: a stalled schedule must fail by name, not pass vacuously."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from _pytest.outcomes import Failed

from korvid.ui.widgets.resource_table import ResourceTable
from tests.performance import pacing
from tests.performance import replay as replay_module
from tests.performance.pacing import SamplePacedSchedule


async def test_release_reports_that_the_schedule_advanced() -> None:
    """The normal path: a waiting sleeper consumes the permit."""
    schedule = SamplePacedSchedule(pairs=2)
    sleeping = asyncio.create_task(schedule.sleep(1.0))
    await asyncio.sleep(0)

    advanced = await schedule.release()

    await sleeping
    assert advanced is True


async def test_release_reports_a_schedule_that_never_advances() -> None:
    """With nothing sleeping on the gate the permit is never consumed.

    `release` used to return silently here, so pacing disengaged and any test
    relying on it could keep passing while proving nothing. It must say so.
    """
    schedule = SamplePacedSchedule(pairs=2)

    advanced = await schedule.release()

    assert advanced is False


async def test_a_stalled_schedule_fails_the_test_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam turns a stalled schedule into a named failure, so a run that
    quietly stopped being paced can never be mistaken for a passing one."""

    async def never_advances(*_args: object, **_kwargs: object) -> float:
        return 0.0

    monkeypatch.setattr(replay_module, "measure_cursor_input", never_advances)
    pacing.sample_paced_schedule(monkeypatch, pairs=2)

    with pytest.raises(Failed, match="sample pacing stalled"):
        await replay_module.measure_cursor_input(None, cast(ResourceTable, None), "down")


async def test_the_final_sample_is_allowed_to_free_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the probe has taken every sample the schedule stops being gated,
    so the last release must not be reported as a stall even though nothing
    is waiting on the gate any more."""

    async def measured(*_args: object, **_kwargs: object) -> float:
        return 0.0

    monkeypatch.setattr(replay_module, "measure_cursor_input", measured)
    schedule = pacing.sample_paced_schedule(monkeypatch, pairs=1)
    # `pairs=1` is two samples: the first is still gated, so give it a sleeper.
    sleeping = asyncio.create_task(schedule.sleep(1.0))
    await asyncio.sleep(0)
    await replay_module.measure_cursor_input(None, cast(ResourceTable, None), "down")
    await sleeping
    assert schedule.free_running is False

    await replay_module.measure_cursor_input(None, cast(ResourceTable, None), "up")

    assert schedule.free_running is True
