"""Reading the cluster's API resources again during a session (#517)."""

from __future__ import annotations

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
)
from korvid.ui.messages import NavigateCommand, UnknownCommand

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

    first = harness.controller.resolve_unknown("widgets")
    second = harness.controller.resolve_unknown("gadgets all")
    await harness.finish()

    assert (first, second) == (True, True)
    assert harness.reads() == 1
    assert harness.messages() == [
        ("navigate", "widgets", "None"),
        ("unknown", "gadgets all", "True"),
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


@pytest.mark.parametrize("discarded_by", ["epoch", "generation"])
async def test_a_context_switch_during_a_read_discards_it(discarded_by: str) -> None:
    epochs = iter([1, 2])
    outcome = Rediscovery(applied=discarded_by != "generation")
    harness = Harness(
        Cluster(outcome, "widgets"),
        epoch=(lambda: next(epochs)) if discarded_by == "epoch" else (lambda: 1),
    )

    harness.controller.resolve_unknown("widgets")
    await harness.finish()

    assert (harness.messages(), harness.updates, harness.notes()) == ([], 0, [])


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
