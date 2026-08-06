"""Row-cell memo (issue #208): a repaint must cost work proportional to the
rows that actually changed, not to the total row count.

Watch summaries are frozen dataclasses replaced wholesale, so a row whose
summary object is unchanged provably renders identical cells — its cells are
reused instead of rebuilt, and the in-place diff skips it without asking the
DataTable what it already holds.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest import mock

from korvid.core.config import KorvidConfig, ViewConfig
from korvid.core.store import Summary
from korvid.k8s.columns import CustomColumn
from korvid.k8s.metrics import PodMetrics
from korvid.k8s.models import PodSummary
from korvid.ui.widgets import resource_table
from korvid.ui.widgets.resource_table import ResourceTable

from .test_app import _pod, make_app
from .waits import until


class _FrozenClock:
    """Stand-in for `datetime` so a repaint can be aged at a chosen instant."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self, tz: Any = None) -> datetime:
        return self._instant


def _spy_emit(table: ResourceTable) -> list[str]:
    """Record the row key of every row whose cells are actually rebuilt."""
    built: list[str] = []
    original = table._emit_row

    def spy(obj: Any, cells: Any, **kwargs: Any) -> Any:
        built.append(f"{obj.namespace}/{obj.name}")
        return original(obj, cells, **kwargs)

    table._emit_row = spy  # type: ignore[method-assign]  # test spy
    return built


def _spy_get_row(table: ResourceTable) -> list[str]:
    """Record the diff's own `get_row` lookups.

    Textual calls `get_row` with a `RowKey` while painting; only the diff
    looks rows up by their string key, so filtering on `str` isolates it.
    """
    looked_up: list[str] = []
    original = table.get_row

    def spy(row_key: Any) -> Any:
        if isinstance(row_key, str):
            looked_up.append(row_key)
        return original(row_key)

    table.get_row = spy  # type: ignore[method-assign]  # test spy
    return looked_up


async def test_repaint_rebuilds_only_the_changed_row() -> None:
    """One MODIFIED pod must rebuild one row's cells, not every row's."""
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        built = _spy_emit(table)
        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="Failed"))
        await until(
            pilot,
            lambda: str(table.get_row("default/beta")[2]) == "Failed",
            label="phase cell updated",
        )
        assert built == ["default/beta"]


async def test_unchanged_repaint_rebuilds_nothing() -> None:
    """A repaint with the same summary objects must not rebuild any cells."""
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        built = _spy_emit(table)
        table.show("pods", app.store.get("pods", "default"), all_namespaces=False, pattern="")
        assert built == []


async def test_unchanged_rows_are_not_read_back_from_the_datatable() -> None:
    """The diff must settle rows from what this widget last emitted, never by
    reading them back out of the DataTable one `get_row` at a time."""
    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        read_row = table.get_row
        looked_up = _spy_get_row(table)
        app.store.apply_event("pods", "default", "MODIFIED", _pod("beta", phase="Failed"))
        await until(
            pilot,
            lambda: str(read_row("default/beta")[2]) == "Failed",
            label="phase cell updated",
        )
        assert looked_up == []


