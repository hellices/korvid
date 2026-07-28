"""Resource list table — supports pods (rich 8 columns) and any generic kind."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from rich.text import Text
from textual.coordinate import Coordinate
from textual.widgets import DataTable
from textual.widgets.data_table import RowDoesNotExist

from korvid.core.config import ViewConfig
from korvid.core.sorting import SortSpec, sort_rows
from korvid.core.store import Summary
from korvid.k8s.columns import MISSING
from korvid.k8s.helm import HelmReleaseSummary, HelmRevisionSummary
from korvid.k8s.metrics import PodMetrics
from korvid.k8s.models import (
    ContainerLimits,
    CSVSummary,
    GenericSummary,
    OLMSubscriptionSummary,
    PackageManifestSummary,
    PodSummary,
    ReplicaSetSummary,
    format_cpu,
    format_memory,
)
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.ui.theme import phase_style, ready_style, restarts_style, usage_style

#: Looks up live metrics for (namespace, name); None disables the join.
MetricsLookup = Callable[[str, str], PodMetrics | None]

_POD_COLS = (
    "NAME",
    "READY",
    "STATUS",
    "RESTARTS",
    "CPU",
    "%CPU/R",
    "MEM",
    "%MEM/R",
    "CPU R/L",
    "MEM R/L",
    "QOS",
    "AGE",
    "NODE",
)
_POD_COLS_ALL_NS = ("NAMESPACE", *_POD_COLS)
_RS_COLS = ("NAME", "REVISION", "DESIRED", "CURRENT", "READY", "AGE")
_RS_COLS_ALL_NS = ("NAMESPACE", *_RS_COLS)
_HELM_COLS = ("NAME", "REVISION", "STATUS", "CHART", "APP VERSION", "AGE")
_HELM_COLS_ALL_NS = ("NAMESPACE", *_HELM_COLS)
_HELM_REV_COLS = ("NAME", "REVISION", "STATUS", "CHART", "APP VERSION", "DESCRIPTION", "AGE")
_HELM_REV_COLS_ALL_NS = ("NAMESPACE", *_HELM_REV_COLS)
_GENERIC_COLS = ("NAME", "AGE")
_GENERIC_COLS_ALL_NS = ("NAMESPACE", "NAME", "AGE")
_PKG_COLS = ("NAME", "CATALOG", "DEFAULT CHANNEL", "CHANNELS", "DESCRIPTION", "AGE")
_PKG_COLS_ALL_NS = ("NAMESPACE", *_PKG_COLS)
_SUB_COLS = ("NAME", "CHANNEL", "SOURCE", "INSTALLED CSV", "STATE", "AGE")
_SUB_COLS_ALL_NS = ("NAMESPACE", *_SUB_COLS)
_CSV_COLS = ("NAME", "DISPLAY NAME", "VERSION", "PHASE", "AGE")
_CSV_COLS_ALL_NS = ("NAMESPACE", *_CSV_COLS)

#: Helm release/revision status colors: steady-state good is green, hard
#: failure red, history entries dim, anything transitional yellow.
_HELM_STATUS_STYLE = {"deployed": "green", "failed": "bold red", "superseded": "dim"}

# Eviction order reversed: pods evicted last render first.
_QOS_RANK = {"Guaranteed": 0, "Burstable": 1, "BestEffort": 2}
# Red is too aggressive for a steady-state view: green → chartreuse → yellow.
_QOS_STYLE = {"Guaranteed": "green", "Burstable": "chartreuse2", "BestEffort": "yellow"}


def _pod_sort_key(pod: PodSummary) -> tuple[int, str]:
    return (_QOS_RANK.get(pod.qos, 3), pod.name)


def _phase_cell(phase: str) -> Text:
    return Text(phase, style=phase_style(phase))


def _cell_width(cell: str | Text) -> int:
    """Rendered width of a table cell (single-line str or Text).

    DataTable renders `str` cells through `Text.from_markup`, so raw markup
    (possible in custom-column values, issue #45) must be measured the same
    way — measuring the raw string would overestimate and trigger a
    column-shrinking width rescan.
    """
    if isinstance(cell, Text):
        return cell.cell_len
    return Text.from_markup(cell, end="").cell_len


def _cells_equal(a: str | Text, b: str | Text) -> bool:
    """Style-aware cell comparison for the in-place diff.

    `Text.__eq__` compares plain text only — a phase flipping color with the
    same wording (e.g. Running turning yellow) is still a visible change and
    must not be skipped.
    """
    if isinstance(a, Text) or isinstance(b, Text):
        if not (isinstance(a, Text) and isinstance(b, Text)):
            return False
        return a.plain == b.plain and str(a.style) == str(b.style)
    return a == b


def _helm_status_cell(status: str) -> Text:
    return Text(status, style=_HELM_STATUS_STYLE.get(status, "yellow"))


_CSV_PHASE_STYLE = {"Succeeded": "green", "Failed": "bold red"}


def _csv_phase_cell(phase: str) -> Text:
    """CSV install phase: Succeeded green, Failed loud, transitional yellow."""
    return Text(phase, style=_CSV_PHASE_STYLE.get(phase, "yellow"))


_COLS_BY_KIND: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "pods": (_POD_COLS, _POD_COLS_ALL_NS),
    "replicasets": (_RS_COLS, _RS_COLS_ALL_NS),
    "helmreleases": (_HELM_COLS, _HELM_COLS_ALL_NS),
    "helmrevisions": (_HELM_REV_COLS, _HELM_REV_COLS_ALL_NS),
    "packagemanifests": (_PKG_COLS, _PKG_COLS_ALL_NS),
    "subscriptions": (_SUB_COLS, _SUB_COLS_ALL_NS),
    "clusterserviceversions": (_CSV_COLS, _CSV_COLS_ALL_NS),
}


#: OLM plurals are only special when served by the OLM API groups: a CRD
#: from another group whose plural happens to be "subscriptions" must keep
#: the generic rendering (its summaries are generic too).
_KIND_GROUPS: dict[str, str] = {
    "packagemanifests": PACKAGES_GROUP,
    "subscriptions": OPERATORS_GROUP,
    "clusterserviceversions": OPERATORS_GROUP,
}


def _typed_kind(kind: str, group: str) -> str:
    """*kind* when its typed rendering applies to this API *group*, else a
    name that falls through every typed lookup to the generic path."""
    expected = _KIND_GROUPS.get(kind)
    if expected is not None and group != expected:
        return f"{group}/{kind}"
    return kind


def _columns_for(kind: str, *, all_namespaces: bool, view: ViewConfig | None) -> tuple[str, ...]:
    """Column headers for *kind*; unknown kinds get the generic NAME/AGE set.

    A configured view (issue #45) appends its custom column names after the
    defaults, or replaces everything but the identity columns (NAME, and
    NAMESPACE in all-namespaces mode) when `replace` is set.
    """
    single, all_ns = _COLS_BY_KIND.get(kind, (_GENERIC_COLS, _GENERIC_COLS_ALL_NS))
    base = all_ns if all_namespaces else single
    if view is None:
        return base
    names = tuple(column.name for column in view.columns)
    if view.replace:
        head = ("NAMESPACE", "NAME") if all_namespaces else ("NAME",)
        return (*head, *names)
    return (*base, *names)


def sanitize_views(
    views: dict[str, ViewConfig],
) -> tuple[dict[str, ViewConfig], tuple[str, ...]]:
    """Drop custom columns that shadow a kind's actual built-in headers.

    Config parsing rejects the universal identity/sort names, but only the
    UI knows each kind's full header set (STATUS, READY, NODE, ...). A
    shadowing name would render two identical headers and decorate both
    with the sort arrow. `replace: true` views keep such names — their
    built-ins are hidden. Called once from the composition root.
    """
    sanitized: dict[str, ViewConfig] = {}
    warnings: list[str] = []
    for kind, view in views.items():
        if view.replace:
            sanitized[kind] = view
            continue
        single, all_ns = _COLS_BY_KIND.get(kind, (_GENERIC_COLS, _GENERIC_COLS_ALL_NS))
        builtin = {header.lower() for header in (*single, *all_ns)}
        kept = tuple(column for column in view.columns if column.name.lower() not in builtin)
        for column in view.columns:
            if column.name.lower() in builtin:
                warnings.append(
                    f"views.{kind}.{column.name}: shadows a built-in column of this kind"
                )
        if kept:
            sanitized[kind] = ViewConfig(columns=kept, replace=view.replace)
    return sanitized, tuple(warnings)


def _ready_cell(ready: str) -> Text:
    return Text(ready, style=ready_style(ready))


def _restarts_cell(restarts: int) -> Text:
    return Text(str(restarts), style=restarts_style(restarts))


#: Ranks usage styles so mixed signals (a limited container near its own
#: ceiling vs an unlimited one bursting) resolve to the more severe color.
_STYLE_SEVERITY = {"green": 0, "dim": 0, "yellow": 1, "bold red": 2}


def _max_container_pct(
    metrics: PodMetrics, limits: tuple[ContainerLimits, ...], key: str
) -> tuple[int | None, bool]:
    """(max per-container usage/limit percent, every sampled container limited).

    Limits are enforced per container by the kubelet, so the danger signal
    is the worst individual ratio - a pod-aggregate sum hides a sidecar
    sitting at its own 100Mi limit next to an idle 900Mi neighbour."""
    by_name = {c.name: (c.cpu_cores if key == "cpu" else c.mem_bytes) for c in limits}
    worst: float | None = None
    all_limited = bool(metrics.containers)
    for sample in metrics.containers:
        usage = sample.cpu_cores if key == "cpu" else float(sample.memory_bytes)
        limit = by_name.get(sample.name)
        if limit is None or limit <= 0:
            all_limited = False
            continue
        pct = usage / float(limit) * 100
        worst = pct if worst is None else max(worst, pct)
    return (None if worst is None else round(worst)), all_limited


def _usage_severity(
    displayed: int,
    usage: float,
    pod: PodSummary,
    metrics: PodMetrics,
    key: str,
) -> str:
    """Style for a usage cell. The number shown is usage-vs-request, but the
    color answers a different question - proximity to an enforced ceiling
    (issue #50). Both ceilings count: a pod-level limit caps the aggregate
    cgroup while each container cgroup still enforces its own limit, so the
    style is the most severe of the two.  When neither fully bounds usage
    (no pod limit and some container unlimited), a yellow-capped
    request-based fallback joins in (burst is expected, never critical).
    """
    pod_limit = pod.cpu_limit_cores if key == "cpu" else pod.mem_limit_bytes
    if pod_limit is not None and pod_limit <= 0:
        pod_limit = None
    worst_pct, all_limited = _max_container_pct(metrics, pod.container_limits, key)
    styles: list[str] = []
    if pod_limit is not None:
        styles.append(usage_style(round(usage / float(pod_limit) * 100)))
    if worst_pct is not None:
        styles.append(usage_style(worst_pct))
    if pod_limit is None and not (worst_pct is not None and all_limited):
        styles.append(usage_style(displayed, cap_at_warn=True))
    return max(styles, key=lambda st: _STYLE_SEVERITY.get(st, 0))


def _percent_of_request(
    usage: float, request: float | None, pod: PodSummary, metrics: PodMetrics, key: str
) -> Text:
    """Usage as % of the exact declared request; '-' when no request is
    declared. The number and the color deliberately answer different
    questions: the number is usage relative to the *request* (scheduling
    footprint), the color is severity relative to enforced *limits* - so
    284%R can legitimately render green. Thresholds are applied to rounded
    values, never to a value the user cannot see (69.9 rounds to 70 before
    the yellow comparison).
    """
    if request is None or request <= 0:
        return Text("-", style="dim")
    displayed = round(usage / request * 100)
    return Text(str(displayed), style=_usage_severity(displayed, usage, pod, metrics, key))


def _usage_cells(pod: PodSummary, metrics: PodMetrics | None) -> tuple[Text, Text, Text, Text]:
    """CPU, %CPU/R, MEM, %MEM/R cells; all '-' without metrics (issue #12:
    graceful degradation when metrics-server is absent)."""
    if metrics is None:
        dash = Text("-", style="dim")
        return (dash, dash.copy(), dash.copy(), dash.copy())
    return (
        Text(format_cpu(metrics.cpu_cores)),
        _percent_of_request(metrics.cpu_cores, pod.cpu_request_cores, pod, metrics, "cpu"),
        Text(format_memory(metrics.memory_bytes)),
        _percent_of_request(
            float(metrics.memory_bytes),
            None if pod.mem_request_bytes is None else float(pod.mem_request_bytes),
            pod,
            metrics,
            "memory",
        ),
    )


def _replicaset_sort_key(rs: ReplicaSetSummary) -> tuple[str, int, str]:
    """Rollout-history order: newest revision first within each namespace
    (matching what ``kubectl rollout history`` users expect); replicasets
    without a numeric revision annotation sort last."""
    revision = -int(rs.revision) if rs.revision.isdigit() else 1
    return (rs.namespace, revision, rs.name)


#: Sort column → header label it decorates with the ▲/▼ indicator.
_SORT_LABELS = {"name": "NAME", "age": "AGE", "cpu": "CPU", "mem": "MEM"}


def _decorate_columns(
    columns: tuple[str, ...], sort: SortSpec | None, custom_names: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Append ▲/▼ to the sorted column's header; untouched when inactive."""
    if sort is None:
        return columns
    label = sort.column if sort.column in custom_names else _SORT_LABELS.get(sort.column)
    arrow = "▼" if sort.descending else "▲"
    return tuple(f"{col} {arrow}" if col == label else col for col in columns)


class ResourceTable(DataTable[str | Text]):
    _last_kind: str | None = None
    _last_all_namespaces: bool | None = None
    _last_sort: SortSpec | None = None
    _active_view: ViewConfig | None = None

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._last_kind = None
        self._last_all_namespaces = None
        self._last_sort = None
        self._active_view = None
        self._pending_rows: list[tuple[str, list[str | Text]]] = []

    def show(
        self,
        kind: str,
        rows: list[Summary],
        *,
        all_namespaces: bool,
        pattern: str,
        metrics: MetricsLookup | None = None,
        group: str = "",
        sort: SortSpec | None = None,
        view: ViewConfig | None = None,
    ) -> None:
        """Render rows into the table; rebuilds columns when (kind, all_namespaces, sort) changes.

        ``group`` is the API group serving *kind*: typed renderings that are
        specific to one group (the OLM tables) apply only there. ``view`` is
        the kind's custom column config (issue #45), if any.
        """
        kind = _typed_kind(kind, group)
        # Same logical view (kind/scope/columns) means the cursor should
        # survive the re-render — including a sort change, where the selected
        # resource moves with its row key (issue #89). Only a different
        # resource set resets to the top.
        same_view = (kind, all_namespaces, view) == (
            self._last_kind,
            self._last_all_namespaces,
            self._active_view,
        )
        restore = self._cursor_snapshot() if same_view else None
        # Build the desired rows first (the builders only fill
        # `_pending_rows`), so a pure data refresh can be diffed against the
        # current table instead of clearing and rebuilding it.
        self._active_view = view
        self._pending_rows = []
        self._render_rows(
            kind, rows, all_namespaces=all_namespaces, pattern=pattern, metrics=metrics, sort=sort
        )
        pending, self._pending_rows = self._pending_rows, []
        if same_view and sort == self._last_sort and self._apply_in_place(pending):
            # Nothing was cleared, so the scroll offset never moved; only
            # the cursor may have slid when rows above it were removed —
            # and even with scroll=False, Textual's cursor watcher schedules
            # a deferred _scroll_cursor_into_view on an index change, so the
            # viewport must be re-asserted after it (same as the rebuild
            # path below).
            if restore is not None:
                offset = (self.scroll_x, self.scroll_y)
                self._restore_cursor(*restore, scroll=False)
                self.call_after_refresh(
                    self.scroll_to, *offset, animate=False, immediate=True, force=True
                )
            return
        if not same_view or sort != self._last_sort:
            viewport = None
            self.clear(columns=True)
            custom_names = tuple(column.name for column in view.columns) if view else ()
            self.add_columns(
                *_decorate_columns(
                    _columns_for(kind, all_namespaces=all_namespaces, view=view),
                    sort,
                    custom_names,
                )
            )
            self._last_kind = kind
            self._last_all_namespaces = all_namespaces
            self._last_sort = sort
        else:
            # Reorder fallback (e.g. a pod inserted mid-table — add_row can
            # only append): DataTable.clear() resets the scroll offset to
            # (0, 0), which would yank a user inspecting the right-hand
            # columns (or a scrolled viewport) back to the top-left. Snapshot
            # the viewport and restore it after the rows are re-added; the
            # cursor snapshot above restores the selection.
            viewport = (self.scroll_x, self.scroll_y)
            self.clear()
        for key, cells in pending:
            self.add_row(*cells, key=key)
        if restore is not None:
            # A background refresh must not scroll the cursor back into
            # view — the viewport restore below keeps the user's position.
            self._restore_cursor(*restore, scroll=viewport is None)
        if viewport is not None:
            # `immediate=True` + `force=True`: the default defers via
            # call_after_refresh, and scrollbar visibility (overflow: auto)
            # may not be recomputed yet right after clear() — the user had
            # already reached this offset, so restore it unconditionally.
            self.scroll_to(*viewport, animate=False, immediate=True, force=True)
            # Even with scroll=False, `watch_cursor_coordinate` schedules a
            # deferred `_scroll_cursor_into_view` whenever the cursor index
            # changed (e.g. a row inserted above it) — which would override
            # the restore above. Re-assert the viewport after that deferred
            # scroll has run.
            self.call_after_refresh(
                self.scroll_to, *viewport, animate=False, immediate=True, force=True
            )

    def _apply_in_place(self, pending: list[tuple[str, list[str | Text]]]) -> bool:
        """Diff *pending* against the current rows and patch the table in
        place; returns False when the update needs the rebuild path.

        In place is only possible when the surviving rows keep their relative
        order and new rows land at the bottom (`add_row` can only append).
        Width updates are requested only for cells wider than their column:
        `update_width=True` is not grow-only — a narrower replacement rescans
        and *shrinks* the column, shifting the layout this path must keep
        still. The trade-off is that a column stays at its widest-seen size
        until the next rebuild.
        """
        current_keys = [row.key.value for row in self.ordered_rows]
        if any(key is None for key in current_keys):
            return False
        new_keys = [key for key, _ in pending]
        current_set = set(current_keys)
        new_set = set(new_keys)
        survivors = [key for key in current_keys if key in new_set]
        fresh = [key for key in new_keys if key not in current_set]
        if new_keys != [*survivors, *fresh]:
            return False
        columns = self.ordered_columns
        if any(len(cells) != len(columns) for _, cells in pending):
            # A malformed row (e.g. a custom view filling a different number
            # of extras per row) must not blow up the refresh tick with a
            # ValueError from zip(strict=True)/add_row: let the rebuild path
            # repaint the whole table instead.
            return False
        for key in current_keys:
            if key not in new_set:
                self.remove_row(cast(str, key))
        for key, cells in pending:
            if key in current_set:
                old_cells = self.get_row(key)
                for column, old_cell, new_cell in zip(columns, old_cells, cells, strict=True):
                    if not _cells_equal(old_cell, new_cell):
                        grew = _cell_width(new_cell) > column.content_width
                        self.update_cell(key, column.key, new_cell, update_width=grew)
            else:
                self.add_row(*cells, key=key)
        return True

    def _cursor_snapshot(self) -> tuple[str, int] | None:
        """(row key, row index) under the cursor, or None on an empty table."""
        if self.row_count == 0 or self.cursor_row < 0:
            return None
        key = self.coordinate_to_cell_key(Coordinate(self.cursor_row, 0)).row_key.value
        return (key, self.cursor_row) if key is not None else None

    def _restore_cursor(self, key: str, index: int, *, scroll: bool = True) -> None:
        """Move the cursor back to *key*; when that row is gone (deleted
        resource), clamp to the old index so the cursor stays in place
        instead of jumping to the top."""
        if self.row_count == 0:
            return
        try:
            row = self.get_row_index(key)
        except RowDoesNotExist:
            row = min(index, self.row_count - 1)
        self.move_cursor(row=row, animate=False, scroll=scroll)

    def _emit_row(self, obj: Summary, cells: list[str | Text], *, all_namespaces: bool) -> None:
        """Finish one row: apply the custom view (issue #45), prepend the
        namespace in all-namespaces mode, and buffer it keyed by ns/name for
        `show()` to diff or add.

        *cells* is the kind's default cell list without the namespace. Rows
        whose summaries carry fewer custom values than configured (e.g.
        seeded before the config existed) pad with `<none>`.
        """
        view = self._active_view
        if view is not None:
            values: tuple[str, ...] = getattr(obj, "custom", ())
            extras: list[str | Text] = [
                values[i] if i < len(values) else MISSING for i in range(len(view.columns))
            ]
            cells = [cells[0], *extras] if view.replace else [*cells, *extras]
        if all_namespaces:
            cells.insert(0, obj.namespace)
        self._pending_rows.append((f"{obj.namespace}/{obj.name}", cells))

    def _render_rows(
        self,
        kind: str,
        rows: list[Summary],
        *,
        all_namespaces: bool,
        pattern: str,
        metrics: MetricsLookup | None,
        sort: SortSpec | None = None,
    ) -> None:
        if sort is not None:
            # User-selected order wins over the per-kind defaults below; the
            # keys come from the data model (issue #37), pre-applied here so
            # every row path renders in the same order.
            custom_names = (
                tuple(column.name for column in self._active_view.columns)
                if self._active_view is not None
                else ()
            )
            rows = sort_rows(rows, sort, metrics=metrics, custom_columns=custom_names)
        presorted = sort is not None
        if kind == "pods":
            self._add_pod_rows(
                rows,
                all_namespaces=all_namespaces,
                pattern=pattern,
                metrics=metrics,
                presorted=presorted,
            )
        elif kind == "replicasets":
            self._add_replicaset_rows(
                rows, all_namespaces=all_namespaces, pattern=pattern, presorted=presorted
            )
        elif kind == "helmreleases":
            self._add_helm_release_rows(
                rows, all_namespaces=all_namespaces, pattern=pattern, presorted=presorted
            )
        elif kind == "helmrevisions":
            self._add_helm_revision_rows(
                rows, all_namespaces=all_namespaces, pattern=pattern, presorted=presorted
            )
        elif kind == "packagemanifests":
            self._add_package_rows(
                rows, all_namespaces=all_namespaces, pattern=pattern, presorted=presorted
            )
        elif kind == "subscriptions":
            self._add_subscription_rows(
                rows, all_namespaces=all_namespaces, pattern=pattern, presorted=presorted
            )
        elif kind == "clusterserviceversions":
            self._add_csv_rows(
                rows, all_namespaces=all_namespaces, pattern=pattern, presorted=presorted
            )
        else:
            self._add_generic_rows(
                rows, all_namespaces=all_namespaces, pattern=pattern, presorted=presorted
            )

    def _add_pod_rows(
        self,
        rows: list[Summary],
        *,
        all_namespaces: bool,
        pattern: str,
        metrics: MetricsLookup | None,
        presorted: bool = False,
    ) -> None:
        pods = cast(list[PodSummary], rows)
        if not presorted:
            pods = sorted(pods, key=_pod_sort_key)
        for pod in pods:
            if pattern and pattern.lower() not in pod.name.lower():
                continue
            usage = metrics(pod.namespace, pod.name) if metrics is not None else None
            cells: list[str | Text] = [
                pod.name,
                _ready_cell(pod.ready),
                _phase_cell(pod.phase),
                _restarts_cell(pod.restarts),
                *_usage_cells(pod, usage),
                f"{pod.cpu_request}/{pod.cpu_limit}",
                f"{pod.mem_request}/{pod.mem_limit}",
                Text(pod.qos, style=_QOS_STYLE.get(pod.qos, "dim")),
                pod.age(),
                pod.node or "-",
            ]
            self._emit_row(pod, cells, all_namespaces=all_namespaces)

    def _add_replicaset_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str, presorted: bool = False
    ) -> None:
        # With a user sort active the incoming order is final: render it in
        # one pass so fallback rows interleave in sorted position instead of
        # being appended after every parsed ReplicaSet. The default view
        # keeps rollout-history order with unparsed rows last.
        if presorted:
            ordered: list[Summary] = list(rows)
        else:
            replicasets = sorted(
                (r for r in rows if isinstance(r, ReplicaSetSummary)), key=_replicaset_sort_key
            )
            # Rows that reached this view without ReplicaSet parsing (e.g. a
            # future path that skips summary_for) still render NAME/AGE rather
            # than silently disappearing.
            fallbacks = sorted(
                (r for r in rows if not isinstance(r, ReplicaSetSummary)),
                key=lambda o: (o.namespace, o.name),
            )
            ordered = [*replicasets, *fallbacks]
        for obj in ordered:
            if pattern and pattern.lower() not in obj.name.lower():
                continue
            if isinstance(obj, ReplicaSetSummary):
                cells: list[str | Text] = [
                    obj.name,
                    obj.revision,
                    str(obj.desired),
                    str(obj.current),
                    _ready_cell(obj.ready),
                    obj.age(),
                ]
            else:
                age = obj.age() if isinstance(obj, GenericSummary) else ""
                cells = [obj.name, "", "", "", "", age]
            self._emit_row(obj, cells, all_namespaces=all_namespaces)

    def _add_helm_release_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str, presorted: bool = False
    ) -> None:
        releases = [r for r in rows if isinstance(r, HelmReleaseSummary)]
        if not presorted:
            releases = sorted(releases, key=lambda r: (r.namespace, r.name))
        for rel in releases:
            if pattern and pattern.lower() not in rel.name.lower():
                continue
            cells: list[str | Text] = [
                rel.name,
                str(rel.revision),
                _helm_status_cell(rel.status),
                rel.chart,
                rel.app_version,
                rel.age(),
            ]
            self._emit_row(rel, cells, all_namespaces=all_namespaces)

    def _add_helm_revision_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str, presorted: bool = False
    ) -> None:
        revisions = [r for r in rows if isinstance(r, HelmRevisionSummary)]
        # Newest revision first: helm history order, matching replicaset views.
        if not presorted:
            revisions = sorted(revisions, key=lambda r: (r.namespace, r.release, -r.revision))
        for rev in revisions:
            if pattern and pattern.lower() not in rev.name.lower():
                continue
            cells: list[str | Text] = [
                rev.name,
                str(rev.revision),
                _helm_status_cell(rev.status),
                rev.chart,
                rev.app_version,
                rev.description,
                rev.age(),
            ]
            self._emit_row(rev, cells, all_namespaces=all_namespaces)

    def _add_fallback_rows(
        self,
        rows: list[Summary],
        *,
        all_namespaces: bool,
        pattern: str,
        width: int,
        presorted: bool = False,
    ) -> None:
        """NAME + blank middle cells + AGE for rows that reached a typed view
        without the matching summary class (e.g. a same-plural kind from a
        different API group) - they render rather than silently disappearing."""
        if not presorted:
            rows = sorted(rows, key=lambda o: (o.namespace, o.name))
        for obj in rows:
            if pattern and pattern.lower() not in obj.name.lower():
                continue
            age = obj.age() if isinstance(obj, GenericSummary) else ""
            cells: list[str | Text] = [obj.name, *[""] * (width - 2), age]
            self._emit_row(obj, cells, all_namespaces=all_namespaces)

    def _add_package_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str, presorted: bool = False
    ) -> None:
        packages = [r for r in rows if isinstance(r, PackageManifestSummary)]
        if not presorted:
            packages = sorted(packages, key=lambda p: (p.namespace, p.name))
        for pkg in packages:
            if pattern and pattern.lower() not in pkg.name.lower():
                continue
            cells: list[str | Text] = [
                pkg.name,
                pkg.catalog or "-",
                pkg.default_channel or "-",
                ",".join(pkg.channels) or "-",
                pkg.description or "-",
                pkg.age(),
            ]
            self._emit_row(pkg, cells, all_namespaces=all_namespaces)
        fallbacks = [r for r in rows if not isinstance(r, PackageManifestSummary)]
        self._add_fallback_rows(
            fallbacks,
            all_namespaces=all_namespaces,
            pattern=pattern,
            width=len(_PKG_COLS),
            presorted=presorted,
        )

    def _add_subscription_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str, presorted: bool = False
    ) -> None:
        subs = [r for r in rows if isinstance(r, OLMSubscriptionSummary)]
        if not presorted:
            subs = sorted(subs, key=lambda s: (s.namespace, s.name))
        for sub in subs:
            if pattern and pattern.lower() not in sub.name.lower():
                continue
            cells: list[str | Text] = [
                sub.name,
                sub.channel or "-",
                sub.source or "-",
                sub.installed_csv or "-",
                sub.state or "-",
                sub.age(),
            ]
            self._emit_row(sub, cells, all_namespaces=all_namespaces)
        fallbacks = [r for r in rows if not isinstance(r, OLMSubscriptionSummary)]
        self._add_fallback_rows(
            fallbacks,
            all_namespaces=all_namespaces,
            pattern=pattern,
            width=len(_SUB_COLS),
            presorted=presorted,
        )

    def _add_csv_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str, presorted: bool = False
    ) -> None:
        csvs = [r for r in rows if isinstance(r, CSVSummary)]
        if not presorted:
            csvs = sorted(csvs, key=lambda c: (c.namespace, c.name))
        for csv in csvs:
            if pattern and pattern.lower() not in csv.name.lower():
                continue
            cells: list[str | Text] = [
                csv.name,
                csv.display_name or "-",
                csv.version or "-",
                _csv_phase_cell(csv.phase),
                csv.age(),
            ]
            self._emit_row(csv, cells, all_namespaces=all_namespaces)
        fallbacks = [r for r in rows if not isinstance(r, CSVSummary)]
        self._add_fallback_rows(
            fallbacks,
            all_namespaces=all_namespaces,
            pattern=pattern,
            width=len(_CSV_COLS),
            presorted=presorted,
        )

    def _add_generic_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str, presorted: bool = False
    ) -> None:
        generics = cast(list[GenericSummary], rows)
        if not presorted:
            generics = sorted(generics, key=lambda o: (o.namespace, o.name))
        for obj in generics:
            if pattern and pattern.lower() not in obj.name.lower():
                continue
            cells: list[str | Text] = [obj.name, obj.age()]
            self._emit_row(obj, cells, all_namespaces=all_namespaces)
