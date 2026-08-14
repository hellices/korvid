"""App-level integration for the operational relationship graph (issue #281,
Task 7): the `g` binding, its exclusive worker, context-switch safety, and
the goto callback's reuse of the existing `_jump_to_object` navigation path.

Tasks 5 (`RelationshipSnapshotLoader`) and 6 (`RelationshipScreen`) already
cover the loader's coverage classification and the screen's own row
rendering/navigation in isolation; this module only proves the integration
boundary Task 7 owns: selecting the exact root identity, running the loader
inside the `"relationships"` worker group, discarding stale results across a
`:ctx` switch, and translating a dismissed `GotoResult` back into a real
`_jump_to_object` navigation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

from textual.widgets import DataTable

from korvid.core.relationships import GraphResource
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    TargetReference,
)
from korvid.ui.app import ContextSwitchResult, KorvidApp
from korvid.ui.messages import SwitchContextCommand
from korvid.ui.widgets.relationship_screen import RelationshipScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
CONFIG_MAPS_META = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)
_ALIASES = build_alias_map([PODS_META, CONFIG_MAPS_META])


def _pod(
    name: str, *, namespace: str = "prod", uid: str = "pod-1", uses_config: str | None = None
) -> PodSummary:
    relationships = RelationshipFacts()
    if uses_config is not None:
        relationships = RelationshipFacts(
            references=(
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference(
                        group="", kind="ConfigMap", namespace=namespace, name=uses_config
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="spec.volumes[0].configMap.name",
                ),
            )
        )
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid=uid,
        relationships=relationships,
    )


def _configmap(name: str, *, namespace: str = "prod") -> GenericSummary:
    return GenericSummary(name=name, namespace=namespace, kind="ConfigMap", created="", uid="cm-1")


class _RelationshipLister:
    """Records `(meta.plural, namespace)` calls; replays results/errors by
    plural and can pause every call until `resume()` (mirrors the
    `_BlockingLister` fake `tests/ui/test_relationship_controller.py`
    already uses for the loader's own concurrency tests)."""

    def __init__(self) -> None:
        self._results: dict[str, list[Any]] = {}
        self._errors: dict[str, Exception] = {}
        self.calls: list[tuple[str, str | None]] = []
        self._gate: asyncio.Event | None = None

    def add(self, meta: ResourceMeta, summaries: list[Any]) -> None:
        self._results[meta.plural] = summaries

    def fail(self, meta: ResourceMeta, exc: Exception) -> None:
        self._errors[meta.plural] = exc

    def pause(self) -> None:
        self._gate = asyncio.Event()

    def resume(self) -> None:
        if self._gate is not None:
            self._gate.set()

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Any]:
        self.calls.append((meta.plural, namespace))
        if self._gate is not None:
            await self._gate.wait()
        if meta.plural in self._errors:
            raise self._errors[meta.plural]
        return list(self._results.get(meta.plural, []))


class _RelEnv:
    """App plus recording fakes for the relationship-graph and context-switch
    collaborators, modeled on `tests/ui/test_ctx_switch.py::_CtxEnv`."""

    def __init__(
        self,
        *,
        pods: tuple[PodSummary, ...] = (),
        configmaps: tuple[GenericSummary, ...] = (),
        with_lister: bool = True,
        contexts: tuple[str, ...] = ("ctx-a", "ctx-b"),
    ) -> None:
        from korvid.core.config import KorvidConfig

        self.lister = _RelationshipLister()
        self.switch_calls: list[str | None] = []
        store = ResourceStore()

        async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
            if kind == "pods":
                for pod in pods:
                    yield ("ADDED", pod)
            elif kind == "configmaps":
                for cfg in configmaps:
                    yield ("ADDED", cfg)
            while True:
                await asyncio.sleep(0.01)

        def list_contexts() -> tuple[list[str], str | None]:
            return list(contexts), "ctx-a"

        async def probe(name: str) -> None:
            return None

        async def switch(name: str | None) -> ContextSwitchResult:
            self.switch_calls.append(name)
            return ContextSwitchResult(
                pod_resize_supported=False, provider_hint=None, context_namespace="prod"
            )

        self.app = KorvidApp(
            config=KorvidConfig(namespace="prod", kube_context="ctx-a"),
            store=store,
            watch_manager=WatchManager(store, source),
            aliases=dict(_ALIASES),
            list_contexts=list_contexts,
            probe_context=probe,
            switch_context=switch,
            list_relationship_objects=self.lister if with_lister else None,
        )


