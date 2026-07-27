"""Prompt collecting namespace / channel / approval for an OLM install.

Part of the issue #29 flow: everything offered comes from the selected
PackageManifest (channels, default channel, catalog source) - no hardcoded
operator knowledge. Submit returns ``(namespace, channel, approval)``; the
caller builds the Subscription manifest and pushes the standard approval
dialog with it shown in full.
"""

from __future__ import annotations

from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Select, Static

from korvid.k8s.olm import APPROVAL_MODES, PackageInstallFacts
from korvid.ui.widgets.confirm_screen import FreshKeysInput

_CSS = """
OperatorInstallPrompt {
    align: center middle;
}
OperatorInstallPrompt > VerticalScroll {
    width: 76;
    height: auto;
    max-height: 80%;
    border: heavy $error;
    padding: 1 2;
    background: $surface;
}
OperatorInstallPrompt .confirm-title {
    text-style: bold;
}
OperatorInstallPrompt .confirm-hint {
    color: $text-muted;
}
OperatorInstallPrompt .install-row {
    height: auto;
    margin-top: 1;
}
OperatorInstallPrompt .install-label {
    width: 16;
    padding-top: 1;
    color: $text-muted;
}
OperatorInstallPrompt .install-row Input {
    width: 1fr;
}
OperatorInstallPrompt .install-row Select {
    width: 1fr;
}
"""


class OperatorInstallPrompt(ModalScreen["tuple[str, str, str] | None"]):
    """Collects install choices for one catalog entry.

    Dismisses with ``(namespace, channel, approval)`` or None when cancelled.
    """

    CSS = _CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, facts: PackageInstallFacts, *, namespace: str) -> None:
        super().__init__()
        self._facts = facts
        self._namespace = namespace
        # Keystrokes queued while the caller fetched the PackageManifest
        # predate this prompt; a buffered Enter must not submit the wizard
        # with defaults before the user has seen it. Same clock as event
        # timestamps (Message.time).
        self._created_time = Message().time

    def compose(self) -> ComposeResult:
        facts = self._facts
        channels = ", ".join(facts.channels) or "(unknown - server validates)"
        with VerticalScroll():
            yield Static(f"Install operator {facts.package}", classes="confirm-title", markup=False)
            yield Static(
                f"Catalog: {facts.catalog_source or '?'}  Channels: {channels}. "
                "Press Enter to review the Subscription manifest (Esc cancels).",
                classes="confirm-hint",
                markup=False,
            )
            with Horizontal(classes="install-row"):
                yield Static("namespace", classes="install-label", markup=False)
                yield FreshKeysInput(
                    self._created_time,
                    value=self._namespace,
                    id="install-namespace",
                    select_on_focus=True,
                )
            with Horizontal(classes="install-row"):
                yield Static("channel", classes="install-label", markup=False)
                if facts.channels:
                    # The valid channel set is known: a Select makes typos
                    # structurally impossible (issue #62).
                    default = (
                        facts.default_channel
                        if facts.default_channel in facts.channels
                        else facts.channels[0]
                    )
                    yield Select.from_values(
                        facts.channels, value=default, allow_blank=False, id="install-channel"
                    )
                else:
                    # Malformed PackageManifest status: fall back to free
                    # text so the server stays the final validator.
                    yield FreshKeysInput(
                        self._created_time,
                        value=facts.default_channel,
                        id="install-channel",
                        select_on_focus=True,
                    )
            with Horizontal(classes="install-row"):
                yield Static("approval", classes="install-label", markup=False)
                yield Select.from_values(
                    APPROVAL_MODES,
                    value=APPROVAL_MODES[0],
                    allow_blank=False,
                    id="install-approval",
                )

    def on_mount(self) -> None:
        self.query(Input).first().focus()

    def _field_value(self, field_id: str) -> str:
        """Current value of a wizard field, whichever widget backs it."""
        widget = self.query_one(f"#{field_id}")
        if isinstance(widget, Select):
            return str(widget.value)
        if isinstance(widget, Input):
            return widget.value.strip()
        raise TypeError(f"unexpected widget for field {field_id!r}")  # pragma: no cover

    def _collect(self) -> tuple[str, str, str] | None:
        """Validated (namespace, channel, approval); None keeps the prompt open."""
        namespace = self._field_value("install-namespace")
        channel = self._field_value("install-channel")
        approval = self._field_value("install-approval")
        if not namespace:
            self.notify("namespace must not be blank", severity="warning")
            return None
        if not channel:
            self.notify("channel must not be blank", severity="warning")
            return None
        # An empty channel list means the PackageManifest status was
        # malformed; the server then stays the sole validator.
        if self._facts.channels and channel not in self._facts.channels:
            self.notify(
                f"unknown channel {channel!r} - this package offers:"
                f" {', '.join(self._facts.channels)}",
                severity="warning",
            )
            return None
        if approval not in APPROVAL_MODES:
            self.notify(
                f"approval must be one of {', '.join(APPROVAL_MODES)}",
                severity="warning",
            )
            return None
        return (namespace, channel, approval)

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        choices = self._collect()
        if choices is None:
            return
        self.dismiss(choices)

    def action_cancel(self) -> None:
        self.dismiss(None)
