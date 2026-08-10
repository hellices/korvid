"""Runtime context switching via `:ctx` (issue #36).

`:ctx` lists kubeconfig contexts, `:ctx <name>` switches the whole session:
the target is auth-probed first (a failure leaves the old context fully
usable), then watches/logs/forwards/store/drill state are torn down and the
session restarts on the new cluster with capabilities re-probed.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import PodSummary
from korvid.ui.app import ContextSwitchResult, KorvidApp
from korvid.ui.messages import ShowContextPicker, SwitchContextCommand
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.status_bar import StatusBar

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_ALIASES = {"pods": _PODS_META, "po": _PODS_META}


def _pod(name: str, ns: str = "default") -> PodSummary:
    return PodSummary(name=name, namespace=ns, phase="Running", ready="1/1", restarts=0, node=None)


class _CtxEnv:
    """App plus recording fakes for the context-switch collaborators."""

    def __init__(
        self,
        *,
        contexts: tuple[str, ...] = ("ctx-a", "ctx-b"),
        probe_error: Exception | None = None,
        switch_error: Exception | None = None,
        result: ContextSwitchResult | None = None,
        audit_path: Path | None = None,
        stream_logs: Any = None,
        probe_gate: asyncio.Event | None = None,
        metrics: Any = None,
    ) -> None:
        self.probe_calls: list[str] = []
        self.switch_calls: list[str | None] = []
        self.probe_error = probe_error
        self.switch_error = switch_error
        self.result = result or ContextSwitchResult(
            pod_resize_supported=True,
            provider_hint="AKS",
            context_namespace="ns-b",
        )
        #: Which cluster the watch source serves; the switch fake flips it.
        self.cluster = "a"
        store = ResourceStore()

        async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
            if kind == "pods":
                yield ("ADDED", _pod(f"pod-{self.cluster}", ns=scope))
            while True:
                await asyncio.sleep(0.01)

        def list_contexts() -> tuple[list[str], str | None]:
            return list(contexts), "ctx-a"

        async def probe(name: str) -> None:
            self.probe_calls.append(name)
            if probe_gate is not None:
                await probe_gate.wait()
            if self.probe_error is not None:
                raise self.probe_error

        async def switch(name: str | None) -> ContextSwitchResult:
            self.switch_calls.append(name)
            if self.switch_error is not None and len(self.switch_calls) == 1:
                raise self.switch_error
            self.cluster = "b" if name == "ctx-b" else "a"
            return self.result

        self.audit = AuditLog(audit_path, context="ctx-a") if audit_path else None
        self.app = KorvidApp(
            config=KorvidConfig(namespace="default", kube_context="ctx-a"),
            store=store,
            watch_manager=WatchManager(store, source),
            aliases=dict(_ALIASES),
            audit=self.audit,
            list_contexts=list_contexts,
            probe_context=probe,
            switch_context=switch,
            stream_logs=stream_logs,
            metrics=metrics,
        )


async def _first_pod_visible(env: _CtxEnv, pilot: Any, name: str) -> None:
    def cond() -> bool:
        table = env.app.query_one(ResourceTable)
        return table.row_count > 0 and name in str(table.ordered_rows[0].key.value)

    await until(pilot, cond, label=f"pod {name} visible")


async def test_switch_success_retargets_session() -> None:
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: app.config.kube_context == "ctx-b",
            label="config context updated",
        )
        assert env.probe_calls == ["ctx-b"]
        assert env.switch_calls == ["ctx-b"]
        # Capability gates re-evaluated from the switch result.
        assert app._pod_resize_supported is True
        assert app._provider_hint == "AKS"
        # Scope follows the new context's kubeconfig namespace.
        assert app.current_scope == "ns-b"
        # The watch restarted against the new cluster.
        await _first_pod_visible(env, pilot, "pod-b")
        status = app.query_one(StatusBar)
        assert "ctx-b" in str(status.render())
        await until(
            pilot,
            lambda: any("ctx-b" in n.message for n in app._notifications),
            label="switch notification",
        )


async def test_switch_clears_old_cluster_state() -> None:
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.filter_pattern = "old-filter"
        app._hints.cache["default/pod-a"] = (0.0, None, None)
        from korvid.ui.widgets.command_bar import CommandBar

        app.query_one(CommandBar).namespace_words = ["team-old"]
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        await _first_pod_visible(env, pilot, "pod-b")
        rows = app.store.get("pods", "ns-b")
        assert [p.name for p in rows] == ["pod-b"]
        assert app.store.get("pods", "default") == []  # old bucket purged
        assert app.filter_pattern == ""
        assert app._hints.cache == {}
        assert app._drill.breadcrumb() == ""
        # Old-cluster namespace completions are purged even though no new
        # prefetch is wired here — stale names must not linger on failure.
        assert app.query_one(CommandBar).namespace_words == []


async def test_switch_reattributes_audit_entries(tmp_path: Path) -> None:
    import json

    env = _CtxEnv(audit_path=tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
    assert env.audit is not None
    env.audit.append(action="delete", kind="pods", namespace="ns-b", name="x")
    entry = json.loads((tmp_path / "audit.jsonl").read_text().splitlines()[-1])
    assert entry["context"] == "ctx-b"


async def test_probe_failure_keeps_old_context_untouched() -> None:
    env = _CtxEnv(probe_error=RuntimeError("Unauthorized"))
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: any("Unauthorized" in n.message for n in app._notifications),
            label="probe failure notification",
        )
        assert env.switch_calls == []  # never got past the probe
        assert app.config.kube_context == "ctx-a"
        assert app.current_scope == "default"
        # The old cluster's rows are still on screen.
        table = app.query_one(ResourceTable)
        assert table.row_count == 1


async def test_same_context_is_a_noop() -> None:
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        app.post_message(SwitchContextCommand("ctx-a"))
        await until(
            pilot,
            lambda: any("Already on" in n.message for n in app._notifications),
            label="no-op notification",
        )
        assert env.probe_calls == []
        assert env.switch_calls == []


async def test_switch_refused_while_agent_turn_live() -> None:
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        gate: asyncio.Event = asyncio.Event()

        async def _busy() -> None:
            await gate.wait()

        app._agent_task = asyncio.create_task(_busy())
        try:
            app.post_message(SwitchContextCommand("ctx-b"))
            await until(
                pilot,
                lambda: any("Agent is busy" in n.message for n in app._notifications),
                label="agent-busy refusal",
            )
            assert env.probe_calls == []
        finally:
            gate.set()
            await app._agent_task


async def test_switch_refused_while_dialog_open() -> None:
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        app.push_screen(PickScreen("pick:", ["x"]))
        await until(
            pilot,
            lambda: isinstance(app.screen, PickScreen),
            label="pick screen open",
        )
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: any("dialog" in n.message.lower() for n in app._notifications),
            label="dialog-open refusal",
        )
        assert env.probe_calls == []


async def test_unknown_context_rejected_before_probe() -> None:
    env = _CtxEnv(contexts=("ctx-a", "ctx-b"))
    app = env.app
    async with app.run_test() as pilot:
        app.post_message(SwitchContextCommand("ctx-nope"))
        await until(
            pilot,
            lambda: any("ctx-nope" in n.message for n in app._notifications),
            label="unknown context notification",
        )
        assert env.probe_calls == []
        assert app.config.kube_context == "ctx-a"


async def test_bare_ctx_opens_picker_with_current_marked() -> None:
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        app.post_message(ShowContextPicker())
        await until(
            pilot,
            lambda: isinstance(app.screen, PickScreen),
            label="context picker open",
        )
        picker = app.screen
        assert isinstance(picker, PickScreen)
        prompts = list(picker._options)
        assert any("ctx-a" in p and "current" in p for p in prompts)
        assert "ctx-b" in prompts


async def test_agent_screen_context_carries_switch_note() -> None:
    env = _CtxEnv()
    app = env.app

    seen_context: list[str] = []

    class _FakeRuntime:
        total_tokens = (0, 0)
        usage_estimated = False

        async def run_turn(self, text: str, screen_context: str) -> AsyncIterator[Any]:
            seen_context.append(screen_context)
            return
            yield  # pragma: no cover - makes this an async generator

    app._agent_runtime = _FakeRuntime()  # type: ignore[assignment]  # fake
    async with app.run_test() as pilot:
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        await app._run_agent_turn("hello")
        assert "context=ctx-b" in seen_context[0]
        assert "switched" in seen_context[0]
        # The note is one-shot: the next turn goes back to plain context.
        await app._run_agent_turn("again")
        assert "switched" not in seen_context[1]


async def test_switch_failure_after_probe_restores_old_context() -> None:
    env = _CtxEnv(switch_error=RuntimeError("kubeconfig vanished"))
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: any("kubeconfig vanished" in n.message for n in app._notifications),
            label="mid-swap failure notification",
        )
        # Recovery re-ran the switch against the old context.
        assert env.switch_calls == ["ctx-b", "ctx-a"]
        await until(
            pilot,
            lambda: app.config.kube_context == "ctx-a",
            label="old context restored",
        )


async def test_picker_maps_display_labels_to_raw_names() -> None:
    """A context literally named "ctx-a (current)" must survive selection
    intact: the current-marker is a display label, never an encoding to
    decode, so its collision with a real name drops the marker instead."""
    env = _CtxEnv(contexts=("ctx-a", "ctx-a (current)"))
    app = env.app
    async with app.run_test() as pilot:
        app.post_message(ShowContextPicker())
        await until(
            pilot,
            lambda: isinstance(app.screen, PickScreen),
            label="context picker open",
        )
        picker = app.screen
        assert isinstance(picker, PickScreen)
        assert list(picker._options) == ["ctx-a", "ctx-a (current)"]
        picker.dismiss("ctx-a (current)")
        await until(
            pilot,
            lambda: env.probe_calls == ["ctx-a (current)"],
            label="raw context name probed",
        )


async def test_switch_refused_while_cluster_write_in_flight() -> None:
    """An approved write worker (e.g. drain) holds the cluster: switching
    mid-flight could land the tail of the write on the wrong cluster."""
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        app._active_cluster_writes = 1
        try:
            app.post_message(SwitchContextCommand("ctx-b"))
            await until(
                pilot,
                lambda: any("write is in progress" in n.message for n in app._notifications),
                label="write-in-progress refusal",
            )
            assert env.probe_calls == []
        finally:
            app._active_cluster_writes = 0


async def test_keybinding_write_refused_while_switching() -> None:
    """The mirror guard: once a switch claims the session, new keybinding
    writes are refused instead of racing the teardown/retarget."""
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        app._ctx_switching = True
        try:
            ok = await app._precheck_keybinding_write("delete", _PODS_META, "default", "pod-a")
            assert ok is False
            await until(
                pilot,
                lambda: any(
                    "context switch is in progress" in n.message for n in app._notifications
                ),
                label="switch-in-progress refusal",
            )
        finally:
            app._ctx_switching = False


async def test_write_slot_reserved_before_worker_starts() -> None:
    """The mutation slot is claimed when the coroutine is constructed (at
    approval time), not when the worker starts — a `:ctx` queued in between
    must already see the write as in flight."""
    from korvid.ui.app import _tracks_cluster_write

    env = _CtxEnv()
    app = env.app
    started = asyncio.Event()

    @_tracks_cluster_write
    async def fake_write(self: KorvidApp) -> None:
        await started.wait()

    async with app.run_test():
        coro = fake_write(app)
        assert app._active_cluster_writes == 1  # reserved synchronously
        task = asyncio.create_task(coro)
        started.set()
        await task
        assert app._active_cluster_writes == 0


async def test_agent_prompt_refused_while_switching() -> None:
    """A prompt submitted mid-switch would run during teardown/retarget with
    the old cluster's screen context — refuse it up front."""
    from korvid.ui.messages import AgentPromptSubmitted

    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        app._ctx_switching = True
        try:
            app.post_message(AgentPromptSubmitted("why is pod-a failing?"))
            await until(
                pilot,
                lambda: any(
                    "context switch is in progress" in n.message for n in app._notifications
                ),
                label="agent-prompt refusal",
            )
            assert app._agent_task is None
        finally:
            app._ctx_switching = False


