"""User-triggered resource and node write workflows (issue #187).

`ResourceWriteController` owns the flows a keybinding raises against the
selected row: delete, rollout restart, the `$EDITOR` round-trip, scale, the
in-place pod resize, cordon/uncordon, and drain - together with the drain's
own mutable lifecycle state, because pressing the drain key again must find
and cancel the worker it started.

What it does **not** own is any part of the write security perimeter. Every
approval dialog, every post-await revalidation, the synchronous in-flight
reservation, the fail-closed intent audit and the audited mutation belong to
`WriteCoordinator`, which this controller composes workflows *out of* and can
never route around: it holds no `WriteOps` mutation path of its own that does
not pass through `WriteGate.confirm` / `WriteGate.run`. The `WriteOps` handle
it does hold is used for two things only - server-side dry-run previews and
the drain plan, both read-only - and to build the operation *factories* the
coordinator constructs after approval.

It reaches Textual through `UiSurface`, reads the view through `ViewState`,
and never imports or holds `KorvidApp`, so every workflow is exercised
without a running app. The app keeps the Textual action handlers as thin
delegates.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import shlex
import subprocess
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import yaml
from textual.app import SuspendNotSupported

from korvid.core.impact import ImpactAction
from korvid.core.resize_impact import classify_pod_resize
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.drain import DrainPlan
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.k8s.olm import OPERATORS_GROUP
from korvid.k8s.writes import WriteOps, restart_stamp
from korvid.tools.write_coordinator import RESTARTABLE as RESTARTABLE
from korvid.tools.write_coordinator import SCALABLE as SCALABLE
from korvid.ui.drain import DrainController
from korvid.ui.node_impact_preview import (
    compose_node_maintenance_lines,
    render_node_maintenance_lines,
)
from korvid.ui.resize_impact_preview import compose_resize_impact_lines
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.confirm_screen import ReplicasPrompt
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.write_coordinator import WriteCoordinator, WriteOrigin, gvr_label, write_locus

logger = logging.getLogger(__name__)


#: `KorvidApp._get_manifest`: (kind alias, namespace, name) -> manifest.
ManifestFetcher = Callable[[str, str | None, str], Awaitable[dict[str, Any]]]

#: The injected editor seam (tests replace the external editor with it).
EditText = Callable[[str], Awaitable[str | None]]

#: Ownership banner lookups (issue #119). Display support, fail-open: both
#: return None rather than block a write.
ManagedNote = Callable[[str, str | None, str], Awaitable[str | None]]
ManagedNoteFrom = Callable[[dict[str, Any], str | None], Awaitable[str | None]]


def _yaml_equal(a: object, b: object) -> bool:
    """Type-sensitive structural equality for parsed YAML documents.
    Python's ``==`` conflates YAML booleans and integers (``True == 1``),
    and comparing ``yaml.safe_dump`` output is not canonical either: shared
    nodes are emitted as anchors/aliases, so an aliased-but-equal document
    would falsely report a change. Compare recursively instead, requiring
    identical scalar types (including mapping keys)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) != len(b):
            return False
        # Fast path for the overwhelmingly common case of string-keyed
        # mappings: direct lookup is O(n), and str-to-str comparison has
        # no cross-type conflation. Kubernetes objects can be large (a
        # ConfigMap may carry thousands of data keys), so the structural
        # scan below must not run on every comparison.
        if all(isinstance(k, str) for k in a) and all(isinstance(k, str) for k in b):
            return all(key in b and _yaml_equal(value, b[key]) for key, value in a.items())
        # Unusual YAML key types: key lookup would conflate True/1 the same
        # way == does, so match key/value pairs structurally. Quadratic,
        # but such mappings are rare and rejected upstream for manifests.
        b_items = list(b.items())
        return all(
            any(
                _yaml_equal(a_key, b_key) and _yaml_equal(a_value, b_value)
                for b_key, b_value in b_items
            )
            for a_key, a_value in a.items()
        )
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_yaml_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def resize_summary(resources: dict[str, dict[str, dict[str, str]]]) -> str:
    """One-line 'app: requests.cpu=200m, limits.memory=1Gi; ...' summary
    shown in the approval dialog and recorded in the audit detail.

    Module-level because the agent's resize tool builds the same operation
    line from the same shape: one phrasing, so the dialog a user approves
    and the one an agent proposes cannot drift apart.
    """
    parts = []
    for container, sections in resources.items():
        changes = ", ".join(
            f"{section}.{quantity}={value}"
            for section, values in sections.items()
            for quantity, value in values.items()
        )
        parts.append(f"{container}: {changes}")
    return "; ".join(parts)


class OperatorUninstalls(Protocol):
    """The two OLM entry points a delete may have to redirect into.

    Structural, and only these two: deleting an OLM Subscription on its own
    leaves the CSV - and therefore the operator - running, so `delete` hands
    those rows to the operator workflow instead of mutating them itself. It
    must not gain the ability to drive the install wizard from here.
    """

    async def uninstall(
        self,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        fetch_kind: str,
        ctx: tuple[ResourceMeta, str | None, str],
    ) -> None:
        """Uninstall the operator behind the Subscription (issue #117)."""

    async def csv_uninstall_redirect(
        self, csv_meta: ResourceMeta, ns: str | None, name: str
    ) -> bool:
        """Whether a CSV delete was redirected to the full uninstall."""


