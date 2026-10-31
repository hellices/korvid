"""Reading the cluster's API resources again during a session (#517)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from textual.message import Message

from korvid.k8s.errors import KubeTimeoutError
from korvid.ui.action_availability import AvailabilityCode
from korvid.ui.api_resources_controller import (
    API_RESOURCES_GROUP,
    ApiResourcesController,
    Rediscovery,
    deferred_navigation_guard,
)
from korvid.ui.messages import NavigateCommand, NavigationOrigin, UnknownCommand

from .test_session_timeline_controller import FakeUiSurface

UNCHANGED = Rediscovery()


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Cluster:
    """Serves `served` once read, as a rediscovery would make it known."""

    def __init__(self, outcome: Rediscovery = UNCHANGED, *served: str) -> None:
        self.outcome = outcome
        self.served = set(served)
        self.known: set[str] = {"pods"}
        self.reads = 0
        self.error: Exception | None = None

    async def rediscover(self) -> Rediscovery:
        self.reads += 1
        if self.error is not None:
            raise self.error
        if self.outcome.applied:
            self.known |= self.served
        return self.outcome

    def resolve(self, word: str) -> str | None:
        return word if word in self.known else None


class Harness:
    def __init__(
        self,
        cluster: Cluster | None,
        *,
        epoch: Callable[[], int] = lambda: 1,
        reads_allowed: Callable[[], bool] = lambda: True,
    ) -> None:
        self.pane: object = "pane-1"
        self.generation = 7
        self.ui = FakeUiSurface(close_worker_coroutines=False)
        self.clock = Clock()
        self.posted: list[Message] = []
        self.updates = 0
        self.cluster = cluster
        self.controller = ApiResourcesController(
            ui=self.ui,
            rediscover=cluster.rediscover if cluster is not None else None,
            known=cluster.resolve if cluster is not None else lambda _: None,
            post=self.posted.append,
            aliases_updated=self._updated,
            epoch=epoch,
            reads_allowed=reads_allowed,
            navigation_position=lambda: (self.pane, self.generation),
            clock=self.clock,
            min_interval=30.0,
        )

    def _updated(self) -> None:
        self.updates += 1

    async def finish(self) -> None:
        """Run every worker the controller started."""
        while self.ui.workers:
            await self.ui.workers.pop(0)

    def messages(self) -> list[tuple[str, ...]]:
        return [self._describe(m) for m in self.posted]

    @staticmethod
    def _describe(message: Message) -> tuple[str, ...]:
        if isinstance(message, NavigateCommand):
            return ("navigate", str(message.view), str(message.namespace))
        assert isinstance(message, UnknownCommand)
        return ("unknown", message.text, str(message.rediscovered))

    def reads(self) -> int:
        return self.cluster.reads if self.cluster is not None else 0

    def notes(self) -> list[tuple[str, Any]]:
        return [(n.message, n.severity) for n in self.ui.notifications]


async def test_a_kind_served_since_startup_opens_after_one_read() -> None:
    harness = Harness(Cluster(Rediscovery(added=("widgets.example.com",)), "widgets"))

    assert harness.controller.resolve_unknown("widgets shop")
    await harness.finish()

    assert harness.ui.worker_groups == [(API_RESOURCES_GROUP, False)]
    assert harness.ui.exclusive_workers == [True]
    assert harness.messages() == [("navigate", "widgets", "shop")]
    assert harness.updates == 1
    assert harness.notes() == []


async def test_a_kind_still_not_served_is_reported_and_not_read_again_soon() -> None:
    harness = Harness(Cluster())

    assert harness.controller.resolve_unknown("widgets")
    await harness.finish()
    asked_again = harness.controller.resolve_unknown("widgets")

    assert harness.messages() == [("unknown", "widgets", "True")]
    assert not asked_again
    assert harness.reads() == 1


async def test_the_rate_limit_lapses() -> None:
    harness = Harness(Cluster())
    harness.controller.resolve_unknown("widgets")
    await harness.finish()

    harness.clock.now += 30.0
    asked_again = harness.controller.resolve_unknown("widgets")
    await harness.finish()

    assert asked_again
    assert harness.reads() == 2


@pytest.mark.parametrize(
    "text",
    ["", "sort a b", "ns a b", "widgets a b", "ctx"],
    ids=["empty", "builtin-arguments", "namespace-arguments", "too-many", "builtin"],
)
def test_only_a_word_that_could_name_a_kind_is_read_again(text: str) -> None:
    harness = Harness(Cluster())

    assert not harness.controller.resolve_unknown(text)
    assert harness.ui.workers == []


async def test_commands_typed_during_a_read_wait_for_it() -> None:
    harness = Harness(Cluster(UNCHANGED, "widgets"))

    first = harness.controller.resolve_unknown("gadgets all")
    second = harness.controller.resolve_unknown("widgets")
    await harness.finish()

    assert (first, second) == (True, True)
    assert harness.reads() == 1
    assert harness.messages() == [
        ("unknown", "gadgets all", "True"),
        ("navigate", "widgets", "None"),
    ]


async def test_an_explicit_refresh_is_not_rate_limited_and_says_what_changed() -> None:
    outcome = Rediscovery(
        added=("widgets.example.com", "gadgets.example.com", "gizmos.example.com", "doodads.io"),
        removed=("olds.example.com",),
        changed=("batches.example.com",),
    )
    harness = Harness(Cluster(outcome))
    harness.controller.resolve_unknown("widgets")
    await harness.finish()

    harness.controller.refresh()
    await harness.finish()

    assert harness.notes() == [
        (
            "API resources read again: 4 added (widgets.example.com, gadgets.example.com, "
            "gizmos.example.com and 1 more), 1 removed (olds.example.com), 1 now served at "
            "another version (batches.example.com)",
            "information",
        )
    ]


async def test_an_explicit_refresh_with_nothing_new_says_so() -> None:
    harness = Harness(Cluster())

    harness.controller.refresh()
    await harness.finish()

    assert harness.notes() == [("API resources read again: no change", "information")]
    assert harness.updates == 1


async def test_a_refresh_while_one_runs_is_not_started_twice() -> None:
    harness = Harness(Cluster())
    harness.controller.refresh()

    harness.controller.refresh()
    started = len(harness.ui.workers)
    await harness.finish()

    assert started == 1
    assert harness.notes()[0] == ("The API resources are already being read", "information")


async def test_group_versions_that_could_not_be_read_keep_their_kinds() -> None:
    outcome = Rediscovery(unreadable=("metrics.k8s.io/v1beta1", "the API group list"))
    harness = Harness(Cluster(outcome))

    harness.controller.resolve_unknown("widgets")
    await harness.finish()

    assert harness.notes() == [
        (
            "Could not read metrics.k8s.io/v1beta1, the API group list: the kinds korvid "
            "already knew from them are kept",
            "warning",
        )
    ]
    assert harness.messages() == [("unknown", "widgets", "True")]


async def test_a_failed_read_keeps_the_known_kinds_and_reports_the_command() -> None:
    cluster = Cluster()
    cluster.error = KubeTimeoutError("discovery timed out")
    harness = Harness(cluster)

    harness.controller.resolve_unknown("widgets")
    await harness.finish()

    assert harness.notes() == [
        (
            "Could not read the cluster's API resources (discovery timed out); the kinds "
            "already known are kept",
            "warning",
        )
    ]
    assert harness.updates == 0
    assert harness.messages() == [("unknown", "widgets", "True")]


class HeldCluster(Cluster):
    """Holds each read until `release` is set, so the context can move meanwhile."""

    def __init__(self, outcome: Rediscovery, *served: str) -> None:
        super().__init__(outcome, *served)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def rediscover(self) -> Rediscovery:
        self.entered.set()
        await self.release.wait()
        return await super().rediscover()


async def test_a_context_switch_during_a_read_discards_it() -> None:
    epoch = [1]
    cluster = HeldCluster(UNCHANGED, "widgets")
    harness = Harness(cluster, epoch=lambda: epoch[0])

    harness.controller.resolve_unknown("widgets")
    read = asyncio.ensure_future(harness.ui.workers.pop(0))
    await cluster.entered.wait()
    epoch[0] = 2
    cluster.release.set()
    await read

    assert (harness.messages(), harness.updates, harness.notes()) == ([], 0, [])


async def test_a_read_the_composition_root_did_not_apply_is_discarded() -> None:
    cluster = Cluster(Rediscovery(applied=False), "widgets")
    harness = Harness(cluster)

    harness.controller.resolve_unknown("widgets")
    await harness.finish()

    assert (harness.messages(), harness.updates, harness.notes()) == ([], 0, [])
    assert cluster.resolve("widgets") is None


def test_without_a_cluster_connection_nothing_is_read() -> None:
    harness = Harness(None)

    reason = harness.controller.unavailable_reason()
    harness.controller.refresh()

    assert reason is not None
    assert reason.code is AvailabilityCode.MISSING_CAPABILITY
    assert not harness.controller.resolve_unknown("widgets")
    assert harness.notes() == [("Reading API resources needs a cluster connection", "warning")]
    assert harness.ui.workers == []


async def test_cancel_workers_stops_a_read_holding_the_old_client() -> None:
    harness = Harness(Cluster())

    await harness.controller.cancel_workers()

    assert harness.ui.cancelled_groups == [API_RESOURCES_GROUP]


def test_nothing_is_read_while_a_context_switch_is_under_way() -> None:
    harness = Harness(Cluster(), reads_allowed=lambda: False)

    harness.controller.refresh()
    settled = harness.controller.resolve_unknown("widgets")

    assert not settled
    assert harness.ui.workers == []
    assert harness.notes() == [
        ("The API resources can be read again once the context switch is done", "warning")
    ]


async def test_a_settled_command_carries_the_pane_it_was_typed_in() -> None:
    harness = Harness(Cluster(UNCHANGED, "widgets"))

    harness.controller.resolve_unknown("widgets shop")
    harness.pane = "pane-2"
    await harness.finish()

    (command,) = harness.posted
    assert isinstance(command, NavigateCommand)
    assert command.origin == NavigationOrigin("pane-1", 7, 1)


class Context:
    def __init__(self) -> None:
        self.current = 3
        self.switching = False

    def crossed(self, epoch: int) -> bool:
        return self.switching or epoch != self.current


class Pane:
    def __init__(self) -> None:
        self.nav_gen = 7


def _deferred(pane: object, generation: int = 7, epoch: int = 3) -> NavigateCommand:
    return NavigateCommand("widgets", "shop", origin=NavigationOrigin(pane, generation, epoch))


def _quiet(*_: object, **__: object) -> None:
    pass


def test_an_ordinary_navigation_runs_unguarded() -> None:
    notes: list[str] = []

    guard = deferred_navigation_guard(
        NavigateCommand("pods"), Pane(), Context(), lambda text, **_: notes.append(text)
    )

    assert (guard, notes) == ((), [])


def test_a_deferred_navigation_runs_while_its_pane_has_not_moved_on() -> None:
    pane, context = Pane(), Context()

    guard = deferred_navigation_guard(_deferred(pane), pane, context, _quiet)

    assert guard is not None
    (current,) = guard
    assert current()
    pane.nav_gen += 1
    assert not current()
    pane.nav_gen -= 1
    context.switching = True
    assert not current()


@pytest.mark.parametrize("moved", ["focus", "navigation"])
def test_a_deferred_navigation_its_pane_moved_past_is_dropped_and_said(moved: str) -> None:
    pane, notes = Pane(), []
    origin = Pane() if moved == "focus" else pane
    generation = 7 if moved == "focus" else 6

    guard = deferred_navigation_guard(
        _deferred(origin, generation), pane, Context(), lambda text, **_: notes.append(text)
    )

    assert guard is None
    assert notes == ["widgets is served now; run :widgets again in the pane you want it in"]


def test_a_deferred_navigation_a_context_switch_crossed_is_dropped_silently() -> None:
    notes: list[str] = []

    guard = deferred_navigation_guard(
        _deferred(Pane(), epoch=2), Pane(), Context(), lambda text, **_: notes.append(text)
    )

    assert (guard, notes) == (None, [])


async def test_of_commands_typed_in_one_pane_during_a_read_the_newest_runs() -> None:
    harness = Harness(Cluster(UNCHANGED, "widgets", "gadgets"))

    harness.controller.resolve_unknown("widgets")
    harness.controller.resolve_unknown("gadgets")
    harness.controller.resolve_unknown("doodads")
    harness.pane = "pane-2"
    harness.controller.resolve_unknown("widgets kube-system")
    await harness.finish()

    assert harness.messages() == [
        ("unknown", "doodads", "True"),
        ("navigate", "widgets", "kube-system"),
    ]


async def test_the_newest_command_of_a_pane_may_be_one_that_resolves() -> None:
    harness = Harness(Cluster(UNCHANGED, "gadgets"))

    harness.controller.resolve_unknown("doodads")
    harness.controller.resolve_unknown("gadgets")
    await harness.finish()

    assert harness.messages() == [("unknown", "doodads", "True"), ("navigate", "gadgets", "None")]
