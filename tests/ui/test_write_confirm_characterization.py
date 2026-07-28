"""U0 characterization of the write-confirmation callbacks (issue #91).

`ui/app.py` nests 12 structurally similar `_done(confirmed)` confirmation
callbacks around its approval dialogs. This module records their
classification (the U0 exit criterion) and pins the decline behavior that
had no confirm-stage coverage. Every test here passes on unmodified code.

Classification of the 12 callbacks:

Group (a) — standard `_run_write` launch (9): delete, rollout restart,
edit, scale, resize, cordon/uncordon, installplan approve, helm
install/upgrade, helm rollback. Shape: `if confirmed:
self.run_worker(self._run_write(...))`. The mutation coroutine is
constructed *inside* the confirmed branch, so a declined dialog never
creates it (no unawaited-coroutine leak — pinned here by the decline
tests under warnings-as-errors).

Group (b) — launch + UID recheck (1): operator install re-checks the
catalog incarnation inside `_done` before launching, because create has
no server-side uid precondition (no target object exists yet).

Group (c) — dedicated drain worker (1): drain assigns
`self._drain_worker = self.run_worker(self._run_drain(...))` so mid-drain
cancellation and the uncordon-refusal guard can find it; it does not go
through `_run_write`.

Group (d) — approval-future completion (1): the agent write gate's
callback resolves an asyncio future; approval timing and expiry are owned
by the awaiting caller, not the callback.

Behaviors already pinned elsewhere (not duplicated here):
approval only by user keystroke (test_write_ops, test_agent_write);
switch refusal during dialogs/turns/writes (test_ctx_switch);
post-await revalidation (test_write_ops, test_olm_view); fail-closed
audit (test_write_ops::test_unwritable_audit_blocks_write); typed-name
confirmation (test_write_ops, test_node_ops, test_protected_contexts);
declined delete/agent-write (test_write_ops, test_agent_write);
drain worker ownership and mid-drain cancel (test_node_ops).
"""

from dataclasses import replace
from pathlib import Path

from korvid.k8s.drain import DrainPlan
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.widgets.resize_prompt import ResizePrompt
from korvid.ui.widgets.resource_table import ResourceTable

from .test_helm_actions import FakeHelm, _rows_listed
from .test_helm_actions import _navigate as _helm_navigate
from .test_helm_actions import make_app as make_helm_app
from .test_node_ops import NodeRecorder, _target, _to_nodes
from .test_node_ops import make_app as make_node_app
from .test_olm_view import Recorder as OlmRecorder
from .test_olm_view import (
    _installplan,
    _installplan_manifest,
    _navigate,
    _package,
    _pkg_manifest,
)
from .test_olm_view import make_app as make_olm_app
from .test_resize_flow import ResizeRecorder
from .test_resize_flow import make_app as make_resize_app
from .test_write_ops import Recorder, _edit_fixtures, _to_view, make_app
from .waits import until

# --- group (a): declined confirmations make no call and write no audit ---


async def test_rollout_restart_declined_makes_no_call_and_no_audit(tmp_path: Path) -> None:
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_view(pilot, "deployments")
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_scale_declined_at_confirm_makes_no_call(tmp_path: Path) -> None:
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_view(pilot, "deployments")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt))
        await pilot.press("5")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_edit_declined_at_confirm_makes_no_call(tmp_path: Path) -> None:
    def bump_image(text: str) -> str:
        return text.replace("nginx:1", "nginx:2")

    get_manifest, edit_text, _seen = _edit_fixtures(bump_image)
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(rec, audit_path, get_manifest=get_manifest, edit_text=edit_text)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_cordon_declined_makes_no_call(tmp_path: Path) -> None:
    rec = NodeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_node_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("c")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="cordon dialog")
        await pilot.press("n")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_resize_declined_at_confirm_makes_no_call(tmp_path: Path) -> None:
    rec = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_resize_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("R")
        await until(pilot, lambda: isinstance(app.screen, ResizePrompt))
        field = app.screen.query_one("#resize-0-requests-cpu")
        field.value = "200m"  # type: ignore[attr-defined]  # Input widget
        field.focus()
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen))
        await pilot.press("n")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


async def test_helm_rollback_declined_makes_no_call(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_helm_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _helm_navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("n")
        await pilot.pause(0.2)
        assert not any(call[0] == "rollback" for call in helm.calls)
        assert not audit_path.exists()


async def test_approve_installplan_declined_makes_no_call(tmp_path: Path) -> None:
    rec = OlmRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_olm_app(
        {"installplans": [_installplan("install-abc", approved=False)]},
        manifests={"install-abc": _installplan_manifest("install-abc", approved=False)},
        audit_path=audit_path,
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "installplans", "installplans")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="installplan listed")
        await pilot.press("I")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirm open")
        await pilot.press("n")
        await pilot.pause(0.2)
        assert rec.calls == []
        assert not audit_path.exists()


# --- group (b): install re-checks the catalog incarnation inside _done ---


async def test_install_cancelled_when_catalog_entry_changes_during_approval(
    tmp_path: Path,
) -> None:
    """Create has no server-side uid precondition, so the install callback
    re-checks the catalog incarnation after the approval keystroke: a row
    deleted and recreated under the same name while the dialog was open
    must cancel, not install from stale facts."""
    rec = OlmRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_olm_app(
        {"packagemanifests": [_package("cert-manager")]},
        manifests={"cert-manager": _pkg_manifest("cert-manager")},
        audit_path=audit_path,
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "operators", "packagemanifests")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="package listed")
        await pilot.press("I")
        await until(
            pilot, lambda: isinstance(app.screen, OperatorInstallPrompt), label="wizard open"
        )
        await pilot.press("enter")  # accept defaults
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirm open")
        # Same name, different incarnation lands while the dialog is open.
        replaced = replace(_package("cert-manager"), uid="pkg-other-incarnation")
        app.store.apply_event(app.current_kind, app.current_scope, "MODIFIED", replaced)
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(
                "changed during the approval dialog" in str(n.message) for n in app._notifications
            ),
            label="stale incarnation rejected",
        )
        assert rec.calls == []
        assert not audit_path.exists()


# --- group (c): drain declined starts no worker and cordons nothing ---


async def test_drain_declined_at_confirm_starts_no_worker(tmp_path: Path) -> None:
    plan = DrainPlan(targets=(_target("web-1"),), skipped_daemonset=(), skipped_mirror=())
    rec = NodeRecorder(plan=plan)
    audit_path = tmp_path / "audit.jsonl"
    app = make_node_app(rec, audit_path)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="drain dialog")
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert app._drain_worker is None
        assert not any(call[0] == "cordon" for call in rec.calls)
        assert not audit_path.exists()