async def test_picker_marks_kubeconfig_default_when_no_explicit_context() -> None:
    """Sessions started without -c/--context still know their active context:
    the picker falls back to what the kubeconfig reports (issue #36 review)."""
    import dataclasses

    env = _CtxEnv()
    app = env.app
    app.config = dataclasses.replace(app.config, kube_context=None)
    async with app.run_test() as pilot:
        app.post_message(ShowContextPicker())
        await until(
            pilot,
            lambda: isinstance(app.screen, PickScreen),
            label="context picker open",
        )
        picker = app.screen
        assert isinstance(picker, PickScreen)
        prompts = list(picker._options)
        assert any("ctx-a" in p and "current" in p for p in prompts)


class _FakeMCP:
    """Recording stand-in for the injected MCPController."""

    def __init__(self, *, drainable: bool = True) -> None:
        self.running = True
        self.drainable = drainable
        self.events: list[str] = []

    async def shutdown(self) -> Any:
        if not self.drainable:
            return object()  # still-pending server task
        self.running = False
        self.events.append("mcp-stopped")
        return None

    async def start(self) -> str:
        self.running = True
        self.events.append("mcp-started")
        return "MCP on :4321"

    def status(self) -> str:
        return "MCP :4321" if self.running else ""


async def test_mcp_quiesced_before_teardown_and_restarted_after_switch() -> None:
    """External MCP callers share the client and alias map being swapped:
    the server drains BEFORE anything is torn down, resumes once the new
    cluster is live, and the restart outcome is surfaced to the operator."""
    env = _CtxEnv()
    app = env.app
    mcp = _FakeMCP()
    app._mcp = cast("Any", mcp)
    teardowns: list[str] = []
    real_teardown = app._teardown_for_context_switch

    async def spying_teardown() -> None:
        teardowns.append(f"teardown(mcp-running={mcp.running})")
        await real_teardown()

    app._teardown_for_context_switch = spying_teardown  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        assert teardowns == ["teardown(mcp-running=False)"]  # quiesced first
        assert mcp.events == ["mcp-stopped", "mcp-started"]
        assert mcp.running is True
        await until(
            pilot,
            lambda: any("MCP on :4321" in n.message for n in app._notifications),
            label="mcp restart notification",
        )


