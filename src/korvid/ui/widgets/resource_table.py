"""Pod list table — the first resource view."""

from __future__ import annotations

from textual.widgets import DataTable

from korvid.k8s.models import PodSummary

COLUMNS = ("NAME", "READY", "STATUS", "RESTARTS", "NODE")


class ResourceTable(DataTable[str]):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns(*COLUMNS)

    def update_rows(self, pods: list[PodSummary], pattern: str = "") -> None:
        self.clear()
        for pod in pods:
            if pattern and pattern.lower() not in pod.name.lower():
                continue
            self.add_row(
                pod.name, pod.ready, pod.phase, str(pod.restarts), pod.node or "-", key=pod.name
            )
