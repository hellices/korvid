# Rediscover API Resources During a Session (#517) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open a kind the cluster started serving during the session, such as a CRD installed after startup, without restarting korvid (#517).

**Architecture:** The composition root gains `rediscover_aliases(kube, aliases, reservations)`. It invalidates the session's discovery, reads it again, and makes the shared alias map match it, keeping what could not be read. `ui/api_resources_controller.py` runs it in one exclusive worker, for `:rediscover` and its palette entry and, rate-limited, before an unknown `:` command is reported. The router asks it first; the command it settles is posted again, marked `rediscovered` if still unknown.

**Tech Stack:** Python 3.11+, Textual, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Bounded:** one read at a time, and commands typed meanwhile wait for it. An unknown command starts a read at most once every 30 seconds; `:rediscover` is not rate-limited.
- **Context-safe:** the context-switch coordinator cancels the worker group before the client swaps. A read whose session generation changed is never applied, and the controller drops a result whose context epoch changed.
- **An alias never moves to another resource:** an open view, its watch and its rows are keyed by an alias, so a refresh keeps every known alias on its resource and drops one whose resource is gone. `AliasReservations` remembers that owner for the rest of the session, so a later refresh does not hand the alias over either, and gives the alias back if the resource is reinstalled; a new session generation (connect or context switch) lifts the reservations. Only an alias nobody held goes to a new resource, and the `:operators` catalog fallback is applied before that rule, not after it.
- **A command runs where it was typed, and only if nothing overtook it:** a settled navigation carries a `NavigationOrigin` (the pane it was typed in, that pane's `nav_gen` then, and the read's context epoch). `deferred_navigation_guard` runs it only while that pane is focused, has not navigated since, and no context switch crossed the epoch, re-checking under the navigation lock; otherwise it says the kind is served now, or nothing if a context switch made it stale. Of the commands one read settles, only the newest typed in each pane may navigate; older ones there are superseded, while every command still unknown is reported. An ordinary navigation calls `navigate_command` exactly as before.
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
from typing import Protocol

from textual.message import Message

from korvid.k8s.errors import ApiStatusError, KubeClientError
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.command import is_builtin_command, parse_command
from korvid.ui.messages import NavigateCommand, NavigationOrigin, UnknownCommand
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
#: The focused pane, and how many navigations it has made.
NavigationPosition = tuple[object, int]


class _Pane(Protocol):
    @property
    def nav_gen(self) -> int: ...


class _Context(Protocol):
    def crossed(self, epoch: int) -> bool: ...


def deferred_navigation_guard(
    message: NavigateCommand, pane: _Pane, context: _Context, notify: Callable[..., object]
) -> tuple[Callable[[], bool], ...] | None:
    """How the app runs `message` in the focused `pane`.

    An ordinary navigation runs as it always has: `()`. One a rediscovery
    settled runs only while it is still current, with a guard the navigation
    re-checks under its lock: its pane is focused and has not navigated since
    the command was typed, and no context switch crossed the read's epoch.
    Otherwise it returns None, after saying the kind is served now unless a
    context switch made the message stale.
    """
    origin = message.origin
    if origin is None:
        return ()

    def current() -> bool:
        return (
            origin.pane is pane
            and origin.generation == pane.nav_gen
            and not context.crossed(origin.epoch)
        )

    if current():
        return (current,)
    if not context.crossed(origin.epoch):
        again = f"run :{message.view} again in the pane you want it in"
        notify(f"{message.view} is served now; {again}", markup=False)
    return None


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
        navigation_position: Callable[[], NavigationPosition],
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
        self._position = navigation_position
        self._clock = clock
        self._min_interval = min_interval
        self._running = False
        self._last_start: float | None = None
        #: Each command a read will settle, and where it was typed.
        self._waiting: list[tuple[str, NavigationPosition]] = []

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
            self._waiting.append((text, self._position()))
            return True
        last = self._last_start
        if last is not None and self._clock() - last < self._min_interval:
            return False
        self._waiting.append((text, self._position()))
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
        # Only the newest command typed in a pane may navigate it: an older
        # one, replayed first, would bump the pane's generation and turn the
        # newer one away as stale.
        newest = {position: index for index, (_, position) in enumerate(waiting)}
        for index, (text, position) in enumerate(waiting):
            self._post_again(text, position, epoch, newest=newest[position] == index)

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

    def _post_again(
        self, text: str, position: NavigationPosition, epoch: int, *, newest: bool
    ) -> None:
        """Run `text` again where it was typed, unless that pane has moved on.

        A command still unknown is reported. A navigation runs only if it is
        the newest command typed in its pane, carrying its `NavigationOrigin`
        for `deferred_navigation_guard` to check when the app handles it.
        """
        command = parse_command(text, self._known)
        if isinstance(command, UnknownCommand):
            self._post(UnknownCommand(text, rediscovered=True))
        elif isinstance(command, NavigateCommand) and newest:
            origin = NavigationOrigin(*position, epoch)
            self._post(NavigateCommand(command.view, command.namespace, origin=origin))

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