class CancellableWork(Protocol):
    """The half of a Textual `Worker` the drain lifecycle needs.

    Only "is it still going?" and "stop it". Holding the `Worker` itself
    would hand a controller `wait()`, the worker's node and its app, which
    is app access routed around `UiSurface`.
    """

    @property
    def is_running(self) -> bool:
        """Whether the work is pending or running."""

    def cancel(self) -> None:
        """Request cancellation."""


@dataclasses.dataclass(frozen=True)
class WriteTarget:
    """What a write flow captured before its first await.

    Every gate after that point compares against *these* values, never
    against a re-read of the view: the RBAC round-trip, a prompt, a dry run,
    an editor session and the approval dialog are all gaps in which the
    selection can move, the workspace can be split or re-scoped, and a
    `:ctx` switch can retarget the whole session.
    """

    meta: ResourceMeta
    namespace: str | None
    name: str
    uid: str | None
    #: Context epoch when the flow began; a switch during any gap aborts it.
    epoch: int
    #: The pane the flow was raised from, and the scope it was showing.
    origin: WriteOrigin
    #: The alias that named the target's kind when it was captured - the
    #: banner and the ownership lookup must describe the row the user acted
    #: on, not whatever the focused pane shows later.
    kind_alias: str

    @property
    def label(self) -> str:
        """`plural.group/name`, as every rejection message spells it."""
        return f"{gvr_label(self.meta)}/{self.name}"


@dataclasses.dataclass(frozen=True)
class ScaleRequest:
    """A captured scale target plus the counts that classify the request.

    `current` is part of the request because it is what makes it a decrease
    at all: it decides whether the blast radius is loaded and it is the
    number the approval line reads `replicas <old> -> <new>` from.
    """

    target: WriteTarget
    current: int | None
    replicas: int


@dataclasses.dataclass(frozen=True)
class ResizeRequest:
    """A captured pod-resize target, the requested resources, and the
    manifest snapshot the prompt was prefilled from (reused for the
    ownership banner and the resize classification instead of a second GET).
    """

    target: WriteTarget
    resources: dict[str, dict[str, dict[str, str]]]
    pod_manifest: dict[str, Any]


