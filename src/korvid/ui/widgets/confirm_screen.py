"""Approval dialogs for cluster write operations (spec §5 #4, §6.2).

ConfirmScreen shows the exact operation about to run and resolves True only
from a user keystroke — no agent tool can open, focus, or confirm it. The
layered variant (``require_name``) demands typing the resource name exactly,
used for high-blast-radius deletes. ReplicasPrompt collects a replica count
for scale operations.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Static

_DIALOG_CSS = """
ConfirmScreen, ReplicasPrompt, ImagePrompt {
    align: center middle;
}
ConfirmScreen > VerticalScroll, ReplicasPrompt > Vertical, ImagePrompt > Vertical {
    width: 70;
    height: auto;
    max-height: 80%;
    border: heavy $error;
    padding: 1 2;
    background: $surface;
}
ConfirmScreen .confirm-title, ReplicasPrompt .confirm-title, ImagePrompt .confirm-title {
    text-style: bold;
}
ConfirmScreen .confirm-hint, ReplicasPrompt .confirm-hint, ImagePrompt .confirm-hint {
    color: $text-muted;
}
ConfirmScreen .confirm-protected {
    color: $text;
    background: $error;
    text-style: bold;
}
ConfirmScreen .confirm-managed {
    color: $warning;
    text-style: bold;
}
ConfirmScreen .confirm-impact {
    color: $text-muted;
}
"""


class FreshKeysInput(Input):
    """Input that discards key events created before ``created_time``.

    Keystrokes buffered while the caller's pre-checks ran (an RBAC or
    manifest-fetch round trip) predate the dialog: they must never type
    into or submit a prompt the user has not yet seen.
    """

    def __init__(self, created_time: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._created_time = created_time

    async def _on_key(self, event: events.Key) -> None:
        if event.time < self._created_time:
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class ConfirmScreen(ModalScreen[bool | None]):
    """y/n approval dialog; with ``require_name`` the exact resource name
    must be typed (cluster-scoped or otherwise high-blast-radius deletes).

    Resolves True on confirmation, False on an explicit decline (`n`), and
    None on dismissal without a decision (Esc). Most callers treat None as
    a cancel; the external-proposal review flow relies on the distinction —
    a dismissed proposal stays pending, a declined one is denied.

    With ``protected_context`` (issue #83) the dialog adds a red protected
    banner and the y/n shortcut is replaced by typing the context name —
    the extra layer every write in a protected context must pass. When
    ``require_name`` is also set, that resource-name gate (at least as
    strong) stays the typed requirement.

    ``managed_note`` (issue #119) renders an ownership banner — "managed by
    helm release X / operator Y, the right lever is Z" — above the preview.
    Purely informational: the approval gate is unchanged, direct writes stay
    legitimate (emergencies, debugging).

    ``impact_lines`` (issue #283) renders a graph-derived blast-radius
    section above the dry-run preview: which observed resources depend on
    this one, how completely korvid could see the cluster, and where the
    traversal stopped. Advisory only - it is already-rendered text, carries
    no decision, and changes no gate. The dialog never parses it as Rich
    markup, so a resource name containing markup stays literal.
    """

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        # An explicit decline that stays available when a typed gate owns
        # the plain `n` key as input text (external proposals need a way to
        # be denied — a dismissal leaves them pending).
        Binding("ctrl+n", "decline", "Decline", show=True),
    ]

    def __init__(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        protected_context: str | None = None,
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._operation = operation
        self._require_name = require_name
        self._preview = preview
        self._preview_title = preview_title
        self._protected_context = protected_context
        self._managed_note = managed_note
        self._impact_lines = impact_lines
        # The value the confirm input must match: the resource name when the
        # caller demanded one, otherwise the protected context name.
        self._typed_gate = require_name or protected_context
        # Same clock as event timestamps (Message.time): key events created
        # before this moment were buffered while the caller's pre-checks ran
        # and must never confirm an operation the user had not yet seen.
        # Captured at construction (always after those pre-checks) rather
        # than on_mount, which can be processed after a legitimate keystroke
        # on the already-visible dialog was created.
        self._created_time = Message().time

    def compose(self) -> ComposeResult:
        # A resize on a multi-container pod can produce more operation and
        # preview lines than a short terminal shows; a scrollable body keeps
        # every requested change reviewable before approval.
        with VerticalScroll():
            yield Static(self._title, classes="confirm-title", markup=False)
            if self._protected_context is not None:
                yield Static(
                    f" ⛨ PROTECTED CONTEXT: {self._protected_context} ",
                    classes="confirm-protected",
                    markup=False,
                )
            yield Static(self._operation, classes="confirm-operation", markup=False)
            if self._managed_note is not None:
                yield Static(f"⚠ {self._managed_note}", classes="confirm-managed", markup=False)
            if self._impact_lines:
                yield Static(self._impact_text(), classes="confirm-impact", markup=False)
            if self._preview is not None:
                yield Static(self._preview_text(), classes="confirm-preview")
            if self._typed_gate is None:
                yield Static("y = confirm    n = decline    Esc = dismiss", classes="confirm-hint")
            else:
                what = (
                    "the resource name"
                    if self._require_name is not None
                    else "the protected context name"
                )
                yield Static(
                    f"Type {what} {self._typed_gate!r} and press Enter to confirm"
                    " (Esc dismisses, Ctrl+N declines)",
                    classes="confirm-hint",
                )
                yield FreshKeysInput(
                    self._created_time, placeholder=self._typed_gate, id="confirm-name"
                )

    def on_mount(self) -> None:
        if self._typed_gate is not None:
            self.query_one(Input).focus()

    def _preview_text(self) -> Text:
        """Impact preview (server dry-run diff, issue #19, or a drain plan,
        issue #40), one styled line per change: additions green, removals
        red, modifications yellow. An empty diff is rendered explicitly -
        'the server reports no changes' is information, distinct from 'no
        preview was available' (no widget at all)."""
        text = Text(self._preview_title, style="bold")
        if not self._preview:
            text.append("\n  no changes reported", style="dim")
            return text
        styles = {"+": "green", "-": "red", "~": "yellow"}
        for line in self._preview:
            text.append("\n  ")
            text.append(line, style=styles.get(line[:1], "dim"))
        return text

    def _impact_text(self) -> Text:
        """Graph-derived blast radius (issue #283), one line per fact.

        Built by appending to a `Text` rather than parsing markup: the lines
        embed cluster-controlled names and evidence paths, and a resource
        called `[bold red]web[/]` must render literally instead of styling
        (or silently disappearing from) an approval dialog.
        """
        lines = self._impact_lines or ()
        if not lines:
            return Text()
        # Appended, not a base style: a `Text` style applies to every span
        # added later, so `Text(title, style="bold")` would bold the whole
        # section and make an advisory note shout louder than the operation.
        text = Text()
        text.append(lines[0], style="bold")
        for line in lines[1:]:
            text.append(f"\n{line}")
        return text

    def on_key(self, event: events.Key) -> None:
        if self._typed_gate is None:
            if event.key == "y":
                event.stop()
                if event.time < self._created_time:
                    return  # buffered before the dialog existed: discard
                self.dismiss(True)
            elif event.key == "n":
                event.stop()
                self.dismiss(False)

    @on(Input.Submitted, "#confirm-name")
    def _check_name(self, event: Input.Submitted) -> None:
        event.stop()
        if event.value == self._typed_gate:
            self.dismiss(True)
        else:
            self.notify(
                f"Name does not match {self._typed_gate!r}",
                severity="warning",
            )

    def action_cancel(self) -> None:
        # Esc is a dismissal, not a decision — distinct from the explicit
        # decline (`n`/Ctrl+N → False) for callers that must tell them apart.
        self.dismiss(None)

    def action_decline(self) -> None:
        self.dismiss(False)


class ReplicasPrompt(ModalScreen[int | None]):
    """Collects the target replica count for a scale operation."""

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, target: str, *, current: int | None) -> None:
        super().__init__()
        self._target = target
        self._current = current

    def compose(self) -> ComposeResult:
        shown = "unknown" if self._current is None else str(self._current)
        with Vertical():
            yield Static(f"Scale {self._target}", classes="confirm-title", markup=False)
            yield Static(f"Current replicas: {shown}", markup=False)
            yield Static("Enter the new replica count (Esc cancels)", classes="confirm-hint")
            # Prefill the actual value (not just a placeholder) so Enter alone
            # keeps the current count; select-on-focus lets typing replace it.
            yield Input(
                value="" if self._current is None else str(self._current),
                id="replicas-input",
                type="integer",
                select_on_focus=True,
            )

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted, "#replicas-input")
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            replicas = int(event.value)
        except ValueError:
            self.notify("Enter a whole number", severity="warning")
            return
        if replicas < 0:
            self.notify("Replicas cannot be negative", severity="warning")
            return
        self.dismiss(replicas)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ImagePrompt(ModalScreen[str | None]):
    """Collects a custom container image reference for the debug fallback.

    Selecting the image is a read-only choice: the pod mutation itself is
    still gated by the ConfirmScreen that follows.
    """

    CSS = _DIALOG_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, target: str) -> None:
        super().__init__()
        self._target = target

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Custom debug image for {self._target}", classes="confirm-title", markup=False
            )
            yield Static("Enter an image reference (Esc cancels)", classes="confirm-hint")
            yield Input(placeholder="registry.example.com/tools/debug:latest", id="image-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted, "#image-input")
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        image = event.value.strip()
        if not image:
            self.notify("Enter an image reference", severity="warning")
            return
        self.dismiss(image)

    def action_cancel(self) -> None:
        self.dismiss(None)