from korvid.composition_support import AliasReservations, rediscover_aliases
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
#: A core kind the snapshots below do not serve.
SECRET = ResourceMeta("Secret", "secrets", "", "v1", True)
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

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert kube.calls == ["invalidate", "read"]
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.example.com"] is WIDGET
    assert aliases["helmreleases"] is HELM_RELEASES_META
    assert outcome == Rediscovery(added=("widgets.example.com",))


async def test_a_kind_no_longer_served_is_dropped_and_a_new_version_replaces_the_old() -> None:
    aliases = _aliases(WIDGET, GADGET)
    kube = Kube(_snapshot(_outcome(GADGET_V2)))

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

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

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

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
    aliases = _aliases(WIDGET, SECRET)
    snapshot = _snapshot(
        _outcome(GADGET),
        groups_complete=root != "groups_complete",
        versions_complete=root != "versions_complete",
    )
    kube = Kube(snapshot)

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["widgets"] is WIDGET
    assert aliases["secrets"] is SECRET
    assert aliases["gadgets"] is GADGET
    assert outcome == Rediscovery(added=("gadgets.example.com",), unreadable=(unreadable,))


async def test_a_read_from_an_earlier_session_is_discarded() -> None:
    aliases = _aliases(WIDGET)
    before = dict(aliases)
    kube = Kube(_snapshot(_outcome(GADGET)))
    kube.switch_during_read = True

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases == before
    assert outcome == Rediscovery(applied=False)


async def test_a_failed_core_read_raises_and_keeps_every_kind() -> None:
    aliases = _aliases(WIDGET)
    before = dict(aliases)
    error = ApiStatusError(500, "InternalError", "boom")
    kube = Kube(_snapshot(_outcome(WIDGET), core=_outcome(complete=False, error=error)))

    with pytest.raises(ApiStatusError, match="InternalError"):
        await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases == before


async def test_operators_opens_the_package_catalog_once_it_is_served() -> None:
    aliases = _aliases()
    kube = Kube(_snapshot(_outcome(PACKAGES)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["operators"] is PACKAGES


OTHER_WIDGET = ResourceMeta("Widget", "widgets", "other.example.org", "v1", True, ("wd",))


async def test_a_new_kind_never_takes_an_alias_another_kind_holds() -> None:
    aliases = _aliases(WIDGET)
    # The other group is advertised first, so a fresh map would give it `widgets`.
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET), _outcome(WIDGET)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["widgets"] is WIDGET
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET


async def test_an_alias_of_a_kind_no_longer_served_is_not_given_to_another() -> None:
    aliases = _aliases(WIDGET)
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert "widgets" not in aliases
    assert "wd" not in aliases
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET


OPERATOR = ResourceMeta("Operator", "operators", "operators.operatorframework.io", "v1", False)


async def test_an_alias_whose_resource_went_away_stays_reserved_for_the_session() -> None:
    aliases = _aliases(WIDGET)
    reservations = AliasReservations()
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))

    await rediscover_aliases(kube, aliases, reservations)
    await rediscover_aliases(kube, aliases, reservations)

    assert "widgets" not in aliases
    assert "wd" not in aliases


async def test_a_new_session_lifts_the_reservations() -> None:
    aliases = _aliases(WIDGET)
    reservations = AliasReservations()
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))
    await rediscover_aliases(kube, aliases, reservations)

    kube.generation = 2
    kube.snapshot = _snapshot(_outcome(OTHER_WIDGET), generation=2)
    await rediscover_aliases(kube, aliases, reservations)

    assert aliases["widgets"] is OTHER_WIDGET


async def test_operators_is_not_handed_to_the_catalog_once_its_kind_is_gone() -> None:
    aliases = _aliases(OPERATOR, PACKAGES)
    kube = Kube(_snapshot(_outcome(PACKAGES)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert "operators" not in aliases


async def test_a_reinstalled_resource_gets_its_reserved_aliases_back() -> None:
    aliases = _aliases(WIDGET)
    reservations = AliasReservations()
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))
    await rediscover_aliases(kube, aliases, reservations)

    kube.snapshot = _snapshot(_outcome(OTHER_WIDGET), _outcome(WIDGET))
    await rediscover_aliases(kube, aliases, reservations)

    assert aliases["widgets"] is WIDGET
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET


async def test_a_kind_served_again_takes_the_version_read_even_if_another_version_failed() -> None:
    aliases = _aliases(GADGET)
    failed = _outcome(group="example.com", version="v1", complete=False)
    kube = Kube(_snapshot(_outcome(GADGET_V2), failed))

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["gadgets"] is GADGET_V2
    assert outcome.unreadable == ("example.com/v1",)
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


_Identity = tuple[str, str, bool]


