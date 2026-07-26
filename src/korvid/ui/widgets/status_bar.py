from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class StatusBar(Static):
    def update_status(
        self,
        context: str | None,
        namespace: str,
        agent_label: str,
        breadcrumb: str = "",
        mcp_label: str = "",
        filter_label: str = "",
    ) -> None:
        ctx = context or "(current)"
        trail = f"  {breadcrumb}" if breadcrumb else ""
        mcp = f"  ⇄{mcp_label}" if mcp_label else ""
        flt = f"  ▼{filter_label}" if filter_label else ""
        # Text keeps user-entered filter text literal (never Rich markup).
        self.update(Text(f"ctx:{ctx}  ns:{namespace}  ⚡{agent_label}{mcp}{flt}{trail}"))