class ResourceWriteController:
    """Composes the resource and node write workflows over one perimeter."""

    def __init__(
        self,
        *,
        writes: WriteCoordinator,
        view: ViewState,
        ui: UiSurface,
        drain: DrainController,
        #: Late-binding: a `:ctx` switch retargets the write client, the
        #: manifest source and the editor seam after construction.
        write_ops: Callable[[], WriteOps | None],
        get_manifest: Callable[[], ManifestFetcher | None],
        edit_text: Callable[[], EditText | None],
        managed_note: ManagedNote,
        managed_note_from: ManagedNoteFrom,
        pod_resize_supported: Callable[[], bool],
        helm_uninstall: Callable[[], None],
        operators: OperatorUninstalls,
    ) -> None:
        self._writes = writes
        self._view = view
        self._ui = ui
        self._drain = drain
        self._write_ops = write_ops
        self._get_manifest = get_manifest
        self._edit_text = edit_text
        self._managed_note = managed_note
        self._managed_note_from = managed_note_from
        self._pod_resize_supported = pod_resize_supported
        self._helm_uninstall = helm_uninstall
        self._operators = operators
        #: The in-flight drain worker, if any - pressing the drain key again
        #: cancels it (evictions stop; the node stays cordoned).
        self._drain_worker: CancellableWork | None = None
        #: The node that worker is draining. Cleared last, while the worker
        #: is still finalizing, so the targeted-cancel and cordon-refusal
        #: guards can still see it.
        self._drain_node: str | None = None

    # ------------------------------------------------------------------
    # Drain lifecycle, observable but not mutable from outside
    # ------------------------------------------------------------------

    @property
    def drain_worker(self) -> CancellableWork | None:
        """The in-flight drain worker, or None when no drain has started."""
        return self._drain_worker

    @property
    def drain_node_name(self) -> str | None:
        """The node currently being drained, or None."""
        return self._drain_node

    # ------------------------------------------------------------------
    # Target capture
    # ------------------------------------------------------------------

    def _capture(self) -> WriteTarget | None:
        """Resolve and pin the selected row for a write flow."""
        resolved = self._writes.write_target()
        return None if resolved is None else self._pin(resolved)

    def _pin(self, resolved: tuple[ResourceMeta, str | None, str, str | None]) -> WriteTarget:
        """Pin a resolved target to the epoch, pane and alias of *now*.

        Called before the flow's first await, because everything after that
        reads "whichever pane is focused now" on "whichever cluster is
        current now".
        """
        meta, ns, name, uid = resolved
        return WriteTarget(
            meta=meta,
            namespace=ns,
            name=name,
            uid=uid,
            epoch=self._writes.epoch(),
            origin=self._writes.write_origin(),
            kind_alias=self._view.canonical_kind(self._view.current_kind()),
        )

    def node_target(self, action: str) -> tuple[WriteOps, ResourceMeta, str, str | None] | None:
        """Resolve the selected node for a node op, or None (with a
        notification) when writes are disabled, nothing is selected, or the
        current view is not the nodes view."""
        ops = self._write_ops()
        if ops is None:
            self._ui.notify(f"{action} unavailable in this session", severity="warning")
            return None
        target = self._writes.write_target()
        if target is None:
            return None
        meta, _, name, uid = target
        if (meta.group, meta.plural) != ("", "nodes"):
            self._ui.notify(f"{action} does not apply to {gvr_label(meta)}", severity="warning")
            return None
        return ops, meta, name, uid

    def _node_write_target(self, action: str) -> tuple[WriteOps, WriteTarget] | None:
        """`node_target` as a captured `WriteTarget` (epoch, origin, alias)."""
        resolved = self.node_target(action)
        if resolved is None:
            return None
        ops, meta, name, uid = resolved
        return ops, WriteTarget(
            meta=meta,
            namespace=None,
            name=name,
            uid=uid,
            epoch=self._writes.epoch(),
            origin=self._writes.write_origin(),
            kind_alias=self._view.canonical_kind(self._view.current_kind()),
        )

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete(self) -> None:
        """Ctrl-D: delete the selected resource behind a layered confirmation
        (cluster-scoped kinds require typing the resource name). On the helm
        release browser the key means `helm uninstall` (issue #117) - helm
        must remove the release's own bookkeeping, a raw Secret delete would
        orphan the deployed resources."""
        current = self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
        if current is not None and (current.group, current.plural) == (
            HELM_RELEASES_META.group,
            HELM_RELEASES_META.plural,
        ):
            self._helm_uninstall()
            return
        ops = self._write_ops()
        if ops is None:
            self._ui.notify("Delete unavailable in this session", severity="warning")
            return
        resolved = self._writes.write_target()
        if resolved is None:
            return
        if await self._delete_redirected(resolved):
            return
        # Pinned *after* the redirect checks: `csv_uninstall_redirect` awaits
        # a lookup, and the epoch and pane this flow revalidates against must
        # belong to the flow that actually continues.
        target = self._pin(resolved)
        meta, ns, name, uid = target.meta, target.namespace, target.name, target.uid
        if not await self._writes.precheck_keybinding_write("delete", meta, ns, name):
            return
        preview = await self._writes.dry_run_preview(ops.preview_delete(meta, ns, name, uid=uid))
        note = await self._managed_note(target.kind_alias, ns, name)
        if not self._writes.context_intact(
            "delete", meta, ns, name, phase="the dry-run preview", epoch=target.epoch
        ):
            return
        # The snapshot is another awaited gap: a `:ctx` switch, a moved
        # selection, a re-focused or re-scoped pane, or a same-named
        # replacement during it must abort before a dialog describes the row
        # the user is no longer on (issue #283).
        impact = await self._writes.impact_preview(
            ImpactAction.DELETE, meta, ns, name, uid, origin=target.origin
        )
        if not self._writes.identity_intact(
            "delete",
            meta,
            ns,
            name,
            uid,
            phase="the impact summary",
            epoch=target.epoch,
            origin=target.origin,
        ):
            return
        await self._writes.confirm(
            f"Delete {target.label}?",
            f"DELETE {target.label}{write_locus(ns)}",
            action="delete",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.delete_object(meta, ns, name, uid=uid),
            require_name=None if meta.namespaced else name,
            preview=preview,
            managed_note=note,
            impact_lines=impact,
        )

    async def _delete_redirected(
        self, resolved: tuple[ResourceMeta, str | None, str, str | None]
    ) -> bool:
        """Whether an OLM row's delete was handed to the operator workflow.

        Deleting a Subscription alone leaves the operator running (the CSV
        stays), and deleting a CSV alone lets OLM reinstall it - both offer
        the full uninstall instead.
        """
        meta, ns, name, uid = resolved
        if (meta.group, meta.plural) == (OPERATORS_GROUP, "subscriptions"):
            await self._operators.uninstall(
                meta,
                ns,
                name,
                uid,
                fetch_kind=self._view.canonical_kind(self._view.current_kind()),
                ctx=(meta, ns, name),
            )
            return True
        if (meta.group, meta.plural) == (OPERATORS_GROUP, "clusterserviceversions"):
            return await self._operators.csv_uninstall_redirect(meta, ns, name)
        return False

    # ------------------------------------------------------------------
    # Rollout restart
    # ------------------------------------------------------------------

    async def rollout_restart(self) -> None:
        """r: rolling restart of the selected deployment/statefulset/daemonset."""
        ops = self._write_ops()
        if ops is None:
            self._ui.notify("Rollout restart unavailable in this session", severity="warning")
            return
        target = self._capture()
        if target is None:
            return
        meta, ns, name, uid = target.meta, target.namespace, target.name, target.uid
        if (meta.group, meta.plural) not in RESTARTABLE:
            self._ui.notify(
                f"rollout restart does not apply to {gvr_label(meta)}", severity="warning"
            )
            return
        if not await self._writes.precheck_keybinding_write("rollout_restart", meta, ns, name):
            return
        # One stamp per approval: the previewed request and the executed
        # write are byte-identical (exact-replay guarantee).
        stamp = restart_stamp()
        preview = await self._writes.dry_run_preview(
            ops.preview_rollout_restart(meta, ns, name, uid=uid, restarted_at=stamp)
        )
        note = await self._managed_note(target.kind_alias, ns, name)
        if not self._writes.context_intact(
            "rollout_restart", meta, ns, name, phase="the dry-run preview", epoch=target.epoch
        ):
            return
        # Same awaited-gap revalidation as delete - see `delete`.
        impact = await self._writes.impact_preview(
            ImpactAction.ROLLOUT_RESTART, meta, ns, name, uid, origin=target.origin
        )
        if not self._writes.identity_intact(
            "rollout_restart",
            meta,
            ns,
            name,
            uid,
            phase="the impact summary",
            epoch=target.epoch,
            origin=target.origin,
        ):
            return
        await self._writes.confirm(
            f"Rollout restart {target.label}?",
            f"PATCH {target.label} pod template (restartedAt annotation){write_locus(ns)}",
            action="rollout_restart",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.rollout_restart_with_stamp(
                meta, ns, name, uid=uid, restarted_at=stamp
            ),
            preview=preview,
            managed_note=note,
            impact_lines=impact,
        )

    # ------------------------------------------------------------------
    # Edit ($EDITOR round-trip)
    # ------------------------------------------------------------------

    async def edit(self) -> None:
        """e: open the selected resource's manifest in $EDITOR and PUT the
        edited version back (kubectl edit parity)."""
        ops = self._write_ops()
        if ops is None or self._get_manifest() is None:
            self._ui.notify("Edit unavailable in this session", severity="warning")
            return
        target = self._capture()
        if target is None:
            return
        meta, ns, name, uid = target.meta, target.namespace, target.name, target.uid
        if not await self._writes.precheck_keybinding_write("edit", meta, ns, name):
            return
        label = target.label
        manifest = await self._fetch_manifest_for_edit(label, target)
        if manifest is None:
            return
        original_text = yaml.safe_dump(manifest, sort_keys=False)
        edit = self._edit_text() or self.edit_in_external_editor
        edited = self._parse_edited_manifest(
            label, manifest, original_text, await edit(original_text)
        )
        if edited is None:
            return
        # The editor round-trip is arbitrarily long: re-validate that the
        # same row is still selected before pushing the confirmation.
        if not self._writes.context_intact(
            "edit", meta, ns, name, phase="the editor session", epoch=target.epoch
        ):
            return
        detail = self._edit_detail(manifest, edited)
        # The pre-edit manifest is already in hand — the banner costs at
        # most the owner-chain walk. That walk is another awaited gap:
        # re-validate the selection after it, like every other pre-dialog
        # await, before pushing the confirmation.
        note = await self._managed_note_from(manifest, ns)
        if not self._writes.context_intact(
            "edit", meta, ns, name, phase="the ownership lookup", epoch=target.epoch
        ):
            return
        await self._writes.confirm(
            f"Apply edited {label}?",
            # Issue #21: the approval dialog summarizes the change, not
            # just the target and verb.
            f"PUT {label}{write_locus(ns)} - {detail}",
            action="edit",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.replace_object(meta, ns, name, edited, uid=uid),
            detail=detail,
            managed_note=note,
        )

    async def _fetch_manifest_for_edit(
        self, label: str, target: WriteTarget
    ) -> dict[str, Any] | None:
        """Fetch the manifest for an edit; None (with a notification) aborts.
        The fetch is another awaited round-trip: a selection change while it
        was in flight must abort before the editor opens for a stale target,
        not merely discard the completed edit afterwards."""
        get_manifest = self._get_manifest()
        if get_manifest is None:
            return None
        try:
            manifest = await get_manifest(target.kind_alias, target.namespace, target.name)
        except Exception as exc:
            self._ui.notify(f"edit {label} failed: {exc}", severity="error")
            return None
        if not self._writes.context_intact(
            "edit",
            target.meta,
            target.namespace,
            target.name,
            phase="the manifest fetch",
            epoch=target.epoch,
        ):
            return None
        # managedFields is server-side bookkeeping noise; kubectl edit hides
        # it too. resourceVersion stays so concurrent modifications 409.
        metadata = manifest.get("metadata")
        if isinstance(metadata, dict):
            metadata.pop("managedFields", None)
        return manifest

    def _parse_edited_manifest(
        self,
        label: str,
        original: dict[str, Any],
        original_text: str,
        edited_text: str | None,
    ) -> dict[str, Any] | None:
        """Validate the editor output; None (with a notification) aborts the
        edit. Re-injects the fetched resourceVersion if the user deleted it -
        an unversioned PUT would silently clobber concurrent changes."""
        if edited_text is None:
            self._ui.notify(f"edit {label} cancelled", severity="warning")
            return None
        if edited_text == original_text:
            self._ui.notify(f"edit {label}: no changes", severity="information")
            return None
        try:
            parsed = yaml.safe_load(edited_text)
        except yaml.YAMLError as exc:
            self._ui.notify(f"edit {label} aborted: invalid YAML: {exc}", severity="error")
            return None
        if not isinstance(parsed, dict):
            self._ui.notify(f"edit {label} aborted: not a mapping", severity="error")
            return None
        if any(not isinstance(key, str) for key in parsed):
            # YAML legally allows non-string mapping keys, but a manifest
            # never has them and the change summary sorts keys together.
            self._ui.notify(f"edit {label} aborted: non-string top-level key", severity="error")
            return None
        self._restore_resource_version(original, parsed)
        if _yaml_equal(parsed, original):
            self._ui.notify(f"edit {label}: no changes", severity="information")
            return None
        return parsed

    @staticmethod
    def _restore_resource_version(original: dict[str, Any], parsed: dict[str, Any]) -> None:
        """Put back the fetched resourceVersion *before* the semantic no-op
        comparison: an edit that only deleted it is still "no changes", and
        `metadata: null` must not defeat the restore - an unversioned PUT
        would silently clobber concurrent changes."""
        original_meta = original.get("metadata")
        rv = original_meta.get("resourceVersion") if isinstance(original_meta, dict) else None
        if rv is None:
            return
        parsed_meta = parsed.get("metadata")
        if not isinstance(parsed_meta, dict):
            parsed_meta = {}
            parsed["metadata"] = parsed_meta
        # Not setdefault: a blank `resourceVersion:` loads as None - the
        # key is present but the PUT would still be unversioned.
        edited_rv = parsed_meta.get("resourceVersion")
        if not (isinstance(edited_rv, str) and edited_rv):
            parsed_meta["resourceVersion"] = rv

    @staticmethod
    def _edit_detail(original: dict[str, Any], edited: dict[str, Any]) -> str:
        """Audit detail: which top-level sections changed. Key presence is
        checked separately (dict.get returns None for both an absent key and
        a present null key) and values compare YAML-canonically."""
        changed = sorted(
            key
            for key in set(original) | set(edited)
            if (key in original) != (key in edited)
            or not _yaml_equal(original.get(key), edited.get(key))
        )
        return "changed: " + ", ".join(changed)

    async def edit_in_external_editor(self, text: str) -> str | None:
        """Suspend the TUI and open $VISUAL/$EDITOR (vi fallback) on a temp
        file; None cancels. Invocation and I/O failures (missing executable,
        malformed quoting, temp-dir exhaustion, undecodable editor output)
        abort with a notification instead of an unhandled action error. The
        blocking call runs in a thread so background tasks keep running
        while the editor is open."""
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        try:
            fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="korvid-edit-")
        except OSError as exc:
            self._ui.notify(f"edit temp file failed: {exc}", severity="error")
            return None
        try:
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                argv = shlex.split(editor)
                if not argv:
                    # A whitespace-only $VISUAL/$EDITOR passes the fallback
                    # expression but yields no executable to run.
                    raise ValueError("empty editor command")
                argv.append(tmp)
                with self._ui.suspend():
                    code = await asyncio.to_thread(subprocess.call, argv)
            except SuspendNotSupported:
                # Windows and other non-suspending drivers: cancel with a
                # notification instead of an unhandled action error.
                self._ui.notify(
                    "edit unavailable: this environment does not support"
                    " suspending the TUI for an external editor",
                    severity="error",
                )
                return None
            except (OSError, ValueError) as exc:
                self._ui.notify(f"editor {editor!r} failed: {exc}", severity="error")
                return None
            self._ui.refresh()
            if code != 0:
                return None
            try:
                # Explicit UTF-8: a locale mismatch or binary editor output
                # raises UnicodeDecodeError (a ValueError, not an OSError).
                return Path(tmp).read_text(encoding="utf-8")
            except (OSError, ValueError) as exc:
                self._ui.notify(f"editor result unreadable: {exc}", severity="error")
                return None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    # ------------------------------------------------------------------
    # Scale
    # ------------------------------------------------------------------

    async def scale(self) -> None:
        """S: scale the selected deployment/replicaset/statefulset (prompt, then confirm)."""
        ops = self._write_ops()
        if ops is None:
            self._ui.notify("Scale unavailable in this session", severity="warning")
            return
        target = self._capture()
        if target is None:
            return
        meta, ns, name = target.meta, target.namespace, target.name
        if (meta.group, meta.plural) not in SCALABLE:
            self._ui.notify(f"scale does not apply to {gvr_label(meta)}", severity="warning")
            return
        # Read before the RBAC await, from the row the target was taken
        # from: the decrease decision must be about that incarnation, not
        # about whatever the store holds once the prompt closes.
        current = self._writes.current_replicas(ns, name)
        if not await self._writes.precheck_keybinding_write("scale", meta, ns, name):
            return
        if not self._scale_intact(target, current, phase="the permission check"):
            return

        def _on_replicas(replicas: int | None) -> None:
            if replicas is None:
                return
            # The dry-run round trip must not run inside a screen callback:
            # a worker fetches the preview, revalidates, then confirms.
            self._ui.run_worker(
                self._confirm_scale(ScaleRequest(target=target, current=current, replicas=replicas))
            )

        await self._ui.push_screen(ReplicasPrompt(target.label, current=current), _on_replicas)

    def _scale_intact(self, target: WriteTarget, current: int | None, *, phase: str) -> bool:
        """The scale flow's gate: identity plus the captured replica count."""
        return self._writes.scale_identity_intact(
            target.meta,
            target.namespace,
            target.name,
            target.uid,
            current,
            phase=phase,
            epoch=target.epoch,
            origin=target.origin,
        )

    async def _confirm_scale(self, request: ScaleRequest) -> None:
        """Dry-run preview + approval dialog for a scale, after the replica
        count is known. Revalidates the selection after the preview round
        trip: keystrokes during the await must never land on a confirmation
        for a different row. The captured `kind_alias` drives the ownership
        banner — it must describe the row the user acted on, not the current
        view.

        A *known decrease* additionally loads the advisory blast radius
        (issue #295); a scale-up, a no-op, or a row with no readable desired
        count has no tested scale-down semantics and gets no section and no
        LIST fan-out. `current` is part of what is revalidated because it is
        what makes this request a decrease at all: it decides whether the
        blast radius is loaded and it is the number the approval line reads
        `replicas <old> -> <new>` from.

        The dialog this pushes is itself an awaited gap, and the longest
        one, so the same gate is handed to `WriteCoordinator.confirm` as its
        `approval_guard` and runs once more on approval - see there for why
        it is deferred rather than run inside the result callback."""
        ops = self._write_ops()
        if ops is None:
            return
        target, current, replicas = request.target, request.current, request.replicas
        meta, ns, name, uid = target.meta, target.namespace, target.name, target.uid
        # The count prompt is the flow's own awaited gap, and the one a user
        # can hold open indefinitely: gate before the dry-run round trip, so
        # a selection, pane, scope, context or replica-count change made
        # while the modal was up costs no API call at all.
        if not self._scale_intact(target, current, phase="the replica count prompt"):
            return
        preview = await self._writes.dry_run_preview(
            ops.preview_scale(meta, ns, name, replicas, uid=uid)
        )
        note = await self._managed_note(target.kind_alias, ns, name)
        # Gate before the snapshot, not only after it. The count prompt and
        # this dry-run round trip are two awaited gaps of their own, and the
        # snapshot is a LIST fan-out across every source in the catalog:
        # once the selection, the pane, its scope, the context or the count
        # the request was classified against has drifted the flow is already
        # doomed, so korvid must not spend that fan-out (nor scope it to a
        # pane the user has left).
        if not self._scale_intact(target, current, phase="the dry-run preview"):
            return
        is_scale_down = self._writes.is_scale_down(current, replicas)
        # The snapshot is another awaited gap — see `delete`.
        impact = (
            await self._writes.impact_preview(
                ImpactAction.SCALE_DOWN, meta, ns, name, uid, origin=target.origin
            )
            if is_scale_down
            else None
        )
        if is_scale_down and not self._scale_intact(target, current, phase="the impact summary"):
            return

        shown = "?" if current is None else current
        await self._writes.confirm(
            f"Scale {target.label}?",
            f"PATCH {target.label}/scale: replicas {shown} -> {replicas}{write_locus(ns)}",
            action="scale",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.scale_object(meta, ns, name, replicas, uid=uid),
            detail=f"replicas -> {replicas}",
            preview=preview,
            managed_note=note,
            impact_lines=impact,
            # The dialog is the flow's last - and longest - awaited gap: it
            # stays up until the user answers. Everything the gates above
            # compared can move while it does, and `current` in particular
            # is what the operation line the user is reading says the object
            # is changing *from*. So the approval is re-validated against
            # the same captured values once more, after the modal is gone
            # and before any worker, reservation, audit record or operation
            # exists.
            approval_guard=lambda: self._scale_intact(
                target, current, phase="the confirmation dialog"
            ),
        )

    # ------------------------------------------------------------------
    # In-place pod resize
    # ------------------------------------------------------------------

    def _resize_pod_target(self) -> tuple[WriteOps, WriteTarget] | None:
        ops = self._write_ops()
        if ops is None:
            self._ui.notify("Resize unavailable in this session", severity="warning")
            return None
        target = self._capture()
        if target is None:
            return None
        if (target.meta.group, target.meta.plural) != ("", "pods"):
            self._ui.notify(
                f"resize does not apply to {gvr_label(target.meta)}", severity="warning"
            )
            return None
        if not self._pod_resize_supported():
            self._ui.notify(
                "This cluster does not expose pods/resize (requires Kubernetes 1.35+)",
                severity="warning",
            )
            return None
        return ops, target

    async def resize_pod(self) -> None:
        """R: in-place resize of the selected pod (prompt, then confirm).

        Only offered on the pods view and only when discovery found the
        pods/resize subresource (Kubernetes 1.35 GA)."""
        resolved = self._resize_pod_target()
        if resolved is None:
            return
        _ops, target = resolved
        meta, ns, name, uid = target.meta, target.namespace, target.name, target.uid
        if not await self._writes.precheck_keybinding_write("resize", meta, ns, name):
            return
        if not self._resize_intact(target, phase="the permission check"):
            return
        fetched = await self._pod_container_resources(ns, name)
        if fetched is None:
            return
        containers, pod_manifest = fetched
        if not self._writes.uid_intact_after_fetch(pod_manifest, ns, name, uid):
            self._ui.notify(
                f"resize {target.label} cancelled - the pod changed during the manifest fetch",
                severity="warning",
            )
            return
        if not self._resize_intact(target, phase="the manifest fetch"):
            return

        def _on_resources(resources: dict[str, dict[str, dict[str, str]]] | None) -> None:
            if not resources:
                return
            # The dry-run round trip must not run inside a screen callback:
            # a worker fetches the preview, revalidates, then confirms.
            self._ui.run_worker(
                self._confirm_resize(
                    ResizeRequest(target=target, resources=resources, pod_manifest=pod_manifest)
                )
            )

        await self._ui.push_screen(ResizePrompt(target.label, containers=containers), _on_resources)

    def _resize_intact(self, target: WriteTarget, *, phase: str) -> bool:
        return self._writes.identity_intact(
            "resize",
            target.meta,
            target.namespace,
            target.name,
            target.uid,
            phase=phase,
            epoch=target.epoch,
            origin=target.origin,
        )

    async def _pod_container_resources(
        self, ns: str | None, name: str
    ) -> tuple[list[tuple[str, dict[str, dict[str, str]]]], dict[str, Any]] | None:
        """Current per-container requests/limits from the live manifest, in
        spec order, to prefill the resize prompt — plus the manifest itself,
        so the ownership banner can reuse the snapshot instead of a second
        GET. None (with a notification) when the manifest cannot be
        fetched."""
        get_manifest = self._get_manifest()
        if get_manifest is None:
            self._ui.notify("Resize unavailable: no manifest source", severity="warning")
            return None
        try:
            manifest = await get_manifest("pods", ns, name)
        except Exception as exc:
            self._ui.notify(f"Could not fetch pod manifest: {exc}", severity="error")
            return None
        containers: list[tuple[str, dict[str, dict[str, str]]]] = []
        for spec in manifest.get("spec", {}).get("containers", []):
            resources = {
                section: dict(values)
                for section, values in spec.get("resources", {}).items()
                if section in ("requests", "limits") and isinstance(values, dict)
            }
            containers.append((str(spec.get("name", "")), resources))
        if not containers:
            self._ui.notify("Pod manifest lists no containers", severity="warning")
            return None
        return containers, manifest

    async def _confirm_resize(self, request: ResizeRequest) -> None:
        """Dry-run preview + approval dialog for an in-place pod resize.
        Revalidates the selection after the preview round trip: keystrokes
        during the await must never land on a confirmation for a different
        row. The request's `pod_manifest` is the snapshot the prompt was
        prefilled from — the banner reuses it instead of refetching the same
        object."""
        ops = self._write_ops()
        if ops is None:
            return
        target, resources = request.target, request.resources
        meta, ns, name, uid = target.meta, target.namespace, target.name, target.uid
        namespace = ns or ""
        if not self._resize_intact(target, phase="the resize prompt"):
            return
        preview = await self._writes.dry_run_preview(
            ops.preview_resize(namespace, name, resources, uid=uid)
        )
        note = await self._managed_note_from(request.pod_manifest, ns)
        if not self._resize_intact(target, phase="the dry-run preview"):
            return
        context = classify_pod_resize(request.pod_manifest, resources)
        graph_lines = await self._writes.impact_preview(
            ImpactAction.POD_RESIZE, meta, ns, name, uid, origin=target.origin
        )
        impact_lines = compose_resize_impact_lines(graph_lines, context)
        if not self._resize_intact(target, phase="the impact preview"):
            return
        summary = resize_summary(resources)
        await self._writes.confirm(
            f"Apply in-place pod resize to pods/{name}?",
            f"PATCH pods/{name}/resize: {summary}{write_locus(ns)}",
            action="resize",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.resize_pod(namespace, name, resources, uid=uid),
            detail=summary,
            preview=preview,
            managed_note=note,
            impact_lines=impact_lines,
            approval_guard=lambda: self._resize_intact(target, phase="the confirmation dialog"),
        )

    # ------------------------------------------------------------------
    # Node cordon / uncordon
    # ------------------------------------------------------------------

    async def cordon(self) -> None:
        """c: mark the selected node unschedulable (kubectl cordon parity)."""
        await self._cordon_action(unschedulable=True)

    async def uncordon(self) -> None:
        """u: mark the selected node schedulable again (kubectl uncordon)."""
        await self._cordon_action(unschedulable=False)

    async def _cordon_action(self, *, unschedulable: bool) -> None:
        """Shared cordon/uncordon flow: SSAR pre-check, dry-run preview,
        approval dialog, audited write (issue #40)."""
        action = "cordon" if unschedulable else "uncordon"
        resolved = self._node_write_target(action)
        if resolved is None:
            return
        ops, target = resolved
        meta, name, uid = target.meta, target.name, target.uid
        worker = self._drain_worker
        if worker is not None and worker.is_running and name == self._drain_node:
            # Uncordoning (or re-cordoning) mid-drain would let new pods
            # schedule behind the drain's back; the drain owns the node's
            # schedulable state until it finishes or is cancelled.
            self._ui.notify(
                f"nodes/{name} is being drained - cancel the drain first",
                severity="warning",
            )
            return
        if not await self._writes.precheck_keybinding_write(action, meta, None, name):
            return
        if not self._node_intact(action, target, phase="the permission check"):
            return
        preview = await self._writes.dry_run_preview(
            ops.preview_cordon(name, unschedulable, uid=uid)
        )
        if not self._node_intact(action, target, phase="the dry-run preview"):
            return
        impact_action = ImpactAction.CORDON_NODE if unschedulable else ImpactAction.UNCORDON_NODE
        flag = "true" if unschedulable else "false"
        await self._writes.confirm(
            f"{action.capitalize()} nodes/{name}?",
            f"PATCH nodes/{name} spec.unschedulable={flag}",
            action=action,
            meta=meta,
            namespace=None,
            name=name,
            op_factory=lambda: ops.cordon_node(name, unschedulable, uid=uid),
            detail=f"spec.unschedulable={flag}",
            preview=preview,
            impact_lines=render_node_maintenance_lines(impact_action),
            approval_guard=lambda: self._node_intact(
                action, target, phase="the confirmation dialog"
            ),
        )

    def _node_intact(self, action: str, target: WriteTarget, *, phase: str) -> bool:
        return self._writes.identity_intact(
            action,
            target.meta,
            None,
            target.name,
            target.uid,
            phase=phase,
            epoch=target.epoch,
            origin=target.origin,
        )

    # ------------------------------------------------------------------
    # Node drain
    # ------------------------------------------------------------------

    async def drain_node(self) -> None:
        """shift+d: drain the selected node behind a typed-name approval
        showing the PDB-aware impact plan (issue #40). Pressing the key
        again while a drain is running cancels it: no further evictions are
        issued and the node stays cordoned."""
        if self._cancel_running_drain():
            return
        resolved = self._node_write_target("drain")
        if resolved is None:
            return
        ops, target = resolved
        meta, name, uid = target.meta, target.name, target.uid
        if not await self._writes.precheck_keybinding_write("drain", meta, None, name):
            return
        try:
            plan = await ops.drain_plan(name)
        except Exception as exc:
            self._ui.notify(
                f"drain nodes/{name} aborted: could not compute the impact plan: {exc}",
                severity="error",
            )
            return
        if not self._node_intact("drain", target, phase="the drain plan"):
            return

        graph_lines = await self._writes.impact_preview(
            ImpactAction.DRAIN_NODE, meta, None, name, uid, origin=target.origin
        )
        impact_lines = compose_node_maintenance_lines(graph_lines, ImpactAction.DRAIN_NODE)
        if not self._node_intact("drain", target, phase="the impact preview"):
            return

        def _done(confirmed: bool | None) -> None:
            if not confirmed or not self._node_intact(
                "drain", target, phase="the confirmation dialog"
            ):
                return
            self._drain_node = name
            # `reserved` counts the drain against `:ctx` switching from the
            # moment the coroutine is built; the worker handle stays here so
            # pressing the drain key again can cancel it.
            self._drain_worker = self._ui.run_worker(
                self._writes.reserved(lambda: self._run_drain(ops, meta, name, uid, plan))
            )

        blocked_now = sum(1 for t in plan.targets if t.pdb_blocked is not None)
        note = f"; {blocked_now} currently PDB-blocked" if blocked_now else ""
        await self._ui.push_screen(
            self._writes.confirm_screen(
                f"Drain nodes/{name}?",
                f"Cordon nodes/{name}, then attempt eviction of {len(plan.targets)} pods"
                f" via the Eviction API{note}"
                " (press the drain key again to cancel mid-drain)",
                require_name=name,
                preview=plan.preview_lines(),
                preview_title="drain impact plan:",
                impact_lines=impact_lines,
            ),
            _done,
        )

    def _cancel_running_drain(self) -> bool:
        """Whether this key press was a cancel of the running drain.

        Cancelling is a targeted act: another node (or a same-named row in
        another view - the binding is global) being selected must not
        silently kill the running drain (issue #40 review). True also when
        the press was refused, because either way no new drain starts.
        """
        worker = self._drain_worker
        if worker is None or not worker.is_running:
            return False
        _, selected = self._view.selected_ns_name()
        kind_meta = self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
        on_nodes = kind_meta is not None and (kind_meta.group, kind_meta.plural) == ("", "nodes")
        if self._drain_node is not None and (not on_nodes or selected != self._drain_node):
            self._ui.notify(
                f"drain of nodes/{self._drain_node} in progress"
                " - press the drain key on it to cancel",
                severity="warning",
            )
            return True
        worker.cancel()
        return True

    async def _run_drain(
        self,
        ops: WriteOps,
        meta: ResourceMeta,
        name: str,
        uid: str | None,
        plan: DrainPlan,
    ) -> None:
        """Delegate the approved drain to `DrainController` (issue #97 U3d).

        `drain_node` wraps this in `WriteCoordinator.reserved` (so the drain
        is counted against `:ctx` switching from the moment the coroutine is
        built) and keeps worker ownership, because pressing the drain key
        again must be able to cancel it.
        """
        try:
            await self._drain.run(ops, meta, name, uid, plan)
        finally:
            # Last: while the worker is finalizing (outcome audit/notify)
            # the targeted-cancel guard must still see its node.
            self._drain_node = None
