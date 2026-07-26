from __future__ import annotations

from textual.widgets import Static


class StatusBar(Static):
    def update_status(
        self,
        context: str | None,
        namespace: str,
        agent_label: str,
        breadcrumb: str = "",
        mcp_label: str = "",
    ) -> None:
        ctx = context or "(current)"
        trail = f"  {breadcrumb}" if breadcrumb else ""
        mcp = f"  ⇄{mcp_label}" if mcp_label else ""
        self.update(f"ctx:{ctx}  ns:{namespace}  ⚡{agent_label}{mcp}{trail}")
