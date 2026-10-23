"""Deterministic wait helper shared by UI tests.

Fixed `pilot.pause(0.2)` sleeps flake on slow CI runners; poll an
observable condition instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class WaitTimeout(Exception):
    """Raised when `until` gives up waiting for a condition.

    Deliberately *not* an `AssertionError`: a wait timeout is an operational
    outcome a caller may catch and report (e.g. the benchmark CLI turns it
    into exit 1), while a genuine `assert` inside production code must keep
    propagating as the programmer error it is.
    """


#: How long `until` polls by default before giving up. The bound only keeps
#: a broken test from hanging - a wait that passes returns as soon as its
#: condition holds - so it is sized for a loaded CI runner, where a wait that
#: spans a durable audit append can sit behind a multi-second `fsync` (#464).
HANG_GUARD_SECONDS = 30.0


async def until(
    pilot: Any,
    cond: Callable[[], object],
    timeout: float = HANG_GUARD_SECONDS,
    label: str = "condition",
) -> None:
    """Advance the app until `cond()` is truthy, or fail after `timeout` seconds.

    The pauses always add up to the full requested timeout (a shorter final
    step covers the remainder, so truncation never shortens the wait), the
    condition is re-checked once after the final pause so it cannot fail on
    an outcome that arrived during the last tick, and `label` names the
    awaited outcome in the failure message for easier CI diagnosis.

    Raises:
        WaitTimeout: `cond()` was still falsy after `timeout` seconds.
    """
    remaining = timeout
    while remaining > 0:
        if cond():
            return
        step = min(0.05, remaining)
        await pilot.pause(step)
        remaining -= step
    if cond():
        return
    raise WaitTimeout(f"{label} not met within {timeout}s")
