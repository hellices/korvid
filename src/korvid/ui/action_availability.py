"""Typed availability values shared by the Action Palette (issue #388).

`ActionPolicy.binding_enabled` (issue #388 task 1) answers a single bool per
action; the palette needs to say *why* an invokable-but-currently-unusable
entry can't run right now (e.g. "select a node first") so it can still be
shown, searchable, with its reason attached, rather than disappearing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from korvid.ui.ui_surface import Severity


class AvailabilityCode(Enum):
    """Machine-readable reason an otherwise-bound action can't run now."""

    WRONG_VIEW = "wrong_view"
    NO_SELECTION = "no_selection"
    READ_ONLY = "read_only"
    MISSING_CAPABILITY = "missing_capability"
    PANE_CLOSED = "pane_closed"
    UNSUPPORTED_RESOURCE = "unsupported_resource"
    PROTECTED_UI = "protected_ui"
    TRANSITION = "transition"


@dataclass(frozen=True, slots=True)
class UnavailableReason:
    """Human-readable explanation paired with its machine-readable code."""

    code: AvailabilityCode
    message: str
    severity: Severity = "warning"


@dataclass(frozen=True, slots=True)
class ActionAvailability:
    """Whether an action's binding is enabled, and why it can't run if not."""

    binding_enabled: bool
    reason: UnavailableReason | None = None

    @property
    def invokable(self) -> bool:
        """Whether the palette should let the user run this entry now."""
        return self.reason is None

    @classmethod
    def enabled(cls) -> ActionAvailability:
        """An always-invokable availability, with no reason attached."""
        return cls(binding_enabled=True)


#: The one wording for "there is no Agent in this composition". The bound
#: `toggle_agent` key and the `:ai`/`:model` command rows are refused for
#: the same reason - the [agent] extra is not installed or was disabled -
#: so they say it identically (issue #388 task 6).
AGENT_UNAVAILABLE = UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, "Agent is not available")

#: The one wording for "a `:ctx` switch is in flight". Every flow that spawns
#: a cluster stream refuses during a switch (issue #84), and each of their
#: palette probes reports it, so the notification the real keypress emits
#: (`ContextSwitchCoordinator.reads_allowed`) and the silent reason the
#: probes return are the same string by construction (issue #388 task 4).
CONTEXT_SWITCH_IN_PROGRESS = UnavailableReason(
    AvailabilityCode.TRANSITION,
    "A context switch is in progress — try again once it completes",
)
