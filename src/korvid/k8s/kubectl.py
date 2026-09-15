"""`kubectl` presence, resolved once per session (issue #388 round 13).

korvid shells out to `kubectl` for the flows the API alone cannot serve -
`exec`, `debug`, `port-forward` - and both owners refuse up front when the
binary is absent. Answering that question is a PATH scan (`shutil.which`
stats a candidate per directory), and the Action Palette asks it on every
catalog derivation, which happens on a keystroke: one row for the shell
key, one for the forward dialog, rebuilt each time the modal opens and
again after it closes. The availability contract forbids a probe from
doing I/O at all, so the session takes one snapshot and every later
question - probe or keypress - is answered from memory.

A `kubectl` that appears on (or vanishes from) PATH *while korvid runs* is
therefore not observed until the next start. That is the deliberate trade:
the alternative is a filesystem scan per rendered row.

Pure layer: stdlib only, no Textual.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable


def kubectl_on_path() -> bool:
    """Whether a `kubectl` binary is reachable on PATH right now."""
    return shutil.which("kubectl") is not None


class KubectlPresence:
    """The session's one answer to "is `kubectl` installed?".

    Constructed by the composition root and injected into every owner that
    asks - so the shell flows, the forward dialog and their palette probes
    all read the same fact, and none of them can drift from another by
    looking again at a different moment.

    Constructing it performs no lookup: the snapshot is taken by the first
    question, which in a running TUI is startup (the first keypress or
    catalog derivation), and never retaken.
    """

    __slots__ = ("_detect", "_present")

    def __init__(self, detect: Callable[[], bool] = kubectl_on_path) -> None:
        """Bind the detection this session will run exactly once.

        Args:
            detect: How to look for the binary. Injected so a test can
                decide the session's answer without a real PATH.
        """
        self._detect = detect
        self._present: bool | None = None

    def __call__(self) -> bool:
        """The session's snapshot, taken on the first call."""
        present = self._present
        if present is None:
            present = self._present = self._detect()
        return present
