"""k9s-style pod drill-down — Enter on a pod opens its container list.

Shows NAME / IMAGE / READY / STATE / RESTARTS per container (init containers
included, marked with "(init)"). Enter or ``l`` streams that container's logs,
``s`` shells into it, Escape closes. Dismisses with ``(action, container)`` or
``None``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

_COLUMNS = ("NAME", "IMAGE", "READY", "STATE", "RESTARTS")


def _state_of(status: dict[str, Any]) -> str:
    state = status.get("state") or {}
    if "running" in state:
        return "Running"
    if "waiting" in state:
        return str((state["waiting"] or {}).get("reason") or "Waiting")
    if "terminated" in state:
        term = state["terminated"] or {}
        reason = term.get("reason") or "Terminated"
        exit_code = term.get("exitCode")
        return f"{reason} ({exit_code})" if exit_code is not None else str(reason)
    return "-"


def build_container_rows(manifest: dict[str, Any]) -> list[tuple[str, str, str, str, str]]:
    """Flatten spec + status into display rows: (name, image, ready, state, restarts)."""
    spec = manifest.get("spec") or {}
    status = manifest.get("status") or {}
    statuses: dict[str, dict[str, Any]] = {}
    for st in (status.get("containerStatuses") or []) + (status.get("initContainerStatuses") or []):
        if isinstance(st, dict) and st.get("name"):
            statuses[str(st["name"])] = st

    rows: list[tuple[str, str, str, str, str]] = []
    for section, suffix in (("containers", ""), ("initContainers", " (init)")):
        for ctr in spec.get(section) or []:
            if not isinstance(ctr, dict) or not ctr.get("name"):
                continue
            name = str(ctr["name"])
            st = statuses.get(name, {})
            rows.append(
                (
                    f"{name}{suffix}",
                    str(ctr.get("image") or "-"),
                    "true" if st.get("ready") else "false",
                    _state_of(st),
                    str(st.get("restartCount", 0)),
                )
            )
    return rows


class ContainersScreen(ModalScreen[tuple[str, str] | None]):
    """Container list for one pod; resolves to (action, container) or None."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("l", "pick_logs", "Logs", show=True),
        Binding("s", "pick_shell", "Shell", show=True),
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    ContainersScreen {
        layout: vertical;
        background: $background;
    }
    ContainersScreen #containers-title {
        padding: 0 1;
    }
    ContainersScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(self, pod: str, rows: list[tuple[str, str, str, str, str]]) -> None:
        super().__init__()
        self._pod = pod
        self._rows = rows

    def compose(self) -> ComposeResult:
        yield Footer()
        yield Static(f"Containers in {self._pod}", id="containers-title", markup=False)
        yield DataTable[str]()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        for row in self._rows:
            # Row key = bare container name (display name may carry "(init)").
            key = row[0].removesuffix(" (init)")
            table.add_row(*row, key=key)
        table.focus()

    def _selected_container(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        row_index = table.cursor_row
        ordered = table.ordered_rows
        if row_index >= len(ordered):
            return None
        return str(ordered[row_index].key.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter is intentionally a no-op here — use ``l`` (logs) or ``s`` (shell)."""
        event.stop()

    def action_pick_logs(self) -> None:
        container = self._selected_container()
        if container is not None:
            self.dismiss(("logs", container))

    def action_pick_shell(self) -> None:
        container = self._selected_container()
        if container is not None:
            self.dismiss(("shell", container))

    def action_close(self) -> None:
        self.dismiss(None)
