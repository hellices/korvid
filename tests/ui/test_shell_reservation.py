"""The shell controller's write reservation must be taken synchronously (#187).

A confirmation callback builds the write coroutine and hands it to
`run_worker`, which starts it on a later event-loop iteration. A `:ctx`
switch processed in that gap must already see the write in flight, so the
reservation belongs at the call, not inside the coroutine body.

Review caught this exact defect twice during the decomposition - once in
the helm uninstall path and again in the `gate.run` adapter - both times
after the code looked obviously correct. It is pinned here.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import weakref
from typing import Any

import pytest
from textual.app import SuspendNotSupported

from korvid.ui.shell_controller import ShellController, ShellSettings


class _RecordingGate:
    """Records when a write slot is reserved and released."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def reserve_write(self) -> Any:
        self.events.append("reserve")
        released = False

        def release() -> None:
            # Idempotent, like the app's real reservation: both the
            # coroutine's finally and the GC finalizer may fire it.
            nonlocal released
            if not released:
                released = True
                self.events.append("release")

        return release

    def epoch(self) -> int:
        return 0

    def switching(self) -> bool:
        return False

    def reads_allowed(self) -> bool:
        return True


def _controller(gate: _RecordingGate) -> ShellController:
    settings = ShellSettings(
        kube_context=None,
        debug_default_image=None,
        debug_images=None,
        node_shell_image=None,
        node_shell_namespace=None,
    )
    unused: Any = None
    return ShellController(
        gate=gate,  # type: ignore[arg-type]  # records only the reservation calls
        view=unused,
        ui=unused,
        debug=lambda: None,
        audit=lambda: None,
        get_manifest=lambda: None,
        pod_containers=lambda ns, name: (),
        node_target=lambda action: None,
        confirm_screen=unused,
        target_uid=unused,
        settings=lambda: settings,
    )


def test_the_write_slot_is_reserved_before_the_coroutine_runs() -> None:
    """Building the coroutine reserves; awaiting it is too late."""
    gate = _RecordingGate()
    controller = _controller(gate)

    coro = controller.run_debug("default", "api-1", None, None)
    try:
        assert gate.events == ["reserve"], "reservation deferred past coroutine construction"
    finally:
        coro.close()


def test_a_coroutine_that_never_runs_still_releases_the_slot() -> None:
    """A worker cancelled before its first step must not leak the slot.

    A leaked +1 blocks every later `:ctx` switch for the session's life.

    `close()` releases deterministically: the returned `ReservedWrite`
    makes it a release point, because a coroutine that never started
    ignores `close()` and never reaches its own `finally`. Collection is
    only the backstop, for an object that is neither closed, awaited, nor
    thrown into.
    """
    gate = _RecordingGate()
    controller = _controller(gate)

    coro = controller.run_debug("default", "api-1", None, None)
    coro.close()
    # Asserted here, while `coro` is still referenced and nothing has been
    # collected: without this the decorator could regress to
    # collector-based release and the test would stay green.
    assert gate.events == ["reserve", "release"]

    del coro
    gc.collect()
    assert gate.events == ["reserve", "release"], "the backstop double-released"


def test_the_release_is_idempotent_across_close_and_collection() -> None:
    """Both the finalizer and the coroutine's own `finally` may fire.

    The count must move by exactly one either way, or a double release
    would let a `:ctx` switch proceed while a write is still in flight -
    the failure mode this counter exists to prevent.
    """
    gate = _RecordingGate()
    controller = _controller(gate)

    coro = controller.run_debug("default", "api-1", None, None)
    coro.close()
    del coro
    gc.collect()
    gc.collect()

    assert gate.events.count("release") == 1


def test_the_slot_is_released_after_the_write_completes() -> None:
    """The normal path releases exactly once."""
    gate = _RecordingGate()
    controller = _controller(gate)
    notices: list[str] = []
    controller._ui = type(  # minimal surface for the one notify this path makes
        "_Ui", (), {"notify": lambda self, message, **kw: notices.append(message)}
    )()

    asyncio.run(controller.run_debug("default", "api-1", None, None))

    assert gate.events == ["reserve", "release"]


def test_pod_shell_refuses_gracefully_when_the_driver_cannot_suspend() -> None:
    """A non-suspending driver (Windows console, web) must not raise.

    The node-shell path already handles this; the pod path let
    `SuspendNotSupported` escape and turn the `s` key into a crash.
    """
    gate = _RecordingGate()
    controller = _controller(gate)
    notices: list[str] = []

    class _Ui:
        def suspend(self) -> Any:
            raise SuspendNotSupported("no tty")

        def notify(self, message: str, **kwargs: Any) -> None:
            notices.append(message)

    controller._ui = _Ui()  # type: ignore[assignment]  # only suspend/notify are reached

    controller.run_shell("default", "api-1", None)

    assert any("suspend" in note for note in notices)