async def test_switch_aborts_before_teardown_when_mcp_wont_drain() -> None:
    """If even cancellation can't stop the MCP server, an in-flight tool call
    could cross the context boundary — the switch aborts with the old
    context fully untouched (watches, store, connection all intact)."""
    env = _CtxEnv()
    app = env.app
    mcp = _FakeMCP(drainable=False)
    app._mcp = cast("Any", mcp)
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: any("did not stop in time" in n.message for n in app._notifications),
            label="abort notice",
        )
        assert env.switch_calls == []  # nothing was swapped or torn down
        assert app.config.kube_context == "ctx-a"
        await _first_pod_visible(env, pilot, "pod-a")  # old watch still live


async def test_mcp_restart_survives_failed_target_swap() -> None:
    """A failed target swap recovers back to the old context — the stopped
    MCP server must still be restarted (now serving the restored cluster)."""
    env = _CtxEnv(switch_error=RuntimeError("target cluster unreachable"))
    app = env.app
    mcp = _FakeMCP()
    app._mcp = cast("Any", mcp)
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: len(env.switch_calls) == 2, label="recovery swap ran")
        await until(pilot, lambda: "mcp-started" in mcp.events, label="mcp restarted")
        assert mcp.events == ["mcp-stopped", "mcp-started"]
        assert mcp.running is True


