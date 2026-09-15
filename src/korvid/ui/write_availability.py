"""Why a resource or node write cannot run right now (issue #388).

`ResourceWriteController` owns the write *flows*; this module owns their
side-effect-free twin: the probe the Action Palette asks before it offers a
row, and the same question a flow answers by refusing with a notification.
Keeping both halves in one place is what keeps them from drifting - every
refusal here is phrased with the wording the matching keypress already
uses, and the shared tables (`RESTARTABLE`, `SCALABLE`) are the single
source the footer legend, the agent's own checks and these probes all read.

Pure Python: no Textual, no `WriteOps` mutation path, no notification. The
seams arrive as plain callables, so probing can never resolve a target the
flow would not resolve, nor tell the user anything - `write_target` is
asked with `notify=False`, exactly once per probe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.ui.action_availability import AvailabilityCode, UnavailableReason
from korvid.ui.view_state import ViewState
from korvid.ui.write_coordinator import WriteCoordinator, gvr_label

#: Workload eligibility is keyed on (group, plural): a custom-group CRD whose
#: plural collides with a built-in (e.g. 'deployments') must never be treated
#: as an apps/* workload. `ActionPolicy._ACTION_VIEWS` gates the keys on the
#: same identities, so the footer legend and the flow agree.
RESTARTABLE: frozenset[tuple[str, str]] = frozenset(
    {("apps", "deployments"), ("apps", "statefulsets"), ("apps", "daemonsets")}
)
SCALABLE: frozenset[tuple[str, str]] = frozenset(
    {("apps", "deployments"), ("apps", "replicasets"), ("apps", "statefulsets")}
)

#: Palette action -> the label the matching flow uses in its own "<Action>
#: unavailable in this session" refusal when this session has no write
#: client. One table, so a probe can never invent wording the keypress does
#: not use (issue #388 task 4).
_WRITE_CLIENT_LABELS: dict[str, str] = {
    "delete_resource": "Delete",
    "rollout_restart": "Rollout restart",
    "edit_resource": "Edit",
    "scale_resource": "Scale",
    "resize_pod": "Resize",
}

#: Palette action -> the word the matching node flow passes to
#: `node_target()`, which is also the word its refusals are phrased with.
#: Private: no caller outside this module needs the action->word mapping
#: itself, only the reason it produces.
_NODE_ACTIONS: dict[str, str] = {
    "cordon_node": "cordon",
    "uncordon_node": "uncordon",
    "drain_node": "drain",
}

#: `WriteCoordinator.write_target`'s resolved row: (meta, namespace, name, uid).
ResolvedTarget = tuple[ResourceMeta, str | None, str, str | None]


@dataclass(frozen=True, slots=True)
class WriteAvailability:
    """The write flows' refusals, answered without running anything.

    Constructed by `ResourceWriteController` from the seams that controller
    was itself injected with, so the probe reads exactly the state the flow
    reads: the same coordinator, the same view, the same late-bound write
    client and manifest source.
    """

    writes: WriteCoordinator
    view: ViewState
    #: Whether this session has a write client at all. The flows read one
    #: before resolving a target, so the probe asks first too.
    write_client_available: Callable[[], bool]
    #: Whether the `$EDITOR` round-trip has a manifest to fetch.
    manifest_source_available: Callable[[], bool]
    pod_resize_supported: Callable[[], bool]
    #: `HelmController`'s own missing-binary refusal, injected rather than
    #: redeclared, so the helm-delete exception below cannot invent a
    #: second wording for it (#388 task 4 review).
    helm_cli_unavailable_reason: Callable[[], UnavailableReason | None]
    #: The node an in-flight drain is still evicting from, or None when no
    #: drain is running. Owned by the controller, because the drain worker
    #: is its mutable lifecycle state, and read live rather than captured:
    #: a drain can start, finish or be cancelled long after this value was
    #: constructed. The *node*, not a per-name predicate, because the drain
    #: key's own answer depends on which node is draining and not only on
    #: whether the selected one is (#388 round 6).
    draining_node: Callable[[], str | None]

    def unavailable_reason(self, action: str) -> UnavailableReason | None:
        """Why `action` can't run right now, or None - a side-effect-free
        probe for the palette (issue #388). Shares `WriteCoordinator`'s
        owner checks (read-only, missing audit, unknown/synthetic kind,
        silent selection) via `write_target(notify=False)`, so it never
        resolves or notifies twice: the same call this method makes to
        probe is the one `_capture()` makes to dispatch, just silenced.

        The session's write client comes first, exactly as every flow reads
        it before resolving a target (#388 task 4): without one the
        keypress refuses with "<Action> unavailable in this session", so the
        palette must not advertise the action as invocable. `edit_resource`
        carries a second half of that same refusal: `edit()` also refuses
        when the manifest source is missing, so the probe must check both
        halves with the one wording the handler uses, not merely the write
        client (#388 task 4 review). `resize_pod` needs the manifest source
        too - it prefills the prompt from the live pod - but refuses it
        later and in different words, so that half lives in `_kind_reason`
        where the handler's own order puts it (#388 round 8).

        `delete_resource` on the helm release browser is the one exception:
        Ctrl-D there routes to `helm uninstall` *before* the generic
        `write_target` path (issue #117), so the generic "this is a
        read-only view" refusal must not apply to it - only the
        read-only/audit gate and the missing-helm-binary gate
        `HelmController.gate()` itself enforces.
        """
        if action in _NODE_ACTIONS:
            return self._node_action_reason(_NODE_ACTIONS[action])
        if action == "delete_resource" and self.is_helm_release_view():
            return self._helm_delete_reason()
        label = _WRITE_CLIENT_LABELS.get(action)
        if label is not None and (
            not self.write_client_available()
            or (action == "edit_resource" and not self.manifest_source_available())
        ):
            return UnavailableReason(
                AvailabilityCode.MISSING_CAPABILITY, f"{label} unavailable in this session"
            )
        reason = self.writes.unavailable_reason()
        if reason is not None:
            return reason
        target = self.writes.write_target(notify=False)
        if target is None:
            # Defensive only: `unavailable_reason()` above shares every
            # check `write_target()` makes, so this is unreachable - kept,
            # with the coordinator's own NO_SELECTION wording (not a second
            # invented one), only so mypy can narrow `target` below.
            return UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected")
        return self._kind_reason(action, target[0])

    def _helm_delete_reason(self) -> UnavailableReason | None:
        """Ctrl-D's refusals on the helm release browser, where the key
        means `helm uninstall` rather than a generic delete (issue #117)."""
        reason = self.writes.readonly_or_audit_reason()
        if reason is not None:
            return reason
        reason = self.helm_cli_unavailable_reason()
        if reason is not None:
            return reason
        _, name = self.view.selected_ns_name(notify=False)
        if name is None:
            return UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected")
        return None

    def _kind_reason(self, action: str, meta: ResourceMeta) -> UnavailableReason | None:
        """The "this kind does not take that write" refusals, with each
        flow's own wording."""
        if action == "rollout_restart" and (meta.group, meta.plural) not in RESTARTABLE:
            return UnavailableReason(
                AvailabilityCode.UNSUPPORTED_RESOURCE,
                f"rollout restart does not apply to {gvr_label(meta)}",
            )
        if action == "scale_resource" and (meta.group, meta.plural) not in SCALABLE:
            return UnavailableReason(
                AvailabilityCode.UNSUPPORTED_RESOURCE, f"scale does not apply to {gvr_label(meta)}"
            )
        if action == "resize_pod":
            if (meta.group, meta.plural) != ("", "pods"):
                return UnavailableReason(
                    AvailabilityCode.UNSUPPORTED_RESOURCE,
                    f"resize does not apply to {gvr_label(meta)}",
                )
            if not self.pod_resize_supported():
                return UnavailableReason(
                    AvailabilityCode.UNSUPPORTED_RESOURCE,
                    "This cluster does not expose pods/resize (requires Kubernetes 1.35+)",
                )
            if not self.manifest_source_available():
                # `resize_pod()` prefills its prompt from the live manifest
                # (`_pod_container_resources`), which refuses with exactly
                # this sentence when no manifest source is wired - after the
                # kind and cluster-capability checks above, which is where
                # the handler asks it too (#388 round 8).
                return UnavailableReason(
                    AvailabilityCode.MISSING_CAPABILITY, "Resize unavailable: no manifest source"
                )
        return None

    def node_unavailable_reason(self, action: str) -> UnavailableReason | None:
        """Why `ResourceWriteController.node_target(action)` would refuse
        right now, or None - the silent twin of the notifications that
        method emits, for the palette and for the node-shell owner that
        resolves its target through it (issue #388 task 4). `action` is the
        same word `node_target` is called with ("cordon", "drain", "node
        shell"), so the wording matches the real refusal exactly."""
        return self._node_reason_and_target(action)[0]

    def _node_reason_and_target(
        self, action: str
    ) -> tuple[UnavailableReason | None, ResolvedTarget | None]:
        """`node_unavailable_reason`'s checks, plus the resolved
        `write_target` on a clean pass - so `_node_action_reason` can reuse
        the one resolve for its own drain-in-progress check instead of
        calling `write_target(notify=False)` a second time (#388 task 4
        review)."""
        if not self.write_client_available():
            return (
                UnavailableReason(
                    AvailabilityCode.MISSING_CAPABILITY, f"{action} unavailable in this session"
                ),
                None,
            )
        reason = self.writes.unavailable_reason()
        if reason is not None:
            return reason, None
        target = self.writes.write_target(notify=False)
        if target is None:
            return UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected"), None
        meta = target[0]
        if (meta.group, meta.plural) != ("", "nodes"):
            return (
                UnavailableReason(
                    AvailabilityCode.UNSUPPORTED_RESOURCE,
                    f"{action} does not apply to {gvr_label(meta)}",
                ),
                None,
            )
        return None, target

    def _node_action_reason(self, action: str) -> UnavailableReason | None:
        """`node_unavailable_reason` plus the refusals an in-flight drain
        owns: the drain holds the node's schedulable state until it
        finishes or is cancelled, and the drain key itself is that cancel."""
        if action == "drain":
            return self._drain_action_reason()
        reason, target = self._node_reason_and_target(action)
        if reason is not None:
            return reason
        return None if target is None else self.drain_in_progress_reason(target[2])

    def _drain_action_reason(self) -> UnavailableReason | None:
        """Why the drain key would do nothing right now, in its own order.

        `ResourceWriteController.drain_node` asks about the running drain
        *before* it resolves anything (`_cancel_running_drain`), so this
        does too: while a drain is in flight the key either cancels it -
        the draining node is the selected row - or refuses with
        `other_drain_reason`, and never reaches the write client, selection
        or kind questions below (#388 round 6). Only with no drain running
        are those the answer.
        """
        draining = self.draining_node()
        if draining is None:
            return self._node_reason_and_target("drain")[0]
        if self._selected_node_name() == draining:
            # Pressing the drain key here *is* the cancel, so it runs.
            return None
        return self.other_drain_reason()

    def _selected_node_name(self) -> str | None:
        """The selected row's name while the nodes view is on screen.

        The drain key is bound app-wide, so `_cancel_running_drain` treats
        a same-named row in another view as "not the draining node"; the
        probe reads the selection the same way, silently.
        """
        current = self.view.aliases().get(self.view.canonical_kind(self.view.current_kind()))
        if current is None or (current.group, current.plural) != ("", "nodes"):
            return None
        return self.view.selected_ns_name(notify=False)[1]

    @staticmethod
    def other_drain_reason() -> UnavailableReason:
        """Why the drain key cannot start a drain while another node is
        draining — the palette row's half of that refusal.

        Bounded on purpose (round 8): the draining node's name is cluster
        data, and a real managed-cluster node name alone outruns a
        36-column row. A refused row is disabled, so no keystroke
        highlights or scrolls it and the clipped words are simply lost.
        `other_drain_detail` keeps the name and the instruction for the
        toast the keypress raises, which can hold them.
        """
        return UnavailableReason(AvailabilityCode.PROTECTED_UI, "Another node drain is in progress")

    @staticmethod
    def other_drain_detail(name: str) -> str:
        """The same refusal `_cancel_running_drain` notifies, naming the
        node to press the drain key on instead."""
        return f"drain of nodes/{name} in progress - press the drain key on it to cancel"

    def drain_in_progress_reason(self, name: str) -> UnavailableReason | None:
        """Why cordon/uncordon must wait for an in-flight drain on *name*.

        Also the wording `_cordon_action` notifies with, so the probe and
        the keypress explain the wait identically."""
        if self.draining_node() == name:
            return UnavailableReason(
                AvailabilityCode.PROTECTED_UI,
                f"nodes/{name} is being drained - cancel the drain first",
            )
        return None

    def is_helm_release_view(self) -> bool:
        """Whether the current view is the helm release browser - the one
        synthetic view where Ctrl-D means `helm uninstall`, not a generic
        write (issue #117). Shared by `delete()` and `unavailable_reason()`
        so the two can never disagree on which view gets the exception."""
        current = self.view.aliases().get(self.view.canonical_kind(self.view.current_kind()))
        return current is not None and (current.group, current.plural) == (
            HELM_RELEASES_META.group,
            HELM_RELEASES_META.plural,
        )
