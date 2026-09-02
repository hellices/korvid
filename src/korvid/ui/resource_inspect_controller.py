"""Read-only resource inspection, extracted from the app (issue #187).

`ResourceInspectController` owns what the user's *look at this* keys do:

- `d` — describe the selected row, and the hierarchy tree's named describe;
- the Secret rule both of those share (values render masked in the
  dedicated viewer, per-key reveal is explicit and audit-logged);
- the provider footer the describe screens carry (issue #30);
- `Enter` on a pod — the container rows and the shell/logs pick;
- `h` — the hint-details overlay behind the two-line strip (issue #34);
- the store lookups those flows share, and the pod-identity guard the
  interactive flows bind an approved action to.

None of it mutates the cluster, so no `WriteGate` appears here. What every
one of these flows *does* need is the guard against acting on a stale
answer: each one awaits (a manifest, an events page), and a `:ctx` switch
or a cursor move during that gap makes the result describe something other
than what the user is looking at. The `ContextGuard` epoch and the uid
re-reads below are that discipline, kept in one place rather than repeated
per keybinding.

The mounted widgets it touches - the table's row cursor and the hint strip
- arrive through the narrow `InspectSurface`; the modals it opens go
through `UiSurface.push_screen`, and `run_worker` ownership stays the
app's.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from korvid.core.audit import AuditLog
from korvid.core.errors import explain_api_error
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import ContainerTrouble, PodSummary
from korvid.ui.hints import EventsFetcher, pod_needs_hint
from korvid.ui.log_controller import StreamLogsFn
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.containers_screen import ContainersScreen, build_container_rows
from korvid.ui.widgets.describe_screen import DescribeScreen, provider_footer_note
from korvid.ui.widgets.hint_detail import HintDetailScreen
from korvid.ui.widgets.secret_screen import SecretScreen
from korvid.ui.workspace_controller import ContextGuard

logger = logging.getLogger(__name__)

#: Upper bound on the hint-overlay events fetch (issue #34): the trouble half
#: comes from the status the app already holds, so a stalled API connection
#: must not delay the overlay past this - the events are marked unavailable
#: instead.
_HINT_EVENTS_TIMEOUT = 3.0


class InspectSurface(ABC):
    """The mounted widgets the inspection flows read and write.

    Two of them: the focused table's row cursor (which row the user is
    actually on *now*, re-read after every await) and the ops hint strip.
    Both are composed and unmounted by the app, so they are reached through
    this surface rather than handed over - a controller holding the widget
    could re-parent or remove it.
    """

    @abstractmethod
    def cursor_row_key(self) -> str | None:
        """Row key under the table cursor, or None (empty table / no cursor).

        Distinct from `WorkspaceSurface.focused_row_key`: this one backs
        background hint/describe flows that can be reached after the table
        has been unmounted (a timer firing during teardown, for instance),
        so implementations must tolerate the widget being gone rather than
        assuming it is mounted."""

    @abstractmethod
    def show_trouble(
        self, trouble: tuple[ContainerTrouble, ...], *, event: str | None = None
    ) -> None:
        """Render the folded two-line hint for the row under the cursor."""

    @abstractmethod
    def clear_hint(self) -> None:
        """Clear the hint strip (no row, or a healthy one)."""


class InspectShell(Protocol):
    """The interactive-session entry point the container pick raises."""

    def run_shell(self, namespace: str, name: str, container: str) -> None: ...


class InspectLogs(Protocol):
    """The log-pane entry point the container pick raises."""

    async def open_pane(self, namespace: str, targets: list[tuple[str, str]]) -> None: ...


class ResourceInspectController:
    """Owns describe, the container pick, and the hint-details overlay."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        view: ViewState,
        context: ContextGuard,
        surface: InspectSurface,
        #: late-bound: both are constructed after this controller, and the
        #: container pick only reaches them once the user has picked.
        shell: Callable[[], InspectShell],
        logs: Callable[[], InspectLogs],
        #: optional: no manifest fetcher means describe reports and stops.
        get_manifest: Callable[
            [], Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None
        ],
        get_events: Callable[[], EventsFetcher | None],
        stream_logs: Callable[[], StreamLogsFn | None],
        target_uid: Callable[[str, str | None, str], Awaitable[str | None]],
        audit: Callable[[], AuditLog | None],
        provider_hint: Callable[[], str | None],
    ) -> None:
        self._ui = ui
        self._view = view
        self._context = context
        self._surface = surface
        self._shell_fn = shell
        self._logs_fn = logs
        self._get_manifest_fn = get_manifest
        self._get_events_fn = get_events
        self._stream_logs_fn = stream_logs
        self._target_uid = target_uid
        self._audit = audit
        self._provider_hint = provider_hint

    # ------------------------------------------------------------------
    # describe
    # ------------------------------------------------------------------

    async def describe_selected(self) -> None:
        """`d` — fetch and display the manifest + events for the selected row."""
        get_manifest = self._get_manifest_fn()
        if get_manifest is None:
            self._ui.notify("Describe unavailable", severity="warning")
            return
        # The fetch would race the client swap and could render either
        # cluster's manifest — refuse up front.
        if not self._context.reads_allowed():
            return
        epoch = self._context.epoch()
        kind = self._view.current_kind()
        namespace, name = self._view.selected_ns_name()
        if namespace is None or name is None:
            return
        ns: str | None = namespace if namespace else None
        try:
            manifest = await get_manifest(kind, ns, name)
        except ApiStatusError as exc:
            self._ui.notify(explain_api_error(exc.status, exc.reason, kind, ns), severity="error")
            return
        except ValueError as exc:
            self._ui.notify(str(exc), severity="error")
            return

        events: list[dict[str, Any]] = []
        get_events = self._get_events_fn()
        # Events are filtered by involvedObject.name only, so restrict to pods
        # to avoid showing events for unrelated objects with the same name.
        if get_events is not None and ns is not None and kind == "pods":
            try:
                events = await get_events.fetch(namespace, name)
            except ApiStatusError as exc:
                # Events are best-effort; surface but still show the manifest.
                self._ui.notify(
                    explain_api_error(exc.status, exc.reason, "events", namespace),
                    severity="warning",
                )

        if self._context.crossed(epoch):
            # The fetches awaited through a context switch: the manifest (or
            # a mixed manifest/events pair) describes the old cluster and
            # must not be pushed over the new session.
            self._ui.notify(
                f"describe {name} cancelled - the kube context changed during the fetch",
                severity="warning",
            )
            return
        await self._push_describe(f"{kind}/{namespace}/{name}", manifest, events)

    async def describe_named(self, kind: str, namespace: str, name: str) -> None:
        """Describe an object named by a hierarchy tree node (no table row
        to read the selection from - `describe_selected`'s selection-bound
        path does not apply)."""
        get_manifest = self._get_manifest_fn()
        if get_manifest is None:
            self._ui.notify("Describe unavailable", severity="warning")
            return
        if not self._context.reads_allowed():
            return
        epoch = self._context.epoch()
        try:
            manifest = await get_manifest(kind, namespace or None, name)
        except ApiStatusError as exc:
            self._ui.notify(
                explain_api_error(exc.status, exc.reason, kind, namespace or None),
                severity="error",
            )
            return
        except ValueError as exc:
            self._ui.notify(str(exc), severity="error")
            return
        if self._context.crossed(epoch):
            return
        title = f"{kind}/{namespace}/{name}" if namespace else f"{kind}/{name}"
        await self._push_describe(title, manifest, [])

    async def _push_describe(
        self, title: str, manifest: dict[str, Any], events: list[dict[str, Any]]
    ) -> None:
        """Open the right viewer for *manifest*.

        The single place the Secret rule lives (spec §5 #9): both describe
        entry points route through here, so a Secret can never reach the
        plain YAML screen by way of a path that forgot to check.
        """
        if manifest.get("kind") == "Secret":
            await self._ui.push_screen(SecretScreen(title, manifest, audit=self._audit()))
            return
        await self._ui.push_screen(
            DescribeScreen(title, manifest, events, footer_note=self.provider_footer(manifest))
        )

    def provider_footer(self, manifest: dict[str, Any]) -> str | None:
        """Describe footer for the user-triggered views (issue #30); the
        agent's own describe renders the identical note."""
        return provider_footer_note(manifest, self._provider_hint())

    # ------------------------------------------------------------------
    # containers
    # ------------------------------------------------------------------

    async def open_containers(self, namespace: str, name: str) -> None:
        """Push the containers screen for a pod; shell/logs run per pick.

        The row fetch and the open screen both span awaited gaps, so the
        context epoch captured here cancels stale picks: a shell or log
        stream started after a completed switch would target the new cluster
        with the old cluster's pod selection.
        """
        epoch = self._context.epoch()
        rows = await self.build_container_rows(namespace, name)
        if not rows:
            self._ui.notify("No containers found for this pod", severity="warning")
            return
        if self._context.crossed(epoch):
            # The row fetch awaited through a context switch: the selection
            # belongs to the old cluster.
            return

        def _on_pick(result: tuple[str, str] | None) -> None:
            if result is None:
                return
            if self._context.crossed(epoch):
                self._ui.notify(
                    f"container action on {name} cancelled - the kube context"
                    " changed while the containers screen was open",
                    severity="warning",
                )
                return
            action, container = result
            if action == "shell":
                self._shell_fn().run_shell(namespace, name, container)
            else:
                if self._stream_logs_fn() is None:
                    self._ui.notify("Log streaming unavailable", severity="warning")
                    return
                self._ui.run_worker(self._logs_fn().open_pane(namespace, [(name, container)]))

        await self._ui.push_screen(ContainersScreen(name, rows), _on_pick)

    async def build_container_rows(
        self, namespace: str, name: str
    ) -> list[tuple[str, str, str, str, str]]:
        """Container rows from the live manifest; store names as fallback."""
        get_manifest = self._get_manifest_fn()
        if get_manifest is not None:
            try:
                manifest = await get_manifest("pods", namespace, name)
            except (ApiStatusError, ValueError) as exc:
                logger.debug("manifest fetch for container list failed: %s", exc)
            else:
                rows = build_container_rows(manifest)
                if rows:
                    return rows
        return [(ctr, "-", "-", "-", "-") for ctr in self.pod_containers(namespace, name)]

    # ------------------------------------------------------------------
    # hint details
    # ------------------------------------------------------------------

    def hint_details(self) -> None:
        """`h` — open the read-only detail overlay for the hinted pod row
        (issue #34): the full trouble list plus recent Warning events -
        everything the two-line strip folded away."""
        if self._view.current_kind() != "pods":
            return
        row_key = self._surface.cursor_row_key()
        if row_key is None:
            return
        summary = self.find_pod_summary(row_key)
        if summary is None or not pod_needs_hint(summary):
            return
        self._ui.run_worker(
            self.open_hint_details(row_key, summary), exclusive=True, group="hint-detail"
        )

    async def open_hint_details(self, row_key: str, summary: PodSummary) -> None:
        """Fetch events best-effort, then push the overlay: the trouble half
        renders even when the events API fails ("unavailable" is stated, not
        conflated with "no events"). The context is revalidated after the
        await - the cursor, view, or screen stack may have changed meanwhile,
        and stale details for the wrong pod are worse than none."""
        events: list[dict[str, Any]] = []
        events_unavailable = False
        get_events = self._get_events_fn()
        if get_events is not None:
            try:
                events = await asyncio.wait_for(
                    get_events.fetch(summary.namespace, summary.name, uid=summary.uid or None),
                    timeout=_HINT_EVENTS_TIMEOUT,
                )
            except Exception:  # events are decoration; trouble alone still helps
                events_unavailable = True
        if self._ui.screen_depth() > 1:  # another dialog opened during the fetch
            return
        if self._view.current_kind() != "pods" or self._surface.cursor_row_key() != row_key:
            return
        fresh = self.find_pod_summary(row_key)
        if fresh is None or fresh.uid != summary.uid:
            return  # deleted or recreated mid-fetch
        if not pod_needs_hint(fresh):
            return  # recovered mid-fetch: the strip is gone, details would be noise
        await self._ui.push_screen(
            HintDetailScreen(
                f"{summary.namespace}/{summary.name}",
                fresh.trouble,
                events,
                events_unavailable=events_unavailable,
            )
        )

    # ------------------------------------------------------------------
    # store lookups and the pod identity guard
    # ------------------------------------------------------------------

    def find_pod_summary(self, row_key: str) -> PodSummary | None:
        """The pods-view summary behind a `namespace/name` row key."""
        parts = row_key.split("/", 1)
        if len(parts) != 2:
            return None
        namespace, name = parts
        for obj in self._view.resources("pods", self._view.current_scope()):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj
        return None

    def find_pod(self, namespace: str, name: str) -> PodSummary | None:
        """The current view's `PodSummary` for a target, or None once it is gone."""
        for obj in self._view.resources(self._view.current_kind(), self._view.current_scope()):
            if obj.namespace == namespace and obj.name == name and isinstance(obj, PodSummary):
                return obj
        return None

    def pod_containers(self, namespace: str, name: str) -> tuple[str, ...]:
        """Container names for a pod from the store, or () when it is gone."""
        summary = self.find_pod(namespace, name)
        return summary.containers if summary is not None else ()

    async def pod_uid_unchanged(
        self, namespace: str, name: str, approved_uid: str | None, *, action: str
    ) -> bool:
        """Re-verify the approved pod incarnation just before `action`
        executes; only the same non-None uid permits the action to proceed.
        """
        if approved_uid is None:
            self._ui.notify(
                f"{action} cancelled - pod {name} could not be verified. "
                "Retry when the cluster is reachable.",
                severity="warning",
            )
            return False
        try:
            current_uid = await self._target_uid("pods", namespace, name)
        except ApiStatusError:
            self._ui.notify(
                f"{action} cancelled - pod {name} no longer exists.",
                severity="warning",
            )
            return False
        if current_uid is None:
            self._ui.notify(
                f"{action} cancelled - pod {name} could not be verified. "
                "Retry when the cluster is reachable.",
                severity="warning",
            )
            return False
        if current_uid != approved_uid:
            self._ui.notify(
                f"{action} cancelled - pod {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return False
        return True
