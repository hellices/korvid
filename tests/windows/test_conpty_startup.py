"""Portable regressions for ConPTY startup cleanup."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from tests.windows import conpty


class _FailingProcess(conpty.ConPtyProcess):
    def __init__(self, **kwargs: Any) -> None:
        raise RuntimeError("constructor failed")


class _DiscardKernel:
    def WaitForSingleObject(self, handle: int, timeout: int) -> int:
        return conpty._WAIT_OBJECT_0


class _DiscardApi:
    def __init__(self) -> None:
        self.kernel32 = _DiscardKernel()
        self.closed: list[int | None] = []

    def close_handle(self, handle: int | None) -> None:
        self.closed.append(handle)
        if handle == 40:
            raise OSError("process close failed")
        if handle == 50:
            raise OSError("job close failed")


class _PseudoKernel:
    def __init__(self, events: list[tuple[str, int]]) -> None:
        self._events = events

    def ClosePseudoConsole(self, hpc: int) -> None:
        self._events.append(("pseudo", hpc))


class _PseudoApi:
    def __init__(self) -> None:
        self.events: list[tuple[str, int]] = []
        self.kernel32 = _PseudoKernel(self.events)

    def close_handle(self, handle: int | None) -> None:
        assert handle is not None
        self.events.append(("handle", handle))
        if handle == 10:
            raise OSError("input close failed")
        if handle == 30:
            raise OSError("output close failed")


def test_constructor_failure_preserves_error_and_attempts_all_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object()
    pseudo = conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30)
    spawned = conpty._SpawnedProcess(process_handle=40, job_handle=50, pid=1234)
    events: list[str] = []

    monkeypatch.setattr(conpty, "_WindowsApi", lambda: api)
    monkeypatch.setattr(conpty, "_create_pseudoconsole", lambda *_args: pseudo)
    monkeypatch.setattr(conpty, "_spawn_process", lambda *_args: spawned)

    def discard(*_args: object) -> None:
        events.append("discard")
        raise OSError("discard failed")

    def close_pseudo(*_args: object) -> None:
        events.append("pseudo")
        raise OSError("pseudo failed")

    monkeypatch.setattr(conpty, "_discard_spawned_process", discard)
    monkeypatch.setattr(conpty, "_close_pseudoconsole", close_pseudo)

    with pytest.raises(RuntimeError, match="constructor failed") as exc_info:
        _FailingProcess.start(
            ["python"],
            cwd=Path("."),
            env={},
            columns=80,
            rows=24,
            capture_limit=32,
            artifact_path=None,
        )

    assert events == ["discard", "pseudo"]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: discard failed",
        "Additional cleanup error: pseudo failed",
    ]


def test_discard_closes_job_when_process_handle_close_fails() -> None:
    api = _DiscardApi()
    spawned = conpty._SpawnedProcess(process_handle=40, job_handle=50, pid=1234)

    with pytest.raises(OSError, match="process close failed") as exc_info:
        conpty._discard_spawned_process(cast(Any, api), spawned)

    assert api.closed == [40, 50]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: job close failed"
    ]


def test_pseudoconsole_cleanup_continues_after_input_close_failure() -> None:
    api = _PseudoApi()
    pseudo = conpty._PseudoConsole(hpc=20, input_handle=10, output_handle=30)

    with pytest.raises(OSError, match="input close failed") as exc_info:
        conpty._close_pseudoconsole(cast(Any, api), pseudo)

    assert api.events == [("handle", 10), ("pseudo", 20), ("handle", 30)]
    assert getattr(exc_info.value, "__notes__", []) == [
        "Additional cleanup error: output close failed"
    ]
