"""Layer-boundary interface for approval-gated cluster writes.

The UI depends on this ABC (AGENTS.md: interfaces at layer boundaries are
``abc.ABC``), not on ``KubeClient`` directly, so tests and alternative
transports can substitute the write path without touching the UI. Every
operation accepts an optional ``uid`` precondition that pins the write to the
exact object incarnation that was approved: the API server answers 409 when
the object was deleted and recreated under the same name meanwhile.
"""

from __future__ import annotations

import abc
from typing import Any

from korvid.k8s.discovery import ResourceMeta


class WriteOps(abc.ABC):
    """Mutating operations the TUI exposes behind approval dialogs."""

    @abc.abstractmethod
    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        """DELETE one object, optionally guarded by a uid precondition."""

    @abc.abstractmethod
    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        """Set spec.replicas via the /scale subresource."""

    @abc.abstractmethod
    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        """Trigger a rolling restart by patching the pod template."""

    @abc.abstractmethod
    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        """PUT-replace the whole object with an edited manifest."""
