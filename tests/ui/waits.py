"""Deterministic wait helper shared by UI tests.

Fixed `pilot.pause(0.2)` sleeps flake on slow CI runners; poll an
observable condition instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


async def until(pilot: Any, cond: Callable[[], object], timeout: float = 5.0) -> None:
    """Advance the app until `cond()` is truthy, or fail after `timeout` seconds."""
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met within timeout")
