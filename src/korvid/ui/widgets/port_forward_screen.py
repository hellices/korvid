"""Port-forward dialog (issue #38): collect local/remote ports for a target.

Prefilled from the target's declared ports; both fields stay editable so a
user can forward an undeclared port (kubectl allows it — declaration is
informational).
"""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_CSS = """
PortForwardScreen {
    align: center middle;
}
PortForwardScreen > Vertical {
    width: 60;
    height: auto;
    border: heavy $primary;
    padding: 1 2;
    background: $surface;
}
PortForwardScreen .pf-title {
    text-style: bold;
}
PortForwardScreen .pf-hint {
    color: $text-muted;
}
PortForwardScreen .pf-row {
    height: auto;
    margin-top: 1;
}
PortForwardScreen .pf-label {
    width: 14;
    padding-top: 1;
    color: $text-muted;
}
PortForwardScreen .pf-row Input {
    width: 1fr;
}
"""


def _parse_port(value: str) -> int | None:
    """A TCP port number, or None when the text is not one."""
    try:
        port = int(value.strip())
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


class PortForwardScreen(ModalScreen["tuple[int, int] | None"]):
    """Dismisses with ``(local_port, remote_port)``, or None when cancelled."""

    CSS = _CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, target: str, remote_ports: list[int]) -> None:
        """Args:
        target: human label of the forward target (``pods/ns/name``).
        remote_ports: declared ports for prefill; may be empty.
        """
        super().__init__()
        self._target = target
        self._remote_ports = remote_ports

    def compose(self) -> ComposeResult:
        prefill = str(self._remote_ports[0]) if self._remote_ports else ""
        detected = ", ".join(str(p) for p in self._remote_ports) or "none declared"
        with Vertical():
            yield Static(f"Port-forward {self._target}", classes="pf-title", markup=False)
            yield Static(
                f"Declared ports: {detected}. Enter starts, Esc cancels.",
                classes="pf-hint",
                markup=False,
            )
            with Horizontal(classes="pf-row"):
                yield Static("remote port", classes="pf-label")
                yield Input(value=prefill, placeholder="e.g. 8080", id="pf-remote")
            with Horizontal(classes="pf-row"):
                yield Static("local port", classes="pf-label")
                yield Input(value=prefill, placeholder="same as remote", id="pf-local")

    def on_mount(self) -> None:
        self.query_one("#pf-local", Input).focus()

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        remote = _parse_port(self.query_one("#pf-remote", Input).value)
        if remote is None:
            self.notify("Remote port must be 1-65535", severity="warning")
            return
        local_text = self.query_one("#pf-local", Input).value
        # An empty local port mirrors the remote one (kubectl's `:port` form
        # picks a random port instead — surprising in a TUI listing).
        local = remote if not local_text.strip() else _parse_port(local_text)
        if local is None:
            self.notify("Local port must be 1-65535", severity="warning")
            return
        self.dismiss((local, remote))

    def action_cancel(self) -> None:
        self.dismiss(None)
