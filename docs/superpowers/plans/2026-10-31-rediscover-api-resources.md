# Rediscover API Resources During a Session (#517) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open a kind the cluster started serving during the session, such as a CRD installed after startup, without restarting korvid (#517).

**Architecture:** The composition root gains `rediscover_aliases(kube, aliases)`. It invalidates the session's discovery, reads it again, and makes the shared alias map match it, keeping what could not be read. `ui/api_resources_controller.py` runs it in one exclusive worker, for `:rediscover` and its palette entry and, rate-limited, before an unknown `:` command is reported. The router asks it first; the command it settles is posted again, marked `rediscovered` if still unknown.

**Tech Stack:** Python 3.11+, Textual, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Bounded:** one read at a time, and commands typed meanwhile wait for it. An unknown command starts a read at most once every 30 seconds; `:rediscover` is not rate-limited.
- **Context-safe:** the context-switch coordinator cancels the worker group before the client swaps. A read whose session generation changed is never applied, and the controller drops a result whose context epoch changed.
- **An alias never moves to another resource:** an open view, its watch and its rows are keyed by an alias, so a refresh keeps every known alias on its resource and drops one whose resource is gone; only an unheld alias goes to a new resource.
- **No read during a context switch:** the controller checks `ContextGuard.reads_allowed()` before starting or queuing a read, because the coordinator has already cancelled the group and the old client would refill the alias map the switch reset.
- **Never drop what could not be read:** a group version that failed or was incomplete keeps the kinds korvid knew from it; an incomplete `/api` or `/apis` root removes nothing. A failed core `v1` or `/apis` read changes nothing and is said.
- **Palette search:** the help text and keywords must not contain the subsequence `h…e…l…m`, which a test reserves for Helm, and must not outrank the rows existing palette tests search for. Hence no "custom resource" or "reload" keyword.
- **Size limits:** `scripts/check_source_size.py` is not edited; `ui/app.py` has 10 lines of room.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the issue

- **The refresh replaces, the startup merge does not.** Startup discovery still only adds, as before. `:rediscover` and the unknown-command read rebuild the alias map, so a kind no longer served is dropped and a new preferred version replaces the old.
- **The command is `:rediscover`.** Its palette entry is **Refresh API resources**.
- **`:operators` waits for the read too.** An unknown `:operators` reads discovery again before the missing-catalog explanation, since a catalog installed during the session is exactly what the read finds.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/ui/api_resources_controller.py` | create | `Rediscovery`, `Rediscover`, `API_RESOURCES_GROUP`, `ApiResourcesController` |
| `src/korvid/composition_support.py` | modify | `rediscover_aliases`, `_alias_operator_catalog` shared with startup |
| `src/korvid/ui/messages.py`, `command.py`, `command_router.py`, `action_policy.py` | modify | `:rediscover`, `UnknownCommand.rediscovered`, `is_builtin_command`, routing and availability |
| `src/korvid/ui/context_switch_coordinator.py`, `worker_failures.py` | modify | cancel the group on a switch; report a failed read |
| `src/korvid/ui/app.py`, `app_runtime.py`, `src/korvid/__main__.py` | modify | the `rediscover` input and the controller's wiring |
| `docs/tui.md`, `docs/keybindings.md`, `docs/release-notes/unreleased.md` | modify | how a kind installed during the session opens |

---

### Task 1: The controller

**Files:** Create `src/korvid/ui/api_resources_controller.py` and `tests/ui/test_api_resources_controller.py`. Modify `src/korvid/ui/messages.py` (`BuiltinOperation.REDISCOVER = "rediscover"`; `UnknownCommand(text, *, rediscovered=False)`) and `src/korvid/ui/command.py` (`is_builtin_command`, and the `:rediscover` descriptor with palette entry `Refresh API resources`, keywords `crd`, `discovery`, `api`, `new kind`).

- [ ] **Step 1: Write the failing tests:**

```python
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
```

- [ ] **Step 2:** Run `uv run --frozen pytest -p no:tach tests/ui/test_api_resources_controller.py -q`. Expected: collection fails, no module.
- [ ] **Step 3: Implement it:**

```python
"""Read the cluster's API resources again during a session (#517).

korvid discovers the API resources at startup and after a context switch. A
kind the cluster starts serving later, such as a CRD a chart installs, is
found by reading discovery again: on demand, through `:rediscover` or the
palette, and once before a `:` command is reported as unknown.

One read runs at a time, in an exclusive worker the context-switch
coordinator cancels. Commands typed during a read wait for it. An unknown
command starts a read only when none started in the last `min_interval`
seconds, so repeated typos do not flood the API server; an explicit refresh
is not rate-limited. A read the context has moved past is discarded: the
composition root never applies one from an earlier session, and the epoch
check drops what is left.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from textual.message import Message

from korvid.k8s.errors import ApiStatusError, KubeClientError
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.command import is_builtin_command, parse_command
from korvid.ui.messages import UnknownCommand
from korvid.ui.ui_surface import UiSurface

API_RESOURCES_GROUP = "api-resources"
#: How long after one read an unknown command may start another, in seconds.
MIN_INTERVAL_SECONDS = 30.0
_NAMES_SHOWN = 3
_UNCONNECTED = UnavailableReason(
    AvailabilityCode.MISSING_CAPABILITY, "Reading API resources needs a cluster connection"
)
_SWITCHING = "The API resources can be read again once the context switch is done"


@dataclass(frozen=True, slots=True)
class Rediscovery:
    """What reading the API resources again changed, by qualified resource name.

    `unreadable` names what could not be read, whose kinds are kept as they
    were. `applied` is false when the session changed during the read, which
    is then discarded.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    applied: bool = True