async def test_keybinding_write_aborted_when_context_changed_during_precheck() -> None:
    """The RBAC pre-check awaits network I/O — if a switch completes during
    that await the prepared intent must not land on the new cluster."""
    env = _CtxEnv()
    app = env.app

    async def permission_check_during_switch(*args: object) -> bool:
        app._ctx_epoch += 1  # a switch was applied while we awaited
        return True

    async with app.run_test() as pilot:
        app._check_permission = permission_check_during_switch
        ok = await app._precheck_keybinding_write("delete", _PODS_META, "default", "pod-a")
        assert ok is False
        await until(
            pilot,
            lambda: any("kube context changed" in n.message for n in app._notifications),
            label="epoch-change refusal",
        )


async def test_write_slot_released_when_coroutine_never_runs() -> None:
    """A reserved write whose coroutine is closed without ever running
    (worker cancelled before start, app shutdown) must not leak the counter
    and block every future `:ctx` switch."""
    import gc

    from korvid.ui.app import _tracks_cluster_write

    env = _CtxEnv()
    app = env.app

    @_tracks_cluster_write
    async def fake_write(self: KorvidApp) -> None:  # pragma: no cover - never runs
        raise AssertionError("must not start")

    async with app.run_test():
        coro = fake_write(app)
        assert app._active_cluster_writes == 1
        coro.close()  # unstarted coroutine: finally inside never executes
        del coro
        gc.collect()
        assert app._active_cluster_writes == 0


