"""Prompt collecting per-container requests/limits for in-place pod resize.

Part of the issue #27 flow: prefilled with the pod's current quantities, it
returns only the values the user actually changed — the ``pods/resize``
strategic merge patch keeps everything else as-is. Empty fields mean "keep
current", never "remove".
"""

from __future__ import annotations

import re
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

#: Container name paired with its current resources mapping
#: (``{"requests": {"cpu": "100m", ...}, "limits": {...}}``).
ContainerResources = tuple[str, dict[str, dict[str, str]]]

#: Kubernetes quantity: plain/decimal number with an optional SI or binary
#: suffix (`m`, `k`, `M`, ..., `Ki`, `Mi`, ...). Good enough to catch typos
#: before the apiserver sees them; the server stays the final validator.
_QUANTITY = re.compile(r"^\d+(\.\d+)?(m|k|M|G|T|P|E|Ki|Mi|Gi|Ti|Pi|Ei)?$")

_SECTIONS = ("requests", "limits")
_QUANTITIES = ("cpu", "memory")

_CSS = """
ResizePrompt {
    align: center middle;
}
ResizePrompt > Vertical {
    width: 76;
    height: auto;
    max-height: 80%;
    border: heavy $error;
    padding: 1 2;
    background: $surface;
}
ResizePrompt .confirm-title {
    text-style: bold;
}
ResizePrompt .confirm-hint {
    color: $text-muted;
}
ResizePrompt .resize-container {
    text-style: bold;
    margin-top: 1;
}
ResizePrompt .resize-row {
    height: auto;
}
ResizePrompt .resize-label {
    width: 16;
    padding-top: 1;
    color: $text-muted;
}
ResizePrompt .resize-row Input {
    width: 1fr;
    margin-right: 1;
}
"""


class ResizePrompt(ModalScreen["dict[str, dict[str, dict[str, str]]] | None"]):
    """Collects new requests/limits for each container of a pod.

    Dismisses with a ``{container: {"requests"/"limits": {"cpu"/"memory":
    quantity}}}`` mapping containing only changed values, or None when
    cancelled.
    """

    CSS = _CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, target: str, *, containers: list[ContainerResources]) -> None:
        super().__init__()
        self._target = target
        self._containers = containers

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Resize {self._target}", classes="confirm-title", markup=False)
            yield Static(
                "Edit quantities and press Enter (Esc cancels). "
                "Empty fields keep the current value.",
                classes="confirm-hint",
            )
            for index, (name, resources) in enumerate(self._containers):
                yield Static(name, classes="resize-container", markup=False)
                for section in _SECTIONS:
                    with Horizontal(classes="resize-row"):
                        yield Static(section, classes="resize-label", markup=False)
                        for quantity in _QUANTITIES:
                            yield Input(
                                value=resources.get(section, {}).get(quantity, ""),
                                placeholder=quantity,
                                id=f"resize-{index}-{section}-{quantity}",
                                select_on_focus=True,
                            )

    def on_mount(self) -> None:
        self.query(Input).first().focus()

    def _collect_changes(self) -> dict[str, dict[str, dict[str, str]]] | None:
        """Changed quantities per container; None when a value is invalid."""
        changes: dict[str, dict[str, dict[str, str]]] = {}
        for index, (name, resources) in enumerate(self._containers):
            for section in _SECTIONS:
                for quantity in _QUANTITIES:
                    field = self.query_one(f"#resize-{index}-{section}-{quantity}", Input)
                    value = field.value.strip()
                    current = resources.get(section, {}).get(quantity, "")
                    if not value or value == current:
                        continue
                    if not _QUANTITY.match(value):
                        self.notify(
                            f"{name} {section}.{quantity}: '{value}' is not a "
                            "valid quantity (e.g. 250m, 512Mi)",
                            severity="warning",
                        )
                        return None
                    changes.setdefault(name, {}).setdefault(section, {})[quantity] = value
        return changes

    @on(Input.Submitted)
    def _submit(self, event: Input.Submitted) -> None:
        event.stop()
        changes = self._collect_changes()
        if changes is None:
            return
        if not changes:
            self.notify("No changes to apply", severity="warning")
            return
        self.dismiss(changes)

    def action_cancel(self) -> None:
        self.dismiss(None)
