"""The configuration propagation controller: availability, runs and late results (#410)."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from typing import Any

from korvid.core.config_consumers import ConsumptionMode, PodIdentity
from korvid.core.config_propagation import ConfigTarget
from korvid.core.evidence_reads import DEFAULT_MAX_BYTES, LIST_PAGE_SIZE, MAX_BYTES
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.evidence_requests import EvidenceClient, EvidencePage, EvidenceTarget
from korvid.ui.action_availability import AvailabilityCode
from korvid.ui.config_propagation_controller import (
    CONFIG_PROPAGATION_GROUP,
    ConfigPropagationController,
)
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.config_propagation_screen import ConfigPropagationScreen
from korvid.ui.workspace_controller import ContextGuard

from .test_session_timeline_controller import FakeUiSurface

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
CONFIG_MAPS = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)
SECRETS = ResourceMeta("Secret", "secrets", "", "v1", True)
PODS = ResourceMeta("Pod", "pods", "", "v1", True)
FOREIGN = ResourceMeta("ConfigMap", "configmaps", "vendor.example.com", "v1", True)
SETTINGS_POD = {
    "metadata": {"name": "api-1", "namespace": "team-a", "uid": "uid-api-1"},
    "spec": {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "settings"}}]}]},
}


class FakeView(ViewState):
    def __init__(self, kind: str = "configmaps", selected: tuple[str, str] | None = None) -> None:
        self.kind = kind
        self.scope = "team-a"
        self.selected = selected
        self.alias_map = {
            "configmaps": CONFIG_MAPS,
            "secrets": SECRETS,
            "pods": PODS,
            "configmaps.vendor.example.com": FOREIGN,
        }

    def current_kind(self) -> str:
        return self.kind

    def canonical_kind(self, kind: str) -> str:
        return kind

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return self.alias_map

    def selected_ns_name(self, *, notify: bool = True) -> tuple[str | None, str | None]:
        return self.selected if self.selected is not None else (None, None)

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return f"uid-{name}"

    def current_scope(self) -> str:
        return self.scope

    def resources(self, kind: str, scope: str) -> list[Any]:
        return []  # pragma: no cover

    def readonly(self) -> bool:
        return False  # pragma: no cover

    def default_namespace(self) -> str | None:
        return None  # pragma: no cover

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural  # pragma: no cover

    def write_locus(self, namespace: str | None) -> str:
        return ""  # pragma: no cover


class FakeContext(ContextGuard):
    def __init__(self) -> None:
        self.current = 1
        self.in_switch = False
        self.allowed = True

    def epoch(self) -> int:
        return self.current

    def switching(self) -> bool:
        return self.in_switch

    def reads_allowed(self) -> bool:
        return self.allowed

    def crossed(self, epoch: int) -> bool:
        return self.in_switch or epoch != self.current


class FakeClient(EvidenceClient):
    def __init__(self) -> None:
        self.generation = 3
        self.gets: list[tuple[EvidenceTarget, str, int]] = []
        self.lists: list[tuple[EvidenceTarget, int, str, int]] = []

    @property
    def session_generation(self) -> int:
        return self.generation

    async def discovery_snapshot(self) -> RawDiscovery:
        served = tuple(
            ServedResource(name, frozenset({"get", "list"}))
            for name in ("configmaps", "secrets", "pods")
        )
        return RawDiscovery(
            self.generation,
            NOW,
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", ()),
            (GroupVersionOutcome("", "v1", served),),
        )

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append((target, limit, continuation, max_bytes))
        return EvidencePage((SETTINGS_POD,))

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name, max_bytes))
        return {"metadata": {"name": name, "namespace": "team-a", "uid": f"uid-{name}"}}


class Harness:
    def __init__(self, *, client: FakeClient | None = None, view: FakeView | None = None) -> None:
        self.ui = FakeUiSurface(close_worker_coroutines=False)
        self.view = view or FakeView(selected=("team-a", "settings"))
        self.context = FakeContext()
        self.client = FakeClient() if client is None else client
        self.navigations: list[tuple[str, str, int, str | None]] = []
        self.controller = ConfigPropagationController(
            ui=self.ui,
            view=self.view,
            context=self.context,
            client=self.client,
            navigate=self._navigate,
            clock=lambda: NOW,
        )

    async def _navigate(self, namespace: str, name: str, epoch: int, uid: str | None) -> None:
        self.navigations.append((namespace, name, epoch, uid))

    async def run(self, index: int = -1) -> None:
        work = self.ui.workers[index]
        assert isinstance(work, Awaitable)
        await work


def _code(harness: Harness) -> AvailabilityCode | None:
    reason = harness.controller.unavailable_reason()
    return None if reason is None else reason.code


def test_it_is_available_on_a_selected_config_map_or_secret() -> None:
    assert _code(Harness()) is None
    assert _code(Harness(view=FakeView("secrets", ("team-a", "creds")))) is None


def test_it_says_why_it_is_unavailable() -> None:
    unconnected = ConfigPropagationController(
        ui=FakeUiSurface(),
        view=FakeView(selected=("team-a", "settings")),
        context=FakeContext(),
        client=None,
        navigate=Harness()._navigate,
    )

    reason = unconnected.unavailable_reason()
    assert reason is not None
    assert reason.code is AvailabilityCode.MISSING_CAPABILITY
    assert _code(Harness(view=FakeView("pods", ("team-a", "api-1")))) is AvailabilityCode.WRONG_VIEW
    assert (
        _code(Harness(view=FakeView("configmaps.vendor.example.com", ("team-a", "x"))))
        is AvailabilityCode.WRONG_VIEW
    )
    assert _code(Harness(view=FakeView("configmaps", None))) is AvailabilityCode.NO_SELECTION


async def test_opening_reads_the_selected_object_and_shows_its_consumers() -> None:
    harness = Harness()

    harness.controller.open_selected()
    await harness.run()

    assert harness.ui.worker_groups == [(CONFIG_PROPAGATION_GROUP, False)]
    (screen,) = harness.ui.screens
    assert isinstance(screen, ConfigPropagationScreen)
    assert screen.report.target == ConfigTarget("ConfigMap", "team-a", "settings", "uid-settings")
    assert [(c.pod, c.mode) for c in screen.report.consumers] == [
        (PodIdentity("team-a", "api-1", "uid-api-1"), ConsumptionMode.ENV_FROM)
    ]
    assert harness.client.gets == [
        (
            EvidenceTarget("", "v1", "configmaps", "team-a", metadata_only=True),
            "settings",
            DEFAULT_MAX_BYTES,
        )
    ]
    assert harness.client.lists == [
        (EvidenceTarget("", "v1", "pods", "team-a"), LIST_PAGE_SIZE, "", MAX_BYTES)
    ]


def test_opening_where_it_is_unavailable_says_why_and_reads_nothing() -> None:
    harness = Harness(view=FakeView("pods", ("team-a", "api-1")))

    harness.controller.open_selected()

    assert harness.ui.workers == []
    assert [n.severity for n in harness.ui.notifications] == ["warning"]


def test_opening_while_reads_are_held_reads_nothing() -> None:
    harness = Harness()
    harness.context.allowed = False

    harness.controller.open_selected()

    assert harness.ui.workers == []


async def test_a_newer_run_drops_an_older_result() -> None:
    harness = Harness()

    harness.controller.open_selected()
    harness.controller.open_selected()
    await harness.run(0)

    assert harness.ui.screens == []
    await harness.run(1)
    assert len(harness.ui.screens) == 1


async def test_a_context_switch_drops_the_result() -> None:
    harness = Harness()

    harness.controller.open_selected()
    harness.context.current = 2
    await harness.run()

    assert harness.ui.screens == []


async def test_new_credentials_drop_the_result() -> None:
    harness = Harness()

    harness.controller.open_selected()
    harness.client.generation = 4
    await harness.run()

    assert harness.ui.screens == []


async def test_choosing_a_consumer_opens_its_pod_in_the_run_epoch() -> None:
    harness = Harness()
    harness.controller.open_selected()
    await harness.run()
    (screen,) = harness.ui.screens
    assert isinstance(screen, ConfigPropagationScreen)
    (consumer,) = screen.report.consumers

    assert harness.ui.screen_callback is not None
    harness.ui.screen_callback(None)
    assert len(harness.ui.workers) == 1
    harness.context.current = 2
    harness.ui.screen_callback(consumer)
    await harness.run()

    assert harness.navigations == [("team-a", "api-1", 1, "uid-api-1")]


async def test_a_dialog_opened_during_the_reads_keeps_the_view_closed() -> None:
    harness = Harness()
    harness.controller.open_selected()
    harness.ui.screen_depth = lambda: 2  # type: ignore[method-assign]  # a dialog opened

    await harness.run()

    assert harness.ui.screens == []


async def test_leaving_the_view_or_the_selection_keeps_the_result_closed() -> None:
    moved = Harness()
    rescoped = Harness()
    reselected = Harness()

    for harness in (moved, rescoped, reselected):
        harness.controller.open_selected()
    moved.view.kind = "pods"
    rescoped.view.scope = "team-b"
    reselected.view.selected = ("team-a", "other")
    for harness in (moved, rescoped, reselected):
        await harness.run()

    assert (moved.ui.screens, rescoped.ui.screens, reselected.ui.screens) == ([], [], [])


async def test_the_worker_group_is_cancelled_on_request() -> None:
    harness = Harness()

    await harness.controller.cancel_workers()

    assert harness.ui.cancelled_groups == [CONFIG_PROPAGATION_GROUP]