#: Reads discovery again and applies it to the session's aliases.
Rediscover = Callable[[], Awaitable[Rediscovery]]


def _names(count_label: str, names: Sequence[str]) -> str:
    shown = ", ".join(names[:_NAMES_SHOWN])
    if len(names) > _NAMES_SHOWN:
        shown += f" and {len(names) - _NAMES_SHOWN} more"
    return f"{len(names)} {count_label} ({shown})"


def summary(outcome: Rediscovery) -> str:
    """What a read changed, for the notification an explicit refresh shows."""
    parts = [
        _names(label, names)
        for label, names in (
            ("added", outcome.added),
            ("removed", outcome.removed),
            ("now served at another version", outcome.changed),
        )
        if names
    ]
    return f"API resources read again: {', '.join(parts) or 'no change'}"


class ApiResourcesController:
    """Reads the API resources again, on demand or for an unknown `:` command."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        rediscover: Rediscover | None,
        known: Callable[[str], str | None],
        post: Callable[[Message], object],
        aliases_updated: Callable[[], None],
        epoch: Callable[[], int],
        reads_allowed: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        min_interval: float = MIN_INTERVAL_SECONDS,
    ) -> None:
        self._ui = ui
        self._rediscover = rediscover
        self._known = known
        self._post = post
        self._aliases_updated = aliases_updated
        self._epoch = epoch
        self._reads_allowed = reads_allowed
        self._clock = clock
        self._min_interval = min_interval
        self._running = False
        self._last_start: float | None = None
        self._waiting: list[str] = []

    def unavailable_reason(self) -> UnavailableReason | None:
        """Why the API resources cannot be read again, or None."""
        return _UNCONNECTED if self._rediscover is None else None

    def refresh(self) -> None:
        """Read the API resources again now, and say what changed."""
        if self._rediscover is None:
            self._ui.notify(_UNCONNECTED.message, severity="warning", markup=False)
            return
        if not self._reads_allowed():
            self._ui.notify(_SWITCHING, severity="warning", markup=False)
            return
        if self._running:
            self._ui.notify("The API resources are already being read")
            return
        self._start(announce=True)

    def resolve_unknown(self, text: str) -> bool:
        """Whether a read will settle the unknown command `text`.

        True when one runs for it: the command is parsed again afterwards and
        posted, as an `UnknownCommand` marked `rediscovered` if still unknown.
        False when it should be reported now: it cannot name a kind, nothing
        can be read, a context switch is under way, or a read started too
        recently. A read must not start during a switch: the coordinator has
        already cancelled this worker group, and the old client would refill
        the alias map the switch has just reset.
        """
        if self._rediscover is None or not self._could_name_a_kind(text):
            return False
        if not self._reads_allowed():
            return False
        if self._running:
            self._waiting.append(text)
            return True
        last = self._last_start
        if last is not None and self._clock() - last < self._min_interval:
            return False
        self._waiting.append(text)
        self._start(announce=False)
        return True

    @staticmethod
    def _could_name_a_kind(text: str) -> bool:
        """Whether `text` is `<kind>` or `<kind> <namespace>` for a word no command owns."""
        parts = text.split()
        return 0 < len(parts) <= 2 and not is_builtin_command(parts[0])

    def _start(self, *, announce: bool) -> None:
        self._running = True
        self._last_start = self._clock()
        self._ui.run_worker(
            self._read(announce, self._epoch()),
            exclusive=True,
            group=API_RESOURCES_GROUP,
            exit_on_error=False,
        )

    async def _read(self, announce: bool, epoch: int) -> None:
        rediscover = self._rediscover
        if rediscover is None:  # pragma: no cover - only started with one
            return
        outcome: Rediscovery | None = None
        failure: str | None = None
        try:
            outcome = await rediscover()
        except (KubeClientError, ApiStatusError) as error:
            failure = str(error)
        finally:
            self._running = False
            waiting, self._waiting = self._waiting, []
        if self._epoch() != epoch or (outcome is not None and not outcome.applied):
            return
        self._report(outcome, failure, announce=announce)
        for text in waiting:
            self._post_again(text)

    def _report(self, outcome: Rediscovery | None, failure: str | None, *, announce: bool) -> None:
        if outcome is None:
            self._ui.notify(
                f"Could not read the cluster's API resources ({failure}); the kinds already "
                "known are kept",
                severity="warning",
                markup=False,
            )
            return
        self._aliases_updated()
        if announce:
            self._ui.notify(summary(outcome), markup=False)
        if outcome.unreadable:
            self._ui.notify(
                f"Could not read {', '.join(outcome.unreadable)}: the kinds korvid already "
                "knew from them are kept",
                severity="warning",
                markup=False,
            )

    def _post_again(self, text: str) -> None:
        command = parse_command(text, self._known)
        if isinstance(command, UnknownCommand):
            command = UnknownCommand(text, rediscovered=True)
        self._post(command)

    async def cancel_workers(self) -> None:
        """Stop a read holding the old client, before a context switch swaps it."""
        await self._ui.cancel_workers(API_RESOURCES_GROUP)
```

- [ ] **Step 4:** Run the tests again. Expected: all pass, with no unawaited-coroutine warning; every test that starts a read awaits it.
- [ ] **Step 5: Commit.**

### Task 2: Rebuilding the aliases

**Files:** Modify `src/korvid/composition_support.py`; create `tests/test_rediscovery.py`.

- [ ] **Step 1: Write the failing tests:**

```python
"""The composition root's rediscovery: what reading discovery again does to the aliases (#517)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from korvid.composition_support import rediscover_aliases
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import GroupVersionOutcome, RawDiscovery, RootOutcome
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.olm import PACKAGES_GROUP
from korvid.ui.api_resources_controller import Rediscovery

