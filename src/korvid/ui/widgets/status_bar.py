from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    def update_status(
        self, context: str | None, namespace: str, agent_label: str, breadcrumb: str = ""
    ) -> None:
        ctx = context or "(current)"
        trail = f"  {breadcrumb}" if breadcrumb else ""
        self.update(f"ctx:{ctx}  ns:{namespace}  ⚡{agent_label}{trail}")
