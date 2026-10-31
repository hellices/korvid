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
        focused_pane: Callable[[], object],
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
        self._focused_pane = focused_pane
        self._clock = clock
        self._min_interval = min_interval
        self._running = False
        self._last_start: float | None = None
        #: Each command a read will settle, and the pane it was typed in.
        self._waiting: list[tuple[str, object]] = []

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
            self._waiting.append((text, self._focused_pane()))
            return True
        last = self._last_start
        if last is not None and self._clock() - last < self._min_interval:
            return False
        self._waiting.append((text, self._focused_pane()))
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
        for text, pane in waiting:
            self._post_again(text, pane)

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

    def _post_again(self, text: str, pane: object) -> None:
        """Run `text` again where it was typed; never in a pane focused since."""
        command = parse_command(text, self._known)
        if isinstance(command, UnknownCommand):
            self._post(UnknownCommand(text, rediscovered=True))
        elif self._focused_pane() is pane:
            self._post(command)
        else:
            kind = text.split()[0]
            self._ui.notify(f"{kind} is served now; run :{text} again to open it", markup=False)

    async def cancel_workers(self) -> None:
        """Stop a read holding the old client, before a context switch swaps it."""
        await self._ui.cancel_workers(API_RESOURCES_GROUP)
