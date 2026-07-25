"""Describe modal — full-screen YAML + events view for a selected resource."""

from __future__ import annotations

from typing import Any, ClassVar

import yaml
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static


def _format_age(event: dict[str, Any]) -> str:
    ts = event.get("lastTimestamp") or event.get("eventTime") or ""
    if not ts:
        return "-"
    # Return just the raw timestamp string; keep it simple.
    return str(ts)


def _render_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "<no events>"
    lines: list[str] = []
    for ev in events:
        ev_type = ev.get("type", "")
        reason = ev.get("reason", "")
        age = _format_age(ev)
        message = ev.get("message", "")
        lines.append(f"{ev_type:<8} {reason:<20} {age:<30} {message}")
    return "\n".join(lines)


def _strip_managed_fields(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the manifest with metadata.managedFields removed."""
    result = dict(manifest)
    if "metadata" in result and isinstance(result["metadata"], dict):
        meta = dict(result["metadata"])
        meta.pop("managedFields", None)
        result["metadata"] = meta
    return result


def _describe_body(manifest: dict[str, Any], events: list[dict[str, Any]]) -> str:
    """Render the shared YAML + events body used by both describe views."""
    cleaned = _strip_managed_fields(manifest)
    yaml_text = yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True)
    events_text = _render_events(events)
    return f"{yaml_text}\n\nEVENTS\n{'─' * 60}\n{events_text}"


class DescribeScreen(ModalScreen[None]):
    """Full-screen modal showing the raw manifest YAML and events for a resource."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    DescribeScreen {
        layout: vertical;
        background: $background;
    }
    DescribeScreen VerticalScroll {
        height: 1fr;
        padding: 0 1;
    }
    DescribeScreen #describe-body {
        width: 100%;
    }
    """

    def __init__(
        self,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> None:
        super().__init__()
        self._title = title
        self._manifest = manifest
        self._events = events

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            # markup=False: manifest/event text is cluster-controlled and may
            # contain bracketed sequences Rich would misinterpret as styles.
            yield Static(
                _describe_body(self._manifest, self._events), id="describe-body", markup=False
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = self._title


class DescribePane(Vertical):
    """Non-modal describe view mounted on the app screen (agent-shared).

    ``DescribeScreen`` is a modal: pushing it makes it the active screen, so
    the chat input underneath cannot take focus. When the agent opens a
    describe while the chat panel is visible, this pane is shown instead —
    it takes the left side of the main screen and the conversation (and its
    input) stay fully interactive. Escape closes it (see App.on_key).
    """

    DEFAULT_CSS = """
    DescribePane {
        dock: left;
        width: 60%;
        background: $background;
        border-right: solid $accent;
    }
    DescribePane #describe-pane-title {
        height: 1;
        background: $surface;
        padding: 0 1;
        text-style: bold;
    }
    DescribePane VerticalScroll {
        height: 1fr;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.display = False
        self.body_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="describe-pane-title")
        with VerticalScroll():
            yield Static("", id="describe-pane-body", markup=False)

    def show(self, title: str, manifest: dict[str, Any], events: list[dict[str, Any]]) -> None:
        self.body_text = _describe_body(manifest, events)
        self.query_one("#describe-pane-title", Static).update(f"{title}  (Esc to close)")
        self.query_one("#describe-pane-body", Static).update(self.body_text)
        self.query_one(VerticalScroll).scroll_home(animate=False)
        self.display = True

    def hide(self) -> None:
        self.display = False
