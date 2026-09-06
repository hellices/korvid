"""Custom columns per resource kind via config (issue #45)."""

from __future__ import annotations

from dataclasses import replace

from korvid.core.config import KorvidConfig, ViewConfig
from korvid.core.store import Summary
from korvid.k8s.columns import CustomColumn
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.k8s.models import GenericSummary
from korvid.k8s.olm import OPERATORS_GROUP
from korvid.ui.widgets import resource_table
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


# ---------------------------------------------------------------------------
# selected-view validation — actual identity header collision check
# ---------------------------------------------------------------------------


class TestValidateSelectedView:
    def test_native_view_drops_names_colliding_with_its_builtin_headers(self) -> None:
        status = CustomColumn("STATUS", "label", "s")
        team = CustomColumn("TEAM", "label", "team")
        selected, warnings = resource_table.validate_selected_view(
            "pods",
            group="",
            synthetic=False,
            view=ViewConfig(columns=(status, team)),
        )
        assert selected is not None
        assert [c.name for c in selected.config.columns] == ["TEAM"]
        assert selected.value_indices == (1,)
        assert any("STATUS" in w for w in warnings)

    def test_replace_views_keep_builtin_like_names(self) -> None:
        status = CustomColumn("STATUS", "label", "s")
        selected, warnings = resource_table.validate_selected_view(
            "pods",
            group="",
            synthetic=False,
            view=ViewConfig(columns=(status,), replace=True),
        )
        assert selected is not None
        assert [c.name for c in selected.config.columns] == ["STATUS"]
        assert selected.value_indices == (0,)
        assert warnings == ()

    def test_append_view_removed_when_all_columns_collide(self) -> None:
        ready = CustomColumn("ready", "label", "r")  # case-insensitive
        selected, warnings = resource_table.validate_selected_view(
            "pods",
            group="",
            synthetic=False,
            view=ViewConfig(columns=(ready,)),
        )
        assert selected is None
        assert len(warnings) == 1

    def test_foreign_subscription_keeps_olm_looking_column(self) -> None:
        channel = CustomColumn("CHANNEL", "label", "channel")
        selected, warnings = resource_table.validate_selected_view(
            "subscriptions",
            group="messaging.example.com",
            synthetic=False,
            view=ViewConfig(columns=(channel,)),
        )
        assert selected is not None
        assert selected.config.columns == (channel,)
        assert selected.value_indices == (0,)
        assert warnings == ()

    def test_foreign_replicaset_keeps_native_looking_column(self) -> None:
        revision = CustomColumn("REVISION", "label", "revision")
        selected, warnings = resource_table.validate_selected_view(
            "replicasets",
            group="example.com",
            synthetic=False,
            view=ViewConfig(columns=(revision,)),
        )
        assert selected is not None
        assert selected.config.columns == (revision,)
        assert selected.value_indices == (0,)
        assert warnings == ()

    def test_same_plural_validation_does_not_mutate_raw_view_between_groups(self) -> None:
        channel = CustomColumn("CHANNEL", "label", "channel")
        team = CustomColumn("TEAM", "label", "team")
        raw = ViewConfig(columns=(channel, team))

        native, native_warnings = resource_table.validate_selected_view(
            "subscriptions",
            group=OPERATORS_GROUP,
            synthetic=False,
            view=raw,
        )
        foreign, foreign_warnings = resource_table.validate_selected_view(
            "subscriptions",
            group="messaging.example.com",
            synthetic=False,
            view=raw,
        )

        assert native is not None
        assert [column.name for column in native.config.columns] == ["TEAM"]
        assert native.value_indices == (1,)
        assert native_warnings
        assert foreign is not None
        assert foreign.config == raw
        assert foreign.value_indices == (0, 1)
        assert foreign_warnings == ()
        assert raw.columns == (channel, team)


async def test_native_collision_filter_preserves_custom_value_alignment_and_warns() -> None:
    status = CustomColumn("STATUS", "label", "status")
    team = CustomColumn("TEAM", "label", "team")
    config = _views_config("pods", ViewConfig(columns=(status, team)))
    pods = [
        replace(_pod("alpha"), custom=("custom-status-a", "zeta")),
        replace(_pod("zeta"), custom=("custom-status-z", "alpha")),
    ]
    app = make_app(pods, config=config)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods rendered")
        headers = _header_labels(table)
        assert headers.count("STATUS") == 1
        assert headers[-1] == "TEAM"
        assert [_row(table, i)[-1] for i in range(table.row_count)] == ["zeta", "alpha"]
        assert any("views.pods.STATUS" in n.message for n in app._notifications)
        await pilot.press("colon")
        await pilot.press(*"sort")
        await pilot.press("space")
        await pilot.press(*"TEAM")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: [_row(table, i)[0] for i in range(table.row_count)] == ["zeta", "alpha"],
            label="pods sorted by retained TEAM value",
        )
        assert sum("views.pods.STATUS" in n.message for n in app._notifications) == 1


