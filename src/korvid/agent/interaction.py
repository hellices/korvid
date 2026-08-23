"""Immutable interaction contracts for the agent UI bridge."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TypeAlias


def _require_nonblank(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    """Stable identity for a selected Kubernetes resource."""

    kind: str
    namespace: str | None
    name: str
    uid: str | None


@dataclass(frozen=True, slots=True)
class PaneContext:
    """Visible state for one workspace pane."""

    kind: str
    scope: str
    filter_pattern: str | None
    selected: ResourceIdentity | None


@dataclass(frozen=True, slots=True)
class ClusterFacts:
    """Immutable cluster facts visible to the agent."""

    provider: str
    distribution: str | None


@dataclass(frozen=True, slots=True)
class InteractionContext:
    """Snapshot of the human-visible agent workspace."""

    kube_context: str | None
    context_epoch: int
    focused_pane: PaneContext
    secondary_pane: PaneContext | None
    timeline_cursor: str | None


@dataclass(frozen=True, slots=True)
class Navigate:
    """Change the primary resource view."""

    view: str
    namespace: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.view, "view")


@dataclass(frozen=True, slots=True)
class SetFilter:
    """Set or clear the active filter."""

    filter_pattern: str | None = None

    def __post_init__(self) -> None:
        if self.filter_pattern is not None:
            _require_nonblank(self.filter_pattern, "filter_pattern")


@dataclass(frozen=True, slots=True)
class SelectResource:
    """Select a specific resource in the focused pane."""

    kind: str
    name: str
    namespace: str | None = None
    uid: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.kind, "kind")
        _require_nonblank(self.name, "name")


@dataclass(frozen=True, slots=True)
class FocusPane:
    """Move focus to one of the two workspace panes."""

    index: int

    def __post_init__(self) -> None:
        if self.index not in {0, 1}:
            raise ValueError("index must be 0 or 1")


@dataclass(frozen=True, slots=True)
class OpenLogs:
    """Open logs for an exact pod target."""

    pod: str
    namespace: str
    container: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.pod, "pod")
        _require_nonblank(self.namespace, "namespace")


@dataclass(frozen=True, slots=True)
class OpenDescribe:
    """Open the describe pane for an exact resource target."""

    kind: str
    name: str
    namespace: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.kind, "kind")
        _require_nonblank(self.name, "name")


@dataclass(frozen=True, slots=True)
class DrillDown:
    """Drill into a named item."""

    name: str

    def __post_init__(self) -> None:
        _require_nonblank(self.name, "name")


@dataclass(frozen=True, slots=True)
class OpenEvidence:
    """Open evidence linked to an exact reference."""

    ref: str

    def __post_init__(self) -> None:
        _require_nonblank(self.ref, "ref")


UiAction: TypeAlias = (
    Navigate
    | SetFilter
    | SelectResource
    | FocusPane
    | OpenLogs
    | OpenDescribe
    | DrillDown
    | OpenEvidence
)


@dataclass(frozen=True, slots=True)
class UiActionResult:
    """Outcome of a typed UI action."""

    ok: bool
    message: str
    context: InteractionContext


class AgentUiBridge(ABC):
    """Bidirectional seam between agent logic and the live workspace."""

    @abstractmethod
    def snapshot(self) -> InteractionContext:
        """Return the current human-visible workspace state."""

    @abstractmethod
    async def apply(self, action: UiAction) -> UiActionResult:
        """Apply one typed action to the workspace."""
