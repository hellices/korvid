"""Describe modal — full-screen YAML + events view for a selected resource."""

from __future__ import annotations

from typing import Any, ClassVar

import yaml
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
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
    DescribeScreen.share {
        background: $background 0%;
    }
    DescribeScreen.share Header,
    DescribeScreen.share VerticalScroll,
    DescribeScreen.share Footer {
        width: 60%;
    }
    DescribeScreen.share VerticalScroll {
        border-right: solid $accent;
    }
    """

    def __init__(
        self,
        title: str,
        manifest: dict[str, Any],
        events: list[dict[str, Any]],
        share_with_agent: bool = False,
    ) -> None:
        super().__init__()
        self._title = title
        self._manifest = manifest
        self._events = events
        if share_with_agent:
            # Agent-opened describe: take the left side only, so the chat
            # panel (and the conversation in progress) stays visible.
            self.add_class("share")

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            cleaned = _strip_managed_fields(self._manifest)
            yaml_text = yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True)
            events_text = _render_events(self._events)
            body = f"{yaml_text}\n\nEVENTS\n{'─' * 60}\n{events_text}"
            # markup=False: manifest/event text is cluster-controlled and may
            # contain bracketed sequences Rich would misinterpret as styles.
            yield Static(body, id="describe-body", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.title = self._title
