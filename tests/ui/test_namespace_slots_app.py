"""Keys 1-9, help and the `:ns` picker share one namespace slot map (issue #406)."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from textual.pilot import Pilot

from korvid.core.config import KorvidConfig, load_config
from korvid.core.keybinding_config import save_keybindings
from korvid.core.namespace_slot_store import ClusterIdentity, NamespaceSlotStore
from korvid.core.namespace_slots import SlotEntry, SlotOrigin
from korvid.core.store import ResourceStore
from korvid.core.watch import WatchManager
from korvid.k8s.errors import ApiStatusError
from korvid.ui.app import KorvidApp
from korvid.ui.context_switch_coordinator import ContextSwitchResult
from korvid.ui.messages import SwitchContextCommand
from korvid.ui.namespace_slot_controller import SlotPersistence
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.namespace_picker import NamespacePicker
from korvid.ui.widgets.namespace_slots_screen import NamespaceSlotsScreen
from korvid.ui.widgets.resource_table import ResourceTable
from tests.app_factory import build_test_app

from .test_app import _DEFAULT_TEST_ALIASES, _pod, fake_source
from .test_keybinding_editor_workflow import _confirm, _open_editor, _stage
from .waits import until

SERVER = "https://dev.example:6443"
IDENTITY = ClusterIdentity("dev", SERVER)


class Cluster:
    """A mutable namespace listing that counts its calls."""

    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.error: Exception | None = None
        self.calls = 0

    async def list_namespaces(self) -> list[str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.names)


def _app(
    cluster: Cluster,
    tmp_path: Path,
    *,
    config: KorvidConfig | None = None,
    save: Callable[..., None] | None = None,
) -> tuple[KorvidApp, NamespaceSlotStore]:
    store = ResourceStore()
    slot_store = NamespaceSlotStore(tmp_path / "namespace-slots.json")
    app = build_test_app(
        config=config or KorvidConfig(namespace="default", favorite_namespaces=("prod",)),
        store=store,
        watch_manager=WatchManager(store, fake_source([_pod("api-1")])),
        list_namespaces=cluster.list_namespaces,
        aliases=dict(_DEFAULT_TEST_ALIASES),
        save_keybindings=save,
        namespace_slots=SlotPersistence(slot_store, lambda context: ("dev", SERVER)),
    )
    return app, slot_store


def _slot(app: KorvidApp, slot: int) -> SlotEntry | None:
    return app._workspace_ctl.slots.slots.get(slot)


async def _ready(pilot: Pilot[None], app: KorvidApp, slot: int, namespace: str) -> None:
    await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="table seeded")
    await until(
        pilot,
        lambda: (entry := _slot(app, slot)) is not None and entry.namespace == namespace,
        label=f"slot {slot} discovered",
    )


async def _help_text(pilot: Pilot[None], app: KorvidApp) -> str:
    await pilot.press("question_mark")
    await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help open")
    assert isinstance(app.screen, HelpScreen)
    text = app.screen.body_text()
    await pilot.press("escape")
    await until(pilot, lambda: len(app.screen_stack) == 1, label="help closed")
    return text


async def _picker_rows(pilot: Pilot[None], app: KorvidApp) -> list[tuple[str, bool]]:
    picker = app.query_one(NamespacePicker)
    await pilot.press("colon", "n", "s", "enter")
    await until(pilot, lambda: picker.display, label="picker open")
    rows = [
        (str(option.prompt), option.disabled)
        for option in (picker.get_option_at_index(i) for i in range(picker.option_count))
    ]
    await pilot.press("escape")
    return rows


async def test_help_picker_and_keys_agree_and_discovery_never_navigates(tmp_path: Path) -> None:
    cluster = Cluster(["prod", "beta", "alpha"])
    app, slot_store = _app(cluster, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await _ready(pilot, app, 3, "beta")
        assert app.current_scope == "default"

        help_text = await _help_text(pilot, app)
        assert "Namespace slots" in help_text
        assert "2          alpha (auto)" in help_text
        assert "1          prod (pinned)" in help_text
        assert ("2  alpha", False) in await _picker_rows(pilot, app)

        await pilot.press("2")
        await until(pilot, lambda: app.current_scope == "alpha", label="slot 2 entered")
    assert slot_store.load(IDENTITY) == {
        2: SlotEntry("alpha", SlotOrigin.AUTO),
        3: SlotEntry("beta", SlotOrigin.AUTO),
    }


async def test_a_namespace_gone_from_the_listing_keeps_its_key_but_refuses_it(
    tmp_path: Path,
) -> None:
    cluster = Cluster(["alpha", "beta"])
    app, _ = _app(cluster, tmp_path, config=KorvidConfig(namespace="default"))
    async with app.run_test(size=(120, 40)) as pilot:
        await _ready(pilot, app, 2, "beta")
        cluster.names = ["alpha", "gamma"]

        rows = await _picker_rows(pilot, app)
        await pilot.press("2")

        assert ("2  beta (unavailable)", True) in rows
        assert ("3  gamma", False) in rows
        await until(
            pilot,
            lambda: any(":slots" in n.message for n in app._notifications),
            label="unavailable slot explained",
        )
        assert app.current_scope == "default"


async def test_a_denied_listing_infers_nothing_and_never_probes(tmp_path: Path) -> None:
    cluster = Cluster([])
    cluster.error = ApiStatusError(403, "Forbidden", "namespaces is forbidden")
    app, slot_store = _app(cluster, tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await until(pilot, lambda: app._workspace_ctl.slots.stale, label="discovery failed")

        help_text = await _help_text(pilot, app)
        await pilot.press("1")
        await until(pilot, lambda: app.current_scope == "prod", label="pin still navigates")

        assert "Last known map" in help_text
        assert cluster.calls == 1
        assert _slot(app, 2) is None
    assert slot_store.load(IDENTITY) == {}


async def _reallocate(pilot: Pilot[None], app: KorvidApp) -> NamespaceSlotsScreen:
    await pilot.press("colon", *"slots", "enter")
    await until(pilot, lambda: isinstance(app.screen, NamespaceSlotsScreen), label="preview open")
    assert isinstance(app.screen, NamespaceSlotsScreen)
    return app.screen


async def test_slots_command_previews_then_saves_only_the_slot_state(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"namespace": "default"}), encoding="utf-8")
    saved_keybindings: list[Any] = []
    cluster = Cluster(["alpha", "old"])
    app, slot_store = _app(
        cluster, tmp_path, config=load_config(config_path), save=saved_keybindings.append
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await _ready(pilot, app, 2, "old")
        cluster.names = ["alpha", "new"]

        screen = await _reallocate(pilot, app)
        assert screen.query_one(".slots-preview") is not None
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (entry := _slot(app, 2)) is not None and entry.namespace == "new",
            label="reallocated",
        )

        assert app.current_scope == "default"
    assert slot_store.load(IDENTITY) == {
        1: SlotEntry("alpha", SlotOrigin.AUTO),
        2: SlotEntry("new", SlotOrigin.AUTO),
    }
    assert saved_keybindings == []
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == {"namespace": "default"}


async def test_escape_keeps_the_current_slots(tmp_path: Path) -> None:
    cluster = Cluster(["alpha", "old"])
    app, slot_store = _app(cluster, tmp_path, config=KorvidConfig(namespace="default"))
    async with app.run_test(size=(120, 40)) as pilot:
        await _ready(pilot, app, 2, "old")
        cluster.names = ["alpha"]

        await _reallocate(pilot, app)
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="preview dismissed")

        entry = _slot(app, 2)
        assert entry == SlotEntry("old", SlotOrigin.AUTO, available=False)
    assert slot_store.load(IDENTITY)[2] == SlotEntry("old", SlotOrigin.AUTO, available=False)


async def test_keybinding_apply_and_reset_leave_the_slot_state_untouched(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"namespace": "default"}), encoding="utf-8")
    cluster = Cluster(["alpha", "beta"])
    app, _ = _app(
        cluster,
        tmp_path,
        config=load_config(config_path),
        save=partial(save_keybindings, config_path),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await _ready(pilot, app, 2, "beta")
        state = (tmp_path / "namespace-slots.json").read_bytes()

        screen = await _open_editor(pilot, app)
        await _stage(pilot, screen, "help", "f1")
        await _confirm(pilot, app)
        screen = await _open_editor(pilot, app)
        await pilot.press("f8")
        await _confirm(pilot, app)

        assert app._keybinding_overrides == {}
        assert _slot(app, 2) == SlotEntry("beta", SlotOrigin.AUTO)
    assert (tmp_path / "namespace-slots.json").read_bytes() == state


class Clusters:
    """Two clusters behind one session; the switch fake flips which one answers."""

    def __init__(self) -> None:
        self.current = "ctx-a"
        self.names = {"ctx-a": ["a-only"], "ctx-b": ["b-only"]}

    async def list_namespaces(self) -> list[str]:
        return list(self.names[self.current])

    async def switch(self, name: str | None) -> ContextSwitchResult:
        self.current = name or "ctx-a"
        return ContextSwitchResult(
            pod_resize_supported=True, provider_hint=None, context_namespace="default"
        )

    async def probe(self, name: str) -> None:
        return None


async def _switch(pilot: Pilot[None], app: KorvidApp, context: str) -> None:
    app.post_message(SwitchContextCommand(context))
    await until(pilot, lambda: app.config.kube_context == context, label=f"on {context}")


def _only(app: KorvidApp, namespace: str) -> Callable[[], bool]:
    return lambda: (
        dict(app._workspace_ctl.slots.slots.items()) == {1: SlotEntry(namespace, SlotOrigin.AUTO)}
    )


async def test_a_context_switch_never_carries_the_old_clusters_slots(tmp_path: Path) -> None:
    clusters = Clusters()
    store = ResourceStore()
    slot_store = NamespaceSlotStore(tmp_path / "namespace-slots.json")
    app = build_test_app(
        config=KorvidConfig(namespace="default", kube_context="ctx-a"),
        store=store,
        watch_manager=WatchManager(store, fake_source([_pod("api-1")])),
        list_namespaces=clusters.list_namespaces,
        aliases=dict(_DEFAULT_TEST_ALIASES),
        list_contexts=lambda: (["ctx-a", "ctx-b"], "ctx-a"),
        probe_context=clusters.probe,
        switch_context=clusters.switch,
        namespace_slots=SlotPersistence(slot_store, lambda context: (context or "", SERVER)),
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await _ready(pilot, app, 1, "a-only")

        await _switch(pilot, app, "ctx-b")
        await until(pilot, _only(app, "b-only"), label="only ctx-b slots")
        await _switch(pilot, app, "ctx-a")
        await until(pilot, _only(app, "a-only"), label="ctx-a slots restored")

    assert slot_store.load(ClusterIdentity("ctx-a", SERVER)) == {
        1: SlotEntry("a-only", SlotOrigin.AUTO)
    }
    assert slot_store.load(ClusterIdentity("ctx-b", SERVER)) == {
        1: SlotEntry("b-only", SlotOrigin.AUTO)
    }
