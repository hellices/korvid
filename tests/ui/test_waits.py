"""Tests for the shared deterministic wait helper."""

from __future__ import annotations

import pytest

from .waits import until


class _FakePilot:
    """Stands in for a Textual pilot: pause() just yields control."""

    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def pause(self, delay: float) -> None:
        self.pauses.append(delay)


async def test_until_returns_when_condition_holds_immediately() -> None:
    pilot = _FakePilot()
    await until(pilot, lambda: True, timeout=0.1)
    assert pilot.pauses == []


async def test_until_checks_condition_once_more_after_final_pause() -> None:
    """A condition that becomes true during the last pause must not fail
    (off-by-one: the loop used to raise without re-checking)."""
    pilot = _FakePilot()
    results = iter([False, False, True])  # timeout=0.1 -> 2 in-loop checks + final
    await until(pilot, lambda: next(results), timeout=0.1)
    assert len(pilot.pauses) == 2


async def test_until_pauses_cover_the_full_requested_timeout() -> None:
    """`int(timeout / 0.05)` truncation must not shorten the wait: the pauses
    add up to the advertised timeout, with a shorter final step."""
    pilot = _FakePilot()
    with pytest.raises(AssertionError, match=r"condition not met within 0\.12s"):
        await until(pilot, lambda: False, timeout=0.12)
    assert sum(pilot.pauses) == pytest.approx(0.12)


async def test_until_sub_interval_timeout_still_pauses_once() -> None:
    """A timeout below one 50ms tick still yields to the app once."""
    pilot = _FakePilot()
    with pytest.raises(AssertionError, match=r"condition not met within 0\.03s"):
        await until(pilot, lambda: False, timeout=0.03)
    assert pilot.pauses == [pytest.approx(0.03)]


async def test_until_raises_with_label_on_timeout() -> None:
    pilot = _FakePilot()
    with pytest.raises(AssertionError, match=r"dialog visible not met within 0\.1s"):
        await until(pilot, lambda: False, timeout=0.1, label="dialog visible")
