"""Node shell via `kubectl debug node/` (issue #46).

`s` on the nodes view opens a privileged debug shell on the selected node
behind an approval dialog that states the privilege escalation; the debug
pod is deleted when the shell exits, and the whole action is audit-logged
fail-closed like every other write.
"""

import asyncio
import json
from collections.abc import AsyncIterator
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
from korvid.ui.shell import DEBUG_IMAGE, build_node_debug_argv
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
) -> KorvidApp:
    store = ResourceStore()
    data: dict[str, list[Summary]] = {
        "nodes": [
            GenericSummary(name="worker-1", namespace="", kind="Node", created="", uid="node-uid-1")
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
        audit=None if audit_path is None else AuditLog(audit_path),
        check_permission=None if permitted is None else check_permission,
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


def _pods_json(*entries: tuple[str, str, str]) -> str:
    """kubectl get pods -o json payload: (name, uid, nodeName) items."""
    return json.dumps(
        {
            "items": [
                {"metadata": {"name": name, "uid": uid}, "spec": {"nodeName": node}}
                for name, uid, node in entries
            ]
        }
    )


def _listing_run(payloads: list[str]):  # type: ignore[no-untyped-def]  # test helper
    """subprocess.run fake consuming one JSON payload per call."""
    calls: list[list[str]] = []

    def run(argv, **kwargs):  # type: ignore[no-untyped-def]  # test helper
        calls.append(list(argv))
        stdout = payloads[min(len(calls) - 1, len(payloads) - 1)]
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    return run, calls


@contextmanager
def _noop_cm() -> Any:
    yield


@contextmanager
def _node_shell_env(run_fake, call_exit: int = 0):  # type: ignore[no-untyped-def]  # test helper
    """Patch kubectl discovery, the interactive call, pod listings, and
    suspend (headless drivers raise SuspendNotSupported); yields the list of
    interactive kubectl invocations."""
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
    run_fake, _ = _listing_run([_pods_json()])
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
            await pilot.press("escape")
            await pilot.pause(0.1)
    assert call_records == []


async def test_confirmed_node_shell_runs_kubectl_debug_node_and_audits(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, _ = _listing_run([_pods_json()])
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")
            await until(pilot, lambda: call_records, label="kubectl debug ran")
            await pilot.pause(0.2)
    assert call_records == [build_node_debug_argv("worker-1", "default")]
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    ours = [e for e in entries if e["action"] == "node-shell"]
    assert ours[0]["outcome"] == "intent"
    assert ours[0]["kind"] == "nodes"
    assert ours[0]["name"] == "worker-1"
    assert ours[-1]["outcome"].startswith("success")


async def test_node_shell_cleans_up_new_debugger_pod(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    run_fake, _ = _listing_run(
        [
            _pods_json(),  # before: nothing
            _pods_json(("node-debugger-worker-1-abcde", "dbg-uid", "worker-1")),
        ]
    )
    with _node_shell_env(run_fake) as _calls:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")
            await until(pilot, lambda: rec.deletes, label="cleanup delete")
    assert rec.deletes == [("pods", "default", "node-debugger-worker-1-abcde", "dbg-uid")]


async def test_node_shell_pre_existing_debugger_pod_not_deleted(tmp_path: Path) -> None:
    """A debugger pod that existed before the shell (another operator's) survives."""
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl")
    old = ("node-debugger-worker-1-old11", "old-uid", "worker-1")
    run_fake, _ = _listing_run([_pods_json(old), _pods_json(old)])
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")
            await until(pilot, lambda: call_records, label="kubectl debug ran")
            await pilot.pause(0.3)
    assert rec.deletes == []


async def test_node_shell_cleanup_failure_warns_and_audits(tmp_path: Path) -> None:
    rec = DeleteRecorder(delete_error=RuntimeError("forbidden"))
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, _ = _listing_run(
        [
            _pods_json(),
            _pods_json(("node-debugger-worker-1-abcde", "dbg-uid", "worker-1")),
        ]
    )
    with _node_shell_env(run_fake) as _calls:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _warned() -> bool:
                return any("node-debugger-worker-1-abcde" in n.message for n in app._notifications)

            await until(pilot, _warned, label="cleanup failure notification")
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert "cleanup failed" in last["outcome"]


async def test_node_shell_refused_in_readonly(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", readonly=True)
    run_fake, _ = _listing_run([_pods_json()])
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmScreen)
            assert any("Read-only" in n.message for n in app._notifications)
    assert call_records == []


async def test_node_shell_refused_without_audit(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, None)
    run_fake, _ = _listing_run([_pods_json()])
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConfirmScreen)
            assert any("audit" in n.message.lower() for n in app._notifications)
    assert call_records == []


async def test_node_shell_rbac_denied_not_offered(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(rec, tmp_path / "audit.jsonl", permitted=False)
    run_fake, _ = _listing_run([_pods_json()])
    with _node_shell_env(run_fake) as call_records:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, ConfirmScreen)
            assert any("missing permission: create pods" in n.message for n in app._notifications)
    assert call_records == []


async def test_node_shell_nonzero_exit_warns_about_policy(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    run_fake, _ = _listing_run([_pods_json()])
    with _node_shell_env(run_fake, call_exit=1) as _calls:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _warned() -> bool:
                return any("PodSecurity" in n.message for n in app._notifications)

            await until(pilot, _warned, label="policy hint notification")
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert last["outcome"].startswith("error: exit 1")


async def test_node_shell_custom_image_and_namespace_from_config(tmp_path: Path) -> None:
    rec = DeleteRecorder()
    app = make_app(
        rec,
        tmp_path / "audit.jsonl",
        node_shell_image="registry.local/toolkit:1",
        node_shell_namespace="debug-ns",
    )
    run_fake, run_calls = _listing_run([_pods_json()])
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
            await until(pilot, lambda: call_records, label="kubectl debug ran")
            await pilot.pause(0.2)
    assert call_records == [
        build_node_debug_argv("worker-1", "debug-ns", image="registry.local/toolkit:1")
    ]
    assert all("-n" in argv and argv[argv.index("-n") + 1] == "debug-ns" for argv in run_calls)


async def test_node_shell_listing_failure_skips_cleanup_with_warning(tmp_path: Path) -> None:
    """If the post-shell pod listing fails, nothing is deleted and the user
    is told to check for leftover debug pods."""
    rec = DeleteRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)

    def failing_run(argv, **kwargs):  # type: ignore[no-untyped-def]  # test helper
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")

    with _node_shell_env(failing_run) as _calls:
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await _to_nodes(pilot)
            await pilot.press("s")
            await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="dialog")
            await pilot.press("y")

            def _warned() -> bool:
                return any("node-debugger" in n.message for n in app._notifications)

            await until(pilot, _warned, label="leftover-pod warning")
    assert rec.deletes == []
    entries = [json.loads(ln) for ln in audit_path.read_text().splitlines()]
    last = [e for e in entries if e["action"] == "node-shell"][-1]
    assert "cleanup skipped" in last["outcome"]
