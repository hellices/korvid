"""The creation constraints controller: availability, runs and late results (#411)."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime
from typing import Any

from korvid.core.creation_constraints import CreationTarget
from korvid.core.creation_failures import Subject
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.evidence_requests import EvidenceClient, EvidencePage, EvidenceTarget
from korvid.ui.action_availability import AvailabilityCode
from korvid.ui.creation_constraints_controller import (
    CREATION_CONSTRAINTS_GROUP,
    CreationConstraintsController,
)
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.creation_constraints_screen import CreationConstraintsScreen, Destination
from korvid.ui.workspace_controller import ContextGuard

from .test_session_timeline_controller import FakeUiSurface

NOW = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
DEPLOYMENTS = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
CRON_JOBS = ResourceMeta("CronJob", "cronjobs", "batch", "v1", True)
PODS = ResourceMeta("Pod", "pods", "", "v1", True)
FOREIGN = ResourceMeta("Deployment", "deployments", "vendor.example.com", "v1", True)
_SERVED = {
    ("", "v1"): ("resourcequotas", "limitranges", "events"),
    ("apps", "v1"): ("deployments", "replicasets"),
    ("batch", "v1"): ("jobs", "cronjobs"),
}
EVENT = {
    "metadata": {"name": "e", "namespace": "shop", "uid": "uid-e"},
    "reason": "FailedCreate",
    "involvedObject": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
    "message": 'pods "x" is forbidden: exceeded quota: compute, requested: pods=1',
}


class FakeView(ViewState):
    def __init__(self, kind: str = "deployments", selected: tuple[str, str] | None = None) -> None:
        self.kind = kind
        self.scope = "shop"
        self.selected = selected
        self.alias_map = {
            "deployments": DEPLOYMENTS,
            "cronjobs": CRON_JOBS,
            "pods": PODS,
            "deployments.vendor.example.com": FOREIGN,
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
        return []

    def readonly(self) -> bool:
        return False

    def default_namespace(self) -> str | None:
        return "shop"

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural

    def write_locus(self, namespace: str | None) -> str:
        return namespace or ""


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
        self.gets: list[tuple[EvidenceTarget, str]] = []
        self.lists: list[EvidenceTarget] = []

    @property
    def session_generation(self) -> int:
        return self.generation

    async def discovery_snapshot(self) -> RawDiscovery:
        outcomes = tuple(
            GroupVersionOutcome(
                group, version, tuple(ServedResource(r, frozenset({"get", "list"})) for r in names)
            )
            for (group, version), names in _SERVED.items()
        )
        groups = tuple((g, v) for g, v in _SERVED if g)
        return RawDiscovery(
            self.generation,
            NOW,
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", groups),
            outcomes,
        )

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append(target)
        if target.resource == "replicasets":
            owner = [{"uid": "uid-api", "controller": True}]
            rs = {
                "metadata": {
                    "name": "api-7d9f",
                    "namespace": "shop",
                    "uid": "uid-rs",
                    "ownerReferences": owner,
                }
            }
            return EvidencePage((rs,))
        if target.resource == "events":
            return EvidencePage((EVENT,))
        return EvidencePage(())

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name))
        return {
            "metadata": {"name": name, "namespace": "shop", "uid": f"uid-{name}"},
            "spec": {},
            "status": {},
        }


class Harness:
    def __init__(self, *, client: FakeClient | None = None, view: FakeView | None = None) -> None:
        self.ui = FakeUiSurface(close_worker_coroutines=False)
        self.view = view or FakeView(selected=("shop", "api"))
        self.context = FakeContext()
        self.client = FakeClient() if client is None else client
        self.navigations: list[tuple[str, str, str, int, str | None]] = []
        self.controller = CreationConstraintsController(
            ui=self.ui,
            view=self.view,
            context=self.context,
            client=self.client,
            navigate=self._navigate,
            clock=lambda: NOW,
        )

    async def _navigate(
        self, kind: str, namespace: str, name: str, epoch: int, uid: str | None
    ) -> None:
        self.navigations.append((kind, namespace, name, epoch, uid))

    async def run(self, index: int = -1) -> None:
        work = self.ui.workers[index]
        assert isinstance(work, Awaitable)
        await work


def _code(harness: Harness) -> AvailabilityCode | None:
    reason = harness.controller.unavailable_reason()
    return None if reason is None else reason.code


def test_it_is_available_on_a_selected_workload() -> None:
    assert _code(Harness()) is None
    assert _code(Harness(view=FakeView("cronjobs", ("shop", "nightly")))) is None


def test_it_says_why_it_is_unavailable() -> None:
    unconnected = CreationConstraintsController(
        ui=FakeUiSurface(),
        view=FakeView(selected=("shop", "api")),
        context=FakeContext(),
        client=None,
        navigate=Harness()._navigate,
    )

    reason = unconnected.unavailable_reason()
    assert reason is not None
    assert reason.code is AvailabilityCode.MISSING_CAPABILITY
    assert _code(Harness(view=FakeView("pods", ("shop", "api-1")))) is AvailabilityCode.WRONG_VIEW
    assert (
        _code(Harness(view=FakeView("deployments.vendor.example.com", ("shop", "x"))))
        is AvailabilityCode.WRONG_VIEW
    )
    assert _code(Harness(view=FakeView("deployments", None))) is AvailabilityCode.NO_SELECTION


async def test_opening_reads_the_selected_workload_and_shows_its_failures() -> None:
    harness = Harness()

    harness.controller.open_selected()
    await harness.run()

    assert harness.ui.worker_groups == [(CREATION_CONSTRAINTS_GROUP, False)]
    assert harness.ui.exclusive_workers == [True]
    (screen,) = harness.ui.screens
    assert isinstance(screen, CreationConstraintsScreen)
    assert screen.report.target == CreationTarget("Deployment", "shop", "api", "uid-api")
    assert [f.subject for f in screen.report.failures] == [
        Subject("ReplicaSet", "api-7d9f", "uid-rs")
    ]
    assert harness.client.gets == [(EvidenceTarget("apps", "v1", "deployments", "shop"), "api")]
    assert {t.resource for t in harness.client.lists} == {
        "replicasets",
        "events",
        "resourcequotas",
        "limitranges",
    }


def test_opening_where_it_is_unavailable_says_why_and_reads_nothing() -> None:
    harness = Harness(view=FakeView("pods", ("shop", "api-1")))

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


async def test_a_context_switch_or_new_credentials_drop_the_result() -> None:
    switched = Harness()
    reauthenticated = Harness()

    switched.controller.open_selected()
    reauthenticated.controller.open_selected()
    switched.context.current = 2
    reauthenticated.client.generation = 4
    await switched.run()
    await reauthenticated.run()

    assert (switched.ui.screens, reauthenticated.ui.screens) == ([], [])


async def test_choosing_a_row_opens_its_object_in_the_run_epoch() -> None:
    harness = Harness()
    harness.controller.open_selected()
    await harness.run()

    assert harness.ui.screen_callback is not None
    harness.ui.screen_callback(None)
    assert len(harness.ui.workers) == 1
    harness.context.current = 2
    harness.ui.screen_callback(Destination("resourcequotas", "shop", "compute", "uid-q"))
    await harness.run()

    assert harness.navigations == [("resourcequotas", "shop", "compute", 1, "uid-q")]
    assert harness.ui.worker_groups[-1] == (CREATION_CONSTRAINTS_GROUP, False)


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
    rescoped.view.scope = "other"
    reselected.view.selected = ("shop", "other")
    for harness in (moved, rescoped, reselected):
        await harness.run()

    assert (moved.ui.screens, rescoped.ui.screens, reselected.ui.screens) == ([], [], [])


async def test_the_worker_group_is_cancelled_on_request() -> None:
    harness = Harness()

    await harness.controller.cancel_workers()

    assert harness.ui.cancelled_groups == [CREATION_CONSTRAINTS_GROUP]
