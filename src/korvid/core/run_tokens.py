"""Drop a diagnostic's late results by the run token they were read under (#409, S4).

A diagnostic reads in the background, and by the time a read finishes the
user may have switched context, korvid may have reconnected with other
credentials, the diagnostic may have rerun, or the user may have selected
something else. Each read records the `RunToken` it started under, and the
controller applies its result only while that token is still the current
one. There is no cache of read results to invalidate.
"""

from __future__ import annotations

from collections.abc import Callable

from korvid.core.capability import RunToken


def _never() -> bool:
    return False


class RunTracker:
    """One diagnostic controller's runs, and which of their results may be applied.

    The controller supplies three read-only sources: the context-switch
    coordinator's epoch, whether a switch is under way, and `KubeClient`'s
    session generation. It calls `next_run()` when it reruns, and when the
    user changes the selection it explains.
    """

    def __init__(
        self,
        *,
        epoch: Callable[[], int],
        session_generation: Callable[[], int],
        switching: Callable[[], bool] = _never,
    ) -> None:
        self._epoch = epoch
        self._session_generation = session_generation
        self._switching = switching
        self._run_generation = 0

    def next_run(self) -> RunToken:
        """Start a run, making every earlier run's results stale; its token."""
        self._run_generation += 1
        return self._current()

    def is_current(self, token: RunToken | None) -> bool:
        """Whether a result read under `token` may still be applied.

        It may not once the context, the session or the run has changed, nor
        while a context switch is under way, nor without a token at all.
        """
        if token is None or self._run_generation == 0 or self._switching():
            return False
        return token == self._current()

    def apply_if_current(self, token: RunToken | None, apply: Callable[[], object]) -> bool:
        """Call `apply` only if `token` is current; whether it was called."""
        if not self.is_current(token):
            return False
        apply()
        return True

    def _current(self) -> RunToken:
        return RunToken(self._epoch(), self._session_generation(), self._run_generation)