async def test_total_switch_failure_mentions_stopped_mcp() -> None:
    """When even the recovery swap fails, the operator learns the embedded
    MCP server was stopped for the switch instead of it dying silently."""
    env = _CtxEnv()
    app = env.app
    mcp = _FakeMCP()
    app._mcp = cast("Any", mcp)

    async def always_failing_switch(name: str | None) -> ContextSwitchResult:
        raise RuntimeError("boom")

    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app._switch_context = always_failing_switch
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: any("MCP server was stopped" in n.message for n in app._notifications),
            label="stopped-MCP notice",
        )
        assert mcp.running is False  # not restarted into a dead session


async def test_delete_aborted_when_context_switches_during_preview(tmp_path: Path) -> None:
    """A context switch that fully completes during the dry-run preview await
    (flag off, epoch bumped) must cancel the write: a same-named row on the
    new cluster would otherwise satisfy the selection-only checks."""
    env = _CtxEnv(audit_path=tmp_path / "audit.log")
    app = env.app

    class _EpochBumpOps:
        def __init__(self) -> None:
            self.deletes: list[str] = []

        async def preview_delete(
            self, meta: Any, ns: Any, name: Any, *, uid: str | None = None
        ) -> list[str]:
            app._ctx_epoch += 1  # a switch was applied while we awaited
            return ["- pod pod-a"]

        async def delete_object(
            self, meta: Any, ns: Any, name: Any, *, uid: str | None = None
        ) -> None:
            self.deletes.append(str(name))

    ops = _EpochBumpOps()
    app._write_ops = cast("Any", ops)
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        await app.action_delete_resource()
        assert len(app.screen_stack) == 1  # no confirmation dialog opened
        assert ops.deletes == []
        await until(
            pilot,
            lambda: any(
                "kube context changed during the dry-run preview" in n.message
                for n in app._notifications
            ),
            label="preview epoch refusal",
        )


async def test_switch_rebinds_helm_wrapper() -> None:
    """HelmCLI pins --kube-context per instance (issue #31): after a `:ctx`
    switch the app must adopt the wrapper rebuilt for the new context, or
    helm writes would keep landing on the old cluster."""
    from korvid.k8s.helmcli import HelmCLI

    new_helm = HelmCLI("/usr/bin/helm", kube_context="ctx-b")
    env = _CtxEnv(
        result=ContextSwitchResult(
            pod_resize_supported=False,
            provider_hint=None,
            context_namespace=None,
            helm=new_helm,
        )
    )
    app = env.app
    app._helm = HelmCLI("/usr/bin/helm", kube_context="ctx-a")
    async with app.run_test() as pilot:
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        assert app._helm is new_helm


async def test_recovery_to_kubeconfig_default_is_success() -> None:
    """None is a legitimate applied context (the kubeconfig default): a
    recovery swap back to it must count as success, restarting the MCP
    server and watches instead of taking the total-failure path
    (issue #36 review round 10)."""
    import dataclasses

    env = _CtxEnv(switch_error=RuntimeError("target cluster unreachable"))
    app = env.app
    app.config = dataclasses.replace(app.config, kube_context=None)
    mcp = _FakeMCP()
    app._mcp = cast("Any", mcp)
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: len(env.switch_calls) == 2, label="recovery swap ran")
        assert env.switch_calls == ["ctx-b", None]
        await until(pilot, lambda: "mcp-started" in mcp.events, label="mcp restarted")
        await until(
            pilot,
            lambda: any("Restored context" in n.message for n in app._notifications),
            label="restored notice",
        )
        assert not any("restart korvid" in n.message for n in app._notifications)


async def test_helm_flow_cancelled_when_context_switched_before_approval() -> None:
    """The helm wrapper captured by an install flow pins the old cluster's
    --kube-context: a switch completed during the wizard or preview must
    cancel before an approval dialog can open (issue #36 review round 10)."""
    from korvid.ui.widgets.helm_install import HelmReleaseChoices

    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        choices = HelmReleaseChoices(
            release="web", version="", namespace="default", edit_values=False
        )
        ok = app._helm_ctl._context_after_preview(
            "helm-install", choices, upgrade=False, epoch=app._ctx_epoch - 1
        )
        assert ok is False
        await until(
            pilot,
            lambda: any("kube context changed" in n.message for n in app._notifications),
            label="helm epoch refusal",
        )


