"""Modal inspector for the exact redacted provider payload."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from korvid.agent.outbound import OutboundSnapshot
from korvid.core.private_export import default_payload_export_dir, write_private_text


class PayloadInspectorScreen(ModalScreen[None]):
    """Read-only view of the latest sanitized provider request."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
        Binding("e", "export", "Export", show=True),
    ]

    DEFAULT_CSS = """
    PayloadInspectorScreen {
        align: center middle;
    }
    PayloadInspectorScreen > VerticalScroll {
        width: 90%;
        height: auto;
        max-height: 80%;
        border: round $accent;
        padding: 1 2;
        background: $surface;
    }
    PayloadInspectorScreen #payload-title {
        text-style: bold;
        margin-bottom: 1;
    }
    """

    def __init__(
        self,
        snapshot: OutboundSnapshot,
        *,
        export_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._content = snapshot.export_json()
        self._export_dir = export_dir

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Final redacted provider payload", id="payload-title", markup=False)
            yield Static(self._content, id="payload-json", markup=False)
        yield Footer()

    def action_export(self) -> None:
        """Explicitly export the displayed sanitized payload."""
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        directory = self._export_dir or default_payload_export_dir()
        try:
            path = write_private_text(
                directory,
                f"korvid-agent-payload-{stamp}",
                ".json",
                self._content,
            )
        except OSError as exc:
            self.notify(f"Failed to export provider payload: {exc}", severity="error", markup=False)
            return
        self.notify(f"Provider payload exported to {path}", markup=False)