NOW = datetime(2026, 10, 31, tzinfo=UTC)
BASE = [PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META]
CORE = ResourceMeta("ConfigMap", "configmaps", "", "v1", True, ("cm",))
WIDGET = ResourceMeta("Widget", "widgets", "example.com", "v1", True, ("wd",))
GADGET = ResourceMeta("Gadget", "gadgets", "example.com", "v1", True)
GADGET_V2 = ResourceMeta("Gadget", "gadgets", "example.com", "v2", True)
METRIC = ResourceMeta("PodMetrics", "pods", "metrics.k8s.io", "v1beta1", True)
PACKAGES = ResourceMeta("PackageManifest", "packagemanifests", PACKAGES_GROUP, "v1", True)


def _outcome(
    meta: ResourceMeta | None = None,
    *,
    group: str = "",
    version: str = "v1",
    complete: bool = True,
    error: ApiStatusError | None = None,
) -> GroupVersionOutcome:
    if meta is not None:
        group, version = meta.group, meta.version
    metas = (meta,) if meta is not None else ()
    return GroupVersionOutcome(group, version, (), metas, complete, error)


def _snapshot(
    *outcomes: GroupVersionOutcome,
    generation: int = 1,
    groups_complete: bool = True,
    versions_complete: bool = True,
    core: GroupVersionOutcome | None = None,
) -> RawDiscovery:
    advertised = tuple((o.group, o.version) for o in outcomes)
    return RawDiscovery(
        generation,
        NOW,
        0.0,
        RootOutcome("/api", (("", "v1"),), versions_complete),
        RootOutcome("/apis", advertised, groups_complete),
        (core or _outcome(CORE), *outcomes),
    )


