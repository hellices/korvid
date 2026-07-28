"""Drill-down navigation state: level stack, owner-uid filter, breadcrumb.

The stack is the single source of truth for both the breadcrumb line and
the owner-uid filter applied to the current table. Each level records the
parent row the user drilled through and the child kind now displayed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DrillLevel:
    parent_kind: str  # canonical lowercase plural, e.g. "deployments"
    parent_name: str
    parent_namespace: str
    parent_uid: str
    child_kind: str  # kind displayed after the drill, e.g. "replicasets"


class NavigationStack:
    def __init__(self) -> None:
        self._levels: list[DrillLevel] = []

    @property
    def active(self) -> bool:
        return bool(self._levels)

    @property
    def parent_uid(self) -> str | None:
        """Owner uid the current view is filtered by; None when not drilled."""
        return self._levels[-1].parent_uid if self._levels else None

    @property
    def child_kind(self) -> str | None:
        """Kind currently displayed by the drill; None when not drilled."""
        return self._levels[-1].child_kind if self._levels else None

    def push(self, level: DrillLevel) -> None:
        self._levels.append(level)

    def copy(self) -> NavigationStack:
        """Independent stack starting at the same position (pane split
        clones the focused view; the two drills then evolve separately)."""
        clone = NavigationStack()
        clone._levels = list(self._levels)
        return clone

    def pop(self) -> DrillLevel | None:
        """Remove the top level; the popped parent_kind is the view to show."""
        return self._levels.pop() if self._levels else None

    def clear(self) -> None:
        self._levels.clear()

    def breadcrumb(self) -> str:
        """'deployments/web > replicasets/web-6d9f88 > pods' style trail."""
        if not self._levels:
            return ""
        parts = [f"{lv.parent_kind}/{lv.parent_name}" for lv in self._levels]
        parts.append(self._levels[-1].child_kind)
        return " > ".join(parts)
