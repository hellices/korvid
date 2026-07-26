"""Resource list table — supports pods (rich 8 columns) and any generic kind."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from rich.text import Text
from textual.widgets import DataTable

from korvid.core.sorting import SortSpec, sort_rows
from korvid.core.store import Summary
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


def _columns_for(kind: str, *, all_namespaces: bool) -> tuple[str, ...]:
    """Column headers for *kind*; unknown kinds get the generic NAME/AGE set."""
    single, all_ns = _COLS_BY_KIND.get(kind, (_GENERIC_COLS, _GENERIC_COLS_ALL_NS))
    return all_ns if all_namespaces else single


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


def _decorate_columns(columns: tuple[str, ...], sort: SortSpec | None) -> tuple[str, ...]:
    """Append ▲/▼ to the sorted column's header; untouched when inactive."""
    if sort is None:
        return columns
    label = _SORT_LABELS.get(sort.column)
    arrow = "▼" if sort.descending else "▲"
    return tuple(f"{col} {arrow}" if col == label else col for col in columns)


class ResourceTable(DataTable[str | Text]):
    _last_kind: str | None = None
    _last_all_namespaces: bool | None = None
    _last_sort: SortSpec | None = None

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._last_kind = None
        self._last_all_namespaces = None
        self._last_sort = None

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
    ) -> None:
        """Render rows into the table; rebuilds columns when (kind, all_namespaces, sort) changes.

        ``group`` is the API group serving *kind*: typed renderings that are
        specific to one group (the OLM tables) apply only there.
        """
        kind = _typed_kind(kind, group)
        if (kind, all_namespaces, sort) != (
            self._last_kind,
            self._last_all_namespaces,
            self._last_sort,
        ):
            self.clear(columns=True)
            self.add_columns(
                *_decorate_columns(_columns_for(kind, all_namespaces=all_namespaces), sort)
            )
            self._last_kind = kind
            self._last_all_namespaces = all_namespaces
            self._last_sort = sort
        else:
            self.clear()
        self._render_rows(
            kind, rows, all_namespaces=all_namespaces, pattern=pattern, metrics=metrics, sort=sort
        )

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
            rows = sort_rows(rows, sort, metrics=metrics)
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
            if all_namespaces:
                cells.insert(0, pod.namespace)
            self.add_row(*cells, key=f"{pod.namespace}/{pod.name}")

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
            if all_namespaces:
                cells.insert(0, obj.namespace)
            self.add_row(*cells, key=f"{obj.namespace}/{obj.name}")

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
            if all_namespaces:
                cells.insert(0, rel.namespace)
            self.add_row(*cells, key=f"{rel.namespace}/{rel.name}")

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
            if all_namespaces:
                cells.insert(0, rev.namespace)
            self.add_row(*cells, key=f"{rev.namespace}/{rev.name}")

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
            if all_namespaces:
                cells.insert(0, obj.namespace)
            self.add_row(*cells, key=f"{obj.namespace}/{obj.name}")

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
            if all_namespaces:
                cells.insert(0, pkg.namespace)
            self.add_row(*cells, key=f"{pkg.namespace}/{pkg.name}")
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
            if all_namespaces:
                cells.insert(0, sub.namespace)
            self.add_row(*cells, key=f"{sub.namespace}/{sub.name}")
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
            if all_namespaces:
                cells.insert(0, csv.namespace)
            self.add_row(*cells, key=f"{csv.namespace}/{csv.name}")
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
            row_key = f"{obj.namespace}/{obj.name}"
            if all_namespaces:
                self.add_row(obj.namespace, obj.name, obj.age(), key=row_key)
            else:
                self.add_row(obj.name, obj.age(), key=row_key)
