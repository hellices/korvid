"""Prompt collecting release / version / namespace / values choices for a
helm install or upgrade (issue #31).

Everything offered comes from the chart the user picked out of their own
helm repos (`helm search repo`) - nothing hardcoded. Submit returns a
`HelmReleaseChoices`; the caller renders the dry-run preview and pushes the
standard approval dialog before anything touches the cluster.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from korvid.k8s.helmcli import ChartHit
from korvid.ui.widgets.confirm_screen import FreshKeysInput

#: values handling offered by the wizard, in display order (default first).
VALUES_MODES: tuple[str, str] = ("chart defaults", "edit in $EDITOR")

#: helm release names and namespaces must be DNS-1123-compatible; reject
#: locally with a message instead of a cryptic server failure after approval.
_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

_CSS = """
HelmInstallPrompt {
    align: center middle;
}
HelmInstallPrompt > VerticalScroll {
    width: 76;
    height: auto;
    max-height: 80%;
    border: heavy $error;
    padding: 1 2;
    background: $surface;
}
HelmInstallPrompt .confirm-title {
    text-style: bold;
}
HelmInstallPrompt .confirm-hint {
    color: $text-muted;
}
HelmInstallPrompt .install-row {
    height: auto;
    margin-top: 1;
}
HelmInstallPrompt .install-label {
    width: 16;
    padding-top: 1;
    color: $text-muted;
}
HelmInstallPrompt .install-row Input {
    width: 1fr;
}
HelmInstallPrompt .install-row Select {
    width: 1fr;
}
HelmInstallPrompt .install-actions {
    height: auto;
    margin-top: 1;
    align-horizontal: right;
}
HelmInstallPrompt .install-actions Button {
    margin-left: 2;
}
"""


@dataclass(frozen=True)
class HelmReleaseChoices:
    """Validated wizard output; ``version == ""`` means the repo's latest."""

    release: str
    version: str
    namespace: str
    edit_values: bool


class HelmInstallPrompt(ModalScreen["HelmReleaseChoices | None"]):
    """Collects install/upgrade choices for one picked chart.

    A non-None ``release`` switches the wizard to upgrade mode: the release
    name and namespace are facts of the row the user selected, shown but not
    editable. Dismisses with a `HelmReleaseChoices`, or None when cancelled.
    """

    CSS = _CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, chart: ChartHit, *, namespace: str, release: str | None = None) -> None:
        super().__init__()
        self._chart = chart
        self._namespace = namespace
        self._release = release
        # Keystrokes queued while the caller ran `helm search repo` predate
        # this prompt; a buffered Enter must not submit it with defaults
        # before the user has seen it (same guard as OperatorInstallPrompt).
        self._created_time = Message().time

    @property
    def _upgrade(self) -> bool:
        return self._release is not None

    def compose(self) -> ComposeResult:
        chart = self._chart
        verb = "Upgrade" if self._upgrade else "Install"
        target = f"{self._release} with {chart.name}" if self._upgrade else chart.name
        with VerticalScroll():
            yield Static(f"{verb} {target}", classes="confirm-title", markup=False)
            yield Static(
                f"Chart: {chart.name} {chart.version} (app {chart.app_version or '?'})."
                " Press Enter in a text field (or the submit button) to review"
                " the dry-run; Esc cancels.",
                classes="confirm-hint",
                markup=False,
            )
            with Horizontal(classes="install-row"):
                yield Static("release", classes="install-label", markup=False)
                yield FreshKeysInput(
                    self._created_time,
                    value=self._release or chart.name.rsplit("/", 1)[-1],
                    id="helm-release",
                    select_on_focus=True,
                    disabled=self._upgrade,
                )
            with Horizontal(classes="install-row"):
                yield Static("version", classes="install-label", markup=False)
                yield FreshKeysInput(
                    self._created_time,
                    value=chart.version,
                    id="helm-version",
                    select_on_focus=True,
                )
            with Horizontal(classes="install-row"):
                yield Static("namespace", classes="install-label", markup=False)
                yield FreshKeysInput(
                    self._created_time,
                    value=self._namespace,
                    id="helm-namespace",
                    select_on_focus=True,
                    disabled=self._upgrade,
                )
            with Horizontal(classes="install-row"):
                yield Static("values", classes="install-label", markup=False)
                yield Select.from_values(
                    VALUES_MODES, value=VALUES_MODES[0], allow_blank=False, id="helm-values"
                )
            with Horizontal(classes="install-actions"):
                yield Button(verb, variant="primary", id="helm-submit")
                yield Button("Cancel", id="helm-cancel")

    def on_mount(self) -> None:
        # In upgrade mode the first inputs are disabled facts; focus the
        # first field the user can actually change.
        for widget in self.query(Input):
            if not widget.disabled:
                widget.focus()
                break

    def _collect(self) -> HelmReleaseChoices | None:
        """Validated choices; None (with a notification) keeps the prompt open."""
        release = self.query_one("#helm-release", Input).value.strip()
        version = self.query_one("#helm-version", Input).value.strip()
        namespace = self.query_one("#helm-namespace", Input).value.strip()
        if not _NAME_RE.match(release):
            self.notify(
                f"invalid release name {release!r} (lowercase letters, digits, '-')",
                severity="warning",
            )
            return None
        if not _NAME_RE.match(namespace):
            self.notify(
                f"invalid namespace {namespace!r} (lowercase letters, digits, '-')",
                severity="warning",
            )
            return None
        edit_values = str(self.query_one("#helm-values", Select).value) == VALUES_MODES[1]
        return HelmReleaseChoices(
            release=release, version=version, namespace=namespace, edit_values=edit_values
        )

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        self._try_submit()

    @on(Button.Pressed, "#helm-submit")
    def _submit_button(self, event: Button.Pressed) -> None:
        event.stop()
        self._try_submit()

    @on(Button.Pressed, "#helm-cancel")
    def _cancel_button(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(None)

    def _try_submit(self) -> None:
        choices = self._collect()
        if choices is None:
            return
        self.dismiss(choices)

    def action_cancel(self) -> None:
        self.dismiss(None)