def test_pod_shell_reports_a_missing_kubectl_instead_of_raising() -> None:
    """kubectl can disappear between the PATH check and the exec."""
    gate = _RecordingGate()
    controller = _controller(gate)
    notices: list[str] = []

    class _Ui:
        def suspend(self) -> Any:
            return contextlib.nullcontext()

        def refresh(self) -> None:
            repaints.append(1)

        def notify(self, message: str, **kwargs: Any) -> None:
            notices.append(message)

    repaints: list[int] = []
    controller._ui = _Ui()  # type: ignore[assignment]  # only these three are reached
    controller._run_interactive = _raise_oserror  # type: ignore[method-assign]  # simulate a vanished binary

    controller.run_shell("default", "api-1", None)

    assert any("kubectl" in note for note in notices)
    # _run_interactive prints its banner before launching kubectl, so the
    # terminal is already dirty when it raises: without a repaint the user
    # is left staring at that banner instead of the TUI.
    assert repaints == [1], "the TUI was never restored after the failed exec"


def _raise_oserror(argv: list[str], banner: str) -> int:
    raise OSError(2, "No such file or directory")


async def test_reserved_write_is_still_consumable_by_await() -> None:
    """The rejected alternative broke exactly this (#237).

    Priming the coroutine to arm its `finally` made it unawaitable, so the
    wrapper has to stay transparent to `await`, not only to worker Tasks.
    """
    from korvid.ui.write_gate import ReservedWrite

    released: list[int] = []

    async def work() -> str:
        return "done"

    reserved = ReservedWrite(work(), lambda: released.append(1))
    assert await reserved == "done"
    # The body has no `finally` of its own, so a release here could only
    # come from the wrapper double-counting a completed write.
    assert released == []


async def test_closing_a_finished_reserved_write_does_not_double_release() -> None:
    """A completed write already released from its own `finally`."""
    from korvid.ui.write_gate import ReservedWrite

    calls: list[int] = []
    released = False

    def release() -> None:
        nonlocal released
        if not released:
            released = True
            calls.append(1)

    async def work() -> None:
        try:
            return None
        finally:
            release()

    reserved = ReservedWrite(work(), release)
    await reserved
    reserved.close()
    assert calls == [1]


async def test_reserved_write_propagates_a_thrown_exception() -> None:
    """`throw` must reach the wrapped coroutine, not be swallowed."""
    from korvid.ui.write_gate import ReservedWrite

    seen: list[str] = []

    async def work() -> None:
        try:
            await asyncio.sleep(0)
        except ValueError:
            seen.append("caught")
            raise

    reserved = ReservedWrite(work(), lambda: None)
    reserved.send(None)
    with pytest.raises(ValueError, match="boom"):
        reserved.throw(ValueError("boom"))
    assert seen == ["caught"]


async def test_direct_await_holds_the_reservation_while_suspended() -> None:
    """The wrapper must survive its own `__await__` (#237 review).

    `await` keeps the iterator `__await__` returns, not the object it came
    from, and a direct `await app._run_write(...)` never binds the wrapper
    to a name. Returning the inner coroutine's iterator therefore left
    nothing referencing the wrapper: a collection while the write was
    suspended mid-flight would fire the finalizer and release the slot
    underneath a running mutation.
    """
    import gc

    from korvid.ui.write_gate import ReservedWrite

    released: list[str] = []
    resume = asyncio.Event()

    async def work() -> str:
        await resume.wait()
        return "done"

    def build() -> Any:
        reserved = ReservedWrite(work(), lambda: released.append("release"))
        weakref.finalize(reserved, released.append, "finalize")
        return reserved

    async def consume() -> str:
        # The wrapper is a temporary: nothing names it, exactly like
        # `await app._run_write(...)`.
        return await build()  # type: ignore[no-any-return]  # test double

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    gc.collect()
    assert released == [], "reservation released while the write was in flight"
    resume.set()
    assert await task == "done"


async def test_cancel_before_the_first_step_releases_the_reservation() -> None:
    """The scenario the issue is actually about (#237 review round 2).

    A worker Task cancelled before it starts does not reach `close()`: the
    event loop throws `CancelledError` into the coroutine. An unstarted
    inner coroutine propagates that without entering the body, so its
    `finally` never runs, and the finished Task can keep the wrapper
    referenced so the finalizer does not fire either.
    """
    from korvid.ui.write_gate import ReservedWrite

    released: list[str] = []

    async def work() -> None:  # pragma: no cover - never starts
        await asyncio.sleep(1)

    task = asyncio.create_task(ReservedWrite(work(), lambda: released.append("release")))
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert released == ["release"]


async def test_a_caught_throw_that_suspends_stays_reserved() -> None:
    """Releasing on every `throw` would drop the slot mid-write.

    A coroutine that catches the thrown exception and suspends again is
    still running, so the write is still in flight.
    """
    from korvid.ui.write_gate import ReservedWrite

    released: list[str] = []
    resume = asyncio.Event()

    async def work() -> str:
        try:
            await asyncio.sleep(0)
        except ValueError:
            await resume.wait()
        return "done"

    reserved = ReservedWrite(work(), lambda: released.append("release"))
    reserved.send(None)
    reserved.throw(ValueError("boom"))
    assert released == [], "still suspended inside the write"
    resume.set()
    with contextlib.suppress(StopIteration):
        reserved.send(None)
    assert released == ["release"]
