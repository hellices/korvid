"""Crash-recovery loop at the composition root (issue #166): a fatal app
exception offers a restart with fresh wiring instead of dying, capped so a
deterministic startup crash cannot loop, and never prompts when
non-interactive or explicitly disabled."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable
from typing import Any

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
    # the documented cap: the RESTART_CAP-th crash within the window stops
    # the loop — crashes 1..CAP-1 are prompted, crash CAP is not
    assert runner.calls == RESTART_CAP
    assert "not restarting" in capsys.readouterr().err


def test_crash_cap_message_is_ascii_encodable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All stderr output from the crash-cap path must be encodable in ASCII
    (and therefore cp1252/any single-byte codepage) — em dashes or other
    non-ASCII would raise UnicodeEncodeError on Windows terminals."""
    runner = FlakyRunner(failures=100)
    with pytest.raises(RuntimeError, match="boom"):
        _run_with_recovery(runner, allow_restart=True, prompt=lambda: "y", clock=lambda: 0.0)
    captured = capsys.readouterr().err
    # This encodes the entire cap diagnostic; would raise on non-ASCII.
    captured.encode("ascii")


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

    with pytest.raises(KeyboardInterrupt, match=r"^$"):
        _run_with_recovery(runner, allow_restart=True, prompt=prompt, clock=lambda: 0.0)


