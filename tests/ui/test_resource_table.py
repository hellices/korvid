from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from korvid.core.store import Summary
from korvid.k8s.models import GenericSummary
from korvid.k8s.olm import OPERATORS_GROUP
from korvid.ui.widgets import resource_table
from korvid.ui.widgets.resource_table import ResourceTable


class _TableApp(App[None]):
    def compose(self) -> ComposeResult:
        yield ResourceTable()


@pytest.mark.parametrize(
    ("plural", "group", "synthetic", "renderer_name", "expected_columns"),
    [
        ("pods", "", False, "_render_pod_rows", resource_table._POD_COLS),
        (
            "replicasets",
            "apps",
            False,
            "_render_replicaset_rows",
            resource_table._RS_COLS,
        ),
        (
            "helmreleases",
            "",
            True,
            "_render_helm_release_rows",
            resource_table._HELM_COLS,
        ),
        (
            "subscriptions",
            OPERATORS_GROUP,
            False,
            "_render_subscription_rows",
            resource_table._SUB_COLS,
        ),
    ],
)
def test_presentation_identity_selects_native_columns_and_renderer(
    plural: str,
    group: str,
    synthetic: bool,
    renderer_name: str,
    expected_columns: tuple[str, ...],
) -> None:
    assert (
        resource_table._columns_for(
            plural,
            group=group,
            synthetic=synthetic,
            all_namespaces=False,
            view=None,
        )
        == expected_columns
    )
    assert (
        resource_table._row_renderer(plural, group=group, synthetic=synthetic).__name__
        == renderer_name
    )


@pytest.mark.parametrize(
    ("plural", "group", "synthetic"),
    [
        ("pods", "example.io", False),
        ("replicasets", "example.io", False),
        ("helmreleases", "example.io", False),
        ("helmreleases", "", False),
    ],
)
async def test_foreign_same_plural_resources_render_as_generic_rows(
    plural: str,
    group: str,
    synthetic: bool,
) -> None:
    app = _TableApp()
    rows: list[Summary] = [
        GenericSummary(
            name="foreign",
            namespace="default",
            kind="Foreign",
            created="",
        )
    ]
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        table.show(
            plural,
            rows,
            all_namespaces=False,
            pattern="",
            group=group,
            synthetic=synthetic,
        )
        await pilot.pause()

        assert [str(column.label) for column in table.columns.values()] == ["NAME", "AGE"]
        assert table.row_count == 1
        assert table.get_row_at(0)[0] == "foreign"
