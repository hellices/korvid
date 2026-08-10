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
from typing import Any

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

    The release fires when the coroutine is collected, not when `close()`
    is called: a coroutine that never started ignores `close()`, and
    arming its `finally` by priming makes the object unawaitable
    (`RuntimeError: coroutine is being awaited already`) anywhere a
    decorated method is consumed by `await` rather than by a worker task.
    The window is therefore bounded by collection, which under CPython
    refcounting is the moment the last reference goes away.
    """
    gate = _RecordingGate()
    controller = _controller(gate)

    coro = controller.run_debug("default", "api-1", None, None)
    coro.close()
    del coro
    gc.collect()

    assert gate.events == ["reserve", "release"]


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
