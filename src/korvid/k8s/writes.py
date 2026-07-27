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
from korvid.k8s.drain import DrainPlan


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
        ``rollout_restart`` signature keep working - the default drops
        the stamp and delegates. Transports that implement
        ``preview_rollout_restart`` should override this so the executed
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

    async def create_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        manifest: dict[str, Any],
    ) -> None:
        """POST a new object onto the collection (OLM install, issue #29).
        Non-abstract on purpose: transports predating the feature keep
        working, and the UI offers install only where discovery found the
        OLM API groups."""
        raise NotImplementedError("this transport does not support object creation")

    async def resize_pod(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> None:
        """In-place resize of a running pod via the ``pods/resize``
        subresource (1.35 GA, issue #27). ``resources`` maps container name
        to its new ``requests``/``limits`` (omitted quantities are kept).
        Non-abstract on purpose: transports predating the feature keep
        working, and the UI offers the action only when discovery says the
        subresource exists (see ``KubeClient.supports_pod_resize``)."""
        raise NotImplementedError("this transport does not support pod resize")

    async def cordon_node(self, name: str, unschedulable: bool, *, uid: str | None = None) -> None:
        """Set or clear ``spec.unschedulable`` on a node (cordon/uncordon,
        issue #40). Non-abstract on purpose: transports predating the
        feature keep working, and the UI reports the action unavailable."""
        raise NotImplementedError("this transport does not support cordon/uncordon")

    async def evict_pod(self, namespace: str, name: str, *, uid: str | None = None) -> None:
        """Evict one pod through the Eviction API (policy/v1). The server
        refuses with 429 when a PodDisruptionBudget has no disruptions left -
        callers surface that instead of hanging. Non-abstract on purpose:
        see ``cordon_node``."""
        raise NotImplementedError("this transport does not support pod eviction")

    async def drain_plan(self, node_name: str) -> DrainPlan:
        """Classify every pod on *node_name* into the eviction plan the
        drain approval dialog shows (see ``korvid.k8s.drain``). A read, but
        it lives here because it is inseparable from the drain write flow:
        the plan is both the impact preview and the execution list.
        Non-abstract on purpose: see ``cordon_node``."""
        raise NotImplementedError("this transport does not support drain planning")

    async def pods_on_node(self, node_name: str) -> tuple[str, ...]:
        """Identifiers (uid, or ``namespace/name`` when the uid is unknown)
        of every evictable pod currently on *node_name*. Used by the
        post-drain termination poll, which only needs presence - the
        default derives from ``drain_plan`` so existing transports keep
        working, while ``KubeClient`` overrides with a single pods list
        (no PDB scans on every poll)."""
        plan = await self.drain_plan(node_name)
        return tuple(t.uid or t.ref for t in plan.targets)

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
        ``uid`` semantics match ``preview_scale``; ``restarted_at`` must be
        the same stamp the executed write will send (see ``restart_stamp``)."""
        return None

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        """Object summary + cascade note after a ``dryRun=All`` delete was
        accepted by the server; None = no preview. ``uid`` semantics match
        ``preview_scale``."""
        return None

    async def preview_resize(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        """Diff lines a ``dryRun=All`` resize would produce; None = no
        preview. ``uid`` semantics match ``preview_scale``."""
        return None

    async def preview_cordon(
        self, name: str, unschedulable: bool, *, uid: str | None = None
    ) -> list[str] | None:
        """Diff lines a ``dryRun=All`` cordon/uncordon patch would produce;
        None = no preview. ``uid`` semantics match ``preview_scale``."""
        return None
