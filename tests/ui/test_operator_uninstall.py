"""Operator uninstall flow (issue #117): ctrl+d on an OLM Subscription
removes the operator - the Subscription first (stopping reinstalls), then its
installed CSV, with OLM garbage-collecting the operator's own Deployment and
RBAC. CRDs and custom resources are never touched. Ctrl+d on a CSV whose
Subscription is known redirects to the same full flow."""

import json
from pathlib import Path
from typing import Any

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.ui.widgets.confirm_screen import ConfirmScreen

from .test_olm_view import (
    SUB_META,
    Recorder,
    _aliases,
    _csv,
    _navigate,
    _subscription,
    make_app,
)
from .waits import until

_SUB_MANIFEST = {
    "metadata": {"name": "cert-manager", "namespace": "operators", "uid": "sub-cert-manager"},
    "status": {"installedCSV": "cert-manager.v1.14.4"},
}
_CSV_MANIFEST = {
    "metadata": {
        "name": "cert-manager.v1.14.4",
        "namespace": "operators",
        "uid": "csv-cert-manager.v1.14.4",
    },
}


def _audit_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_ctrl_d_on_subscription_uninstalls_subscription_then_csv(tmp_path: Path) -> None:
    ops = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": _SUB_MANIFEST, "cert-manager.v1.14.4": _CSV_MANIFEST},
        audit_path,
        write_ops=ops,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks
        assert "OPERATOR UNINSTALL cert-manager" in operation
        assert "DELETE subscriptions.operators.coreos.com/cert-manager" in operation
        assert (
            "DELETE clusterserviceversions.operators.coreos.com/cert-manager.v1.14.4" in operation
        )
        assert "garbage-collects" in operation
        assert "CRDs and custom resources are KEPT" in operation
        await pilot.press("y")
        await until(pilot, lambda: len(ops.calls) == 2, label="both deletes ran")
        # Subscription first (stops OLM reinstalling), then the CSV;
        # both uid-pinned to the incarnations shown in the dialog.
        assert ops.calls == [
            ("delete", "subscriptions", "operators", "cert-manager", "sub-cert-manager"),
            (
                "delete",
                "clusterserviceversions",
                "operators",
                "cert-manager.v1.14.4",
                "csv-cert-manager.v1.14.4",
            ),
        ]
        await until(
            pilot,
            lambda: audit_path.exists() and audit_path.read_text().count("success") == 2,
            label="both writes audited",
        )
        entries = _audit_entries(audit_path)
        assert [e["action"] for e in entries] == ["uninstall"] * 4
        assert [e["outcome"] for e in entries] == ["intent", "success", "intent", "success"]
        assert entries[0]["kind"] == "subscriptions"
        assert entries[2]["kind"] == "clusterserviceversions"


async def test_subscription_without_installed_csv_removes_subscription_only(
    tmp_path: Path,
) -> None:
    ops = Recorder()
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": {"metadata": {"name": "cert-manager", "uid": "sub-cert-manager"}}},
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks
        assert "no installed CSV" in operation
        assert "clusterserviceversions" not in operation
        await pilot.press("y")
        await until(pilot, lambda: len(ops.calls) == 1, label="subscription delete ran")
        assert ops.calls == [
            ("delete", "subscriptions", "operators", "cert-manager", "sub-cert-manager")
        ]


async def test_uninstall_blocked_when_csv_delete_is_forbidden(tmp_path: Path) -> None:
    """The dialog offers an atomic uninstall (Subscription + CSV): a known
    CSV-delete denial must block *before* approval, or the flow deletes the
    Subscription and then fails, leaving the operator half-uninstalled."""
    ops = Recorder()
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": _SUB_MANIFEST, "cert-manager.v1.14.4": _CSV_MANIFEST},
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )

    async def deny_csv_delete(
        verb: str, plural: str, subresource: str, ns: str | None, group: str, name: str
    ) -> bool:
        return plural != "clusterserviceversions"

    app._check_permission = deny_csv_delete
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: any("missing permission" in str(n.message) for n in app._notifications),
            label="denial surfaced",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert ops.calls == []


