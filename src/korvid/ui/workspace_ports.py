"""Structural ports the workspace flows are wired through.

Extracted from `workspace_controller.py` unchanged: these are declarations,
not behaviour — the watch and metrics lifecycles the navigation flows drive,
the relationship loader `g` runs, the log/hint surfaces navigation touches,
and the subset of a Textual key event the pane chord consumes. They live
beside the controller rather than inside it so the module that owns the
workflows stays about the workflows - and so a module that only needs to
name one of these ports (`write_coordinator`) does not have to import the
controller to get it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from korvid.core.relationships import GraphResource
from korvid.k8s.discovery import ResourceMeta


class WatchLifecycle(Protocol):
    """The watch start/stop surface the workspace flows need (structural)."""

    @property
    def active(self) -> set[tuple[str, str]]: ...

    async def start(self, kind: str, scope: str) -> None: ...

    async def stop(self, kind: str, scope: str) -> None: ...


class MetricsLifecycle(Protocol):
    """The metrics poller start/stop surface (structural)."""

    async def start(self, namespace: str | None) -> None: ...

    async def stop(self) -> None: ...


class RelationshipLoading(Protocol):
    """The bounded relationship-snapshot loader (structural)."""

    async def load(
        self, root: GraphResource, namespace: str | None, aliases: Mapping[str, ResourceMeta]
    ) -> Any: ...


class WorkspaceLogs(Protocol):
    """The log-pane teardown the navigation and close flows trigger."""

    async def close_if_owned_by(self, pane: object) -> None: ...


class WorkspaceHints(Protocol):
    """The pods hint-strip refresh the focus flows trigger."""

    def refresh_for_focus(self) -> None: ...


class KeyEvent(Protocol):
    """The subset of a Textual key event the pane chord consumes.

    `stop`/`prevent_default` mirror `textual.events.Key`'s own signatures
    (an optional bool, a `Message` return) so the real event satisfies this
    structurally while a test can pass a lightweight fake.
    """

    key: str

    def stop(self, stop: bool = ...) -> Any: ...

    def prevent_default(self, prevent: bool = ...) -> Any: ...
