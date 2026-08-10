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
import gc
from typing import Any

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

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the reservation test must not need gate.{name}")


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


def test_closing_an_unrun_coroutine_releases_the_slot() -> None:
    """A worker cancelled before it starts must not leak the reservation.

    A leaked +1 blocks every later `:ctx` switch for the session's life.
    """
    gate = _RecordingGate()
    controller = _controller(gate)

    coro = controller.run_debug("default", "api-1", None, None)
    coro.close()
    # The finalizer fires on collection, not on close: a never-started
    # coroutine never reaches its own `finally`.
    del coro
    gc.collect()

    assert gate.events == ["reserve", "release"]


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
