"""Node shell via `kubectl debug node/` (issue #46).

`s` on the nodes view opens a privileged debug shell on the selected node
behind an approval dialog that states the privilege escalation; the debug
pod is deleted when the shell exits, and the whole action is audit-logged
fail-closed like every other write.
"""

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.shell import DEBUG_IMAGE, build_node_debug_create_argv, build_pod_attach_argv
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))
_ALIASES = {"pods": _PODS_META, "nodes": _NODES_META}


class DeleteRecorder(WriteOps):
    """WriteOps fake recording delete_object calls (node shell cleanup)."""

    def __init__(self, delete_error: Exception | None = None) -> None:
        self.deletes: list[tuple[str, str | None, str, str | None]] = []
        self.delete_error = delete_error

    async def delete_object(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        if self.delete_error is not None:
            raise self.delete_error
        self.deletes.append((meta.plural, namespace, name, uid))

    async def scale_object(self, meta, namespace, name, replicas, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        pass

    async def rollout_restart(self, meta, namespace, name, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        pass

    async def replace_object(self, meta, namespace, name, manifest, *, uid=None):  # type: ignore[no-untyped-def]  # fake
        pass


def make_app(
    recorder: DeleteRecorder,
    audit_path: Path | None,
    *,
    readonly: bool = False,
    permitted: bool | None = None,
    node_shell_image: str | None = None,
    node_shell_namespace: str | None = None,
    get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None = None,
    audit_log: AuditLog | None = None,
    extra_nodes: tuple[str, ...] = (),
    permission_gate: asyncio.Event | None = None,
    permission_started: asyncio.Event | None = None,
) -> KorvidApp:
    store = ResourceStore()
    data: dict[str, list[Summary]] = {
        "nodes": [
            GenericSummary(
                name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1"
            ),
            *(
                GenericSummary(
                    name=extra, namespace="", kind="Node", created="", uid=f"uid-{extra}"
                )
                for extra in extra_nodes
            ),
        ],
        "pods": [],
    }

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        if permission_started is not None:
            permission_started.set()
        if permission_gate is not None:
            await permission_gate.wait()
        assert permitted is not None
        return permitted

    return KorvidApp(
        config=KorvidConfig(
            namespace="default",
            readonly=readonly,
            node_shell_image=node_shell_image,
            node_shell_namespace=node_shell_namespace,
        ),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        write_ops=recorder,
        audit=audit_log
        if audit_log is not None
        else (None if audit_path is None else AuditLog(audit_path)),
        check_permission=None if permitted is None else check_permission,
        get_manifest=get_manifest,
    )


async def _to_nodes(pilot) -> None:  # type: ignore[no-untyped-def]  # Pilot's app type isn't exposed
    await pilot.press("colon")
    for ch in "nodes":
        await pilot.press(ch)
    await pilot.press("enter")

    def _node_row_rendered() -> bool:
        table = pilot.app.query_one(ResourceTable)
        return table.row_count > 0 and str(table.get_row_at(0)[0]) == "worker-1"

    await until(pilot, _node_row_rendered, label="nodes view rendered")


DBG_POD = "node-debugger-worker-1-abcde"
DBG_UID = "dbg-uid"


def _pod_json(name: str = DBG_POD, uid: str = DBG_UID) -> bytes:
    """`kubectl debug --attach=false -o json` output: the created pod."""
    return json.dumps({"metadata": {"name": name, "uid": uid}}).encode()


def _kubectl_run(create_result: Any = None, wait_rc: int = 0):  # type: ignore[no-untyped-def]  # test helper
    """subprocess.run fake serving the detached create and the Ready wait."""
    calls: list[list[str]] = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]  # test helper
        calls.append(list(argv))
        if "debug" in argv:
            if create_result is not None:
                return create_result
            return SimpleNamespace(returncode=0, stdout=_pod_json(), stderr=b"")
        return SimpleNamespace(returncode=wait_rc, stdout=b"", stderr=b"")

    return run, calls


@contextmanager
def _noop_cm() -> Any:
    yield


@contextmanager
def _node_shell_env(run_fake, call_exit: int = 0):  # type: ignore[no-untyped-def]  # test helper
    """Patch kubectl discovery, the interactive attach, the create/wait
    subprocesses, and suspend (headless drivers raise SuspendNotSupported);
    yields the list of interactive kubectl invocations."""
    call_records: list[list[str]] = []

    def fake_call(argv):  # type: ignore[no-untyped-def]  # test helper
        call_records.append(list(argv))
        return call_exit

    with (
        patch("korvid.ui.app.shutil.which", return_value="/usr/bin/kubectl"),
        patch("korvid.ui.app.subprocess.call", side_effect=fake_call),
        patch("korvid.ui.app.subprocess.run", side_effect=run_fake),
        patch.object(KorvidApp, "suspend", side_effect=lambda: _noop_cm()),
    ):
        yield call_records


async def test_s_on_nodes_view_opens_privileged_approval_dialog(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            body = screen._operation.lower()
            assert "privileged" in body
            assert DEBUG_IMAGE in screen._operation
            assert "host" in body
            assert "--profile=sysadmin" in body
            assert "kubectl 1.30+" in screen._operation
            await pilot.press("escape")
            await pilot.pause(0.1)
    assert call_records == []


async def test_confirmed_node_shell_creates_waits_attaches_and_audits(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, run_calls = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")
            await until(pilot, lambda: call_records, label="kubectl attach ran")

            def _outcome_written() -> bool:
                return audit_path.is_file() and '"success' in audit_path.read_text()

            await until(pilot, _outcome_written, label="outcome audit record")
    assert run_calls[0] == build_node_debug_create_argv("worker-1", "default")
    assert run_calls[1][:2] == ["kubectl", "wait"]
    assert f"pod/{DBG_POD}" in run_calls[1]
    assert call_records == [build_pod_attach_argv("default", DBG_POD)]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    ours = [e for e in entries if e["action"] == "node-shell"]
    assert ours[0]["outcome"] == "intent"
    assert ours[0]["kind"] == "nodes"
    assert ours[0]["name"] == "worker-1"
    assert ours[-1]["outcome"].startswith("success")


async def test_node_shell_deletes_exactly_its_own_pod(tmp_path: Path) -> None:
    """The pod created by this invocation (name+uid from the detached
    create) is the only thing deleted — never a namespace-wide guess that
    could catch another operator's debugger."""
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake) as _calls:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")
            await until(pilot, lambda: rec.deletes, label="cleanup delete")
    assert rec.deletes == [("pods", "default", DBG_POD, DBG_UID)]


async def test_node_shell_cleanup_failure_warns_and_audits(tmp_path: Path) -> None:
    rec = DeleteRecorder(delete_error=RuntimeError("forbidden"))
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake) as _calls:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _warned() -> bool:
                return any(DBG_POD in n.message for n in app._notifications)

            await until(pilot, _warned, label="cleanup failure notification")
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert f"cleanup failed for: {DBG_POD}" in last["outcome"]


async def test_node_shell_refused_in_readonly(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(
                pilot,
                lambda: any("Read-only" in n.message for n in app._notifications),
                label="read-only refusal",
            )
            assert not isinstance(app.screen, ConfirmScreen)
    assert call_records == []


async def test_node_shell_refused_without_audit(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, None)
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(
                pilot,
                lambda: any("audit" in n.message.lower() for n in app._notifications),
                label="audit refusal",
            )
            assert not isinstance(app.screen, ConfirmScreen)
    assert call_records == []


async def test_node_shell_rbac_denied_not_offered(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=False)
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(
                pilot,
                lambda: any(
                    "missing permission: create pods" in n.message for n in app._notifications
                ),
                label="rbac denial",
            )
            assert not isinstance(app.screen, ConfirmScreen)
    assert call_records == []


async def test_node_shell_create_failure_warns_about_policy(tmp_path: Path) -> None:
    """PodSecurity admission refusals surface at pod creation: the hint
    points at node_shell.namespace, nothing attaches, nothing is deleted."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, _ = _kubectl_run(
        create_result=SimpleNamespace(
            returncode=1,
            stdout=b"",
            stderr=b'Error from server (Forbidden): pods "node-debugger-worker-1-abcde"'
            b" is forbidden: violates PodSecurity",
        )
    )
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _warned() -> bool:
                return any("PodSecurity" in n.message for n in app._notifications)

            await until(pilot, _warned, label="policy hint notification")
    assert call_records == []
    assert rec.deletes == []
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert last["outcome"] == "error: pod creation rejected"


async def test_node_shell_unidentifiable_create_output_aborts(tmp_path: Path) -> None:
    """If kubectl succeeds but the created pod cannot be identified, korvid
    must not attach or guess at cleanup — and the audit trail must say the
    cleanup was skipped and where to look, not that creation failed."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, _ = _kubectl_run(
        create_result=SimpleNamespace(returncode=0, stdout=b"not json", stderr=b"")
    )
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _warned() -> bool:
                return any("did not report" in n.message for n in app._notifications)

            await until(pilot, _warned, label="unidentifiable-pod warning")
    assert call_records == []
    assert rec.deletes == []
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert "could not be identified" in last["outcome"]
    assert "cleanup skipped: check namespace default" in last["outcome"]


async def test_node_shell_wait_failure_warns_but_still_cleans_up(tmp_path: Path) -> None:
    """A pod that never becomes Ready still gets the attach attempt (the
    error is visible in the terminal) and is still deleted afterwards."""
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    run_fake, _ = _kubectl_run(wait_rc=1)
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")
            await until(pilot, lambda: rec.deletes, label="cleanup delete")
            assert any("did not become Ready" in n.message for n in app._notifications)
    assert call_records == [build_pod_attach_argv("default", DBG_POD)]
    assert rec.deletes == [("pods", "default", DBG_POD, DBG_UID)]


async def test_node_shell_custom_image_and_namespace_from_config(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(
        rec,
        tmp_path / "audit.jsonl",
        node_shell_image="registry.local/toolkit:1",
        node_shell_namespace="debug-ns",
    )
    run_fake, run_calls = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            screen = app.screen
            assert isinstance(screen, ConfirmScreen)
            assert "registry.local/toolkit:1" in screen._operation
            assert "debug-ns" in screen._operation
            await pilot.press("y")
            await until(pilot, lambda: call_records, label="kubectl attach ran")
            await until(pilot, lambda: rec.deletes, label="cleanup delete")
    assert run_calls[0] == build_node_debug_create_argv(
        "worker-1", "debug-ns", image="registry.local/toolkit:1"
    )
    assert call_records == [build_pod_attach_argv("debug-ns", DBG_POD)]
    assert rec.deletes == [("pods", "debug-ns", DBG_POD, DBG_UID)]


async def test_node_shell_aborts_when_node_replaced_after_prompt(tmp_path: Path) -> None:
    """The approval is bound to the node incarnation on screen: a node
    deleted and recreated under the same name while the dialog was open must
    not receive the privileged shell."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"

    async def get_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        return {"metadata": {"uid": "replacement-uid"}}

    app = make_app(rec, audit_path, get_manifest=get_manifest)
    run_fake, run_calls = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _cancelled() -> bool:
                return any("was replaced" in n.message for n in app._notifications)

            await until(pilot, _cancelled, label="replacement cancel notification")
    assert call_records == []
    assert not any("debug" in argv for argv in run_calls)
    assert not audit_path.is_file() or "intent" not in audit_path.read_text()


class _ExplodingAudit(AuditLog):
    """AuditLog whose persistence always fails."""

    def append(self, **kwargs: Any) -> None:
        raise OSError("disk full")


async def test_node_shell_blocked_when_audit_append_fails(tmp_path: Path) -> None:
    """Fail-closed invariant: if the intent record cannot persist, the
    debugger pod must never be created."""
    rec = DeleteRecorder()
    app = make_app(rec, None, audit_log=_ExplodingAudit(tmp_path / "audit.jsonl", context="test"))
    run_fake, run_calls = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _blocked() -> bool:
                return any("Write blocked" in n.message for n in app._notifications)

            await until(pilot, _blocked, label="write-blocked notification")
    assert call_records == []
    assert not any("debug" in argv for argv in run_calls)
    assert rec.deletes == []


async def test_node_shell_cancelled_when_selection_moves_during_rbac_check(
    tmp_path: Path,
) -> None:
    """The approval must stay bound to the row that initiated it: moving the
    cursor while the SSAR pre-check is in flight cancels the offer."""
    rec = DeleteRecorder()
    gate = asyncio.Event()
    started = asyncio.Event()
    app = make_app(
        rec,
        tmp_path / "audit.jsonl",
        permitted=True,
        permission_gate=gate,
        permission_started=started,
        extra_nodes=("worker-2",),
    )
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, started.is_set, label="flow parked on the gated SSAR")
            await pilot.press("down")  # move to worker-2
            gate.set()

            def _cancelled() -> bool:
                return any("selection changed" in n.message for n in app._notifications)

            await until(pilot, _cancelled, label="selection-changed cancel")
            assert not isinstance(app.screen, ConfirmScreen)
    assert call_records == []


async def test_node_shell_nonzero_attach_exit_has_no_policy_hint(tmp_path: Path) -> None:
    """A created pod means the shell ran: a non-zero attach exit is the
    user's own shell status (exit 1, Ctrl-C), not admission refusal."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, _ = _kubectl_run()
    with _node_shell_env(run_fake, call_exit=1) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")
            await until(pilot, lambda: rec.deletes, label="debug pod cleanup")
    assert call_records != []
    assert not any("PodSecurity" in n.message for n in app._notifications)
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert last["outcome"].startswith("error: exit 1")


async def test_node_shell_cancelled_worker_still_deletes_pod(tmp_path: Path) -> None:
    """A worker cancellation while waiting for readiness must not strand the
    privileged host-mounted pod: the finalizer deletes it and records the
    interrupted outcome before the cancellation propagates."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    wait_entered = asyncio.Event()
    release = threading.Event()
    loop_box: list[asyncio.AbstractEventLoop] = []

    def run_fake(argv, **kwargs):  # type: ignore[no-untyped-def]  # test helper
        if "debug" in argv:
            return SimpleNamespace(returncode=0, stdout=_pod_json(), stderr=b"")
        loop_box[0].call_soon_threadsafe(wait_entered.set)
        release.wait(timeout=10)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with _node_shell_env(run_fake) as call_records:
        async with app.run_test():
            loop_box.append(asyncio.get_running_loop())
            task = asyncio.ensure_future(
                app._run_node_shell(rec, "worker-1", "default", DEBUG_IMAGE, None)
            )
            await asyncio.wait_for(wait_entered.wait(), timeout=5)
            task.cancel()
            release.set()
            await asyncio.wait([task])
            assert task.cancelled()
    assert call_records == []  # never attached
    assert rec.deletes == [("pods", "default", DBG_POD, DBG_UID)]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert last["outcome"].startswith("error: interrupted; cleanup: deleted")


async def test_node_shell_create_without_uid_aborts(tmp_path: Path) -> None:
    """A create response missing the uid must be treated as unidentifiable:
    without it the cleanup delete would lose its uid precondition and could
    remove a same-name replacement pod."""
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    run_fake, _ = _kubectl_run(
        create_result=SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"metadata": {"name": DBG_POD}}).encode(),
            stderr=b"",
        )
    )
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _warned() -> bool:
                return any("uid" in n.message for n in app._notifications)

            await until(pilot, _warned, label="missing-uid warning")
    assert call_records == []
    assert rec.deletes == []


async def test_node_shell_cancelled_during_create_still_deletes_pod(tmp_path: Path) -> None:
    """Cancelling the worker while the detached create is in flight must not
    leak the pod kubectl creates moments later: the create is settled and,
    when it yields a pod identity, finalized before the cancellation
    propagates."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    create_entered = asyncio.Event()
    release = threading.Event()
    loop_box: list[asyncio.AbstractEventLoop] = []

    def run_fake(argv, **kwargs):  # type: ignore[no-untyped-def]  # test helper
        assert "debug" in argv  # wait/attach must never run
        loop_box[0].call_soon_threadsafe(create_entered.set)
        release.wait(timeout=10)
        return SimpleNamespace(returncode=0, stdout=_pod_json(), stderr=b"")

    with _node_shell_env(run_fake) as call_records:
        async with app.run_test():
            loop_box.append(asyncio.get_running_loop())
            task = asyncio.ensure_future(
                app._run_node_shell(rec, "worker-1", "default", DEBUG_IMAGE, None)
            )
            await asyncio.wait_for(create_entered.wait(), timeout=5)
            task.cancel()
            release.set()
            await asyncio.wait([task])
            assert task.cancelled()
    assert call_records == []  # never attached
    assert rec.deletes == [("pods", "default", DBG_POD, DBG_UID)]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert last["outcome"].startswith("error: interrupted; cleanup: deleted")


async def test_create_failure_outcomes_distinguish_launch_error_from_timeout(
    tmp_path: Path,
) -> None:
    """A kubectl launch failure never reached the cluster, so its outcome
    must not claim a timeout or send the operator hunting for leftover pods;
    a real timeout may have created a pod and must keep the namespace hint."""
    import subprocess

    app = make_app(DeleteRecorder(), tmp_path / "audit.jsonl")
    async with app.run_test():
        with patch("korvid.ui.app.subprocess.run", side_effect=OSError("kubectl vanished")):
            outcome = await app._create_node_debug_pod("worker-1", "default", DEBUG_IMAGE)
        assert outcome == "error: kubectl could not be launched; no pod created"

        with patch(
            "korvid.ui.app.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["kubectl"], timeout=30),
        ):
            outcome = await app._create_node_debug_pod("worker-1", "default", DEBUG_IMAGE)
        assert outcome == "error: pod creation timed out; cleanup skipped: check namespace default"


async def test_ambiguous_create_failure_keeps_namespace_hint(tmp_path: Path) -> None:
    """A non-zero kubectl exit does not prove the create was rejected — the
    server can commit the pod and the client still fail (lost response). Only
    clearly identified admission rejections claim nothing was created; other
    failures must keep the cleanup-skipped namespace hint."""
    app = make_app(DeleteRecorder(), tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        failure = SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"error: unexpected EOF reading response"
        )
        with patch("korvid.ui.app.subprocess.run", return_value=failure):
            outcome = await app._create_node_debug_pod("worker-1", "default", DEBUG_IMAGE)
        assert outcome == "error: pod creation failed; cleanup skipped: check namespace default"

        def _hinted() -> bool:
            return any("may still have been created" in n.message for n in app._notifications)

        await until(pilot, _hinted, label="ambiguous-create namespace hint")

        # A bare "forbidden" substring (pod/image name, quoted inside an
        # unrelated error) must not be mistaken for an admission rejection.
        failure = SimpleNamespace(
            returncode=1, stdout=b"", stderr=b'error: watch of pod "forbidden-checker" failed'
        )
        with patch("korvid.ui.app.subprocess.run", return_value=failure):
            outcome = await app._create_node_debug_pod("worker-1", "default", DEBUG_IMAGE)
        assert outcome == "error: pod creation failed; cleanup skipped: check namespace default"
