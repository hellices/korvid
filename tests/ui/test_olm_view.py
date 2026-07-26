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
from korvid.k8s.models import CSVSummary, OLMSubscriptionSummary, PackageManifestSummary
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.ui.app import KorvidApp
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


def make_app(
    data: dict[str, list[Summary]],
    manifests: dict[str, dict[str, Any]] | None = None,
    audit_path: Path | None = None,
    *,
    olm: bool = True,
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
        aliases=_aliases(olm=olm),
        get_manifest=get_manifest,
        audit=AuditLog(audit_path) if audit_path is not None else None,
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
        assert labels == ["NAME", "CATALOG", "DEFAULT CHANNEL", "CHANNELS", "AGE"]
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
