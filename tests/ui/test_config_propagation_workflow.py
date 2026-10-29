"""`:consumers` end to end: a ConfigMap row, its consumers, and their Pod (#410)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.evidence_requests import EvidenceClient, EvidencePage, EvidenceTarget
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.widgets.config_propagation_screen import ConfigPropagationScreen
from tests.app_factory import build_test_app
from tests.ui.waits import screen_ready, until

CONFIG_MAPS = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)
SETTINGS_POD = {
    "metadata": {"name": "api-1", "namespace": "default", "uid": "uid-api-1"},
    "spec": {
        "containers": [
            {
                "name": "app",
                "envFrom": [{"configMapRef": {"name": "settings"}}],
                "env": [{"name": "TOKEN", "value": "s3cr3t-literal"}],
            }
        ]
    },
}


class Client(EvidenceClient):
    """Serves configmaps, secrets and pods, and one Pod that reads `settings`."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    @property
    def session_generation(self) -> int:
        return 1

    async def discovery_snapshot(self) -> RawDiscovery:
        served = tuple(
            ServedResource(name, frozenset({"get", "list"}))
            for name in ("configmaps", "secrets", "pods")
        )
        return RawDiscovery(
            1,
            datetime.now(UTC),
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", ()),
            (GroupVersionOutcome("", "v1", served),),
        )

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.reads.append(f"list {target.resource}")
        return EvidencePage((SETTINGS_POD,))

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.reads.append(f"get {target.resource}/{name} metadata={target.metadata_only}")
        return {"metadata": {"name": name, "namespace": "default", "uid": "uid-settings"}}


async def _source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
    if kind == "configmaps":
        yield ("ADDED", GenericSummary("settings", "default", "ConfigMap", "", uid="uid-settings"))
    if kind == "pods":
        pod = PodSummary("api-1", "default", "Running", "1/1", 0, None, uid="uid-api-1")
        yield ("ADDED", pod)
    await asyncio.Event().wait()


async def _command(pilot: Any, text: str) -> None:
    await pilot.press("colon", *text, "enter")


async def test_consumers_of_a_config_map_lead_to_their_pod() -> None:
    client = Client()
    store = ResourceStore()
    app = build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source),
        aliases=build_alias_map([PODS_META, CONFIG_MAPS]),
        agent_available=False,
        evidence_client=client,
    )
    async with app.run_test() as pilot:
        await _command(pilot, "configmaps")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "settings"),
            label="ConfigMap selected",
        )

        await _command(pilot, "consumers")
        await until(pilot, lambda: screen_ready(app, ConfigPropagationScreen), label="view open")
        screen = app.screen
        assert isinstance(screen, ConfigPropagationScreen)
        assert [c.pod.name for c in screen.report.consumers] == ["api-1"]
        assert "s3cr3t-literal" not in repr(screen.report)
        assert client.reads == ["get configmaps/settings metadata=True", "list pods"]

        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "pods", label="Pod view")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "api-1"),
            label="consumer Pod selected",
        )


async def test_consumers_on_another_view_says_what_to_open() -> None:
    store = ResourceStore()
    app = build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source),
        aliases=build_alias_map([PODS_META, CONFIG_MAPS]),
        agent_available=False,
        evidence_client=Client(),
    )
    async with app.run_test() as pilot:
        await _command(pilot, "consumers")
        await until(
            pilot,
            lambda: any("Open ConfigMaps or Secrets" in n.message for n in app._notifications),
            label="reason shown",
        )
        assert not isinstance(app.screen, ConfigPropagationScreen)