def test_system_exit_propagates_without_prompting() -> None:
    def runner() -> None:
        raise SystemExit(2)

    def prompt() -> str:
        raise AssertionError("prompt must never be called on SystemExit")

    with pytest.raises(SystemExit, match="2"):
        _run_with_recovery(runner, allow_restart=True, prompt=prompt, clock=lambda: 0.0)


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_main_arms_and_disarms_runner_watchdog_on_every_exit(
    failure: type[BaseException] | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import korvid.__main__ as main_mod

    events: list[str] = []
    loops: list[asyncio.AbstractEventLoop] = []

    class Watchdog:
        def __init__(self, interval: float, function: Callable[[], None]) -> None:
            assert interval == main_mod._RUNNER_SHUTDOWN_SECONDS == 5.0
            assert function is main_mod._force_runner_exit
            self.daemon = False

        def start(self) -> None:
            assert self.daemon
            events.append("watchdog armed")

        def cancel(self) -> None:
            events.append("watchdog disarmed")

    async def run(**kwargs: object) -> None:
        loops.append(asyncio.get_running_loop())
        events.append("run returned")
        if failure is not None:
            raise failure("primary failure")

    monkeypatch.setattr(threading, "Timer", Watchdog)
    monkeypatch.setattr(main_mod, "_run", run)
    monkeypatch.setattr(sys, "argv", ["korvid", "--no-restart"])
    if failure is None:
        main_mod.main()
    else:
        with pytest.raises(failure, match="primary failure"):
            main_mod.main()
    assert events == ["run returned", "watchdog armed", "watchdog disarmed"]
    assert len(loops) == 1
    assert loops[0].is_closed()


def test_main_sanitizes_loop_reports_before_runner_finalization(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import korvid.__main__ as main_mod

    async def run(**kwargs: object) -> None:
        loop = asyncio.get_running_loop()
        loop.call_exception_handler(
            {"message": "SECRET_CLEANUP_PAYLOAD", "exception": RuntimeError("private data")},
        )
        assert "Event loop operation failed" in caplog.text

    monkeypatch.setattr(main_mod, "_run", run)
    monkeypatch.setattr(sys, "argv", ["korvid", "--no-restart"])
    main_mod.main()
    assert "Event loop operation failed" in caplog.text
    assert "cleanup" not in caplog.text.lower()
    assert "SECRET_CLEANUP_PAYLOAD" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("failure", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_runner_close_failure_is_terminal_without_secret_disclosure(
    failure: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    import korvid.__main__ as main_mod

    runner = asyncio.Runner()
    original_close = runner.close
    exits: list[int] = []

    async def run(**kwargs: object) -> None:
        return None

    def broken_close() -> None:
        raise failure("SECRET_CLEANUP_PAYLOAD")

    monkeypatch.setattr(asyncio, "Runner", lambda: runner)
    monkeypatch.setattr(runner, "close", broken_close)
    monkeypatch.setattr(main_mod, "_run", run)
    monkeypatch.setattr(os, "_exit", exits.append)
    monkeypatch.setattr(sys, "argv", ["korvid", "--no-restart"])
    try:
        main_mod.main()
        assert exits == [1]
        stderr = capfd.readouterr().err
        assert "Event loop finalization failed; exiting without restart" in caplog.text
        assert "SECRET_CLEANUP_PAYLOAD" not in stderr + caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        original_close()


def test_runner_failure_stays_terminal_when_diagnostic_logging_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import korvid.__main__ as main_mod

    runner = asyncio.Runner()
    original_close = runner.close
    exits: list[int] = []

    def broken_close() -> None:
        raise RuntimeError("finalization failed")

    def broken_log(*args: object, **kwargs: object) -> None:
        raise ValueError("stderr is closed")

    monkeypatch.setattr(runner, "close", broken_close)
    monkeypatch.setattr(main_mod.logger, "critical", broken_log)
    monkeypatch.setattr(os, "_exit", exits.append)
    try:
        with pytest.raises(ValueError, match="stderr is closed"):
            main_mod._close_runner(runner)
        assert exits == [1]
    finally:
        original_close()


def test_runner_watchdog_never_waits_for_stderr() -> None:
    script = textwrap.dedent(
        """
        import os
        import korvid.__main__ as main_mod

        read_descriptor, write_descriptor = os.pipe()
        os.set_blocking(write_descriptor, False)
        for size in (4096, 1):
            try:
                while True:
                    os.write(write_descriptor, b"x" * size)
            except BlockingIOError:
                pass
        os.set_blocking(write_descriptor, True)
        os.dup2(write_descriptor, 2)
        os.close(write_descriptor)
        print("terminal policy invoked", flush=True)
        main_mod._force_runner_exit()
        """
    )

    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Shutdown watchdog waited for stderr to be drained", pytrace=False)

    assert result.returncode == 1
    assert result.stdout == "terminal policy invoked\n"


def test_restart_prompt_writes_to_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Interactivity keys off stdin/stderr, so the question must go to
    stderr — a redirected stdout would otherwise swallow it (and be
    contaminated by it)."""
    from korvid.__main__ import _restart_prompt

    monkeypatch.setattr("builtins.input", lambda: "y")
    assert _restart_prompt() == "y"
    captured = capsys.readouterr()
    assert "restart?" in captured.err
    assert captured.out == ""


def test_traceback_is_logged_on_every_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FlakyRunner(failures=1)
    with caplog.at_level("ERROR"):
        _run_with_recovery(runner, allow_restart=True, prompt=lambda: "y", clock=lambda: 0.0)
    assert any("crashed" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records)  # full traceback recorded


async def test_startup_failure_after_connect_closes_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wiring exception between `kube.connect()` and the run loop must not
    leak the connected client into a recovery restart (review on #179)."""
    import korvid.__main__ as main_mod
    from korvid.core.config import KorvidConfig

    class FakeKube:
        def __init__(self, **kwargs: object) -> None:
            self.closed = False

        async def connect(self, context: str | None) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    created: list[FakeKube] = []

    def make_kube(**kwargs: object) -> FakeKube:
        kube = FakeKube(**kwargs)
        created.append(kube)
        return kube

    monkeypatch.setattr(main_mod, "KubeClient", make_kube)
    monkeypatch.setattr(
        main_mod,
        "_load_startup_config",
        lambda readonly, mcp, namespace: KorvidConfig(namespace="default"),
    )

    async def boom(kube: object, *, readonly: bool = False) -> bool:
        raise RuntimeError("startup probe failed")

    monkeypatch.setattr(main_mod, "_probe_pod_resize", boom)

    with pytest.raises(RuntimeError, match="startup probe failed"):
        await main_mod._run()
    assert created  # the client was built…
    assert created[0].closed  # …and the connected client never leaks


def _main_gate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    argv: list[str],
    stdin_tty: bool,
    stderr_tty: bool,
) -> bool:
    """Run main() with the recovery loop stubbed; return the allow_restart
    it was gated with."""
    import types

    import korvid.__main__ as main_mod

    captured: dict[str, bool] = {}

    def fake_recovery(
        runner: object, *, allow_restart: bool, prompt: object, clock: object
    ) -> None:
        captured["allow_restart"] = allow_restart

    monkeypatch.setattr(main_mod, "_run_with_recovery", fake_recovery)
    monkeypatch.setattr(sys, "argv", ["korvid", *argv])
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(isatty=lambda: stdin_tty))
    monkeypatch.setattr(sys, "stderr", types.SimpleNamespace(isatty=lambda: stderr_tty))
    main_mod.main()
    return captured["allow_restart"]


def test_main_enables_restart_only_on_a_full_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main_gate(monkeypatch, argv=[], stdin_tty=True, stderr_tty=True) is True


def test_main_disables_restart_when_stdin_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _main_gate(monkeypatch, argv=[], stdin_tty=False, stderr_tty=True) is False


def test_main_disables_restart_when_stderr_is_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _main_gate(monkeypatch, argv=[], stdin_tty=True, stderr_tty=False) is False


def test_main_disables_restart_with_the_no_restart_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _main_gate(monkeypatch, argv=["--no-restart"], stdin_tty=True, stderr_tty=True) is False


async def test_provider_created_before_a_later_wiring_failure_is_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider is owned by the teardown guard's box the moment it
    exists — a failure in the rest of the agent wiring (tools, session,
    configurator) must still close it (review on #179)."""
    import korvid.__main__ as main_mod

    class FakeProvider:
        pass

    fake = FakeProvider()

    def build_wiring(*args: object, **kwargs: object) -> object:
        provider_box = kwargs["provider_box"]
        assert isinstance(provider_box, list)
        provider_box[0] = fake  # provider exists…
        raise RuntimeError("agent wiring failed after provider creation")

    monkeypatch.setattr(main_mod, "_build_agent_wiring", build_wiring)

    class FakeKube:
        def __init__(self, **kwargs: object) -> None:
            self.closed = False

        async def connect(self, context: str | None) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def list_events_for(self, *a: object, **k: object) -> list[object]:
            return []

    closed: list[object] = []

    class FakeKubeFactory:
        def __call__(self, **kwargs: object) -> FakeKube:
            kube = FakeKube(**kwargs)
            closed.append(kube)
            return kube

    from korvid.core.config import KorvidConfig

    monkeypatch.setattr(main_mod, "KubeClient", FakeKubeFactory())
    monkeypatch.setattr(
        main_mod,
        "_load_startup_config",
        lambda readonly, mcp, namespace: KorvidConfig(namespace="default"),
    )

    async def ok_probe(kube: object, *, readonly: bool = False) -> bool:
        return False

    monkeypatch.setattr(main_mod, "_probe_pod_resize", ok_probe)

    from korvid.k8s.csp import ProviderInfo

    async def ok_csp(kube: object) -> ProviderInfo:
        return ProviderInfo(provider="unknown", distribution=None)

    monkeypatch.setattr(main_mod, "_probe_cloud_provider", ok_csp)

    provider_seen: list[object] = []

    async def fake_teardown(state: Any, kube: object) -> None:
        provider_seen.append(state.provider_box[0])
        await kube.close()  # type: ignore[attr-defined]  # fake in test

    monkeypatch.setattr(main_mod, "_teardown", fake_teardown)

    with pytest.raises(RuntimeError, match="agent wiring failed"):
        await main_mod._run()
    assert provider_seen == [fake]  # …and teardown was handed exactly it