async def test_foreign_subscription_renders_and_sorts_native_looking_custom_column() -> None:
    channel = CustomColumn("CHANNEL", "label", "channel")
    config = _views_config("subscriptions", ViewConfig(columns=(channel,)))
    foreign = ResourceMeta("Subscription", "subscriptions", "messaging.example.com", "v1", True)
    rows: list[Summary] = [
        GenericSummary(
            name="zeta",
            namespace="default",
            kind="Subscription",
            created="",
            custom=("beta",),
        ),
        GenericSummary(
            name="alpha",
            namespace="default",
            kind="Subscription",
            created="",
            custom=("alpha",),
        ),
    ]
    app = make_app(
        [],
        extra_data={"subscriptions": rows},
        aliases={"pods": ResourceMeta("Pod", "pods", "", "v1", True), "subscriptions": foreign},
        config=config,
    )
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await pilot.press(*"subscriptions")
        await pilot.press("enter")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="subscriptions rendered")
        assert _header_labels(table) == ["NAME", "AGE", "CHANNEL"]
        await pilot.press("colon")
        await pilot.press(*"sort")
        await pilot.press("space")
        await pilot.press(*"CHANNEL")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: [_row(table, i)[0] for i in range(table.row_count)] == ["alpha", "zeta"],
            label="subscriptions sorted by CHANNEL",
        )


async def test_foreign_replicaset_renders_native_looking_custom_column() -> None:
    revision = CustomColumn("REVISION", "label", "revision")
    config = _views_config("replicasets", ViewConfig(columns=(revision,)))
    foreign = ResourceMeta("ReplicaSet", "replicasets", "example.com", "v1", True)
    rows: list[Summary] = [
        GenericSummary(
            name="custom-rs",
            namespace="default",
            kind="ReplicaSet",
            created="",
            custom=("custom-revision",),
        )
    ]
    app = make_app(
        [],
        extra_data={"replicasets": rows},
        aliases={"pods": ResourceMeta("Pod", "pods", "", "v1", True), "replicasets": foreign},
        config=config,
    )
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await pilot.press(*"replicasets")
        await pilot.press("enter")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="replicaset rendered")
        assert _header_labels(table) == ["NAME", "AGE", "REVISION"]
        assert _row(table, 0)[-1] == "custom-revision"


async def test_qualified_flux_view_gets_columns_without_leaking_to_synthetic_helm() -> None:
    key = "helmreleases.helm.toolkit.fluxcd.io"
    flux = ResourceMeta("HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io", "v2", True)
    config = _views_config(key, ViewConfig(columns=(_TEAM,)))
    rows: list[Summary] = [
        GenericSummary(
            name="zeta",
            namespace="default",
            kind="HelmRelease",
            created="",
            custom=("beta",),
        ),
        GenericSummary(
            name="alpha",
            namespace="default",
            kind="HelmRelease",
            created="",
            custom=("alpha",),
        ),
    ]
    app = make_app(
        [],
        extra_data={key: rows},
        aliases={
            "pods": ResourceMeta("Pod", "pods", "", "v1", True),
            "helmreleases": HELM_RELEASES_META,
            key: flux,
        },
        config=config,
    )
    async with app.run_test() as pilot:
        await pilot.press("colon")
        await pilot.press(*"helmreleases")
        await pilot.press("enter")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: app.current_kind == "helmreleases", label="synthetic selected")
        assert "TEAM" not in _header_labels(table)

        await pilot.press("colon")
        await pilot.press(*key)
        await pilot.press("enter")
        await until(pilot, lambda: table.row_count == 2, label="Flux releases rendered")
        assert _header_labels(table) == ["NAME", "AGE", "TEAM"]
        await pilot.press("colon")
        await pilot.press(*"sort")
        await pilot.press("space")
        await pilot.press(*"TEAM")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: [_row(table, i)[0] for i in range(table.row_count)] == ["alpha", "zeta"],
            label="Flux releases sorted by TEAM",
        )
