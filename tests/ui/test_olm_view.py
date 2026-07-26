"""OLM operator catalog: `:operators` view, install wizard, approval (#29)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from rich.text import Text

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import (
    CSVSummary,
    GenericSummary,
    OLMSubscriptionSummary,
    PackageManifestSummary,
)
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
PKG_META = ResourceMeta("PackageManifest", "packagemanifests", PACKAGES_GROUP, "v1", True)
SUB_META = ResourceMeta("Subscription", "subscriptions", OPERATORS_GROUP, "v1alpha1", True)
CSV_META = ResourceMeta(
    "ClusterServiceVersion", "clusterserviceversions", OPERATORS_GROUP, "v1alpha1", True, ("csv",)
)
IP_META = ResourceMeta("InstallPlan", "installplans", OPERATORS_GROUP, "v1alpha1", True)


def _aliases(*, olm: bool = True) -> dict[str, ResourceMeta]:
    aliases: dict[str, ResourceMeta] = {"pods": _PODS_META}
    if olm:
        aliases.update(
            {
                "packagemanifests": PKG_META,
                "operators": PKG_META,
                "subscriptions": SUB_META,
                "clusterserviceversions": CSV_META,
                "csv": CSV_META,
                "installplans": IP_META,
            }
        )
    return aliases


def _package(name: str, catalog: str = "operatorhubio-catalog") -> PackageManifestSummary:
    return PackageManifestSummary(
        name=name,
        namespace="olm",
        kind="PackageManifest",
        created="2026-07-26T10:00:00Z",
        uid=f"pkg-{name}",
        catalog=catalog,
        default_channel="stable",
        channels=("candidate", "stable"),
    )


def _subscription(name: str, state: str = "AtLatestKnown") -> OLMSubscriptionSummary:
    return OLMSubscriptionSummary(
        name=name,
        namespace="operators",
        kind="Subscription",
        created="2026-07-26T10:00:00Z",
        uid=f"sub-{name}",
        channel="stable",
        source="operatorhubio-catalog",
        installed_csv=f"{name}.v1.14.4",
        state=state,
    )


def _csv(name: str, phase: str) -> CSVSummary:
    return CSVSummary(
        name=name,
        namespace="operators",
        kind="ClusterServiceVersion",
        created="2026-07-26T10:00:00Z",
        uid=f"csv-{name}",
        version="1.14.4",
        phase=phase,
        display_name=name.split(".")[0],
    )


class Recorder(WriteOps):
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("delete", meta.plural, namespace, name))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("scale", meta.plural, namespace, name, replicas))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("restart", meta.plural, namespace, name))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("replace", meta.plural, namespace, name, manifest, uid))

    async def create_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        manifest: dict[str, Any],
    ) -> None:
        self.calls.append(("create", meta.plural, namespace, manifest))


def make_app(
    data: dict[str, list[Summary]],
    manifests: dict[str, dict[str, Any]] | None = None,
    audit_path: Path | None = None,
    *,
    olm: bool = True,
    write_ops: WriteOps | None = None,
    aliases: dict[str, ResourceMeta] | None = None,
) -> KorvidApp:
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return (manifests or {}).get(name, {"metadata": {"name": name}})

    async def list_namespaces() -> list[str]:
        return ["default", "operators"]

    return KorvidApp(
        config=KorvidConfig(namespace="operators"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=aliases if aliases is not None else _aliases(olm=olm),
        get_manifest=get_manifest,
        audit=AuditLog(audit_path) if audit_path is not None else None,
        write_ops=write_ops,
    )


async def _navigate(pilot, command: str, expect_kind: str) -> None:  # type: ignore[no-untyped-def]  # Pilot is generic; concrete app type not exposed
    await pilot.press("colon")
    for ch in command:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")
    await until(
        pilot, lambda: pilot.app.current_kind == expect_kind, label=f"view is {expect_kind}"
    )


async def test_operators_command_lists_packagemanifests_with_catalog_columns() -> None:
    app = make_app({"packagemanifests": [_package("cert-manager"), _package("argocd-operator")]})
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "operators", "packagemanifests")
        table = app.query_one(ResourceTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == ["NAME", "CATALOG", "DEFAULT CHANNEL", "CHANNELS", "DESCRIPTION", "AGE"]
        await until(pilot, lambda: table.row_count == 2, label="packages listed")
        rows = {str(table.get_row_at(i)[0]): table.get_row_at(i) for i in range(2)}
        assert str(rows["cert-manager"][1]) == "operatorhubio-catalog"
        assert str(rows["cert-manager"][2]) == "stable"
        assert str(rows["cert-manager"][3]) == "candidate,stable"


async def test_operators_command_without_olm_explains_why() -> None:
    app = make_app({}, olm=False)
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await pilot.press("colon")
        for ch in "operators":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("OLM" in n.message for n in app._notifications),
            label="OLM-absent explanation shown",
        )
        assert app.current_kind == "pods"  # the view did not change


async def test_subscriptions_view_shows_olm_columns() -> None:
    app = make_app({"subscriptions": [_subscription("cert-manager")]})
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "subscriptions", "subscriptions")
        table = app.query_one(ResourceTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == ["NAME", "CHANNEL", "SOURCE", "INSTALLED CSV", "STATE", "AGE"]
        await until(pilot, lambda: table.row_count == 1, label="subscription listed")
        row = table.get_row_at(0)
        assert str(row[1]) == "stable"
        assert str(row[2]) == "operatorhubio-catalog"
        assert str(row[3]) == "cert-manager.v1.14.4"
        assert str(row[4]) == "AtLatestKnown"


async def test_csv_phase_styling_highlights_failures() -> None:
    app = make_app(
        {
            "clusterserviceversions": [
                _csv("cert-manager.v1.14.4", "Succeeded"),
                _csv("broken.v0.0.1", "Failed"),
                _csv("rolling.v2.0.0", "Installing"),
            ]
        }
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "csv", "clusterserviceversions")
        table = app.query_one(ResourceTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == ["NAME", "DISPLAY NAME", "VERSION", "PHASE", "AGE"]
        await until(pilot, lambda: table.row_count == 3, label="csvs listed")
        by_name = {str(table.get_row_at(i)[0]): table.get_row_at(i) for i in range(3)}
        ok = by_name["cert-manager.v1.14.4"][3]
        failed = by_name["broken.v0.0.1"][3]
        rolling = by_name["rolling.v2.0.0"][3]
        assert isinstance(ok, Text)
        assert isinstance(failed, Text)
        assert isinstance(rolling, Text)
        assert ok.style == "green"
        assert failed.style == "bold red"
        assert rolling.style == "yellow"


def _pkg_manifest(name: str) -> dict[str, Any]:
    return {
        "apiVersion": f"{PACKAGES_GROUP}/v1",
        "kind": "PackageManifest",
        "metadata": {"name": name, "namespace": "olm", "uid": f"pkg-{name}"},
        "status": {
            "catalogSource": "operatorhubio-catalog",
            "catalogSourceNamespace": "olm",
            "defaultChannel": "stable",
            "channels": [{"name": "candidate"}, {"name": "stable"}],
        },
    }


def _installplan(name: str, *, approved: bool) -> GenericSummary:
    return GenericSummary(
        name=name,
        namespace="operators",
        kind="InstallPlan",
        created="2026-07-26T10:00:00Z",
        uid=f"ip-{name}",
    )


def _installplan_manifest(
    name: str, *, approved: bool, approval_mode: str = "Manual"
) -> dict[str, Any]:
    return {
        "apiVersion": f"{OPERATORS_GROUP}/v1alpha1",
        "kind": "InstallPlan",
        "metadata": {
            "name": name,
            "namespace": "operators",
            "uid": f"ip-{name}",
            "resourceVersion": "41",
        },
        "spec": {
            "approval": approval_mode,
            "approved": approved,
            "clusterServiceVersionNames": ["cert-manager.v1.14.4"],
        },
    }


async def test_install_key_walks_wizard_confirm_and_creates_subscription(
    tmp_path: Path,
) -> None:
    """I on a catalog row: wizard -> full Subscription manifest in the
    approval dialog -> create_object + fail-closed audit."""
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        {"packagemanifests": [_package("cert-manager")]},
        manifests={"cert-manager": _pkg_manifest("cert-manager")},
        audit_path=audit_path,
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "operators", "packagemanifests")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="package listed")
        await pilot.press("I")
        await until(
            pilot, lambda: isinstance(app.screen, OperatorInstallPrompt), label="wizard open"
        )
        await pilot.press("enter")  # accept defaults: operators/stable/Automatic
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirm open")
        body = app.screen.query_one(".confirm-operation").render()
        assert "channel: stable" in str(body)  # the manifest is shown in full
        assert "installPlanApproval: Automatic" in str(body)
        await pilot.press("y")
        await until(pilot, lambda: bool(rec.calls), label="create executed")
        kind, plural, ns, manifest = rec.calls[0]
        assert (kind, plural, ns) == ("create", "subscriptions", "operators")
        assert manifest["spec"] == {
            "name": "cert-manager",
            "channel": "stable",
            "source": "operatorhubio-catalog",
            "sourceNamespace": "olm",
            "installPlanApproval": "Automatic",
        }
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="audit recorded",
        )


async def test_install_key_outside_catalog_view_warns(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(
        {"subscriptions": [_subscription("cert-manager")]},
        audit_path=tmp_path / "audit.jsonl",
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "subscriptions", "subscriptions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="subscription listed")
        await pilot.press("I")
        await until(
            pilot,
            lambda: any("does not apply" in str(n.message) for n in app._notifications),
            label="warned",
        )
        assert rec.calls == []


async def test_approve_key_on_pending_installplan_replaces_with_approved(
    tmp_path: Path,
) -> None:
    rec = Recorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(
        {"installplans": [_installplan("install-abc", approved=False)]},
        manifests={"install-abc": _installplan_manifest("install-abc", approved=False)},
        audit_path=audit_path,
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "installplans", "installplans")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="installplan listed")
        await pilot.press("I")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="confirm open")
        body = str(app.screen.query_one(".confirm-operation").render())
        assert "cert-manager.v1.14.4" in body  # what the approval unblocks
        await pilot.press("y")
        await until(pilot, lambda: bool(rec.calls), label="replace executed")
        kind, plural, ns, name, manifest, uid = rec.calls[0]
        assert (kind, plural, ns, name) == ("replace", "installplans", "operators", "install-abc")
        assert manifest["spec"]["approved"] is True
        assert uid == "ip-install-abc"
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="audit recorded",
        )


async def test_approve_key_on_already_approved_installplan_notifies(tmp_path: Path) -> None:
    rec = Recorder()
    app = make_app(
        {"installplans": [_installplan("install-abc", approved=True)]},
        manifests={"install-abc": _installplan_manifest("install-abc", approved=True)},
        audit_path=tmp_path / "audit.jsonl",
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "installplans", "installplans")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="installplan listed")
        await pilot.press("I")
        await until(
            pilot,
            lambda: any("already approved" in str(n.message) for n in app._notifications),
            label="already-approved notice",
        )
        assert rec.calls == []


async def test_install_cancelled_when_catalog_row_was_recreated(tmp_path: Path) -> None:
    """The fetched PackageManifest must be the same incarnation the user
    selected: a stale row (deleted + recreated under the same name) must not
    feed the wizard."""
    rec = Recorder()
    manifest = _pkg_manifest("cert-manager")
    manifest["metadata"]["uid"] = "pkg-other-incarnation"
    app = make_app(
        {"packagemanifests": [_package("cert-manager")]},
        manifests={"cert-manager": manifest},
        audit_path=tmp_path / "audit.jsonl",
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "operators", "packagemanifests")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="package listed")
        await pilot.press("I")
        await until(
            pilot,
            lambda: any("changed" in str(n.message) for n in app._notifications),
            label="stale row rejected",
        )
        assert not isinstance(app.screen, OperatorInstallPrompt)
        assert rec.calls == []


async def test_approve_key_on_automatic_installplan_notifies(tmp_path: Path) -> None:
    """Only pending *manual* plans are approvable: an Automatic plan is
    OLM's own to approve, and flipping it would race the operator."""
    rec = Recorder()
    app = make_app(
        {"installplans": [_installplan("install-abc", approved=False)]},
        manifests={
            "install-abc": _installplan_manifest(
                "install-abc", approved=False, approval_mode="Automatic"
            )
        },
        audit_path=tmp_path / "audit.jsonl",
        write_ops=rec,
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "installplans", "installplans")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="installplan listed")
        await pilot.press("I")
        await until(
            pilot,
            lambda: any("Automatic" in str(n.message) for n in app._notifications),
            label="automatic plan refused",
        )
        assert rec.calls == []


