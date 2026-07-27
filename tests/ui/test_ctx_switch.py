"""Runtime context switching via `:ctx` (issue #36).

`:ctx` lists kubeconfig contexts, `:ctx <name>` switches the whole session:
the target is auth-probed first (a failure leaves the old context fully
usable), then watches/logs/forwards/store/drill state are torn down and the
session restarts on the new cluster with capabilities re-probed.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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
    ) -> None:
        self.probe_calls: list[str] = []
        self.switch_calls: list[str | None] = []
        self.probe_error = probe_error
        self.switch_error = switch_error
        self.result = result or ContextSwitchResult(
            pod_resize_supported=True,
            provider_hint="AKS",
            fallback_namespaces=("team-b",),
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
        assert app._fallback_namespaces == ("team-b",)
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
        app._hint_event_cache["default/pod-a"] = (0.0, None, None)
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="switched")
        await _first_pod_visible(env, pilot, "pod-b")
        rows = app.store.get("pods", "ns-b")
        assert [p.name for p in rows] == ["pod-b"]
        assert app.store.get("pods", "default") == []  # old bucket purged
        assert app.filter_pattern == ""
        assert app._hint_event_cache == {}
        assert app._drill.breadcrumb() == ""


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
        await pilot.pause()
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
