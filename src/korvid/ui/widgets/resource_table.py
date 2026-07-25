"""Resource list table — supports pods (rich 8 columns) and any generic kind."""

from __future__ import annotations

from typing import cast

from rich.text import Text
from textual.widgets import DataTable

from korvid.core.store import Summary
from korvid.k8s.models import GenericSummary, PodSummary, ReplicaSetSummary
from korvid.ui.theme import phase_style, ready_style, restarts_style

_POD_COLS = ("NAME", "READY", "STATUS", "RESTARTS", "CPU R/L", "MEM R/L", "QOS", "NODE")
_POD_COLS_ALL_NS = ("NAMESPACE", *_POD_COLS)
_RS_COLS = ("NAME", "REVISION", "DESIRED", "CURRENT", "READY", "AGE")
_RS_COLS_ALL_NS = ("NAMESPACE", *_RS_COLS)
_GENERIC_COLS = ("NAME", "AGE")
_GENERIC_COLS_ALL_NS = ("NAMESPACE", "NAME", "AGE")

# Eviction order reversed: pods evicted last render first.
_QOS_RANK = {"Guaranteed": 0, "Burstable": 1, "BestEffort": 2}
# Red is too aggressive for a steady-state view: green → chartreuse → yellow.
_QOS_STYLE = {"Guaranteed": "green", "Burstable": "chartreuse2", "BestEffort": "yellow"}


def _pod_sort_key(pod: PodSummary) -> tuple[int, str]:
    return (_QOS_RANK.get(pod.qos, 3), pod.name)


def _phase_cell(phase: str) -> Text:
    return Text(phase, style=phase_style(phase))


def _ready_cell(ready: str) -> Text:
    return Text(ready, style=ready_style(ready))


def _restarts_cell(restarts: int) -> Text:
    return Text(str(restarts), style=restarts_style(restarts))


def _replicaset_sort_key(rs: ReplicaSetSummary) -> tuple[str, int, str]:
    """Rollout-history order: newest revision first within each namespace
    (matching what ``kubectl rollout history`` users expect); replicasets
    without a numeric revision annotation sort last."""
    revision = -int(rs.revision) if rs.revision.isdigit() else 1
    return (rs.namespace, revision, rs.name)


class ResourceTable(DataTable[str | Text]):
    _last_kind: str | None = None
    _last_all_namespaces: bool | None = None

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self._last_kind = None
        self._last_all_namespaces = None

    def show(self, kind: str, rows: list[Summary], *, all_namespaces: bool, pattern: str) -> None:
        """Render rows into the table; rebuilds columns when (kind, all_namespaces) changes."""
        if (kind, all_namespaces) != (self._last_kind, self._last_all_namespaces):
            self.clear(columns=True)
            if kind == "pods":
                self.add_columns(*(_POD_COLS_ALL_NS if all_namespaces else _POD_COLS))
            elif kind == "replicasets":
                self.add_columns(*(_RS_COLS_ALL_NS if all_namespaces else _RS_COLS))
            else:
                self.add_columns(*(_GENERIC_COLS_ALL_NS if all_namespaces else _GENERIC_COLS))
            self._last_kind = kind
            self._last_all_namespaces = all_namespaces
        else:
            self.clear()

        if kind == "pods":
            self._add_pod_rows(rows, all_namespaces=all_namespaces, pattern=pattern)
        elif kind == "replicasets":
            self._add_replicaset_rows(rows, all_namespaces=all_namespaces, pattern=pattern)
        else:
            self._add_generic_rows(rows, all_namespaces=all_namespaces, pattern=pattern)

    def _add_pod_rows(self, rows: list[Summary], *, all_namespaces: bool, pattern: str) -> None:
        pods = cast(list[PodSummary], rows)
        for pod in sorted(pods, key=_pod_sort_key):
            if pattern and pattern.lower() not in pod.name.lower():
                continue
            cells: list[str | Text] = [
                pod.name,
                _ready_cell(pod.ready),
                _phase_cell(pod.phase),
                _restarts_cell(pod.restarts),
                f"{pod.cpu_request}/{pod.cpu_limit}",
                f"{pod.mem_request}/{pod.mem_limit}",
                Text(pod.qos, style=_QOS_STYLE.get(pod.qos, "dim")),
                pod.node or "-",
            ]
            if all_namespaces:
                cells.insert(0, pod.namespace)
            self.add_row(*cells, key=f"{pod.namespace}/{pod.name}")

    def _add_replicaset_rows(
        self, rows: list[Summary], *, all_namespaces: bool, pattern: str
    ) -> None:
        replicasets = [r for r in rows if isinstance(r, ReplicaSetSummary)]
        for rs in sorted(replicasets, key=_replicaset_sort_key):
            if pattern and pattern.lower() not in rs.name.lower():
                continue
            cells: list[str | Text] = [
                rs.name,
                rs.revision,
                str(rs.desired),
                str(rs.current),
                _ready_cell(rs.ready),
                rs.age(),
            ]
            if all_namespaces:
                cells.insert(0, rs.namespace)
            self.add_row(*cells, key=f"{rs.namespace}/{rs.name}")
        # Rows that reached this view without ReplicaSet parsing (e.g. a
        # future path that skips summary_for) still render NAME/AGE rather
        # than silently disappearing.
        fallbacks = [r for r in rows if not isinstance(r, ReplicaSetSummary)]
        for obj in sorted(fallbacks, key=lambda o: (o.namespace, o.name)):
            if pattern and pattern.lower() not in obj.name.lower():
                continue
            age = obj.age() if isinstance(obj, GenericSummary) else ""
            fallback_cells: list[str | Text] = [obj.name, "", "", "", "", age]
            if all_namespaces:
                fallback_cells.insert(0, obj.namespace)
            self.add_row(*fallback_cells, key=f"{obj.namespace}/{obj.name}")

    def _add_generic_rows(self, rows: list[Summary], *, all_namespaces: bool, pattern: str) -> None:
        generics = cast(list[GenericSummary], rows)
        for obj in sorted(generics, key=lambda o: (o.namespace, o.name)):
            if pattern and pattern.lower() not in obj.name.lower():
                continue
            row_key = f"{obj.namespace}/{obj.name}"
            if all_namespaces:
                self.add_row(obj.namespace, obj.name, obj.age(), key=row_key)
            else:
                self.add_row(obj.name, obj.age(), key=row_key)