class Kube(KubeClient):
    """Serves one discovery snapshot, and records that it was invalidated first."""

    def __init__(self, snapshot: RawDiscovery, *, generation: int = 1) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.generation = generation
        self.calls: list[str] = []
        self.switch_during_read = False

    @property
    def session_generation(self) -> int:
        return self.generation

    def invalidate_discovery(self) -> None:
        self.calls.append("invalidate")

    async def discovery_snapshot(self) -> RawDiscovery:
        self.calls.append("read")
        if self.switch_during_read:
            self.generation += 1
        return self.snapshot


def _aliases(*metas: ResourceMeta) -> dict[str, ResourceMeta]:
    return build_alias_map([*BASE, CORE, *metas])


async def test_a_kind_served_since_the_last_read_is_added() -> None:
    aliases = _aliases()
    kube = Kube(_snapshot(_outcome(WIDGET)))

    outcome = await rediscover_aliases(kube, aliases)

    assert kube.calls == ["invalidate", "read"]
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.example.com"] is WIDGET
    assert aliases["helmreleases"] is HELM_RELEASES_META
    assert outcome == Rediscovery(added=("widgets.example.com",))


async def test_a_kind_no_longer_served_is_dropped_and_a_new_version_replaces_the_old() -> None:
    aliases = _aliases(WIDGET, GADGET)
    kube = Kube(_snapshot(_outcome(GADGET_V2)))

    outcome = await rediscover_aliases(kube, aliases)

    assert "widgets" not in aliases
    assert "wd" not in aliases
    assert aliases["gadgets"] is GADGET_V2
    assert aliases["pods"] is PODS_META
    assert outcome == Rediscovery(
        removed=("widgets.example.com",), changed=("gadgets.example.com",)
    )


async def test_a_group_version_that_could_not_be_read_keeps_its_kinds() -> None:
    aliases = _aliases(WIDGET, METRIC)
    failed = _outcome(
        group="metrics.k8s.io",
        version="v1beta1",
        complete=False,
        error=ApiStatusError(503, "ServiceUnavailable", "unavailable"),
    )
    kube = Kube(_snapshot(failed))

    outcome = await rediscover_aliases(kube, aliases)

    assert aliases["pods.metrics.k8s.io"] is METRIC
    assert "widgets" not in aliases
    assert outcome == Rediscovery(
        removed=("widgets.example.com",), unreadable=("metrics.k8s.io/v1beta1",)
    )


@pytest.mark.parametrize(
    ("root", "unreadable"),
    [("groups_complete", "the API group list"), ("versions_complete", "the core API version list")],
)
async def test_an_incomplete_version_list_removes_nothing(root: str, unreadable: str) -> None:
    aliases = _aliases(WIDGET)
    snapshot = _snapshot(
        _outcome(GADGET),
        groups_complete=root != "groups_complete",
        versions_complete=root != "versions_complete",
    )
    kube = Kube(snapshot)

    outcome = await rediscover_aliases(kube, aliases)

    assert aliases["widgets"] is WIDGET
    assert aliases["gadgets"] is GADGET
    assert outcome == Rediscovery(added=("gadgets.example.com",), unreadable=(unreadable,))


async def test_a_read_from_an_earlier_session_is_discarded() -> None:
    aliases = _aliases(WIDGET)
    before = dict(aliases)
    kube = Kube(_snapshot(_outcome(GADGET)))
    kube.switch_during_read = True

    outcome = await rediscover_aliases(kube, aliases)

    assert aliases == before
    assert outcome == Rediscovery(applied=False)


