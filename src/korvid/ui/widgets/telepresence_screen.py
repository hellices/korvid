"""Telepresence status panel (issue #159 phase 1).

Read-only modal showing the local telepresence connection state and any
active intercepts. Pure presentation - the app queries the CLI (on
explicit user action only; see k8s/telepresence.py's daemon-spawn caveat)
and hands the results in.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from korvid.k8s.telepresence import ActiveIntercept, TelepresenceStatus


def status_lines(status: TelepresenceStatus) -> list[str]:
    """Human lines for the connection section."""
    if status.error:
        return [f"telepresence reported: {status.error}"]
    lines = [
        f"user daemon: {'running' if status.user_running else 'not running'}"
        + (f" (v{status.version})" if status.version else ""),
        f"root daemon: {'running' if status.root_running else 'not running'}",
    ]
    if status.connected:
        ctx = f" to {status.kubernetes_context}" if status.kubernetes_context else ""
        lines.append(f"session: connected{ctx}")
        if status.traffic_manager_version:
            lines.append(f"traffic manager: v{status.traffic_manager_version}")
    else:
        lines.append("session: not connected")
    return lines


def intercept_lines(intercepts: list[ActiveIntercept]) -> list[str]:
    """Human lines for the active-intercepts section; [] when none."""
    lines = []
    for row in intercepts:
        target = f"{row.kind or 'workload'} {row.namespace}/{row.workload}"
        port = f" port {row.port}" if row.port else ""
        who = f" by {row.client}" if row.client else ""
        lines.append(f"{target}{port}{who}")
    return lines


class TelepresenceScreen(ModalScreen[None]):
    """Read-only telepresence connection/intercept panel (`:tp`)."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss_screen", "Close", show=True),
        Binding("q", "dismiss_screen", "Close", show=False),
    ]

    DEFAULT_CSS = """
    TelepresenceScreen {
        align: center middle;
    }
    TelepresenceScreen > VerticalScroll {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    TelepresenceScreen .tp-title {
        text-style: bold;
        padding-bottom: 1;
    }
    TelepresenceScreen .tp-section {
        text-style: bold underline;
        padding-top: 1;
    }
    TelepresenceScreen .tp-note {
        color: $text-muted;
        padding-top: 1;
    }
    """

    def __init__(self, status: TelepresenceStatus, intercepts: list[ActiveIntercept]) -> None:
        super().__init__()
        self._status = status
        self._intercepts = list(intercepts)

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Telepresence", classes="tp-title")
            yield Static("Connection", classes="tp-section")
            for line in status_lines(self._status):
                yield Static(line, markup=False)
            if self._status.connected:
                yield Static("Active intercepts", classes="tp-section")
                rows = intercept_lines(self._intercepts)
                if rows:
                    for line in rows:
                        yield Static(line, markup=False)
                else:
                    yield Static("(none)", markup=False)
            yield Static(
                "Read-only. Querying interacts with telepresence's local "
                "daemons; intercept start/stop stays in the telepresence CLI "
                "for now.",
                classes="tp-note",
                markup=False,
            )

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)
