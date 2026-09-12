"""Agent surfaces and bounded ownership of composition-root cleanup."""

from __future__ import annotations

import asyncio
import contextvars
import gc
import importlib.util
import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import korvid.__main__ as main_mod
from korvid.core.config import KorvidConfig, ModelConnectionConfig
from korvid.k8s.csp import ProviderInfo


@pytest.mark.parametrize(
    "module",
    ["korvid.agent.provider_plugin", "korvid.providers.plugin_registry"],
)
def test_unwired_provider_construction_modules_are_absent(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_agent_package_has_no_symbol_reexport_facade() -> None:
    import korvid.agent

    assert "__getattr__" not in vars(korvid.agent)
    assert "__all__" not in vars(korvid.agent)


def test_model_profile_vocabulary_has_no_disconnected_flow_registry() -> None:
    from korvid.agent import model_profiles

    assert not hasattr(model_profiles, "SpecialFlowRegistry")
    assert "SpecialFlowRegistry" not in model_profiles.__all__


def test_unconsumed_screen_string_adapter_is_absent() -> None:
    from korvid.agent import outbound

    assert not hasattr(outbound, "sanitize_screen_context")


def test_removed_prompt_registries_have_no_startup_compatibility_hint() -> None:
    import korvid.__main__

    assert not hasattr(korvid.__main__, "_PROMPT_PACKAGING_HINT")


class _GatedClose:
    def __init__(self, *, resist_cancel: bool = False, fail: bool = False) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.finished = asyncio.Event()
        self.resist_cancel = resist_cancel
        self.fail = fail

    async def aclose(self) -> None:
        self.started.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    if not self.resist_cancel:
                        raise
            if self.fail:
                raise RuntimeError("SECRET_CLEANUP_PAYLOAD")
        finally:
            self.finished.set()


class _Kube:
    def __init__(self, *, fail: bool = False) -> None:
        self.closed = asyncio.Event()
        self.fail = fail

    async def close(self) -> None:
        self.closed.set()
        if self.fail:
            raise RuntimeError("SECRET_CLEANUP_PAYLOAD")


class _MCP:
    def __init__(self, task: asyncio.Task[None] | None = None, *, fail: bool = False) -> None:
        self.task = task
        self.fail = fail

    async def shutdown(self) -> asyncio.Task[None] | None:
        if self.fail:
            raise RuntimeError("SECRET_CLEANUP_PAYLOAD")
        return self.task

    def pending_task(self) -> asyncio.Task[None] | None:
        return self.task if self.task is not None and not self.task.done() else None


@pytest.fixture
def forced_exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Expire cleanup budgets without sleeping; never exit the pytest process."""
    monkeypatch.setattr(main_mod, "_CLEANUP_GRACE_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(main_mod, "_CLEANUP_CANCEL_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(main_mod, "_MCP_SHUTDOWN_GRACE_SECONDS", 0.0, raising=False)
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)
    return exits


async def _join(task: asyncio.Task[None]) -> None:
    await asyncio.wait_for(asyncio.shield(task), timeout=2)


async def test_stuck_provider_cannot_prevent_kubernetes_cleanup(forced_exits: list[int]) -> None:
    provider = _GatedClose()
    kube = _Kube()
    cleanup = asyncio.create_task(
        main_mod._shutdown(None, cast("Any", provider), cast("Any", kube))
    )
    try:
        await _join(cleanup)
        assert provider.cancelled.is_set()
        assert kube.closed.is_set()
        assert forced_exits == []
    finally:
        provider.release.set()
        await _join(cleanup)


@pytest.mark.parametrize("failure", [ValueError, OSError])
async def test_standalone_shutdown_stays_terminal_when_diagnostic_logging_fails(
    failure: type[Exception], forced_exits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _GatedClose(resist_cancel=True)
    kube = _Kube()

    def broken_log(*args: object, **kwargs: object) -> None:
        raise failure("diagnostic unavailable")

    monkeypatch.setattr(main_mod.logger, "critical", broken_log)
    try:
        with pytest.raises(failure, match="diagnostic unavailable"):
            await main_mod._shutdown(None, cast("Any", provider), cast("Any", kube))
        assert provider.cancelled.is_set()
        assert kube.closed.is_set()
        assert forced_exits == [1]
    finally:
        provider.release.set()
        await asyncio.wait_for(provider.finished.wait(), timeout=2)


@pytest.mark.parametrize("component", ["provider", "mcp"])
async def test_noncooperative_cleanup_exits_only_after_other_clients_close(
    component: str,
    forced_exits: list[int],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stuck = _GatedClose(resist_cancel=True)
    kube = _Kube()
    observability = _GatedClose()
    observability.release.set()
    state = main_mod._RunState(observability=main_mod.ObservabilityWiring(metrics=observability))
    server: asyncio.Task[None] | None = None
    if component == "provider":
        state.provider_box[0] = cast("Any", stuck)
    else:
        server = asyncio.create_task(stuck.aclose())
        await asyncio.wait_for(stuck.started.wait(), timeout=2)
        state.mcp = cast("Any", _MCP(server))

    def terminal_exit(status: int) -> None:
        assert kube.closed.is_set()
        assert observability.finished.is_set()
        forced_exits.append(status)

    monkeypatch.setattr(os, "_exit", terminal_exit)
    cleanup = asyncio.create_task(main_mod._teardown(state, cast("Any", kube)))
    try:
        await _join(cleanup)
        assert stuck.cancelled.is_set()
        assert forced_exits == [1]
        assert state.close_tasks
        assert "exiting without restart" in caplog.text
    finally:
        stuck.release.set()
        await _join(cleanup)
        if server is not None:
            await _join(server)
        if hasattr(state, "close_tasks") and state.close_tasks:
            await asyncio.wait_for(asyncio.gather(*state.close_tasks), timeout=2)


@pytest.mark.parametrize("operation", ["rebuild", "disconnect", "failed_rebuild"])
async def test_replacement_cleanup_is_owned_and_drained_by_run_state(
    operation: str, forced_exits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    old_provider = _GatedClose()
    new_provider = _GatedClose()
    old_session = _GatedClose()
    new_session = _GatedClose()
    old_session.release.set()
    new_session.release.set()
    if operation != "failed_rebuild":
        new_provider.release.set()
    else:
        old_provider.release.set()
    sessions = iter([old_session, new_session])
    monkeypatch.setattr(main_mod, "_missing_extra_packages", lambda roots: [])
    monkeypatch.setattr(main_mod, "_create_initial_provider", lambda *args: old_provider)
    monkeypatch.setattr(
        main_mod, "_create_provider_from_active_profile", lambda *args: new_provider
    )
    monkeypatch.setattr(main_mod, "_resolve_agent_policy", lambda *args: None)
    monkeypatch.setattr(main_mod, "_build_session", lambda *args, **kwargs: next(sessions))
    state = main_mod._RunState()
    wiring = main_mod._build_agent_wiring(
        KorvidConfig(),
        cast("Any", object()),
        {},
        provider_box=state.provider_box,
        session_box=state.session_box,
        close_tasks=state.close_tasks,
    )

    def failed_session(*args: object, **kwargs: object) -> None:
        raise ValueError("session construction failed")

    if operation == "disconnect":
        wiring.disconnect()
    else:
        assert wiring.rebuild is not None
        profile = ModelConnectionConfig(model="openai/test")
        if operation == "failed_rebuild":
            monkeypatch.setattr(main_mod, "_build_session", failed_session)
            with pytest.raises(ValueError, match="session construction failed"):
                wiring.rebuild(profile, None)
        else:
            wiring.rebuild(profile, None)

    assert len(state.close_tasks) == 1
    retired = new_provider if operation == "failed_rebuild" else old_provider
    await asyncio.wait_for(retired.started.wait(), timeout=2)
    kube = _Kube()
    cleanup = asyncio.create_task(main_mod._teardown(state, cast("Any", kube)))
    try:
        await _join(cleanup)
        assert retired.cancelled.is_set()
        assert old_session.finished.is_set()
        assert kube.closed.is_set()
        assert state.close_tasks == set()
        assert forced_exits == []
    finally:
        old_provider.release.set()
        new_provider.release.set()
        await _join(cleanup)


async def test_cancelled_replacement_session_still_releases_its_provider(
    forced_exits: list[int],
) -> None:
    session = _GatedClose()
    provider = _GatedClose()
    provider.release.set()
    state = main_mod._RunState()
    main_mod._close_agent_in_background(
        cast("Any", session), cast("Any", provider), state.close_tasks
    )
    await asyncio.wait_for(session.started.wait(), timeout=2)
    cleanup = asyncio.create_task(main_mod._teardown(state, cast("Any", _Kube())))
    try:
        await _join(cleanup)
        assert session.cancelled.is_set()
        assert provider.finished.is_set()
        assert state.close_tasks == set()
        assert forced_exits == []
    finally:
        session.release.set()
        await _join(cleanup)


@pytest.mark.parametrize(
    "component", ["session", "provider", "mcp", "discovery", "kube", "observability"]
)
async def test_cleanup_failures_are_logged_without_payloads_and_do_not_skip_clients(
    component: str, forced_exits: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    session = _GatedClose(fail=component == "session")
    provider = _GatedClose(fail=component == "provider")
    observability = _GatedClose(fail=component == "observability")
    for closer in (session, provider, observability):
        closer.release.set()
    kube = _Kube(fail=component == "kube")
    state = main_mod._RunState(
        session_box=[cast("Any", session)],
        provider_box=[cast("Any", provider)],
        mcp=cast("Any", _MCP(fail=component == "mcp")),
        observability=main_mod.ObservabilityWiring(metrics=observability),
    )
    if component == "discovery":
        discovery = _GatedClose(fail=True)
        discovery.release.set()
        state.discovery_box.append(asyncio.create_task(discovery.aclose()))
        await asyncio.wait_for(discovery.finished.wait(), timeout=2)
    with caplog.at_level("DEBUG", logger=main_mod.__name__):
        await main_mod._teardown(state, cast("Any", kube))
    assert session.finished.is_set()
    assert provider.finished.is_set()
    assert kube.closed.is_set()
    assert observability.finished.is_set()
    assert state.close_tasks == set()
    assert forced_exits == []
    assert any("failed" in record.message for record in caplog.records)
    assert "SECRET_CLEANUP_PAYLOAD" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_leftover_mcp_exception_is_consumed_and_logged(
    forced_exits: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    server = _GatedClose(fail=True)
    server.release.set()
    task = asyncio.create_task(server.aclose())
    await asyncio.wait_for(server.finished.wait(), timeout=2)
    state = main_mod._RunState(mcp=cast("Any", _MCP(task)))
    with caplog.at_level("DEBUG", logger=main_mod.__name__):
        await main_mod._teardown(state, cast("Any", _Kube()))
    assert "MCP server task failed" in caplog.text
    assert "SECRET_CLEANUP_PAYLOAD" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert forced_exits == []


async def test_teardown_cancellation_still_releases_clients(
    forced_exits: list[int],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(main_mod, "_CLEANUP_GRACE_SECONDS", 60.0, raising=False)
    provider = _GatedClose()
    kube = _Kube()
    observability = _GatedClose()
    observability.release.set()
    state = main_mod._RunState(
        provider_box=[cast("Any", provider)],
        observability=main_mod.ObservabilityWiring(metrics=observability),
    )
    cleanup = asyncio.create_task(main_mod._teardown(state, cast("Any", kube)))
    await asyncio.wait_for(provider.started.wait(), timeout=2)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await _join(cleanup)
    assert provider.cancelled.is_set()
    assert kube.closed.is_set()
    assert observability.finished.is_set()
    assert forced_exits == []
    assert "deadline expired" not in caplog.text


async def test_wire_and_run_hands_agent_wiring_the_run_owned_task_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def build_agent(*args: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise RuntimeError("agent wiring reached")

    monkeypatch.setattr(main_mod, "_build_agent_wiring", build_agent)
    monkeypatch.setattr(main_mod, "_probe_pod_resize", AsyncMock(return_value=False))
    monkeypatch.setattr(
        main_mod,
        "_probe_cloud_provider",
        AsyncMock(return_value=ProviderInfo(provider="unknown", distribution=None)),
    )
    state = main_mod._RunState()
    with pytest.raises(RuntimeError, match="agent wiring reached"):
        await main_mod._wire_and_run(KorvidConfig(), cast("Any", object()), state)
    assert captured["close_tasks"] is state.close_tasks


@pytest.mark.parametrize("component", ["mcp", "session", "provider"])
def test_noncooperative_cleanup_does_not_hang_asyncio_run_final_gather(component: str) -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import sys
        from types import SimpleNamespace
        import korvid.__main__ as main_mod
        from korvid.core.config import KorvidConfig

        component = sys.argv[1]
        main_mod._CLEANUP_GRACE_SECONDS = 0.0
        main_mod._CLEANUP_CANCEL_SECONDS = 0.0
        main_mod._MCP_SHUTDOWN_GRACE_SECONDS = 0.0

        class Kube:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def connect(self, context: str | None) -> None:
                return None

            async def close(self) -> None:
                print("kube closed", flush=True)

        async def stubborn(started: asyncio.Event) -> None:
            started.set()
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue

        async def wire(config: object, kube: object, state: main_mod._RunState) -> None:
            started = asyncio.Event()
            if component == "mcp":
                from korvid.mcp.server import MCPController

                async def wait_started() -> int:
                    await started.wait()
                    return 12345

                server = SimpleNamespace(
                    run=lambda: stubborn(started),
                    wait_started=wait_started,
                    request_shutdown=lambda: None,
                )
                state.mcp = MCPController(lambda: server)
                await state.mcp.start()
            else:
                closing: asyncio.Task[None] | None = None

                async def close() -> None:
                    nonlocal closing
                    if closing is None:
                        closing = asyncio.create_task(stubborn(started))
                    await asyncio.shield(closing)

                box = state.session_box if component == "session" else state.provider_box
                box[0] = SimpleNamespace(aclose=close)

        main_mod.KubeClient = Kube
        main_mod._load_startup_config = lambda *args: KorvidConfig()
        main_mod._wire_and_run = wire

        sys.argv = ["korvid", "--no-restart"]
        main_mod.main()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, component],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert result.stdout == "kube closed\n"
    assert "exiting without restart" in result.stderr


async def test_final_sweep_does_not_cancel_tasks_from_before_the_run(
    forced_exits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    unrelated = _GatedClose()
    unrelated_task = asyncio.create_task(unrelated.aclose())
    kube = _Kube()
    fake_kube = AsyncMock(wraps=kube)
    fake_kube.connect = AsyncMock()
    monkeypatch.setattr(main_mod, "KubeClient", lambda **kwargs: fake_kube)
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda *args: KorvidConfig())
    monkeypatch.setattr(main_mod, "_wire_and_run", AsyncMock())
    try:
        await main_mod._run()
        assert kube.closed.is_set()
        assert not unrelated_task.done()
        assert not unrelated.cancelled.is_set()
        assert forced_exits == []
    finally:
        unrelated.release.set()
        await _join(unrelated_task)


@pytest.mark.parametrize("finish_on_close", [False, True])
async def test_runtime_tasks_are_not_cancelled_before_kubernetes_closes(
    finish_on_close: bool, forced_exits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    kube = _Kube()
    started = asyncio.Event()
    release = kube.closed if finish_on_close else asyncio.Event()
    closed_when_finished: list[bool] = []
    runtime_tasks: list[asyncio.Task[None]] = []

    async def runtime() -> None:
        started.set()
        try:
            await release.wait()
        finally:
            closed_when_finished.append(kube.closed.is_set())

    async def wire(config: object, client: object, state: main_mod._RunState) -> None:
        runtime_tasks.append(asyncio.create_task(runtime()))
        await asyncio.wait_for(started.wait(), timeout=2)

    fake_kube = SimpleNamespace(connect=AsyncMock(), close=kube.close)
    monkeypatch.setattr(main_mod, "KubeClient", lambda **kwargs: fake_kube)
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda *args: KorvidConfig())
    monkeypatch.setattr(main_mod, "_wire_and_run", wire)
    try:
        await _join(asyncio.create_task(main_mod._run()))
        assert closed_when_finished == [True]
        assert runtime_tasks[0].cancelled() is not finish_on_close
        assert forced_exits == []
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*runtime_tasks, return_exceptions=True), timeout=2)


@pytest.mark.parametrize("resist_cancel", [False, True])
async def test_final_sweep_cancels_new_descendants_before_terminal_decision(
    resist_cancel: bool, forced_exits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    kube = _Kube()
    child = _GatedClose(resist_cancel=resist_cancel)
    parent_started = asyncio.Event()
    release_parent = asyncio.Event()
    runtime_tasks: list[asyncio.Task[None]] = []

    async def parent() -> None:
        parent_started.set()
        try:
            await release_parent.wait()
        finally:
            runtime_tasks.append(asyncio.create_task(child.aclose()))

    async def wire(config: object, client: object, state: main_mod._RunState) -> None:
        runtime_tasks.append(asyncio.create_task(parent()))
        await asyncio.wait_for(parent_started.wait(), timeout=2)

    fake_kube = SimpleNamespace(connect=AsyncMock(), close=kube.close)
    monkeypatch.setattr(main_mod, "KubeClient", lambda **kwargs: fake_kube)
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda *args: KorvidConfig())
    monkeypatch.setattr(main_mod, "_wire_and_run", wire)
    try:
        await _join(asyncio.create_task(main_mod._run()))
        assert kube.closed.is_set()
        assert child.cancelled.is_set()
        assert child.finished.is_set() is not resist_cancel
        assert forced_exits == ([1] if resist_cancel else [])
    finally:
        release_parent.set()
        child.release.set()
        await asyncio.wait_for(asyncio.gather(*runtime_tasks, return_exceptions=True), timeout=2)


async def test_final_sweep_bounds_repeated_descendant_respawns(
    forced_exits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    kube = _Kube()
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled_generations: list[int] = []
    runtime_tasks: list[asyncio.Task[None]] = []

    async def descendant(generation: int) -> None:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled_generations.append(generation)
            if generation < 4:
                runtime_tasks.append(asyncio.create_task(descendant(generation + 1)))
            raise

    async def wire(config: object, client: object, state: main_mod._RunState) -> None:
        runtime_tasks.append(asyncio.create_task(descendant(0)))
        await asyncio.wait_for(started.wait(), timeout=2)

    fake_kube = SimpleNamespace(connect=AsyncMock(), close=kube.close)
    monkeypatch.setattr(main_mod, "KubeClient", lambda **kwargs: fake_kube)
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda *args: KorvidConfig())
    monkeypatch.setattr(main_mod, "_wire_and_run", wire)
    try:
        await _join(asyncio.create_task(main_mod._run()))
        assert kube.closed.is_set()
        assert cancelled_generations == [0, 1]
        assert len(runtime_tasks) == 3
        assert forced_exits == [1]
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(*runtime_tasks, return_exceptions=True), timeout=2)


async def test_shielded_child_failure_during_kube_cleanup_is_consumed(
    forced_exits: list[int],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(main_mod, "_CLEANUP_GRACE_SECONDS", 60.0)
    release = asyncio.Event()
    finished = asyncio.Event()
    callbacks_done = asyncio.Event()
    unhandled: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    async def child() -> None:
        try:
            await release.wait()
            raise RuntimeError("SECRET_CLEANUP_PAYLOAD")
        finally:
            finished.set()

    async def close_provider() -> None:
        protected = asyncio.shield(asyncio.create_task(child()))
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await protected

    async def close_kube() -> None:
        release.set()
        await finished.wait()
        loop.call_soon(callbacks_done.set)
        await callbacks_done.wait()

    async def wire(config: object, kube: object, state: main_mod._RunState) -> None:
        state.provider_box[0] = cast("Any", SimpleNamespace(aclose=close_provider))

    kube = SimpleNamespace(connect=AsyncMock(), close=close_kube)
    monkeypatch.setattr(main_mod, "KubeClient", lambda **kwargs: kube)
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda *args: KorvidConfig())
    monkeypatch.setattr(main_mod, "_wire_and_run", wire)
    loop.set_exception_handler(lambda loop, context: unhandled.append(context))
    try:
        await main_mod._run()
        gc.collect()
        assert callbacks_done.is_set()
        assert unhandled == []
        assert "run background task failed" in caplog.text
        assert "SECRET_CLEANUP_PAYLOAD" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
        assert forced_exits == []
    finally:
        release.set()
        loop.set_exception_handler(previous_handler)


async def test_run_owns_children_and_restores_the_delegated_task_factory(
    forced_exits: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    previous_factory = loop.get_task_factory()
    previous_handler = loop.get_exception_handler()
    marker = contextvars.ContextVar("cleanup_factory_context", default="ambient")
    context = contextvars.copy_context()
    context.run(marker.set, "explicit")
    delegated: list[asyncio.Future[Any]] = []

    def factory(
        owner_loop: asyncio.AbstractEventLoop, coroutine: Any, **kwargs: Any
    ) -> asyncio.Future[Any]:
        task = asyncio.Task(coroutine, loop=owner_loop, **kwargs)
        delegated.append(task)
        return task

    async def child() -> str:
        return marker.get()

    async def wire(config: object, kube: object, state: main_mod._RunState) -> None:
        task = asyncio.create_task(child(), name="context probe", context=context)
        assert task not in state.close_tasks
        assert task in delegated
        assert task.get_name() == "context probe"
        assert await task == "explicit"
        raise RuntimeError("wiring failed")

    kube = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    monkeypatch.setattr(main_mod, "KubeClient", lambda **kwargs: kube)
    monkeypatch.setattr(main_mod, "_load_startup_config", lambda *args: KorvidConfig())
    monkeypatch.setattr(main_mod, "_wire_and_run", wire)
    loop.set_task_factory(factory)
    try:
        with pytest.raises(RuntimeError, match="wiring failed"):
            await main_mod._run()
        assert loop.get_task_factory() is factory
        assert loop.get_exception_handler() is previous_handler
        assert marker.get() == "ambient"
        assert kube.close.await_count == 1
        assert forced_exits == []
    finally:
        loop.set_task_factory(previous_factory)


@pytest.mark.parametrize(
    "scenario", ["shielded_failure", "executor", "asyncgen", "asyncgen_failure"]
)
def test_main_bounds_actual_runner_finalization_without_secret_disclosure(scenario: str) -> None:
    script = textwrap.dedent(
        """
        import asyncio
        import sys
        import threading
        from collections.abc import AsyncIterator
        from types import SimpleNamespace
        import korvid.__main__ as main_mod
        from korvid.core.config import KorvidConfig

        scenario = sys.argv[1]
        main_mod._CLEANUP_GRACE_SECONDS = 60.0 if scenario == "shielded_failure" else 0.0
        main_mod._CLEANUP_CANCEL_SECONDS = 0.0
        main_mod._RUNNER_SHUTDOWN_SECONDS = 0.2
        release = asyncio.Event()
        child_finished = asyncio.Event()
        generators: list[AsyncIterator[None]] = []

        class Kube:
            def __init__(self, **kwargs: object) -> None:
                pass

            async def connect(self, context: str | None) -> None:
                return None

            async def close(self) -> None:
                if scenario == "shielded_failure":
                    release.set()
                    await child_finished.wait()
                    callbacks_done = asyncio.Event()
                    asyncio.get_running_loop().call_soon(callbacks_done.set)
                    await callbacks_done.wait()
                print("kube closed", flush=True)

        async def child() -> None:
            try:
                await release.wait()
                raise RuntimeError("SECRET_CLEANUP_PAYLOAD")
            finally:
                child_finished.set()

        async def close_provider() -> None:
            protected = asyncio.shield(asyncio.create_task(child()))
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
            await protected

        async def finalize_generator() -> AsyncIterator[None]:
            try:
                yield None
            finally:
                print("asyncgen finalizing", flush=True)
                if scenario == "asyncgen_failure":
                    raise RuntimeError("SECRET_CLEANUP_PAYLOAD")
                await asyncio.Event().wait()

        async def wire(config: object, kube: object, state: main_mod._RunState) -> None:
            if scenario == "shielded_failure":
                state.provider_box[0] = SimpleNamespace(aclose=close_provider)
            elif scenario == "executor":
                loop = asyncio.get_running_loop()
                started = asyncio.Event()

                def blocking_work() -> None:
                    loop.call_soon_threadsafe(started.set)
                    threading.Event().wait()

                async def close_in_executor() -> None:
                    await asyncio.to_thread(blocking_work)

                provider = SimpleNamespace(aclose=close_in_executor)
                main_mod._close_provider_in_background(provider, state.close_tasks)
                await started.wait()
                print("executor started", flush=True)
            else:
                generator = finalize_generator()
                generators.append(generator)
                await anext(generator)

        main_mod.KubeClient = Kube
        main_mod._load_startup_config = lambda *args: KorvidConfig()
        main_mod._wire_and_run = wire
        sys.argv = ["korvid", "--no-restart"]
        main_mod.main()
        """
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, scenario],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"Runner did not exit after output: {error.stdout!r}", pytrace=False)
    terminal = scenario in {"executor", "asyncgen"}
    assert result.returncode == (1 if terminal else 0)
    assert "kube closed\n" in result.stdout
    if scenario == "executor":
        assert result.stdout == "executor started\nkube closed\n"
    if scenario.startswith("asyncgen"):
        assert result.stdout == "kube closed\nasyncgen finalizing\n"
    if not terminal:
        assert "failed" in result.stderr
    assert "SECRET_CLEANUP_PAYLOAD" not in result.stderr
    assert "Task exception was never retrieved" not in result.stderr