async def test_hint_fetch_result_dropped_when_context_switches() -> None:
    """A hint-event fetch resolving after a completed :ctx switch must not
    write the cache (either path): teardown cleared it, and a late result
    would resurrect old-cluster hints (issue #36 review round 13)."""
    env = _CtxEnv()
    app = env.app

    class _BumpingEvents:
        def __init__(self, *, error: bool) -> None:
            self.error = error

        async def fetch(self, ns: str, name: str, uid: str | None = None) -> list[Any]:
            app._ctx_epoch += 1  # a switch completes during the fetch
            if self.error:
                raise RuntimeError("client closed by the switch")
            return []

    async with app.run_test():
        summary = _pod("pod-a")
        for error in (False, True):
            app._get_events = cast("Any", _BumpingEvents(error=error))
            await app._hints.fetch_event("default/pod-a", "default/pod-a#u1", summary)
            assert app._hints.cache == {}


async def test_switch_cancels_hint_refresh_timer() -> None:
    """Teardown stops the parked-cursor hint refresh: its old-cluster
    row_key must not re-trigger a fetch after retarget."""
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app._hints.timer = app.set_timer(60, lambda: None)
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        assert app._hints.timer is None


async def test_mcp_toggle_refused_while_switching() -> None:
    """:mcp on landing mid-swap could restart the server against the client
    and alias map being replaced — refused while the switch runs."""
    env = _CtxEnv()
    app = env.app
    mcp = _FakeMCP()
    app._mcp = cast("Any", mcp)
    async with app.run_test() as pilot:
        app._ctx_switching = True
        try:
            app._handle_mcp_command(["on"])
            await until(
                pilot,
                lambda: any(
                    "context switch is in progress" in n.message for n in app._notifications
                ),
                label="mcp toggle refusal",
            )
        finally:
            app._ctx_switching = False
        assert "mcp-started" not in mcp.events


async def test_transfer_cancelled_when_context_switched_while_dialog_open() -> None:
    """A transfer dialog chain that stayed open across a completed :ctx
    switch must not stream: the pod selection (and its fail-open uid)
    belongs to the old cluster (issue #36 review round 13)."""
    from korvid.core.transfer import TransferSpec

    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        spec = TransferSpec(direction="download", remote_path="/tmp/x", local_path="/tmp/x")
        app._start_transfer("default", "pod-a", None, spec, None, app._ctx_epoch - 1)
        await until(
            pilot,
            lambda: any("kube context" in n.message for n in app._notifications),
            label="transfer epoch refusal",
        )
        assert app._transfer_task is None


async def test_describe_cancelled_when_context_switches_during_fetch() -> None:
    """A describe whose manifest fetch straddles a completed :ctx switch must
    not render the old cluster's manifest (issue #36 review round 14)."""
    env = _CtxEnv()
    app = env.app

    async def fake_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        app._ctx_epoch += 1  # a switch completed while the fetch was in flight
        return {"metadata": {"name": name, "uid": "u1"}}

    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app._get_manifest = fake_manifest
        await app.action_describe()
        await until(
            pilot,
            lambda: any(
                "cancelled - the kube context changed during the fetch" in n.message
                for n in app._notifications
            ),
            label="describe epoch refusal",
        )
        assert len(app.screen_stack) == 1


async def test_switch_to_kubeconfig_active_context_is_noop() -> None:
    """Sessions started without -c run on the kubeconfig's active context:
    `:ctx <that-name>` is a friendly no-op, not a probe/teardown cycle
    (issue #36 review round 14)."""
    import dataclasses

    env = _CtxEnv()
    app = env.app
    app.config = dataclasses.replace(app.config, kube_context=None)
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-a"))
        await until(
            pilot,
            lambda: any("Already on context ctx-a" in n.message for n in app._notifications),
            label="no-op notification",
        )
        assert env.probe_calls == []
        assert env.switch_calls == []


async def test_switch_refused_while_namespace_picker_open() -> None:
    """The namespace picker is an inline widget (not on the screen stack):
    the blocker must still catch it (issue #36 review round 14)."""
    from korvid.ui.widgets.namespace_picker import NamespacePicker

    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.query_one(NamespacePicker).display = True
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: any("Close the namespace picker" in n.message for n in app._notifications),
            label="picker blocker notification",
        )
        assert env.switch_calls == []


