"""Layer-boundary interface for the read surface the agent tools use.

The agent's ToolExecutor depends on this ABC (AGENTS.md: interfaces at
layer boundaries are `abc.ABC`), not on `KubeClient` directly, so the
eval harness's scenario-seeded fake can substitute the read path under
strict type checking — a drift between `KubeClient` and the fake becomes
a type error instead of a silent desynchronization.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Any

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HelmReleaseSummary
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary


class ReadOps(abc.ABC):
    """Read-only cluster operations the agent read tools touch."""

    @abc.abstractmethod
    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        """LIST any resource kind and return GenericSummary items."""

    @abc.abstractmethod
    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        """Fetch the raw manifest for a single object; 404 → ApiStatusError."""

    @abc.abstractmethod
    async def list_helm_releases(self, namespace: str | None) -> list[HelmReleaseSummary]:
        """Latest revision per helm release (Secret-parsed, no helm binary)."""

    @abc.abstractmethod
    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return core v1 Events for the involved object."""

    @abc.abstractmethod
    def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[LogLine]:
        """Stream log lines from a pod container, one LogLine per line."""
