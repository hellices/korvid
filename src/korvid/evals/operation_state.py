"""Mutable fake cluster state plus the complete operation-eval `WriteOps`.

`StatefulFakeKubeClient` keeps the existing deep-copy read semantics and
shares one private mutable object store with `StatefulFakeWriteOps`, so a
write the production app executes is visible to the next authoritative
read. There is no generic patch/apply surface: only the operations the
Slice A pack grades exist, and everything else fails closed as an
`ApiStatusError` so the production app audits and reports a failure. A
plain return would have been audited as success, and a
`NotImplementedError` would have escaped the app's `ApiStatusError`
handling — neither is an honest answer.

The write fake also observes the production audit boundary: an injected
`audit_intent_probe` re-reads the real audit file immediately before each
mutation, so the fail-closed ordering is provable from persisted evidence
instead of from a subclassed or wrapped `AuditLog`.

Source-checkout evaluation code: imports `korvid.evals` and `korvid.k8s` only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, NoReturn

from korvid.evals.fake_kube import FakeKubeClient
from korvid.evals.operation import OPERATION_GOAL_KINDS, OperationCluster, walk_path
from korvid.evals.operation_journal import ActionJournal, JournalTarget, summarize
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.drain import DrainPlan
from korvid.k8s.dryrun import diff_manifests
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import WriteOps, restart_stamp

__all__ = [
    "RESTART_ANNOTATION",
    "AuditIntentProbe",
    "AuditRecord",
    "FakeClusterState",
    "StatefulFakeKubeClient",
    "StatefulFakeWriteOps",
    "parse_audit_records",
]

#: The annotation `kubectl rollout restart` writes; the fake stamps the
#: identical key so a live calibration compares like with like.
RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"

_SCALABLE_KINDS = OPERATION_GOAL_KINDS["scale"]
_RESTARTABLE_KINDS = OPERATION_GOAL_KINDS["rollout_restart"]
_FAKE = "operation eval fake"
_POD = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_NODE = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))


@dataclass(frozen=True)
class AuditRecord:
    """One parsed line of the real `korvid.core.audit.AuditLog` file.

    Only the fields the ordering proof needs. The audit log itself is used
    unmodified: nothing here subclasses, wraps, or imports a private name
    from `korvid.core.audit`.
    """

    action: str
    kind: str
    group: str
    namespace: str | None
    name: str
    outcome: str
    context: str | None


#: Returns every audit record persisted so far. `tests/evals/operation_app.py`
#: binds this to the real audit file the production `AuditLog` is writing;
#: unit tests pass a stub.
AuditIntentProbe = Callable[[], tuple[AuditRecord, ...]]


def parse_audit_records(text: str) -> tuple[AuditRecord, ...]:
    """Parse audit JSONL into typed records.

    Blank and malformed lines are skipped rather than raised: a torn final
    record is what the production log repairs on its next append, and an
    unreadable line is simply not evidence that an intent was persisted.
    """
    records: list[AuditRecord] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        namespace = entry.get("namespace")
        context = entry.get("context")
        records.append(
            AuditRecord(
                action=str(entry.get("action") or ""),
                kind=str(entry.get("kind") or ""),
                group=str(entry.get("group") or ""),
                namespace=None if namespace is None else str(namespace),
                name=str(entry.get("name") or ""),
                outcome=str(entry.get("outcome") or ""),
                context=None if context is None else str(context),
            )
        )
    return tuple(records)


def _group_of(manifest: Mapping[str, Any]) -> str:
    api_version = str(manifest.get("apiVersion") or "")
    group, _, _version = api_version.rpartition("/")
    return group


def _safe_summarize(**fields: Any) -> str:
    """Best-effort journal detail that never masks the real operation result."""

    try:
        return summarize(**fields)
    except ValueError:
        safe_fields: dict[str, Any] = {}
        for key, value in fields.items():
            try:
                summarize(**{key: value})
            except ValueError:
                continue
            safe_fields[key] = value
        return summarize(**safe_fields) if safe_fields else ""


class FakeClusterState:
    """One mutable object store, shared by the read and write fakes.

    Constructed over the *same* list instance the reader holds, so a write
    is observable through the reader without any synchronization step that
    could drift.
    """

    def __init__(self, objects: list[dict[str, Any]], *, reconcile_status: bool = True) -> None:
        self._objects = objects
        #: When True the fake mirrors the new spec into the status fields
        #: after a write, standing in for exactly one reconciliation round.
        #: The fake invents no scheduler and no kubelet.
        self.reconcile_status = reconcile_status
        self._revision = 100_000

    def find(
        self, *, group: str, kind: str, namespace: str | None, name: str
    ) -> dict[str, Any] | None:
        """The live manifest dict, or None. Mutating it mutates the store."""
        index = self._index_of(group=group, kind=kind, namespace=namespace, name=name)
        if index is not None:
            return self._objects[index]
        return None

    def _index_of(self, *, group: str, kind: str, namespace: str | None, name: str) -> int | None:
        for index, manifest in enumerate(self._objects):
            metadata = manifest.get("metadata") or {}
            if (
                str(manifest.get("kind") or "") == kind
                and _group_of(manifest) == group
                and str(metadata.get("namespace") or "") == (namespace or "")
                and str(metadata.get("name") or "") == name
            ):
                return index
        return None

    def snapshot(
        self, *, group: str, kind: str, namespace: str | None, name: str
    ) -> dict[str, Any] | None:
        """A deep copy for a caller that must not be able to mutate state."""
        found = self.find(group=group, kind=kind, namespace=namespace, name=name)
        return None if found is None else deepcopy(found)

    def uid_of(self, *, group: str, kind: str, namespace: str | None, name: str) -> str | None:
        """The live object's uid, or None when it does not exist."""
        found = self.find(group=group, kind=kind, namespace=namespace, name=name)
        if found is None:
            return None
        uid = (found.get("metadata") or {}).get("uid")
        return str(uid) if uid else None

    def read(
        self, *, group: str, kind: str, namespace: str | None, name: str, path: str
    ) -> tuple[bool, Any]:
        """`(found, value)` for a typed state path. `found` is False when
        the object or any segment is absent — distinct from a `None` value.

        Delegates to `walk_path` so authoritative state, the grader, and
        the harness's read-credit check share one walk.
        """
        return walk_path(self.find(group=group, kind=kind, namespace=namespace, name=name), path)

    def replace_object(self, manifest: Mapping[str, Any]) -> None:
        """Delete and recreate a same-named object (a fixture actor's
        same-name replacement). Anything holding the old uid must now
        conflict rather than mutate the newcomer."""
        replacement = deepcopy(dict(manifest))
        metadata = replacement.get("metadata") or {}
        index = self._index_of(
            group=_group_of(replacement),
            kind=str(replacement.get("kind") or ""),
            namespace=str(metadata.get("namespace") or ""),
            name=str(metadata.get("name") or ""),
        )
        if index is None:
            self._objects.append(replacement)
            return
        self._objects[index] = replacement

    def replace_incarnation(
        self, *, group: str, kind: str, namespace: str | None, name: str, uid: str
    ) -> bool:
        """Recreate the named object under a new uid; report whether it existed.

        The replacement is built from the live object, so a fixture that
        declares `dialog_intervention.replace_target.uid` cannot change the
        name, namespace, or spec the approval was bound to — only the
        incarnation. `metadata.resourceVersion` advances because a
        recreated object is not the one that was read.
        """
        index = self._index_of(group=group, kind=kind, namespace=namespace, name=name)
        if index is None:
            return False
        existing = self._objects[index]
        replacement = deepcopy(existing)
        metadata = replacement.setdefault("metadata", {})
        metadata["uid"] = uid
        metadata["resourceVersion"] = self.next_revision()
        self._objects[index] = replacement
        return True

    def next_revision(self) -> str:
        """A fresh, monotonically increasing `metadata.resourceVersion`."""
        self._revision += 1
        return str(self._revision)


