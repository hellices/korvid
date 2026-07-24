from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    def update_status(self, context: str | None, namespace: str, agent_label: str) -> None:
        ctx = context or "(current)"
        self.update(f"ctx:{ctx}  ns:{namespace}  ⚡{agent_label}")