async def test_a_failed_core_read_raises_and_keeps_every_kind() -> None:
    aliases = _aliases(WIDGET)
    before = dict(aliases)
    error = ApiStatusError(500, "InternalError", "boom")
    kube = Kube(_snapshot(_outcome(WIDGET), core=_outcome(complete=False, error=error)))

    with pytest.raises(ApiStatusError, match="InternalError"):
        await rediscover_aliases(kube, aliases)

    assert aliases == before


async def test_operators_opens_the_package_catalog_once_it_is_served() -> None:
    aliases = _aliases()
    kube = Kube(_snapshot(_outcome(PACKAGES)))

    await rediscover_aliases(kube, aliases)

    assert aliases["operators"] is PACKAGES


OTHER_WIDGET = ResourceMeta("Widget", "widgets", "other.example.org", "v1", True, ("wd",))


async def test_a_new_kind_never_takes_an_alias_another_kind_holds() -> None:
    aliases = _aliases(WIDGET)
    # The other group is advertised first, so a fresh map would give it `widgets`.
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET), _outcome(WIDGET)))

    await rediscover_aliases(kube, aliases)

    assert aliases["widgets"] is WIDGET
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET


async def test_an_alias_of_a_kind_no_longer_served_is_not_given_to_another() -> None:
    aliases = _aliases(WIDGET)
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))

    await rediscover_aliases(kube, aliases)

    assert "widgets" not in aliases
    assert "wd" not in aliases
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET
```

- [ ] **Step 2: Implement it.** Move the startup discovery's `:operators` aliasing into `_alias_operator_catalog`, then add:

```python
def _alias_operator_catalog(aliases: dict[str, ResourceMeta]) -> None:
    """Where OLM serves the operator catalog, `:operators` opens it - unless a
    real kind (e.g. OLM v1's Operator) already claims that alias."""
    pkg_meta = aliases.get(f"packagemanifests.{PACKAGES_GROUP}")
    if pkg_meta is not None:
        aliases.setdefault("operators", pkg_meta)


def _unreadable(snapshot: RawDiscovery) -> tuple[str, ...]:
    """What a discovery could not read in full, so its kinds are kept as they were."""
    names = [
        f"{outcome.group}/{outcome.version}" if outcome.group else outcome.version
        for outcome in snapshot.group_versions
        if not outcome.complete
    ]
    if not snapshot.core_root.complete:
        names.append("the core API version list")
    if not snapshot.group_root.complete:
        names.append("the API group list")
    return tuple(names)


def _kept(snapshot: RawDiscovery, meta: ResourceMeta) -> bool:
    """Whether a known kind stays although this discovery did not report it."""
    if not (snapshot.core_root.complete and snapshot.group_root.complete):
        return True
    return any(o.group == meta.group and not o.complete for o in snapshot.group_versions)


def _stable_aliases(
    known: dict[str, ResourceMeta], fresh: dict[str, ResourceMeta], snapshot: RawDiscovery
) -> dict[str, ResourceMeta]:
    """`fresh`, with every alias kept on the resource it already named.

    An open view, its watch and its rows are keyed by an alias, so moving one
    to another API group mid-session would make describe or a write reach a
    different resource than the rows show. A known alias keeps its resource,
    at the version now served; one whose resource is gone is dropped, never
    handed to another; only an alias nobody held goes to a new resource.
    """
    served = {meta.identity: meta for meta in fresh.values()}
    stable: dict[str, ResourceMeta] = {}
    for alias, meta in known.items():
        if meta.identity in served:
            stable[alias] = served[meta.identity]
        elif _kept(snapshot, meta):
            stable[alias] = meta
    for alias, meta in fresh.items():
        if alias not in known:
            stable.setdefault(alias, meta)
    return stable


