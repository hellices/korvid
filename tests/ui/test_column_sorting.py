"""Column sorting UI tests (issue #37): shift+N/A/C/M toggle data-model
sorts, the header shows ▲/▼, the choice survives watch updates and is
scoped per view kind."""

from __future__ import annotations

from dataclasses import replace

from korvid.core.store import Summary
from korvid.k8s.metrics import PodMetrics
from korvid.k8s.models import GenericSummary
from korvid.ui.widgets.describe_screen import DescribePane
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
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
    pods = [_pod("small"), _pod("big")]
    usage = [_metrics("small", 0.1, 10 * 2**20), _metrics("big", 0.1, 500 * 2**20)]
    app, _ = make_app_with_metrics(pods, [usage])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        await pilot.press("M")
        await until(
            pilot,
            lambda: _names(table) == ["big", "small"],
            label="mem descending",
        )


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


async def test_shift_n_still_steps_search_when_log_pane_open() -> None:
    """N must keep meaning 'previous hit' inside an open pane, not re-sort."""
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
