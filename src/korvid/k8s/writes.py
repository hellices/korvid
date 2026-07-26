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
from datetime import datetime
from typing import Any

from korvid.k8s.discovery import ResourceMeta


def restart_stamp() -> str:
    """One restartedAt value per approval request. The caller generates it
    once and passes the same stamp to the dry-run preview and to the executed
    write, so the diff shown to the user is byte-identical to what runs
    (exact-replay guarantee, issue #19)."""
    return datetime.now().astimezone().isoformat()


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

    async def rollout_restart_with_stamp(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> None:
        """Timestamp-aware restart hook for exact preview replay (issue #19).
        Non-abstract on purpose: subclasses implementing only the original
        :meth:`rollout_restart` signature keep working - the default drops
        the stamp and delegates. Transports that implement
        :meth:`preview_rollout_restart` should override this so the executed
        write sends the exact ``restarted_at`` value the preview showed."""
        await self.rollout_restart(meta, namespace, name, uid=uid)

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

    # -- Dry-run previews (issue #19). Non-abstract on purpose: transports
    # -- without server-side dryRun support inherit "no preview" and the
    # -- approval dialog falls back to the synthesized operation string.

    async def preview_scale(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        """Diff lines a ``dryRun=All`` scale would produce; None = no preview.
        A ``uid`` must be carried as the same precondition as the real write,
        so the preview replays the exact request being approved."""
        return None

    async def preview_rollout_restart(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> list[str] | None:
        """Diff lines a ``dryRun=All`` restart would produce; None = no preview.
        ``uid`` semantics match :meth:`preview_scale`; ``restarted_at`` must be
        the same stamp the executed write will send (see :func:`restart_stamp`)."""
        return None

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        """Object summary + cascade note after a ``dryRun=All`` delete was
        accepted by the server; None = no preview. ``uid`` semantics match
        :meth:`preview_scale`."""
        return None