async def test_foreign_subscriptions_kind_keeps_generic_columns() -> None:
    """A CRD from another API group whose plural happens to be
    'subscriptions' must not get the OLM typed table."""
    foreign = ResourceMeta("Subscription", "subscriptions", "messaging.example.com", "v1", True)
    app = make_app(
        {
            "subscriptions": [
                GenericSummary(
                    name="events-sub",
                    namespace="operators",
                    kind="Subscription",
                    created="2026-07-26T10:00:00Z",
                    uid="f1",
                )
            ]
        },
        aliases={"subscriptions": foreign},
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "subscriptions", "subscriptions")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="row listed")
        headers = [str(col.label) for col in table.columns.values()]
        assert "CHANNEL" not in headers
        assert "INSTALLED CSV" not in headers
        assert table.get_row_at(0)[0] == "events-sub"


async def test_group_qualified_alias_navigates_to_olm_not_foreign_crd() -> None:
    """When a foreign CRD wins the bare 'subscriptions' alias, the
    kubectl-style plural.group alias must still watch and render the OLM
    Subscription - not silently open the foreign resource."""
    foreign = ResourceMeta("Subscription", "subscriptions", "messaging.example.com", "v1", True)
    olm_sub = SUB_META
    qualified = f"subscriptions.{OPERATORS_GROUP}"
    app = make_app(
        {
            "subscriptions": [
                GenericSummary(
                    name="foreign-sub",
                    namespace="operators",
                    kind="Subscription",
                    created="2026-07-26T10:00:00Z",
                    uid="f1",
                )
            ],
            qualified: [_subscription("cert-manager")],
        },
        aliases={"subscriptions": foreign, qualified: olm_sub},
    )
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, qualified, qualified)
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="olm subscription listed")
        headers = [str(col.label) for col in table.columns.values()]
        assert "CHANNEL" in headers  # the OLM typed table, not the generic one
        assert table.get_row_at(0)[0] == "cert-manager"
