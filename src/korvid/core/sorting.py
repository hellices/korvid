"""User-controlled column sorting for resource tables (issue #37).

Pure functions/dataclasses — no Textual imports. Sorting operates on the
data model, never on rendered strings: CPU compares `cpu_cores` floats
(`150m` < `1`), age compares creation timestamps (`3h` vs `25m` never
compares lexically), and rows with missing values sort last in both
directions with a deterministic (namespace, name) tie-break.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from korvid.core.store import Summary
from korvid.k8s.metrics import PodMetrics

#: Sortable columns; CPU/MEM only produce values on views with a metrics lookup.
SORT_COLUMNS = ("name", "age", "cpu", "mem")

#: Looks up live metrics for (namespace, name); None disables metric sorts.
MetricsLookup = Callable[[str, str], PodMetrics | None]


@dataclass(frozen=True)
class SortSpec:
    """A user-selected sort: which column and which direction."""

    column: str
    descending: bool


def toggle_sort(current: SortSpec | None, column: str) -> SortSpec:
    """Next sort state after pressing a sort key.

    Repeating the active column flips the direction. Selecting a new column
    starts ascending for `name` and descending for `age`/`cpu`/`mem` —
    triage starts from "newest / hungriest first".
    """
    if current is not None and current.column == column:
        return SortSpec(column, not current.descending)
    # "Newest / hungriest first" for the metric-like builtins; name and
    # custom string columns start ascending.
    return SortSpec(column, column in ("age", "cpu", "mem"))


#: Custom column values that mean "no comparable value" (issue #45).
_CUSTOM_MISSING = {"<none>", "<err>"}


def _custom_value(row: Summary, index: int) -> str | None:
    """The row's custom column string at *index*, or None when missing."""
    values: tuple[str, ...] = getattr(row, "custom", ())
    if index >= len(values) or values[index] in _CUSTOM_MISSING:
        return None
    return values[index].lower()


def _value(row: Summary, column: str, metrics: MetricsLookup | None) -> float | str | None:
    """The comparable data-model value for a row, or None when missing."""
    if column == "name":
        return row.name.lower()
    if column == "age":
        # Parse to an epoch key: RFC 3339 strings are not chronological
        # lexically once offsets or fractional seconds differ
        # (`10:00+01:00` sorts after `09:30Z` but is older).
        created = getattr(row, "created", "")
        if not created:
            return None
        try:
            return datetime.fromisoformat(created).timestamp()
        except ValueError:
            return None  # unparsable timestamps are missing, not comparable
    if metrics is None:
        return None
    usage = metrics(row.namespace, row.name)
    if usage is None:
        return None
    return usage.cpu_cores if column == "cpu" else float(usage.memory_bytes)


def sort_rows(
    rows: Sequence[Summary],
    spec: SortSpec,
    *,
    metrics: MetricsLookup | None = None,
    custom_columns: Sequence[str] = (),
) -> list[Summary]:
    """Rows reordered by the spec; missing values always sort last.

    Args:
        rows: Summaries in any order.
        spec: Active column + direction.
        metrics: Live usage lookup; required for `cpu`/`mem` values (rows
            without a sample — or all rows when the lookup is None — are
            treated as missing).
        custom_columns: Names of the view's user-configured columns (issue
            #45), in declared order; matching sorts compare the row's
            `custom` strings case-insensitively.

    Raises:
        ValueError: If `spec.column` is neither in `SORT_COLUMNS` nor in
            `custom_columns`.
    """
    keyed: list[tuple[Any, Summary]]
    if spec.column in custom_columns:
        index = list(custom_columns).index(spec.column)
        keyed = [(_custom_value(row, index), row) for row in rows]
    elif spec.column in SORT_COLUMNS:
        keyed = [(_value(row, spec.column, metrics), row) for row in rows]
    else:
        raise ValueError(f"unsupported sort column {spec.column!r}; expected one of {SORT_COLUMNS}")
    present = [(value, row) for value, row in keyed if value is not None]
    missing = [row for value, row in keyed if value is None]
    # Two stable passes keep the (namespace, name) tie-break ascending even
    # when the value order is descending.
    present.sort(key=lambda pair: (pair[1].namespace, pair[1].name))
    present.sort(key=lambda pair: pair[0], reverse=spec.descending)
    missing.sort(key=lambda row: (row.namespace, row.name))
    return [row for _, row in present] + missing