async def test_debug_fallback_cancelled_when_context_switched(tmp_path: Path) -> None:
    """A debug offer whose RBAC/manifest pre-checks straddle a completed
    :ctx switch must not open pickers for an old-cluster pod - kubectl debug
    mutates the pod spec (issue #36 review round 14)."""
    env = _CtxEnv(audit_path=tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        await app._shell._offer_debug_fallback("default", "pod-a", None, 1, app._ctx_epoch - 1)
        await until(
            pilot,
            lambda: any(
                "Debug fallback for pod-a cancelled - the kube context changed" in n.message
                for n in app._notifications
            ),
            label="debug fallback epoch refusal",
        )
        assert len(app.screen_stack) == 1


async def test_switch_adopts_context_namespace_as_session_default() -> None:
    """The target context's kubeconfig namespace becomes the session default:
    toggling all-namespaces off must return to it, not to the startup
    namespace (issue #36 review round 16)."""
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: app.current_scope == "ns-b",
            label="scope follows new context",
        )
        assert app.config.namespace == "ns-b"
        await app.action_toggle_all_namespaces()
        await until(pilot, lambda: app.current_scope != "ns-b", label="all-namespaces on")
        await app.action_toggle_all_namespaces()
        await until(
            pilot,
            lambda: app.current_scope == "ns-b",
            label="toggle-off returns to the new context's namespace",
        )


async def test_namespace_picker_list_dropped_when_context_switches() -> None:
    """A namespace listing that straddles a completed :ctx switch must not
    open the picker: its old-cluster options would navigate the new cluster
    (issue #36 review round 16)."""
    from korvid.ui.messages import ShowNamespacePicker
    from korvid.ui.widgets.namespace_picker import NamespacePicker

    env = _CtxEnv()
    app = env.app

    async def stale_list() -> list[str]:
        app._ctx_epoch += 1  # a switch completed while the LIST was in flight
        return ["old-ns"]

    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app._list_namespaces = stale_list
        await app.on_show_namespace_picker(ShowNamespacePicker())
        await until(
            pilot,
            lambda: any(
                "Namespace picker cancelled - the kube context changed" in n.message
                for n in app._notifications
            ),
            label="picker epoch refusal",
        )
        assert app.query_one(NamespacePicker).display is False


async def test_mcp_toggle_queued_before_switch_rechecks_inside_lock() -> None:
    """A toggle worker queued before :ctx claimed _ctx_switching must not
    start the server mid-swap: it serializes on _nav_lock (held by the
    switch through quiesce/teardown/retarget) and re-checks the flag inside
    (issue #36 review round 21)."""
    env = _CtxEnv()
    app = env.app
    mcp = _FakeMCP()
    app._mcp = cast("Any", mcp)
    async with app.run_test() as pilot:
        await app._nav_lock.acquire()  # stand-in for the switch holding it
        try:
            app._handle_mcp_command(["on"])  # pre-check passes; worker blocks
            await pilot.pause()
            app._ctx_switching = True  # the switch claims while the toggle waits
        finally:
            app._nav_lock.release()
        try:
            await until(
                pilot,
                lambda: any(
                    "context switch is in progress" in n.message for n in app._notifications
                ),
                label="in-lock mcp toggle refusal",
            )
        finally:
            app._ctx_switching = False
        assert "mcp-started" not in mcp.events


async def test_failed_mcp_restart_after_switch_notifies_as_error() -> None:
    """MCPController.start() reports failures as "ERROR ..." strings: the
    post-switch restart must surface those with error severity, matching
    the :mcp on path (issue #36 review round 22)."""
    env = _CtxEnv()
    app = env.app
    mcp = _FakeMCP()

    async def failing_start() -> str:
        return "ERROR MCP failed to bind :4321"

    mcp.start = failing_start  # type: ignore[method-assign]
    app._mcp = cast("Any", mcp)
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: any(
                n.severity == "error" and "ERROR MCP failed" in n.message
                for n in app._notifications
            ),
            label="mcp restart error severity",
        )


async def test_switch_closes_live_log_pane() -> None:
    """A live log stream (long-lived read) must die with the old cluster:
    the teardown closes the pane and cancels its stream tasks (issue #84)."""
    from korvid.k8s.logs import LogLine
    from korvid.ui.widgets.log_pane import LogPane

    streaming = asyncio.Event()

    async def stream(
        namespace: str,
        pod: str,
        container: str = "",
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[LogLine]:
        streaming.set()
        yield LogLine(pod=pod, container=container, text="line0")
        while True:
            await asyncio.sleep(0.01)

    env = _CtxEnv(stream_logs=stream)
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        await pilot.press("l")
        await until(pilot, lambda: streaming.is_set(), label="stream started")
        assert app.query_one(LogPane).display is True
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: app.config.kube_context == "ctx-b",
            label="config context updated",
        )
        assert app.query_one(LogPane).display is False
        assert not app._log_tasks