async def _show_pods(env: _RelEnv, pilot: Any) -> None:
    """Pods is the app's default startup view; wait for the initial watch
    stream's rows to land before interacting with the table."""

    def cond() -> bool:
        table = env.app.query_one(ResourceTable)
        return table.row_count > 0

    await until(pilot, cond, label="pods visible")


def _relationship_workers_finished(app: KorvidApp) -> bool:
    return all(w.is_finished for w in app.workers if w.group == "relationships")


async def test_g_opens_relationships_for_selected_resource() -> None:
    env = _RelEnv(pods=(_pod("api-0", uid="pod-1"),))
    env.lister.add(CONFIG_MAPS_META, [])
    app = env.app
    async with app.run_test() as pilot:
        await _show_pods(env, pilot)
        await pilot.press("g")
        await until(
            pilot,
            lambda: isinstance(app.screen, RelationshipScreen),
            label="relationship screen opened",
        )
        screen = cast(RelationshipScreen, app.screen)
        assert screen.root == GraphResource(
            group="", kind="Pod", namespace="prod", name="api-0", uid="pod-1"
        )
        assert ("pods", "prod") in env.lister.calls
        assert ("configmaps", "prod") in env.lister.calls


async def test_context_switch_discards_inflight_graph() -> None:
    env = _RelEnv(pods=(_pod("api-0", uid="pod-1"),))
    env.lister.pause()
    app = env.app
    async with app.run_test() as pilot:
        await _show_pods(env, pilot)
        await pilot.press("g")
        await until(pilot, lambda: env.lister.calls != [], label="loader started")
        app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: app.config.kube_context == "ctx-b", label="context switched")
        env.lister.resume()
        await until(pilot, lambda: _relationship_workers_finished(app), label="graph worker reaped")
        assert not isinstance(app.screen, RelationshipScreen)
        assert env.switch_calls == ["ctx-b"]


async def test_g_without_selected_row_does_not_start_loader() -> None:
    env = _RelEnv(pods=())
    app = env.app
    async with app.run_test() as pilot:
        await pilot.press("g")
        await pilot.pause()
        assert env.lister.calls == []
        assert not isinstance(app.screen, RelationshipScreen)
        assert any("No resource selected" in n.message for n in app._notifications)


async def test_g_is_unavailable_without_relationship_lister() -> None:
    env = _RelEnv(pods=(_pod("api-0"),), with_lister=False)
    app = env.app
    assert app._relationship_loader is None
    async with app.run_test() as pilot:
        await _show_pods(env, pilot)
        await pilot.press("g")
        await pilot.pause()
        assert not isinstance(app.screen, RelationshipScreen)
        assert any(
            "Relationships unavailable in this session" in n.message for n in app._notifications
        )


async def test_failed_root_source_shows_incomplete_graph() -> None:
    env = _RelEnv(pods=(_pod("api-0", uid="pod-1"),))
    env.lister.fail(PODS_META, ApiStatusError(403, "Forbidden"))
    env.lister.add(CONFIG_MAPS_META, [])
    app = env.app
    async with app.run_test() as pilot:
        await _show_pods(env, pilot)
        await pilot.press("g")
        await until(
            pilot,
            lambda: isinstance(app.screen, RelationshipScreen),
            label="relationship screen opened despite the failed source",
        )
        screen = cast(RelationshipScreen, app.screen)
        assert screen.graph.incomplete
        banner = str(screen.query_one("#relationship-coverage").render())
        assert "forbidden" in banner


async def test_graph_goto_reuses_normal_navigation() -> None:
    pod = _pod("api-0", uid="pod-1", uses_config="cm-a")
    cfg = _configmap("cm-a")
    env = _RelEnv(pods=(pod,), configmaps=(cfg,))
    # The pod's own declared reference (its `USES_CONFIG` fact) is only fed
    # into the graph if the loader's own "pods" LIST also returns it - the
    # selected row's identity and the fact-bearing snapshot input are
    # separate concerns the loader joins together.
    env.lister.add(PODS_META, [pod])
    env.lister.add(CONFIG_MAPS_META, [cfg])
    app = env.app
    async with app.run_test() as pilot:
        await _show_pods(env, pilot)
        await pilot.press("g")
        await until(
            pilot,
            lambda: isinstance(app.screen, RelationshipScreen),
            label="relationship screen opened",
        )
        # Row 0 is the "Dependencies" section header; row 1 is the resolved
        # ConfigMap dependency edge added right after it.
        table = app.screen.query_one(DataTable)
        table.move_cursor(row=1)
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "configmaps", label="navigated")
        assert app.current_namespace == "prod"
        namespace, name = app._selected_ns_name()
        assert (namespace, name) == ("prod", "cm-a")