async def test_uninstall_aborted_when_csv_api_is_undiscovered(tmp_path: Path) -> None:
    """An installed CSV that cannot be targeted (CSV API undiscovered) must
    abort the uninstall - deleting only the Subscription would leave the
    operator running after the user approved a full uninstall."""
    ops = Recorder()
    aliases = _aliases()
    del aliases["clusterserviceversions"], aliases["csv"]
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": _SUB_MANIFEST},
        tmp_path / "audit.jsonl",
        write_ops=ops,
        aliases=aliases,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: any(
                "aborted" in str(n.message) and "cert-manager.v1.14.4" in str(n.message)
                for n in app._notifications
            ),
            label="abort notified",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert ops.calls == []


async def test_uninstall_aborted_when_csv_uid_cannot_be_established(tmp_path: Path) -> None:
    """A failed (non-404) CSV uid lookup must abort instead of deleting the
    CSV unpinned - an unpinned delete could remove a replacement object
    created while the dialog was open."""
    ops = Recorder()
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": _SUB_MANIFEST, "cert-manager.v1.14.4": _CSV_MANIFEST},
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )
    real_get_manifest = app._get_manifest
    assert real_get_manifest is not None

    async def failing_csv_lookup(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if "clusterserviceversions" in kind:
            raise ApiStatusError(500, "boom")
        return await real_get_manifest(kind, ns, name)

    app._get_manifest = failing_csv_lookup
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: any("aborted" in str(n.message) for n in app._notifications),
            label="abort notified",
        )
        assert not isinstance(app.screen, ConfirmScreen)
        assert ops.calls == []


async def test_csv_already_gone_removes_subscription_only(tmp_path: Path) -> None:
    """A 404 on the CSV lookup is not an error: the CSV is already gone and
    the flow degrades to a subscription-only uninstall."""
    ops = Recorder()
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": _SUB_MANIFEST},
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )
    real_get_manifest = app._get_manifest
    assert real_get_manifest is not None

    async def csv_gone(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        if "clusterserviceversions" in kind:
            raise ApiStatusError(404, "NotFound")
        return await real_get_manifest(kind, ns, name)

    app._get_manifest = csv_gone
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks
        assert "already gone" in operation
        assert "cert-manager.v1.14.4" in operation
        await pilot.press("y")
        await until(pilot, lambda: len(ops.calls) == 1, label="subscription delete ran")
        assert ops.calls[0][1] == "subscriptions"


async def test_apply_uninstall_reserves_the_cluster_write_slot_synchronously(
    tmp_path: Path,
) -> None:
    """The whole two-delete operation counts as one in-flight cluster write
    from the moment the confirmation callback constructs it - a `:ctx`
    switch queued in the callback-to-worker gap must see it (issue #36)."""
    ops = Recorder()
    app = make_app({}, {}, tmp_path / "audit.jsonl", write_ops=ops)
    async with app.run_test():
        coro = app._olm._operator_apply_uninstall(
            ops,
            SUB_META,
            "operators",
            "cert-manager",
            "sub-cert-manager",
            fetch_kind="subscriptions",
            csv_meta=None,
            csv_name="",
            csv_uid=None,
        )
        try:
            assert app._active_cluster_writes == 1  # reserved before the worker starts
            await coro
        finally:
            coro.close()  # keep a failed assert from leaking the coroutine
        assert app._active_cluster_writes == 0


async def test_installed_csv_advancing_mid_dialog_aborts_the_uninstall(tmp_path: Path) -> None:
    """OLM can advance `status.installedCSV` in place while the dialog is
    open (same Subscription uid): the approved deletes would then target a
    stale CSV. The apply step re-verifies the Subscription and aborts."""
    ops = Recorder()
    manifests: dict[str, dict[str, Any]] = {
        "cert-manager": json.loads(json.dumps(_SUB_MANIFEST)),
        "cert-manager.v1.14.4": _CSV_MANIFEST,
    }
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        manifests,
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        # The operator upgrades while the dialog is open: same Subscription
        # uid, new installed CSV.
        manifests["cert-manager"]["status"]["installedCSV"] = "cert-manager.v1.14.5"
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("changed while the dialog" in str(n.message) for n in app._notifications),
            label="abort notified",
        )
        assert ops.calls == []


async def test_subscription_uninstall_declined_runs_nothing(tmp_path: Path) -> None:
    ops = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": _SUB_MANIFEST, "cert-manager.v1.14.4": _CSV_MANIFEST},
        audit_path,
        write_ops=ops,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("n")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="dialog dismissed")
        assert ops.calls == []
        assert not audit_path.exists() or "uninstall" not in audit_path.read_text()


