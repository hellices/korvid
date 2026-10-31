"""A kind the cluster starts serving during a session, end to end (#517)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from korvid.composition_support import AliasReservations, rediscover_aliases
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import GroupVersionOutcome, RawDiscovery, RootOutcome
from korvid.k8s.models import GenericSummary
from korvid.ui.api_resources_controller import Rediscovery
from korvid.ui.messages import NavigateCommand
from tests.app_factory import build_test_app
from tests.ui.waits import until

WIDGETS = ResourceMeta("Widget", "widgets", "example.com", "v1", True, ("wd",))


class Cluster(KubeClient):
    """Discovery serves `WIDGETS` once `installed`; it never did at startup."""

    def __init__(self) -> None:
        super().__init__()
        self.installed = False
        self.reads = 0

    def invalidate_discovery(self) -> None:
        pass

    async def discovery_snapshot(self) -> RawDiscovery:
        self.reads += 1
        served = (
            (GroupVersionOutcome("example.com", "v1", (), (WIDGETS,)),) if self.installed else ()
        )
        return RawDiscovery(
            self.session_generation,
            datetime.now(UTC),
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", tuple((o.group, o.version) for o in served)),
            (GroupVersionOutcome("", "v1", (), (PODS_META,)), *served),
        )


WATCHED: list[tuple[str, str]] = []


async def _source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
    WATCHED.append((kind, scope))
    if (kind, scope) == ("widgets", "default"):
        yield ("ADDED", GenericSummary("blue", "default", "Widget", "", uid="uid-blue"))
    await asyncio.Event().wait()


def _app(cluster: Cluster) -> Any:
    store = ResourceStore()
    aliases = build_alias_map([PODS_META])
    reservations = AliasReservations()

    async def rediscover() -> Rediscovery:
        return await rediscover_aliases(cluster, aliases, reservations)

    return build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source),
        aliases=aliases,
        agent_available=False,
        rediscover=rediscover,
    )


async def _command(pilot: Any, text: str) -> None:
    await pilot.press("colon", *text, "enter")


def _messages(app: Any) -> list[str]:
    return [n.message for n in app._notifications]


async def test_a_crd_installed_after_startup_opens_through_its_command() -> None:
    WATCHED.clear()
    cluster = Cluster()
    cluster.installed = True
    app = _app(cluster)
    async with app.run_test() as pilot:
        await _command(pilot, "wd")

        await until(pilot, lambda: app.current_kind == "widgets", label="Widget view")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "blue"),
            label="Widget row shown",
        )
        assert cluster.reads == 1
        assert not any("Unknown resource" in m for m in _messages(app))
        assert [w for w in WATCHED if w[0] == "widgets"] == [("widgets", "default")]


async def test_a_kind_still_not_served_is_reported_and_not_read_again_at_once() -> None:
    cluster = Cluster()
    app = _app(cluster)
    async with app.run_test() as pilot:
        await _command(pilot, "wd")
        await until(
            pilot,
            lambda: any("Unknown resource or command: wd" in m for m in _messages(app)),
            label="reported unknown",
        )
        await _command(pilot, "wd")
        await until(
            pilot,
            lambda: sum("Unknown resource or command: wd" in m for m in _messages(app)) == 2,
            label="reported again",
        )

        assert cluster.reads == 1
        assert app.current_kind == "pods"


async def test_rediscover_says_what_the_cluster_now_serves() -> None:
    cluster = Cluster()
    app = _app(cluster)
    async with app.run_test() as pilot:
        cluster.installed = True
        await _command(pilot, "rediscover")

        await until(
            pilot,
            lambda: "API resources read again: 1 added (widgets.example.com)" in _messages(app),
            label="refresh summary",
        )
        assert app._command_bar.known("wd") == "widgets"


class SlowCluster(Cluster):
    """Holds each discovery read until `release` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def discovery_snapshot(self) -> RawDiscovery:
        await self.release.wait()
        return await super().discovery_snapshot()


async def test_a_kind_found_after_focus_moved_does_not_open_in_the_other_pane() -> None:
    cluster = SlowCluster()
    cluster.installed = True
    app = _app(cluster)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+w", "v")
        await until(pilot, lambda: app._workspace.pane_count == 2, label="split")
        await _command(pilot, "wd")
        await pilot.press("ctrl+w", "w")
        await until(pilot, lambda: app._workspace.focused_index == 0, label="focus on pane 1")

        cluster.release.set()
        await until(
            pilot,
            lambda: (
                "widgets is served now; run :widgets again in the pane you want it in"
                in _messages(app)
            ),
            label="told instead of opened",
        )

        assert [pane.kind for pane in app._workspace.panes] == ["pods", "pods"]


async def test_a_navigation_found_before_a_context_switch_is_not_applied_after_it() -> None:
    app = _app(Cluster())
    async with app.run_test() as pilot:
        stale = app._ctx.epoch() - 1
        app.post_message(NavigateCommand("widgets", origin=app._pane, epoch=stale))
        # Handled in order: once this one lands, the stale one has been handled.
        app.post_message(NavigateCommand(None, "kube-system"))
        await until(pilot, lambda: app._pane.scope == "kube-system", label="later navigation")

        assert app.current_kind == "pods"


async def test_a_stale_navigation_from_another_pane_says_nothing() -> None:
    app = _app(Cluster())
    async with app.run_test() as pilot:
        stale = app._ctx.epoch() - 1
        app.post_message(NavigateCommand("widgets", origin=object(), epoch=stale))
        app.post_message(NavigateCommand(None, "kube-system"))
        await until(pilot, lambda: app._pane.scope == "kube-system", label="later navigation")

        assert not any("is served now" in m for m in _messages(app))
