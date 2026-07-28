from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


class StatusBar(Static):
    DEFAULT_CSS = """
    StatusBar.protected {
        background: $error 25%;
    }
    """

    def update_status(
        self,
        context: str | None,
        namespace: str,
        agent_label: str,
        breadcrumb: str = "",
        mcp_label: str = "",
        filter_label: str = "",
        progress_label: str = "",
        protected: bool = False,
    ) -> None:
        ctx = context or "(current)"
        trail = f"  {breadcrumb}" if breadcrumb else ""
        mcp = f"  ⇄{mcp_label}" if mcp_label else ""
        flt = f"  ▼{filter_label}" if filter_label else ""
        prog = f"  ⏳{progress_label}" if progress_label else ""
        # Text keeps user-entered filter text literal (never Rich markup).
        line = Text()
        if protected:
            # Protected contexts (issue #83): loud red marker + tinted bar.
            line.append(" ⛨ PROTECTED ", style="bold white on red")
            line.append("  ")
        line.append(f"ctx:{ctx}  ns:{namespace}  ⚡{agent_label}{mcp}{flt}{prog}{trail}")
        self.set_class(protected, "protected")
        self.update(line)