async def test_changed_metrics_rebuild_the_row_without_a_new_summary() -> None:
    """Usage cells come from the metrics sample, not the summary: a new sample
    for the same pod object must still re-render that row."""
    samples: dict[tuple[str, str], PodMetrics] = {}

    def lookup(namespace: str, name: str) -> PodMetrics | None:
        return samples.get((namespace, name))

    app = make_app([_pod("alpha")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pods loaded")
        rows = app.store.get("pods", "default")
        table.show("pods", rows, all_namespaces=False, pattern="", metrics=lookup)
        built = _spy_emit(table)
        samples[("default", "alpha")] = PodMetrics(
            name="alpha", namespace="default", cpu_cores=0.5, memory_bytes=1024 * 1024
        )
        table.show("pods", rows, all_namespaces=False, pattern="", metrics=lookup)
        assert built == ["default/alpha"]
        assert str(table.get_row("default/alpha")[4]) != "-"


async def test_view_signature_change_discards_the_memo() -> None:
    """Switching to the all-namespaces column set must rebuild every row: the
    memoised cells were built without the NAMESPACE column."""
    app = make_app([_pod("alpha"), _pod("beta")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 2, label="pods loaded")
        built = _spy_emit(table)
        table.show("pods", app.store.get("pods", "default"), all_namespaces=True, pattern="")
        assert built == ["default/alpha", "default/beta"]
        assert str(table.get_row("default/alpha")[0]) == "default"


async def test_age_uses_one_clock_reading_per_repaint() -> None:
    """Every row in a repaint is aged against the same instant, so a repaint
    that straddles a second boundary cannot report inconsistent ages."""
    import korvid.k8s.models as models

    app = make_app([_pod("alpha"), _pod("beta"), _pod("gamma")])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 3, label="pods loaded")
        original = models.format_age
        seen: list[Any] = []

        def counting_format_age(value: str, now: Any = None) -> str:
            seen.append(now)
            return original(value, now=now)

        models.format_age = counting_format_age  # type: ignore[assignment]  # test spy
        try:
            # Fresh summary objects: identity differs, so every row rebuilds.
            fresh: list[Summary] = [_pod("alpha"), _pod("beta"), _pod("gamma")]
            table.show("pods", fresh, all_namespaces=False, pattern="")
        finally:
            models.format_age = original
        assert len(seen) >= 3, seen
        assert all(now is not None for now in seen), seen
        assert len(set(seen)) == 1, seen


async def test_age_refreshes_for_an_unchanged_summary_as_time_passes() -> None:
    """The memo must not freeze AGE: a pod nobody touches still ages, so the
    stamp has to carry the age string and re-render the row when it rolls over."""
    created = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    pod = PodSummary(
        name="alpha",
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        created=created,
    )
    app = make_app([pod])
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pod loaded")
        rows: list[Summary] = [pod]
        table.show("pods", rows, all_namespaces=False, pattern="")
        assert str(table.get_row("default/alpha")[-2]) == "5m"
        # Same summary object, clock advanced past the next minute boundary.
        with mock.patch.object(
            resource_table, "datetime", _FrozenClock(datetime.now(UTC) + timedelta(minutes=1))
        ):
            table.show("pods", rows, all_namespaces=False, pattern="")
        assert str(table.get_row("default/alpha")[-2]) == "6m"


async def test_replace_view_rows_ignore_hidden_volatile_cells() -> None:
    """A `replace: true` custom view keeps only NAME plus the configured
    values — all carried by the frozen summary. Nothing volatile survives into
    the row, so a metrics poll must not rebuild cells the view discards."""
    samples: dict[tuple[str, str], PodMetrics] = {}

    def lookup(namespace: str, name: str) -> PodMetrics | None:
        return samples.get((namespace, name))

    config = KorvidConfig(
        namespace="default",
        views={"pods": ViewConfig(columns=(CustomColumn("TEAM", "label", "team"),), replace=True)},
    )
    app = make_app([replace(_pod("alpha"), custom=("payments",))], config=config)
    async with app.run_test() as pilot:
        table = app.query_one(ResourceTable)
        await until(pilot, lambda: table.row_count == 1, label="pods loaded")
        view = config.views["pods"]
        rows = app.store.get("pods", "default")
        table.show("pods", rows, all_namespaces=False, pattern="", metrics=lookup, view=view)
        built = _spy_emit(table)
        samples[("default", "alpha")] = PodMetrics(
            name="alpha", namespace="default", cpu_cores=0.5, memory_bytes=1024 * 1024
        )
        table.show("pods", rows, all_namespaces=False, pattern="", metrics=lookup, view=view)
        assert built == []
        assert [str(cell) for cell in table.get_row("default/alpha")] == ["alpha", "payments"]
