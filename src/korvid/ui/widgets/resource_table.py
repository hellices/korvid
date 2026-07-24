"""Pod list table — the first resource view."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable

from korvid.k8s.models import PodSummary

COLUMNS = ("NAME", "READY", "STATUS", "RESTARTS", "CPU R/L", "MEM R/L", "QOS", "NODE")

# Eviction order reversed: pods evicted last render first.
_QOS_RANK = {"Guaranteed": 0, "Burstable": 1, "BestEffort": 2}
# Red is too aggressive for a steady-state view: green → chartreuse → yellow.
_QOS_STYLE = {"Guaranteed": "green", "Burstable": "chartreuse2", "BestEffort": "yellow"}


def _sort_key(pod: PodSummary) -> tuple[int, str]:
    return (_QOS_RANK.get(pod.qos, 3), pod.name)


class ResourceTable(DataTable[str | Text]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns(*COLUMNS)

    def update_rows(self, pods: list[PodSummary], pattern: str = "") -> None:
        self.clear()
        for pod in sorted(pods, key=_sort_key):
            if pattern and pattern.lower() not in pod.name.lower():
                continue
            self.add_row(
                pod.name,
                pod.ready,
                pod.phase,
                str(pod.restarts),
                f"{pod.cpu_request}/{pod.cpu_limit}",
                f"{pod.mem_request}/{pod.mem_limit}",
                Text(pod.qos, style=_QOS_STYLE.get(pod.qos, "dim")),
                pod.node or "-",
                key=pod.name,
            )