async def rediscover_aliases(kube: KubeClient, aliases: dict[str, ResourceMeta]) -> Rediscovery:
    """Read discovery afresh and make the session's aliases match it (#517).

    Unlike the startup merge, a kind the cluster no longer serves is dropped
    and one served at a new preferred version replaces the old, except where
    discovery could not be read in full: those kinds are kept. An alias never
    moves to another resource, as `_stable_aliases` says. A read that a
    context switch overtook changes nothing. A failed core or `/apis` read
    raises, as `project_resources` does, and changes nothing either.
    """
    generation = kube.session_generation
    kube.invalidate_discovery()
    snapshot = await kube.discovery_snapshot()
    if generation != kube.session_generation or snapshot.generation != generation:
        return Rediscovery(applied=False)
    fresh = _stable_aliases(
        aliases,
        build_alias_map(
            [PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META, *project_resources(snapshot)]
        ),
        snapshot,
    )
    _alias_operator_catalog(fresh)
    before = {meta.identity: meta for meta in aliases.values() if not meta.synthetic}
    after = {meta.identity: meta for meta in fresh.values() if not meta.synthetic}
    aliases.clear()
    aliases.update(fresh)
    return Rediscovery(
        added=tuple(sorted(m.qualified_name for i, m in after.items() if i not in before)),
        removed=tuple(sorted(m.qualified_name for i, m in before.items() if i not in after)),
        changed=tuple(
            sorted(
                m.qualified_name
                for i, m in after.items()
                if i in before and before[i].version != m.version
            )
        ),
        unreadable=_unreadable(snapshot),
    )
```

- [ ] **Step 3:** Run `uv run --frozen pytest -p no:tach tests/test_rediscovery.py tests/test_resource_wiring.py -q`. Expected: all pass. Then commit.

### Task 3: Routing and wiring

- [ ] Add tests: the router's `FakeApiResources` cases in `tests/ui/test_command_router.py`, `test_composed_command_reasons_ask_the_api_resources_owner_about_rediscover`, the coordinator's cancel order, the worker-failure label `Reading API resources failed`, and `test_wire_and_run_rediscovers_through_the_session_client_and_aliases`.
- [ ] Give the router an `ApiResourceCommands` collaborator, `_rediscover` for `BuiltinOperation.REDISCOVER`, and ask it first in `route_unknown`:

```python
    def route_unknown(self, command: UnknownCommand) -> None:
        """Read the API again for it, explain a missing operator catalog, or report it.

        A kind the cluster started serving since the last discovery resolves
        once the API resources are read again: the owner then posts the
        command again, marked `rediscovered` if it is still unknown.
        """
        text = command.text
        api_resources = self._api_resources
        if (
            not command.rediscovered
            and api_resources is not None
            and api_resources.resolve_unknown(text)
        ):
            return
        parts = text.strip().split()
        head = parts[0] if parts else ""
        args = parts[1:]
        if head == "operators" and len(args) <= 1 and self._operators.explain_missing_catalog():
            return
        self._report_unknown(text)
```

- [ ] Add `rediscover` to `compose_command_reasons`; `cancel_worker_group(API_RESOURCES_GROUP)` after the creation group; `rediscover: Rediscover | None` on `KorvidApp` and `AppRuntimeInputs`. In `__main__.py`, pass `rediscover=lambda: rediscover_aliases(kube, aliases)` to `KorvidApp` and build the controller with `known=lambda word: view.canonical_kind(word) if word in view.aliases() else None`, `post=app.post_message`, `aliases_updated=app.on_aliases_updated`, `epoch=context.epoch` and `reads_allowed=context.reads_allowed`.
- [ ] Run the router, policy, coordinator, wiring and palette tests, then commit.

### Task 4: End to end

Create `tests/ui/test_api_resources_workflow.py`:

```python
"""A kind the cluster starts serving during a session, end to end (#517)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from korvid.composition_support import rediscover_aliases
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import GroupVersionOutcome, RawDiscovery, RootOutcome
from korvid.k8s.models import GenericSummary
from korvid.ui.api_resources_controller import Rediscovery
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


async def _source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
    if kind == "widgets":
        yield ("ADDED", GenericSummary("blue", "default", "Widget", "", uid="uid-blue"))
    await asyncio.Event().wait()


def _app(cluster: Cluster) -> Any:
    store = ResourceStore()
    aliases = build_alias_map([PODS_META])

    async def rediscover() -> Rediscovery:
        return await rediscover_aliases(cluster, aliases)

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
```

### Task 5: Document it, then run the full gate

- [ ] `docs/tui.md`: under "Read the cockpit", how a kind installed during the session opens. Add `docs/keybindings.md`'s `:rediscover` row and a release-notes entry.
- [ ] Run `make check`. Expected: every step passes.
