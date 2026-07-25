"""Resource list table — supports pods (rich 8 columns) and any generic kind."""

from __future__ import annotations

from typing import cast

from rich.text import Text
from textual.widgets import DataTable

from korvid.core.store import Summary
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.theme import phase_style, ready_style, restarts_style

_POD_COLS = ("NAME", "READY", "STATUS", "RESTARTS", "CPU R/L", "MEM R/L", "QOS", "NODE")
_POD_COLS_ALL_NS = ("NAMESPACE", *_POD_COLS)
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
            else:
                self.add_columns(*(_GENERIC_COLS_ALL_NS if all_namespaces else _GENERIC_COLS))
            self._last_kind = kind
            self._last_all_namespaces = all_namespaces
        else:
            self.clear()

        if kind == "pods":
            pods = cast(list[PodSummary], rows)
            for pod in sorted(pods, key=_pod_sort_key):
                if pattern and pattern.lower() not in pod.name.lower():
                    continue
                row_key = f"{pod.namespace}/{pod.name}"
                qos_cell: str | Text = Text(pod.qos, style=_QOS_STYLE.get(pod.qos, "dim"))
                if all_namespaces:
                    self.add_row(
                        pod.namespace,
                        pod.name,
                        _ready_cell(pod.ready),
                        _phase_cell(pod.phase),
                        _restarts_cell(pod.restarts),
                        f"{pod.cpu_request}/{pod.cpu_limit}",
                        f"{pod.mem_request}/{pod.mem_limit}",
                        qos_cell,
                        pod.node or "-",
                        key=row_key,
                    )
                else:
                    self.add_row(
                        pod.name,
                        _ready_cell(pod.ready),
                        _phase_cell(pod.phase),
                        _restarts_cell(pod.restarts),
                        f"{pod.cpu_request}/{pod.cpu_limit}",
                        f"{pod.mem_request}/{pod.mem_limit}",
                        qos_cell,
                        pod.node or "-",
                        key=row_key,
                    )
        else:
            generics = cast(list[GenericSummary], rows)
            for obj in sorted(generics, key=lambda o: (o.namespace, o.name)):
                if pattern and pattern.lower() not in obj.name.lower():
                    continue
                row_key = f"{obj.namespace}/{obj.name}"
                if all_namespaces:
                    self.add_row(obj.namespace, obj.name, obj.age(), key=row_key)
                else:
                    self.add_row(obj.name, obj.age(), key=row_key)
