"""Column sorting UI tests (issue #37): shift+N/A/C/M toggle data-model
sorts, the header shows ▲/▼, the choice survives watch updates and is
scoped per view kind."""

from __future__ import annotations

from dataclasses import replace

from korvid.core.store import Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.metrics import PodMetrics
from korvid.k8s.models import GenericSummary, ReplicaSetSummary
from korvid.ui.widgets.describe_screen import DescribePane
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _DEFAULT_TEST_ALIASES, _pod, make_app
from .test_metrics_wiring import make_app_with_metrics
from .waits import until


def _names(table: ResourceTable) -> list[str]:
    return [str(table.get_row_at(i)[0]) for i in range(table.row_count)]


def _header_labels(table: ResourceTable) -> list[str]:
    return [str(col.label) for col in table.columns.values()]


def _deploy(name: str, created: str) -> GenericSummary:
    return GenericSummary(name=name, namespace="default", kind="Deployment", created=created)


def _metrics(name: str, cpu: float, mem: int) -> PodMetrics:
    return PodMetrics(name=name, namespace="default", cpu_cores=cpu, memory_bytes=mem)


async def test_shift_n_sorts_by_name_and_toggles_direction() -> None:
    app = make_app([_pod("beta"), _pod("alpha"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        await pilot.press("N")
        await until(
            pilot,
            lambda: _names(table) == ["alpha", "beta", "gamma"],
            label="name ascending",
        )
        assert any(label.startswith("NAME") and "▲" in label for label in _header_labels(table))
        await pilot.press("N")
        await until(
            pilot,
            lambda: _names(table) == ["gamma", "beta", "alpha"],
            label="name descending",
        )
        assert any(label.startswith("NAME") and "▼" in label for label in _header_labels(table))


async def test_shift_a_sorts_pods_newest_first() -> None:
    pods = [
        replace(_pod("old"), created="2026-07-26T08:00:00Z"),
        replace(_pod("young"), created="2026-07-26T11:00:00Z"),
        replace(_pod("mid"), created="2026-07-26T10:00:00Z"),
    ]
    app = make_app(pods)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        await pilot.press("A")
        await until(
            pilot,
            lambda: _names(table) == ["young", "mid", "old"],
            label="newest first",
        )
        # Pods have an AGE column so the ▲/▼ direction stays visible (the
        # indicator must never point at a column the user cannot see).
        assert any(label.startswith("AGE") and "▼" in label for label in _header_labels(table))


async def test_shift_c_sorts_by_cpu_missing_metrics_last() -> None:
    pods = [_pod("cold"), _pod("hot"), _pod("nometrics")]
    usage = [_metrics("cold", 0.1, 10), _metrics("hot", 1.5, 10)]
    app, _ = make_app_with_metrics(pods, [usage])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        # Wait for the metrics join before sorting on it.
        await until(
            pilot,
            lambda: any(
                "1500m" in str(cell) for i in range(table.row_count) for cell in table.get_row_at(i)
            ),
            label="metrics joined",
        )
        await pilot.press("C")
        await until(
            pilot,
            lambda: _names(table) == ["hot", "cold", "nometrics"],
            label="cpu descending, missing last",
        )
        assert any(label.startswith("CPU") and "▼" in label for label in _header_labels(table))


async def test_shift_m_sorts_by_memory() -> None:
    # Alphabetical order (hungry < tiny) opposes memory order so a no-op
    # M binding cannot pass this by accident.
    pods = [_pod("tiny"), _pod("hungry")]
    usage = [_metrics("tiny", 0.1, 10 * 2**20), _metrics("hungry", 0.1, 500 * 2**20)]
    app, _ = make_app_with_metrics(pods, [usage])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        # Wait for the metrics join before sorting on it.
        await until(
            pilot,
            lambda: any(
                "500" in str(cell) for i in range(table.row_count) for cell in table.get_row_at(i)
            ),
            label="metrics joined",
        )
        await pilot.press("M")
        await until(
            pilot,
            lambda: _names(table) == ["hungry", "tiny"],
            label="mem descending",
        )
        assert any(label.startswith("MEM") and "▼" in label for label in _header_labels(table))


async def test_sort_survives_watch_updates() -> None:
    app = make_app([_pod("bb"), _pod("aa")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await pilot.press("N")
        await pilot.press("N")  # name descending
        await until(pilot, lambda: _names(table) == ["bb", "aa"], label="descending")
        app.store.apply_event("pods", "default", "ADDED", _pod("cc"))
        await until(
            pilot,
            lambda: _names(table) == ["cc", "bb", "aa"],
            label="new row lands in sorted position",
        )


async def test_sort_is_scoped_per_view_kind() -> None:
    deploys: list[Summary] = [
        _deploy("front", "2026-07-26T08:00:00Z"),
        _deploy("back", "2026-07-26T11:00:00Z"),
    ]
    app = make_app([_pod("bb"), _pod("aa")], extra_data={"deployments": deploys})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await pilot.press("N")
        await pilot.press("N")  # pods: name descending
        await until(pilot, lambda: _names(table) == ["bb", "aa"], label="pods descending")
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        # Deployments view keeps its own (default, name-ascending) order.
        await until(pilot, lambda: _names(table) == ["back", "front"], label="deploy default")
        await pilot.press("A")
        await until(
            pilot,
            lambda: (
                _names(table) == ["back", "front"]
                and any("AGE" in label and "▼" in label for label in _header_labels(table))
            ),
            label="deploy age sort",
        )
        # Back to pods: the earlier name-descending choice is restored.
        await pilot.press("colon")
        for ch in "pods":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: _names(table) == ["bb", "aa"], label="pods sort restored")


async def test_cpu_mem_sort_is_ignored_outside_pods_view() -> None:
    """Views without CPU/MEM columns (only pods get metrics) must ignore
    Shift-C/M instead of silently replacing the order with a name sort
    while showing no indicator."""
    deploys: list[Summary] = [
        _deploy("zz-front", "2026-07-26T08:00:00Z"),
        _deploy("aa-back", "2026-07-26T11:00:00Z"),
    ]
    app = make_app([], extra_data={"deployments": deploys})
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: table.row_count == 2, label="deployments loaded")
        before = _names(table)
        await pilot.press("C")
        await pilot.press("M")
        await pilot.pause()
        assert _names(table) == before
        assert not any("▲" in label or "▼" in label for label in _header_labels(table))


async def test_replicaset_fallback_rows_interleave_in_user_sort_order() -> None:
    """With a user sort active, GenericSummary fallback rows must land in
    sorted position, not be appended after every parsed ReplicaSet."""
    rs_meta = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True, ("rs",))
    rows: list[Summary] = [
        ReplicaSetSummary(
            name="zeta",
            namespace="default",
            kind="ReplicaSet",
            created="2026-07-26T08:00:00Z",
            revision="1",
            desired=1,
            current=1,
            ready="1/1",
        ),
        GenericSummary(
            name="alpha", namespace="default", kind="ReplicaSet", created="2026-07-26T09:00:00Z"
        ),
    ]
    app = make_app(
        [],
        extra_data={"replicasets": rows},
        aliases={**_DEFAULT_TEST_ALIASES, "replicasets": rs_meta},
    )
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await pilot.press("colon")
        for ch in "replicasets":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(pilot, lambda: table.row_count == 2, label="replicasets loaded")
        await pilot.press("N")
        await until(
            pilot,
            lambda: _names(table) == ["alpha", "zeta"],
            label="fallback row interleaved by name",
        )


async def test_shift_n_still_steps_search_when_describe_pane_open() -> None:
    """N must keep meaning 'previous hit' inside an open pane, not re-sort
    (the log-pane path is covered in test_log_pane.py)."""
    app = make_app([_pod("bb"), _pod("aa")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        pane = app.query_one(DescribePane)
        pane.display = True
        await pilot.press("N")
        await pilot.pause()
        # No sort was applied: order still the default and no indicator shown.
        assert not any("▲" in label or "▼" in label for label in _header_labels(table))


# ---------------------------------------------------------------------------
# Interactive column selection (issue #138): `o` opens a sort picker; a
# header click sorts by that column. `:sort` and shift+N/A/C/M unchanged.
# ---------------------------------------------------------------------------


async def test_o_opens_sort_picker_and_applies_the_selected_column() -> None:
    from korvid.ui.widgets.pick_screen import PickScreen

    pods = [
        replace(_pod("old"), created="2026-07-26T08:00:00Z"),
        replace(_pod("young"), created="2026-07-26T11:00:00Z"),
    ]
    app = make_app(pods)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await pilot.press("o")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="picker open")
        # pods offer every builtin sortable column
        from textual.widgets import OptionList

        options = [
            str(app.screen.query_one(OptionList).get_option_at_index(i).prompt)
            for i in range(app.screen.query_one(OptionList).option_count)
        ]
        assert [o.split()[0] for o in options] == ["name", "age", "cpu", "mem"]
        await pilot.press("down")  # age
        await pilot.press("enter")
        await until(pilot, lambda: _names(table) == ["young", "old"], label="newest first")
        assert any(label.startswith("AGE") and "▼" in label for label in _header_labels(table))


async def test_sort_picker_repick_flips_direction_and_marks_active() -> None:
    from textual.widgets import OptionList

    from korvid.ui.widgets.pick_screen import PickScreen

    app = make_app([_pod("beta"), _pod("alpha")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await pilot.press("o")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="picker open")
        await pilot.press("enter")  # name ascending
        await until(pilot, lambda: _names(table) == ["alpha", "beta"], label="name ascending")
        await pilot.press("o")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="picker reopen")
        options = [
            str(app.screen.query_one(OptionList).get_option_at_index(i).prompt)
            for i in range(app.screen.query_one(OptionList).option_count)
        ]
        assert options[0].startswith("name")
        assert "▲" in options[0]  # active column is marked
        await pilot.press("enter")  # re-pick flips to descending
        await until(pilot, lambda: _names(table) == ["beta", "alpha"], label="name descending")


async def test_sort_picker_lists_custom_columns_and_hides_metrics_off_pods() -> None:
    from textual.widgets import OptionList

    from korvid.core.config import KorvidConfig, ViewConfig
    from korvid.k8s.columns import CustomColumn
    from korvid.k8s.models import GenericSummary
    from korvid.ui.widgets.pick_screen import PickScreen

    team = CustomColumn("TEAM", "label", "team")
    config = KorvidConfig(namespace="default", views={"deployments": ViewConfig(columns=(team,))})
    deploys: list[Summary] = [
        GenericSummary(
            name="api",
            namespace="default",
            kind="Deployment",
            created="2026-07-26T08:00:00Z",
            custom=("payments",),
        ),
        GenericSummary(
            name="web",
            namespace="default",
            kind="Deployment",
            created="2026-07-26T09:00:00Z",
            custom=("billing",),
        ),
    ]
    app = make_app([], extra_data={"deployments": deploys}, config=config)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="deploys loaded")
        await pilot.press("o")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="picker open")
        options = [
            str(app.screen.query_one(OptionList).get_option_at_index(i).prompt)
            for i in range(app.screen.query_one(OptionList).option_count)
        ]
        # no cpu/mem off the pods view; the custom column is offered
        assert [o.split()[0] for o in options] == ["name", "age", "TEAM"]
        await pilot.press("down", "down")  # TEAM
        await pilot.press("enter")
        await until(pilot, lambda: _names(table) == ["web", "api"], label="TEAM ascending")


async def test_header_click_sorts_by_that_column() -> None:
    app = make_app([_pod("beta"), _pod("alpha")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        from textual.widgets import DataTable

        name_key = next(iter(table.columns))
        table.post_message(
            DataTable.HeaderSelected(table, name_key, 0, table.columns[name_key].label)
        )
        await until(pilot, lambda: _names(table) == ["alpha", "beta"], label="name ascending")
        # clicking the active column flips the direction
        name_key = next(iter(table.columns))
        table.post_message(
            DataTable.HeaderSelected(table, name_key, 0, table.columns[name_key].label)
        )
        await until(pilot, lambda: _names(table) == ["beta", "alpha"], label="name descending")


async def test_header_click_on_unsortable_column_notifies() -> None:
    app = make_app([_pod("beta"), _pod("alpha")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        from textual.widgets import DataTable

        keys = list(table.columns)
        # READY is a pods column with no data-model sort key
        ready_idx = [str(table.columns[k].label) for k in keys].index("READY")
        table.post_message(
            DataTable.HeaderSelected(
                table, keys[ready_idx], ready_idx, table.columns[keys[ready_idx]].label
            )
        )
        await until(
            pilot,
            lambda: any("not sortable" in n.message for n in app._notifications),
            label="unsortable notified",
        )
        assert not any("▲" in label or "▼" in label for label in _header_labels(table))


async def test_sort_picker_handles_custom_column_names_with_spaces() -> None:
    from textual.widgets import OptionList

    from korvid.core.config import KorvidConfig, ViewConfig
    from korvid.k8s.columns import CustomColumn
    from korvid.k8s.models import GenericSummary
    from korvid.ui.widgets.pick_screen import PickScreen

    team = CustomColumn("TEAM NAME", "label", "team")
    config = KorvidConfig(namespace="default", views={"deployments": ViewConfig(columns=(team,))})
    deploys: list[Summary] = [
        GenericSummary(
            name="api",
            namespace="default",
            kind="Deployment",
            created="2026-07-26T08:00:00Z",
            custom=("payments",),
        ),
        GenericSummary(
            name="web",
            namespace="default",
            kind="Deployment",
            created="2026-07-26T09:00:00Z",
            custom=("billing",),
        ),
    ]
    app = make_app([], extra_data={"deployments": deploys}, config=config)
    async with app.run_test() as pilot:
        await pilot.press("colon")
        for ch in "deployments":
            await pilot.press(ch)
        await pilot.press("enter")
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="deploys loaded")
        await pilot.press("o")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="picker open")
        options = app.screen.query_one(OptionList)
        assert options.option_count == 3  # name, age, TEAM NAME
        await pilot.press("down", "down")  # TEAM NAME
        await pilot.press("enter")
        await until(pilot, lambda: _names(table) == ["web", "api"], label="TEAM NAME ascending")


async def test_header_click_sorts_the_clicked_pane_not_the_focused_one() -> None:
    """Split workspace: clicking a header must sort the pane that owns the
    table, not whichever pane happens to hold focus."""
    from textual.widgets import DataTable

    app = make_app([_pod("beta"), _pod("alpha")])
    async with app.run_test() as pilot:
        table0 = app.query_one(ResourceTable)
        await until(pilot, lambda: table0.row_count == 2, label="pods loaded")
        await pilot.press("ctrl+w", "v")  # split; focus moves to pane 1
        await until(pilot, lambda: len(app.query(ResourceTable)) == 2, label="split")
        tables = list(app.query(ResourceTable))
        pane0_table = next(t for t in tables if t.id == app._panes[0].table_id)
        assert app._focused_pane == 1  # pane 1 focused, pane 0 clicked
        key = next(iter(pane0_table.columns))
        pane0_table.post_message(
            DataTable.HeaderSelected(pane0_table, key, 0, pane0_table.columns[key].label)
        )
        await until(
            pilot,
            lambda: app._panes[0].sorts.get("pods") is not None,
            label="clicked pane sorted",
        )
        assert app._panes[0].sorts["pods"].column == "name"
        assert "pods" not in app._panes[1].sorts  # the focused pane untouched
        # A second click flips the clicked pane to descending - visibly.
        key = next(iter(pane0_table.columns))
        pane0_table.post_message(
            DataTable.HeaderSelected(pane0_table, key, 0, pane0_table.columns[key].label)
        )
        await until(
            pilot,
            lambda: (
                [str(pane0_table.get_row_at(i)[0]) for i in range(pane0_table.row_count)]
                == ["beta", "alpha"]
            ),
            label="clicked pane descending",
        )
