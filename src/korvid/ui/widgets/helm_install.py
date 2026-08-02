"""Prompt collecting release / version / namespace / values choices for a
helm install or upgrade (issue #31).

Everything offered comes from the chart the user picked out of their own
helm repos (`helm search repo`) - nothing hardcoded. Submit returns a
`HelmReleaseChoices`; the caller renders the dry-run preview and pushes the
standard approval dialog before anything touches the cluster.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

from korvid.k8s.helmcli import ChartHit, required_values_from_schema
from korvid.ui.widgets.confirm_screen import FreshKeysInput

#: chart metadata providers (issue #151): both repo-local `helm show` reads,
#: injected so the wizard stays testable without a helm binary. None means
#: the feature is unavailable (degraded session) and the wizard behaves as
#: before.
SchemaFn = Callable[[str, str], Awaitable["dict[str, Any] | None"]]
ReadmeFn = Callable[[str, str], Awaitable[str]]

#: values handling offered by the wizard, in display order (default first).
VALUES_MODES: tuple[str, str] = ("chart defaults", "edit in $EDITOR")

#: upgrade mode prepends (and defaults to) reusing the release's current
#: overrides: a plain `helm upgrade` silently resets them to chart defaults.
REUSE_VALUES_MODE = "reuse current values"
UPGRADE_VALUES_MODES: tuple[str, ...] = (REUSE_VALUES_MODE, *VALUES_MODES)

#: helm release names are DNS-1123 subdomains (dots allowed) capped at 53
#: characters; namespaces are DNS-1123 labels capped at 63. Reject locally
#: with a message instead of a cryptic server failure after approval.
_LABEL = r"[a-z0-9]([-a-z0-9]*[a-z0-9])?"
_RELEASE_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")
_RELEASE_MAX = 53
_NAMESPACE_RE = re.compile(rf"^{_LABEL}$")
_NAMESPACE_MAX = 63

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
HelmInstallPrompt #helm-required {
    margin-top: 1;
    color: $warning;
}
"""


@dataclass(frozen=True)
class HelmReleaseChoices:
    """Validated wizard output; ``version == ""`` means the repo's latest."""

    release: str
    version: str
    namespace: str
    edit_values: bool
    reuse_values: bool = False


class HelmInstallPrompt(ModalScreen["HelmReleaseChoices | None"]):
    """Collects install/upgrade choices for one picked chart.

    A non-None ``release`` switches the wizard to upgrade mode: the release
    name and namespace are facts of the row the user selected, shown but not
    editable. Dismisses with a `HelmReleaseChoices`, or None when cancelled.
    """

    CSS = _CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("f1", "show_readme", "Chart README", show=True),
    ]

    def __init__(
        self,
        chart: ChartHit,
        *,
        namespace: str,
        release: str | None = None,
        get_schema: SchemaFn | None = None,
        get_readme: ReadmeFn | None = None,
    ) -> None:
        super().__init__()
        self._chart = chart
        self._namespace = namespace
        self._release = release
        #: chart metadata providers (issue #151); None degrades gracefully.
        self._get_schema = get_schema
        self._get_readme = get_readme
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
                modes = UPGRADE_VALUES_MODES if self._upgrade else VALUES_MODES
                yield Select.from_values(modes, value=modes[0], allow_blank=False, id="helm-values")
            required = Static("", id="helm-required", markup=False)
            required.display = False
            yield required
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
        if self._get_schema is not None:
            self.run_worker(self._load_required_values(), group="helm-chart-info")

    async def _load_required_values(self) -> None:
        """Fetch values.schema.json required fields (issue #151) - advisory:
        every failure degrades to no section, never blocks the wizard."""
        version = self.query_one("#helm-version", Input).value.strip()
        if self._get_schema is None:  # pragma: no cover - guarded by the caller
            return
        try:
            schema = await self._get_schema(self._chart.name, version)
        except Exception:
            return
        rows = required_values_from_schema(schema)
        if not rows:
            return
        lines = "\n".join(f"  {path}: {kind}" for path, kind in rows)
        section = self.query_one("#helm-required", Static)
        section.update(f"Required values (from the chart's schema):\n{lines}")
        section.display = True

    def action_show_readme(self) -> None:
        """`F1` (issue #151): the chart's README in a scrollable modal -
        prerequisites and mandatory settings without leaving the wizard."""
        if self._get_readme is None:
            return
        version = self.query_one("#helm-version", Input).value.strip()
        self.run_worker(self._open_readme(version), group="helm-chart-info")

    async def _open_readme(self, version: str) -> None:
        if self._get_readme is None:  # pragma: no cover - guarded by the caller
            return
        try:
            text = await self._get_readme(self._chart.name, version)
        except Exception:
            self.notify("chart README unavailable", severity="warning")
            return
        self.app.push_screen(ChartReadmeScreen(self._chart.name, text))

    def _collect(self) -> HelmReleaseChoices | None:
        """Validated choices; None (with a notification) keeps the prompt open."""
        release = self.query_one("#helm-release", Input).value.strip()
        version = self.query_one("#helm-version", Input).value.strip()
        namespace = self.query_one("#helm-namespace", Input).value.strip()
        if len(release) > _RELEASE_MAX or not _RELEASE_RE.match(release):
            self.notify(
                f"invalid release name {release!r} "
                f"(DNS-1123 subdomain, at most {_RELEASE_MAX} chars)",
                severity="warning",
            )
            return None
        if len(namespace) > _NAMESPACE_MAX or not _NAMESPACE_RE.match(namespace):
            self.notify(
                f"invalid namespace {namespace!r} (DNS-1123 label, at most {_NAMESPACE_MAX} chars)",
                severity="warning",
            )
            return None
        mode = str(self.query_one("#helm-values", Select).value)
        return HelmReleaseChoices(
            release=release,
            version=version,
            namespace=namespace,
            edit_values=mode == VALUES_MODES[1],
            reuse_values=mode == REUSE_VALUES_MODE,
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


class ChartReadmeScreen(ModalScreen[None]):
    """Read-only scrollable pager for a chart's README (issue #151)."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "dismiss_screen", "Close", show=True),
        Binding("q", "dismiss_screen", "Close", show=False),
    ]

    DEFAULT_CSS = """
    ChartReadmeScreen {
        align: center middle;
    }
    ChartReadmeScreen > VerticalScroll {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 85%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    ChartReadmeScreen #readme-title {
        text-style: bold;
        padding-bottom: 1;
    }
    """

    def __init__(self, chart: str, text: str) -> None:
        super().__init__()
        self._chart = chart
        self._text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(f"README — {self._chart}", id="readme-title", markup=False)
            yield Static(self._text, id="readme-body", markup=False)

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)
