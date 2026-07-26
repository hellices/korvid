"""Helm release browser: `:helm` view, revision drill-down, describe (#28)."""

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
from korvid.k8s.helm import (
    HELM_RELEASES_META,
    HELM_REVISIONS_META,
    HelmReleaseSummary,
    HelmRevisionSummary,
    release_uid,
)
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "helm": HELM_RELEASES_META,
    "helmreleases": HELM_RELEASES_META,
    "helmrevisions": HELM_REVISIONS_META,
}


def _release(
    name: str,
    revision: int = 1,
    status: str = "deployed",
    chart: str = "nginx-1.2.3",
    app_version: str = "1.25",
) -> HelmReleaseSummary:
    return HelmReleaseSummary(
        name=name,
        namespace="default",
        kind="HelmRelease",
        created="2026-07-26T10:00:00Z",
        uid=release_uid("default", name),
        revision=revision,
        status=status,
        chart=chart,
        app_version=app_version,
    )


def _revision(
    release: str,
    revision: int,
    status: str = "superseded",
    description: str = "Upgrade complete",
) -> HelmRevisionSummary:
    return HelmRevisionSummary(
        name=f"{release}.v{revision}",
        namespace="default",
        kind="HelmRevision",
        created="2026-07-26T10:00:00Z",
        uid=f"secret-uid-{release}-{revision}",
        owner_uids=(release_uid("default", release),),
        release=release,
        revision=revision,
        status=status,
        chart="nginx-1.2.3",
        app_version="1.25",
        description=description,
    )


def make_app(
    data: dict[str, list[Summary]],
    manifests: dict[str, dict[str, Any]] | None = None,
    audit_path: Path | None = None,
) -> tuple[KorvidApp, list[tuple[str, str | None, str]]]:
    store = ResourceStore()
    describe_calls: list[tuple[str, str | None, str]] = []

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in data.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def get_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        describe_calls.append((kind, namespace, name))
        return (manifests or {}).get(name, {"kind": "HelmRelease", "metadata": {"name": name}})

    async def list_namespaces() -> list[str]:
        return ["default"]

    app = KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
        get_manifest=get_manifest,
        audit=AuditLog(audit_path) if audit_path is not None else None,
    )
    return app, describe_calls


def _default_data() -> dict[str, list[Summary]]:
    return {
        "helmreleases": [
            _release("web", revision=3),
            _release("db", revision=1, status="failed", chart="postgres-9.0.1", app_version="16"),
        ],
        "helmrevisions": [
            _revision("web", 1),
            _revision("web", 2),
            _revision("web", 3, status="deployed"),
            _revision("db", 1, status="failed"),
        ],
    }


async def _navigate(pilot, command: str) -> None:  # type: ignore[no-untyped-def]  # Pilot is generic; concrete app type not exposed
    await pilot.press("colon")
    for ch in command:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")
    await pilot.pause(0.1)


async def test_helm_command_lists_releases_with_helm_columns() -> None:
    app, _ = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "helm")
        assert app.current_kind == "helmreleases"
        table = app.query_one(ResourceTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == ["NAME", "REVISION", "STATUS", "CHART", "APP VERSION", "AGE"]
        await until(pilot, lambda: table.row_count == 2, label="releases listed")
        rows = {str(table.get_row_at(i)[0]): table.get_row_at(i) for i in range(2)}
        assert str(rows["web"][1]) == "3"
        assert str(rows["web"][2]) == "deployed"
        assert str(rows["web"][3]) == "nginx-1.2.3"
        assert str(rows["web"][4]) == "1.25"


async def test_failed_release_status_is_highlighted() -> None:
    app, _ = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "helm")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="releases listed")
        by_name = {str(table.get_row_at(i)[0]): table.get_row_at(i) for i in range(2)}
        db_status = by_name["db"][2]
        web_status = by_name["web"][2]
        assert isinstance(db_status, Text)
        assert isinstance(web_status, Text)
        assert db_status.style == "bold red"
        assert web_status.style == "green"


async def test_enter_on_release_drills_into_its_revisions() -> None:
    app, _ = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "helm")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="releases listed")
        # Cursor starts on row 0; move to "web" if needed (rows sorted by name: db, web).
        names = [str(table.get_row_at(i)[0]) for i in range(table.row_count)]
        for _ in range(names.index("web")):
            await pilot.press("down")
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "helmrevisions", label="drilled")
        await until(pilot, lambda: table.row_count == 3, label="web revisions only")
        revs = sorted(str(table.get_row_at(i)[1]) for i in range(table.row_count))
        assert revs == ["1", "2", "3"]
        await pilot.press("escape")
        await until(pilot, lambda: app.current_kind == "helmreleases", label="popped")


async def test_revisions_view_shows_history_columns() -> None:
    app, _ = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "helmrevisions")
        table = app.query_one(ResourceTable)
        labels = [str(col.label) for col in table.columns.values()]
        assert labels == [
            "NAME",
            "REVISION",
            "STATUS",
            "CHART",
            "APP VERSION",
            "DESCRIPTION",
            "AGE",
        ]
        await until(pilot, lambda: table.row_count == 4, label="revisions listed")


async def test_d_on_release_describes_the_helm_release() -> None:
    app, describe_calls = make_app(_default_data())
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        await _navigate(pilot, "helm")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="releases listed")
        await pilot.press("d")
        await until(pilot, lambda: len(describe_calls) == 1, label="describe fetched")
        kind, namespace, name = describe_calls[0]
        assert kind == "helmreleases"
        assert namespace == "default"
        assert name in {"web", "db"}


async def test_write_actions_reject_synthetic_helm_kinds(tmp_path: Path) -> None:
    """Helm browser rows are read-only views over Secrets: Ctrl-D must not
    open an approval dialog, and an agent-side write against the synthetic
    kind must come back as an ERROR - neither may reach the API."""
    app, _ = make_app(_default_data(), audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="releases listed")
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert len(app.screen_stack) == 1  # no ConfirmScreen pushed
        result = app._agent_write_op(
            "delete", "helmreleases", "web", "default", None, None, restarted_at="s"
        )
        assert isinstance(result, str)
        assert result.startswith("ERROR:")
        assert "read-only" in result