async def test_logs_refused_during_switch_via_ctx_flow() -> None:
    """`l` pressed while a real `:ctx` switch is in flight (blocked at the
    auth probe, which runs after `_ctx_switching` is claimed) is refused up
    front (issue #84)."""
    from korvid.ui.widgets.log_pane import LogPane

    async def stream(
        namespace: str,
        pod: str,
        container: str = "",
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[Any]:
        return
        yield  # pragma: no cover

    gate = asyncio.Event()
    env = _CtxEnv(stream_logs=stream, probe_gate=gate)
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: env.probe_calls == ["ctx-b"] and app._ctx_switching,
            label="switch flow blocked at the probe",
        )
        try:
            await pilot.press("l")
            await until(
                pilot,
                lambda: any(
                    "context switch is in progress" in n.message for n in app._notifications
                ),
                label="logs refusal notification",
            )
            assert app.query_one(LogPane).display is False
            assert not app._log_tasks
        finally:
            gate.set()
        await until(
            pilot,
            lambda: app.config.kube_context == "ctx-b",
            label="switch completed",
        )


async def test_switch_adopts_protection_and_clears_on_switch_back() -> None:
    """Switching into a protected context turns the marker on and warns;
    switching back to an unprotected one clears it (issue #83)."""
    env = _CtxEnv(
        result=ContextSwitchResult(
            pod_resize_supported=False,
            provider_hint=None,
            context_namespace="ns-b",
            protected_context="ctx-b",
        )
    )
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        assert app._protected_context is None
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(
            pilot,
            lambda: app._protected_context == "ctx-b",
            label="protection adopted",
        )
        status = app.query_one(StatusBar)
        assert "PROTECTED" in str(status.render())
        await until(
            pilot,
            lambda: any("protected" in n.message.lower() for n in app._notifications),
            label="protected warning",
        )
        # Back to an unprotected context: marker clears.
        env.result = ContextSwitchResult(
            pod_resize_supported=False,
            provider_hint=None,
            context_namespace=None,
            protected_context=None,
        )
        app.post_message(SwitchContextCommand("ctx-a"))
        await until(
            pilot,
            lambda: app._protected_context is None,
            label="protection cleared",
        )
        assert "PROTECTED" not in str(app.query_one(StatusBar).render())


async def test_switch_collapses_split_workspace() -> None:
    """A context switch resets *all* cluster state. The non-focused pane's
    kind/scope/filter/drill and its watch belong to the old cluster: keeping
    the split would leave its rows stale-but-actionable, so the switch
    collapses back to a single pane on the new cluster's default view."""
    env = _CtxEnv()
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        await pilot.press("ctrl+w", "v")
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        await until(pilot, lambda: len(app.query(ResourceTable)) == 1, label="single pane")
        assert len(app._panes) == 1
        assert app._focused_pane == 0
        assert app.current_kind == "pods"
        await _first_pod_visible(env, pilot, "pod-b")


async def test_switch_restarts_metrics_for_same_namespace() -> None:
    """`_metrics_target` caches the running poll target; the switch teardown
    stops the poller, so a stale cache would make `_sync_metrics_poller`
    treat the new (same-namespace) target as already served and never
    restart metrics on the new cluster."""
    from korvid.k8s.metrics import MetricsPoller, PodMetrics

    calls: list[str | None] = []

    async def fetch(namespace: str | None) -> list[PodMetrics]:
        calls.append(namespace)
        return []

    env = _CtxEnv(
        result=ContextSwitchResult(
            pod_resize_supported=True,
            provider_hint=None,
            context_namespace="default",  # same namespace as before the switch
        ),
        metrics=MetricsPoller(fetch, interval=0.05),
    )
    app = env.app
    async with app.run_test() as pilot:
        await _first_pod_visible(env, pilot, "pod-a")
        await until(pilot, lambda: len(calls) > 0, label="poller running before switch")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        calls.clear()
        await until(pilot, lambda: len(calls) > 0, label="poller restarted after switch")
        assert calls
