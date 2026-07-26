"""Deterministic wait helper shared by UI tests.

Fixed `pilot.pause(0.2)` sleeps flake on slow CI runners; poll an
observable condition instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def until(
    pilot: Any,
    cond: Callable[[], object],
    timeout: float = 5.0,
    label: str = "condition",
) -> None:
    """Advance the app until `cond()` is truthy, or fail after `timeout` seconds.

    The condition is re-checked once after the final pause so it cannot fail
    on an outcome that arrived during the last tick, and `label` names the
    awaited outcome in the failure message for easier CI diagnosis.
    """
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    if cond():
        return
    raise AssertionError(f"{label} not met within {timeout}s")