class StatefulFakeKubeClient(FakeKubeClient):
    """`FakeKubeClient` whose object store is mutable and shared."""

    def __init__(self, cluster: OperationCluster) -> None:
        super().__init__(cluster)
        # The same list instance the base class reads from: a write through
        # `state` is observable by the very next `get_object`, and reads
        # still hand out deep copies.
        self.state = FakeClusterState(self._objects, reconcile_status=cluster.reconcile_status)


class StatefulFakeWriteOps(WriteOps):
    """Every `WriteOps` operation, backed by the shared fake state.

    Supported: scale (Deployment, StatefulSet) and rollout restart
    (Deployment, StatefulSet, DaemonSet). Everything else fails closed
    with an `ApiStatusError` so the production app records and reports a
    failure instead of auditing a success.

    `audit_intent_probe` is the fail-closed ordering evidence: it re-reads
    the real audit file immediately before each mutation, so the journal
    shows `audit_intent_observed -> mutation_started -> mutation_finished`
    without anything wrapping the production `AuditLog`.
    """

    def __init__(
        self,
        state: FakeClusterState,
        journal: ActionJournal,
        *,
        context: str,
        audit_intent_probe: AuditIntentProbe | None = None,
    ) -> None:
        self._state = state
        self._journal = journal
        self._context = context
        self._audit_intent_probe = audit_intent_probe
        self._consumed_audit_intents: dict[tuple[str, str, str, str, str, str], int] = {}

    # -- helpers ------------------------------------------------------

    def _target(
        self, meta: ResourceMeta, namespace: str | None, name: str, uid: str | None
    ) -> JournalTarget:
        return JournalTarget(
            context=self._context,
            namespace=namespace,
            group=meta.group,
            kind=meta.kind,
            plural=meta.plural,
            name=name,
            uid=uid,
        )

    def _observe_audit_intent(
        self, action: str, meta: ResourceMeta, namespace: str | None, name: str, uid: str | None
    ) -> None:
        """Journal whether this write's audit intent is already durable.

        Called immediately before the mutation and never after it, so the
        journal alone proves the ordering the design requires. Observation
        only: enforcement stays in production `KorvidApp._run_write`, which
        blocks the write when the intent cannot be persisted. Refusing here
        would grade eval-only enforcement instead of the product. With no
        probe injected (unit tests of the fake itself) nothing is claimed.

        Matching includes the **context**: the design journals context
        identity at every boundary, and per-run audit paths are what makes
        a context-blind match safe today — Slice B's shared log would not.
        """
        if self._audit_intent_probe is None:
            return
        matched = [
            record
            for record in self._audit_intent_probe()
            if record.outcome == "intent"
            and record.action == action
            and record.kind == meta.plural
            and record.group == meta.group
            and (record.context or "") == self._context
            and (record.namespace or "") == (namespace or "")
            and record.name == name
        ]
        key = (self._context, action, meta.group, meta.plural, namespace or "", name)
        consumed = self._consumed_audit_intents.get(key, 0)
        available = max(len(matched) - consumed, 0)
        if available > 0:
            self._consumed_audit_intents[key] = consumed + 1
        self._journal.append(
            event="audit_intent_observed" if available > 0 else "audit_intent_missing",
            actor="audit",
            action=action,
            target=self._target(meta, namespace, name, uid),
            result="durable" if available > 0 else "absent",
            detail=_safe_summarize(action=action, context=self._context, count=available),
        )

    def _unsupported(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        uid: str | None,
        status: int,
        reason: str,
    ) -> NoReturn:
        self._journal.append(
            event="unsupported_write",
            actor="write_ops",
            action=action,
            target=self._target(meta, namespace, name, uid),
            result="refused",
            detail=_safe_summarize(
                action=action, kind=meta.kind, status=status, reason="unsupported"
            ),
        )
        raise ApiStatusError(status, reason)

    def _resolve(
        self, action: str, meta: ResourceMeta, namespace: str | None, name: str, uid: str | None
    ) -> dict[str, Any]:
        """The live manifest for an approved write, or a fail-closed error.

        A missing uid is a harness hard failure, not a soft warning: the
        production path captures the precondition before the dialog opens,
        so its absence means composition or precondition propagation is
        broken and the run must not be scored.
        """
        target = self._target(meta, namespace, name, uid)
        if uid is None:
            self._journal.append(
                event="write_without_uid",
                actor="write_ops",
                action=action,
                target=target,
                result="hard_failure",
                detail=_safe_summarize(action=action, reason="no_uid_precondition"),
            )
            raise ApiStatusError(400, f"{_FAKE}: refusing a write with no uid precondition")
        found = self._state.find(group=meta.group, kind=meta.kind, namespace=namespace, name=name)
        if found is None:
            self._journal.append(
                event="wrong_target_write",
                actor="write_ops",
                action=action,
                target=target,
                result="refused",
                detail=_safe_summarize(action=action, reason="not_found"),
            )
            raise ApiStatusError(404, f"{meta.plural} {namespace or ''}/{name} not found")
        live = (found.get("metadata") or {}).get("uid")
        if str(live or "") != uid:
            self._journal.append(
                event="uid_conflict",
                actor="write_ops",
                action=action,
                target=target,
                result="conflict",
                detail=_safe_summarize(action=action, uid=uid, reason="uid_changed"),
            )
            raise ApiStatusError(
                409, "the target changed since it was approved - refresh and retry"
            )
        return found

    def _bump(self, manifest: dict[str, Any]) -> None:
        metadata = manifest.setdefault("metadata", {})
        metadata["resourceVersion"] = self._state.next_revision()

    # -- supported writes ---------------------------------------------

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        if meta.kind not in _SCALABLE_KINDS:
            self._unsupported(
                "scale",
                meta,
                namespace,
                name,
                uid,
                422,
                f"{_FAKE}: scale is not supported for {meta.kind}",
            )
        manifest = self._resolve("scale", meta, namespace, name, uid)
        target = self._target(meta, namespace, name, uid)
        before = int((manifest.get("spec") or {}).get("replicas", 0))
        self._observe_audit_intent("scale", meta, namespace, name, uid)
        self._journal.append(
            event="mutation_started",
            actor="write_ops",
            action="scale",
            target=target,
            approval="approved",
            pre_state={"spec.replicas": before},
            result="started",
        )
        manifest.setdefault("spec", {})["replicas"] = replicas
        self._bump(manifest)
        if self._state.reconcile_status:
            status = manifest.setdefault("status", {})
            status["replicas"] = replicas
            status["readyReplicas"] = replicas
            status["availableReplicas"] = replicas
        self._journal.append(
            event="mutation_finished",
            actor="write_ops",
            action="scale",
            target=target,
            approval="approved",
            pre_state={"spec.replicas": before},
            post_state={"spec.replicas": replicas},
            result="success",
        )

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        await self.rollout_restart_with_stamp(
            meta, namespace, name, uid=uid, restarted_at=restart_stamp()
        )

    async def rollout_restart_with_stamp(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> None:
        if meta.kind not in _RESTARTABLE_KINDS:
            self._unsupported(
                "rollout_restart",
                meta,
                namespace,
                name,
                uid,
                422,
                f"{_FAKE}: rollout restart is not supported for {meta.kind}",
            )
        manifest = self._resolve("rollout_restart", meta, namespace, name, uid)
        target = self._target(meta, namespace, name, uid)
        stamp = restarted_at or restart_stamp()
        metadata = manifest.setdefault("metadata", {})
        before = int(metadata.get("generation", 0))
        self._observe_audit_intent("rollout_restart", meta, namespace, name, uid)
        self._journal.append(
            event="mutation_started",
            actor="write_ops",
            action="rollout_restart",
            target=target,
            approval="approved",
            pre_state={"metadata.generation": before},
            result="started",
        )
        template = manifest.setdefault("spec", {}).setdefault("template", {})
        annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
        annotations[RESTART_ANNOTATION] = stamp
        metadata["generation"] = before + 1
        self._bump(manifest)
        if self._state.reconcile_status:
            manifest.setdefault("status", {})["observedGeneration"] = before + 1
        self._journal.append(
            event="mutation_finished",
            actor="write_ops",
            action="rollout_restart",
            target=target,
            approval="approved",
            pre_state={"metadata.generation": before},
            post_state={"metadata.generation": before + 1},
            result="success",
        )

    # -- previews ------------------------------------------------------

    async def preview_scale(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> list[str] | None:
        current = self._preview_target(meta, namespace, name, uid)
        if current is None:
            return None
        proposed = deepcopy(current)
        proposed.setdefault("spec", {})["replicas"] = replicas
        return diff_manifests(current, proposed)

    async def preview_rollout_restart(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> list[str] | None:
        current = self._preview_target(meta, namespace, name, uid)
        if current is None:
            return None
        proposed = deepcopy(current)
        stamp = restarted_at or restart_stamp()
        template = proposed.setdefault("spec", {}).setdefault("template", {})
        annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
        annotations[RESTART_ANNOTATION] = stamp
        return diff_manifests(current, proposed)

    def _preview_target(
        self, meta: ResourceMeta, namespace: str | None, name: str, uid: str | None
    ) -> dict[str, Any] | None:
        if uid is None:
            return None
        current = self._state.snapshot(
            group=meta.group, kind=meta.kind, namespace=namespace, name=name
        )
        if current is None:
            return None
        live_uid = self._state.uid_of(
            group=meta.group, kind=meta.kind, namespace=namespace, name=name
        )
        if live_uid != uid:
            return None
        return current

    # -- unsupported writes, refused as API errors ----------------------

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self._unsupported(
            "delete", meta, namespace, name, uid, 405, f"{_FAKE}: delete is not supported"
        )

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self._unsupported(
            "replace", meta, namespace, name, uid, 422, f"{_FAKE}: replace is not supported"
        )

    async def create_object(
        self, meta: ResourceMeta, namespace: str | None, manifest: dict[str, Any]
    ) -> None:
        self._unsupported(
            "create", meta, namespace, "", None, 405, f"{_FAKE}: create is not supported"
        )

    async def resize_pod(
        self,
        namespace: str,
        name: str,
        resources: dict[str, dict[str, dict[str, str]]],
        *,
        uid: str | None = None,
    ) -> None:
        self._unsupported(
            "resize", _POD, namespace, name, uid, 405, f"{_FAKE}: pod resize is not supported"
        )

    async def cordon_node(self, name: str, unschedulable: bool, *, uid: str | None = None) -> None:
        self._unsupported(
            "cordon", _NODE, None, name, uid, 405, f"{_FAKE}: cordon/uncordon is not supported"
        )

    async def evict_pod(self, namespace: str, name: str, *, uid: str | None = None) -> None:
        self._unsupported(
            "evict", _POD, namespace, name, uid, 405, f"{_FAKE}: pod eviction is not supported"
        )

    async def drain_plan(self, node_name: str) -> DrainPlan:
        self._unsupported(
            "drain_plan",
            _NODE,
            None,
            node_name,
            None,
            405,
            f"{_FAKE}: drain planning is not supported",
        )
