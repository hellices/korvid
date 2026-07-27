"""Custom columns per resource kind via config (issue #45)."""

from __future__ import annotations

from dataclasses import replace

from korvid.core.config import KorvidConfig, ViewConfig
from korvid.core.store import Summary
from korvid.k8s.columns import CustomColumn
from korvid.k8s.models import GenericSummary
from korvid.ui.widgets.resource_table import ResourceTable, _columns_for

from .test_app import _pod, make_app
from .waits import until

_TEAM = CustomColumn("TEAM", "label", "team")
_IMAGE = CustomColumn("IMAGE", "jsonpath", ".spec.containers[0].image")


def _header_labels(table: ResourceTable) -> list[str]:
    return [str(col.label) for col in table.columns.values()]


def _row(table: ResourceTable, index: int) -> list[str]:
    return [str(cell) for cell in table.get_row_at(index)]


# ---------------------------------------------------------------------------
# _columns_for
# ---------------------------------------------------------------------------


class TestColumnsFor:
    def test_appends_custom_names_to_defaults(self) -> None:
        view = ViewConfig(columns=(_TEAM, _IMAGE))
        cols = _columns_for("deployments", all_namespaces=False, view=view)
        assert cols == ("NAME", "AGE", "TEAM", "IMAGE")

    def test_replace_keeps_name_only(self) -> None:
        view = ViewConfig(columns=(_TEAM,), replace=True)
        cols = _columns_for("pods", all_namespaces=False, view=view)
        assert cols == ("NAME", "TEAM")

    def test_replace_keeps_namespace_in_all_namespaces(self) -> None:
        view = ViewConfig(columns=(_TEAM,), replace=True)
        cols = _columns_for("pods", all_namespaces=True, view=view)
        assert cols == ("NAMESPACE", "NAME", "TEAM")

    def test_no_view_keeps_defaults(self) -> None:
        assert _columns_for("deployments", all_namespaces=False, view=None) == ("NAME", "AGE")


# ---------------------------------------------------------------------------
# rendering through the app
# ---------------------------------------------------------------------------


def _views_config(kind: str, view: ViewConfig) -> KorvidConfig:
    return KorvidConfig(namespace="default", views={kind: view})


async def test_pod_view_appends_custom_cells() -> None:
    config = _views_config("pods", ViewConfig(columns=(_TEAM,)))
    pods = [replace(_pod("api-1"), custom=("payments",))]
    app = make_app(pods, config=config)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod rendered")
        assert _header_labels(table)[-1] == "TEAM"
        assert _row(table, 0)[-1] == "payments"


async def test_generic_view_replace_renders_name_plus_custom() -> None:
    view = ViewConfig(columns=(_TEAM, _IMAGE), replace=True)
    config = _views_config("deployments", view)
    deploys: list[Summary] = [
        GenericSummary(
            name="api",
            namespace="default",
            kind="Deployment",
            created="2026-07-26T08:00:00Z",
            custom=("payments", "ghcr.io/acme/api:1.2.3"),
        )
    ]
    app = make_app([], extra_data={"deployments": deploys}, config=config)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="deploy rendered")
        assert _header_labels(table) == ["NAME", "TEAM", "IMAGE"]
        assert _row(table, 0) == ["api", "payments", "ghcr.io/acme/api:1.2.3"]


async def test_rows_without_custom_values_pad_with_none() -> None:
    """Summaries created before the column config (or by paths that skip
    extraction) still render — padded with `<none>`, never crashing."""
    config = _views_config("pods", ViewConfig(columns=(_TEAM,)))
    app = make_app([_pod("api-1")], config=config)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod rendered")
        assert _row(table, 0)[-1] == "<none>"


async def test_sort_command_orders_by_custom_column() -> None:
    config = _views_config("pods", ViewConfig(columns=(_TEAM,)))
    pods = [
        replace(_pod("a"), custom=("payments",)),
        replace(_pod("b"), custom=("billing",)),
    ]
    app = make_app(pods, config=config)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods rendered")
        await pilot.press("colon")
        for ch in "sort TEAM":
            await pilot.press(*(["space"] if ch == " " else [ch]))
        await pilot.press("enter")
        await until(
            pilot,
            lambda: [_row(table, i)[0] for i in range(table.row_count)] == ["b", "a"],
            label="sorted by TEAM ascending",
        )
        assert any("TEAM" in label and "▲" in label for label in _header_labels(table))


async def test_sort_command_unknown_column_notifies() -> None:
    app = make_app([_pod("a")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod rendered")
        await pilot.press("colon")
        for ch in "sort NOPE":
            await pilot.press(*(["space"] if ch == " " else [ch]))
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("NOPE" in n.message for n in app._notifications),
            label="unknown column notified",
        )


async def test_config_warnings_notified_at_startup() -> None:
    config = KorvidConfig(namespace="default", warnings=("views.pods.BAD: jsonpath is empty",))
    app = make_app([_pod("a")], config=config)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: any("views.pods.BAD" in n.message for n in app._notifications),
            label="config warning notified",
        )


async def test_replace_view_ignores_hidden_builtin_sort_keys() -> None:
    """With replace: true, AGE/CPU/MEM are not rendered — their key actions
    must not silently reorder rows by an invisible field (PR #78 review)."""
    view = ViewConfig(columns=(_TEAM,), replace=True)
    config = _views_config("pods", view)
    pods = [
        replace(_pod("young"), created="2026-07-27T00:00:00Z", custom=("z",)),
        replace(_pod("old"), created="2026-07-01T00:00:00Z", custom=("a",)),
    ]
    app = make_app(pods, config=config)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods rendered")
        before = [_row(table, i)[0] for i in range(table.row_count)]
        await pilot.press("A")  # sort_by_age — AGE column is hidden
        await until(pilot, lambda: True, label="settle")
        assert [_row(table, i)[0] for i in range(table.row_count)] == before
        assert all("▲" not in label for label in _header_labels(table))
        assert all("▼" not in label for label in _header_labels(table))


async def test_replace_view_sort_command_rejects_hidden_builtin() -> None:
    view = ViewConfig(columns=(_TEAM,), replace=True)
    config = _views_config("pods", view)
    app = make_app([replace(_pod("a"), custom=("x",))], config=config)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod rendered")
        await pilot.press("colon")
        for ch in "sort age":
            await pilot.press(*(["space"] if ch == " " else [ch]))
        await pilot.press("enter")
        await until(
            pilot,
            lambda: any("age" in n.message for n in app._notifications),
            label="hidden builtin rejected",
        )
        # name stays sortable: NAME is an identity column replace keeps.
        assert not any("'name'" in n.message for n in app._notifications)