@dataclasses.dataclass
class AliasReservations:
    """Which resource each alias has named in this session (#517).

    An alias stays reserved for its resource after that resource is no longer
    served, so no later rediscovery hands it to another. A new session
    generation, a connect or a context switch, lifts every reservation: the
    switch has already closed the views keyed by them.
    """

    generation: int | None = None
    owners: dict[str, _Identity] = dataclasses.field(default_factory=dict)

    def for_session(self, generation: int) -> dict[str, _Identity]:
        """The reservations of session `generation`, empty for a new one."""
        if generation != self.generation:
            self.generation = generation
            self.owners = {}
        return self.owners


def _stable_aliases(
    known: dict[str, ResourceMeta],
    fresh: dict[str, ResourceMeta],
    snapshot: RawDiscovery,
    owners: dict[str, _Identity],
) -> dict[str, ResourceMeta]:
    """`fresh`, with every alias kept on the resource it has named this session.

    An open view, its watch and its rows are keyed by an alias, so moving one
    to another API group mid-session would make describe or a write reach a
    different resource than the rows show. A known alias keeps its resource,
    at the version now served; one whose resource is gone is dropped and stays
    reserved in `owners`; only an alias nobody held goes to a new resource.
    """
    for alias, meta in known.items():
        owners.setdefault(alias, meta.identity)
    served = {meta.identity: meta for meta in fresh.values()}
    # Every alias whose owner is served, including a resource reinstalled
    # after it went away, names it again at the version now served.
    stable = {alias: served[owner] for alias, owner in owners.items() if owner in served}
    for alias, meta in known.items():
        if alias not in stable and _kept(snapshot, meta):
            stable[alias] = meta
    for alias, meta in fresh.items():
        if owners.setdefault(alias, meta.identity) == meta.identity:
            stable.setdefault(alias, meta)
    return stable


async def rediscover_aliases(
    kube: KubeClient, aliases: dict[str, ResourceMeta], reservations: AliasReservations
) -> Rediscovery:
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
    candidate = build_alias_map(
        [PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META, *project_resources(snapshot)]
    )
    # The catalog's `:operators` follows the same ownership rule as every alias.
    _alias_operator_catalog(candidate)
    fresh = _stable_aliases(aliases, candidate, snapshot, reservations.for_session(generation))
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

- [ ] Add `rediscover` to `compose_command_reasons`; `cancel_worker_group(API_RESOURCES_GROUP)` after the creation group; `rediscover: Rediscover | None` on `KorvidApp` and `AppRuntimeInputs`. In `__main__.py`, create one `alias_reservations = AliasReservations()` for the run, pass `rediscover=lambda: rediscover_aliases(kube, aliases, alias_reservations)` to `KorvidApp`, and build the controller with `known=lambda word: view.canonical_kind(word) if word in view.aliases() else None`, `post=app.post_message`, `aliases_updated=app.on_aliases_updated`, `epoch=context.epoch`, `reads_allowed=context.reads_allowed` and `navigation_position=lambda: (app._pane, app._pane.nav_gen)`; `on_navigate_command` asks `deferred_navigation_guard` before navigating.
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

from korvid.composition_support import AliasReservations, rediscover_aliases
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import GroupVersionOutcome, RawDiscovery, RootOutcome
from korvid.k8s.models import GenericSummary
from korvid.ui.api_resources_controller import Rediscovery
from korvid.ui.messages import NavigateCommand, NavigationOrigin
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
        app.post_message(_stale(app, app._pane, stale))
        # Handled in order: once this one lands, the stale one has been handled.
        app.post_message(NavigateCommand(None, "kube-system"))
        await until(pilot, lambda: app._pane.scope == "kube-system", label="later navigation")

        assert app.current_kind == "pods"


async def test_a_stale_navigation_from_another_pane_says_nothing() -> None:
    app = _app(Cluster())
    async with app.run_test() as pilot:
        stale = app._ctx.epoch() - 1
        app.post_message(_stale(app, object(), stale))
        app.post_message(NavigateCommand(None, "kube-system"))
        await until(pilot, lambda: app._pane.scope == "kube-system", label="later navigation")

        assert not any("is served now" in m for m in _messages(app))


def _stale(app: Any, pane: object, epoch: int) -> NavigateCommand:
    generation = getattr(pane, "nav_gen", 0)
    return NavigateCommand("widgets", origin=NavigationOrigin(pane, generation, epoch))


async def test_a_kind_found_after_a_newer_navigation_does_not_undo_it() -> None:
    cluster = SlowCluster()
    cluster.installed = True
    app = _app(cluster)
    async with app.run_test() as pilot:
        await _command(pilot, "wd")
        await _command(pilot, "ns kube-system")
        await until(pilot, lambda: app._pane.scope == "kube-system", label="newer navigation")

        cluster.release.set()
        await until(
            pilot,
            lambda: (
                "widgets is served now; run :widgets again in the pane you want it in"
                in _messages(app)
            ),
            label="told instead of opened",
        )

        assert (app.current_kind, app._pane.scope) == ("pods", "kube-system")
```

### Task 5: Document it, then run the full gate

- [ ] `docs/tui.md`: under "Read the cockpit", how a kind installed during the session opens. Add `docs/keybindings.md`'s `:rediscover` row and a release-notes entry.
- [ ] Run `make check`. Expected: every step passes.
