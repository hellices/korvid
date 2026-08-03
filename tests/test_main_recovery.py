"""Crash-recovery loop at the composition root (issue #166): a fatal app
exception offers a restart with fresh wiring instead of dying, capped so a
deterministic startup crash cannot loop, and never prompts when
non-interactive or explicitly disabled."""

from __future__ import annotations

import pytest

from korvid.__main__ import (
    RESTART_CAP,
    RESTART_WINDOW_SECONDS,
    _run_with_recovery,
)


class FlakyRunner:
    """Raises for the first *failures* calls, then returns cleanly."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"boom {self.calls}")


def test_crash_offers_restart_and_the_second_run_executes() -> None:
    runner = FlakyRunner(failures=1)
    answers = iter(["y"])
    _run_with_recovery(
        runner,
        allow_restart=True,
        prompt=lambda: next(answers),
        clock=lambda: 0.0,
    )
    assert runner.calls == 2  # crashed once, restarted once, exited clean


def test_empty_answer_defaults_to_restart() -> None:
    runner = FlakyRunner(failures=1)
    _run_with_recovery(runner, allow_restart=True, prompt=lambda: "", clock=lambda: 0.0)
    assert runner.calls == 2


def test_declined_restart_reraises_the_crash() -> None:
    runner = FlakyRunner(failures=1)
    with pytest.raises(RuntimeError, match="boom 1"):
        _run_with_recovery(runner, allow_restart=True, prompt=lambda: "n", clock=lambda: 0.0)
    assert runner.calls == 1


def test_restart_disabled_reraises_without_prompting() -> None:
    runner = FlakyRunner(failures=1)

    def prompt() -> str:
        raise AssertionError("prompt must never be called when restart is disabled")

    with pytest.raises(RuntimeError, match="boom 1"):
        _run_with_recovery(runner, allow_restart=False, prompt=prompt, clock=lambda: 0.0)
    assert runner.calls == 1


def test_crash_cap_stops_a_deterministic_crash_loop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = FlakyRunner(failures=100)
    with pytest.raises(RuntimeError, match="boom"):
        _run_with_recovery(runner, allow_restart=True, prompt=lambda: "y", clock=lambda: 0.0)
    # cap crashes within the window are prompted; the next one stops the loop
    assert runner.calls == RESTART_CAP + 1
    assert "not restarting" in capsys.readouterr().err


def test_old_crashes_age_out_of_the_window() -> None:
    runner = FlakyRunner(failures=RESTART_CAP + 1)
    times = iter(
        [i * (RESTART_WINDOW_SECONDS + 1) for i in range(RESTART_CAP + 2)]
    )  # each crash lands in its own window
    _run_with_recovery(runner, allow_restart=True, prompt=lambda: "y", clock=lambda: next(times))
    assert runner.calls == RESTART_CAP + 2  # every crash prompted, then clean exit


def test_clean_exit_never_prompts() -> None:
    runner = FlakyRunner(failures=0)

    def prompt() -> str:
        raise AssertionError("prompt must never be called on a clean exit")

    _run_with_recovery(runner, allow_restart=True, prompt=prompt, clock=lambda: 0.0)
    assert runner.calls == 1


def test_keyboard_interrupt_propagates_without_prompting() -> None:
    def runner() -> None:
        raise KeyboardInterrupt

    def prompt() -> str:
        raise AssertionError("prompt must never be called on KeyboardInterrupt")

    with pytest.raises(KeyboardInterrupt):
        _run_with_recovery(runner, allow_restart=True, prompt=prompt, clock=lambda: 0.0)


def test_system_exit_propagates_without_prompting() -> None:
    def runner() -> None:
        raise SystemExit(2)

    def prompt() -> str:
        raise AssertionError("prompt must never be called on SystemExit")

    with pytest.raises(SystemExit):
        _run_with_recovery(runner, allow_restart=True, prompt=prompt, clock=lambda: 0.0)


def test_traceback_is_logged_on_every_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FlakyRunner(failures=1)
    with caplog.at_level("ERROR"):
        _run_with_recovery(runner, allow_restart=True, prompt=lambda: "y", clock=lambda: 0.0)
    assert any("crashed" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)  # full traceback recorded
