"""Minimal regression tests for the shared wait helper."""

from __future__ import annotations

import pytest

from .waits import WaitTimeout, until


class _FakePilot:
    async def pause(self, delay: float) -> None:
        return None


async def test_until_checks_condition_again_after_the_final_pause() -> None:
    pilot = _FakePilot()
    checks = 0

    def cond() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 2

    await until(pilot, cond, timeout=0.05)
    assert checks == 2


async def test_until_timeout_is_not_an_assertion_error() -> None:
    pilot = _FakePilot()
    with pytest.raises(WaitTimeout, match=r"condition not met within 0\.05s") as caught:
        await until(pilot, lambda: False, timeout=0.05)
    assert not isinstance(caught.value, AssertionError)