class FailingSubscriptionDelete(Recorder):
    """The Subscription delete fails; the CSV must then be left alone."""

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        await super().delete_object(meta, namespace, name, uid=uid)
        if meta.plural == "subscriptions":
            raise RuntimeError("boom")


async def test_failed_subscription_delete_leaves_the_csv(tmp_path: Path) -> None:
    ops = FailingSubscriptionDelete()
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        {"cert-manager": _SUB_MANIFEST, "cert-manager.v1.14.4": _CSV_MANIFEST},
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="rows")
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("failed" in str(n.message) for n in app._notifications),
            label="failure notified",
        )
        assert len(ops.calls) == 1  # deleting the CSV alone would be reinstalled
        assert ops.calls[0][1] == "subscriptions"


async def test_ctrl_d_on_csv_with_known_subscription_redirects_to_full_flow(
    tmp_path: Path,
) -> None:
    ops = Recorder()
    app = make_app(
        {
            "subscriptions": [_subscription("cert-manager")],
            "clusterserviceversions": [_csv("cert-manager.v1.14.4", "Succeeded")],
        },
        {"cert-manager": _SUB_MANIFEST, "cert-manager.v1.14.4": _CSV_MANIFEST},
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )
    async with app.run_test() as pilot:
        # Visit the subscriptions view first so the store knows the
        # Subscription -> CSV link, then act from the CSV view.
        await _navigate(pilot, "subscriptions", "subscriptions")
        await until(pilot, lambda: bool(app.store.get("subscriptions", "operators")), label="subs")
        await _navigate(pilot, "clusterserviceversions", "clusterserviceversions")
        await until(
            pilot,
            lambda: bool(app.store.get("clusterserviceversions", "operators")),
            label="csvs",
        )
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await until(
            pilot,
            lambda: any("reinstall" in str(n.message) for n in app._notifications),
            label="reinstall warning",
        )
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks
        assert "OPERATOR UNINSTALL cert-manager" in operation
        assert "DELETE subscriptions.operators.coreos.com/cert-manager" in operation
        await pilot.press("y")
        await until(pilot, lambda: len(ops.calls) == 2, label="both deletes ran")
        assert ops.calls[0][1] == "subscriptions"
        assert ops.calls[1][1] == "clusterserviceversions"


async def test_ctrl_d_on_csv_without_known_subscription_is_a_plain_delete(
    tmp_path: Path,
) -> None:
    ops = Recorder()
    app = make_app(
        {"clusterserviceversions": [_csv("orphan.v1.0.0", "Succeeded")]},
        {"orphan.v1.0.0": {"metadata": {"name": "orphan.v1.0.0", "uid": "csv-orphan.v1.0.0"}}},
        tmp_path / "audit.jsonl",
        write_ops=ops,
    )
    async with app.run_test() as pilot:
        await _navigate(pilot, "clusterserviceversions", "clusterserviceversions")
        await until(
            pilot,
            lambda: bool(app.store.get("clusterserviceversions", "operators")),
            label="csvs",
        )
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks
        assert "OPERATOR UNINSTALL" not in operation
        assert "DELETE clusterserviceversions.operators.coreos.com/orphan.v1.0.0" in operation
        await pilot.press("y")
        await until(pilot, lambda: len(ops.calls) == 1, label="plain delete ran")
        assert ops.calls[0][:2] == ("delete", "clusterserviceversions")


async def test_gate_run_reserves_the_cluster_write_slot_synchronously(
    tmp_path: Path,
) -> None:
    """`WriteGate.run` must reserve before it returns, like `_run_write` does.

    The install dialog owns its own approval, so it calls the gate's run
    directly from the confirmation callback and hands the coroutine to
    `run_worker`. An adapter that only reserves once the coroutine starts
    leaves a gap in which a queued `:ctx` sees zero active writes, switches,
    and lets the approved install execute against the previous cluster
    (issue #36).
    """
    app = make_app({}, {}, tmp_path / "audit.jsonl", write_ops=Recorder())
    async with app.run_test():

        async def op() -> None:
            return None

        coro = app._olm._gate.run("operator-install", SUB_META, "operators", "x", op)
        try:
            assert app._active_cluster_writes == 1, "reserved before the worker starts"
            await coro
        finally:
            coro.close()  # keep a failed assert from leaking the coroutine
        assert app._active_cluster_writes == 0, "the reservation outlived the write"
