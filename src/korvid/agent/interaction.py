"""Immutable interaction contracts for the agent UI bridge."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
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


def pane_filter_matches(
    pattern: str,
    name: str,
    labels: Mapping[str, str] | None = None,
    phase: str | None = None,
) -> bool:
    """Apply the same resource filter semantics the live workspace uses."""
    from korvid.core.filters import parse_filter

    return parse_filter(pattern).matches(name, labels, phase)


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


#: Every typed action an armed tool can produce, and nothing else.
#:
#: `ToolHarness._ui_action` is the only producer in production, keyed on a
#: registry tool's validated dispatch target, so a member here without a
#: tool behind it is an action the model can never call — one the eval
#: bridge and the live bridge would both still implement and nobody would
#: exercise. A new action starts with a registry schema and eval evidence,
#: then joins this union.
UiAction: TypeAlias = Navigate | SetFilter | OpenLogs | OpenDescribe | DrillDown


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
