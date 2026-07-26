"""Port-forward dialog (issue #38): collect local/remote ports for a target.

Prefilled from the target's declared ports. For pods both fields stay fully
editable — an undeclared remote port is forwardable (kubectl treats pod
declarations as informational). For services kubectl resolves the remote
value against `Service.spec.ports` and rejects anything undeclared, so the
dialog constrains the remote port to the discovered service ports.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from korvid.core.portforward import ForwardRecord, ForwardRegistry
from korvid.k8s.portforward import FORWARDABLE_KINDS

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

    def __init__(
        self, target: str, remote_ports: list[int], *, restrict_remote: bool = False
    ) -> None:
        """Args:
        target: human label of the forward target (``pods/ns/name``).
        remote_ports: declared ports for prefill; may be empty.
        restrict_remote: when True (Service targets with discovered ports),
            only ``remote_ports`` values are accepted as the remote port —
            kubectl rejects a Service forward to an undeclared port.
        """
        super().__init__()
        self._target = target
        self._remote_ports = remote_ports
        self._restrict_remote = restrict_remote and bool(remote_ports)

    def compose(self) -> ComposeResult:
        prefill = str(self._remote_ports[0]) if self._remote_ports else ""
        detected = ", ".join(str(p) for p in self._remote_ports) or "none declared"
        ports_label = "Service ports" if self._restrict_remote else "Declared ports"
        with Vertical():
            yield Static(f"Port-forward {self._target}", classes="pf-title", markup=False)
            yield Static(
                f"{ports_label}: {detected}. Enter starts, Esc cancels.",
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
        if self._restrict_remote and remote not in self._remote_ports:
            # kubectl resolves Service remote ports against spec.ports and
            # fails on anything undeclared — reject it before spawning.
            declared = ", ".join(str(p) for p in self._remote_ports)
            self.notify(
                f"Service forwards must target a declared service port ({declared})",
                severity="warning",
            )
            return
        local_text = self.query_one("#pf-local", Input).value
        # An empty local port mirrors the remote one (kubectl's `:port` form
        # picks a random port instead — surprising in a TUI listing).
        local = remote if not local_text.strip() else _parse_port(local_text)
        if local is None:
            self.notify("Local port must be 1-65535", severity="warning")
            return
        if local < 1024:
            # Heads-up, not a block: non-root binds below 1024 usually fail,
            # and kubectl would only report it as a delayed "broken" toast.
            self.notify(
                f"Local port {local} is privileged — binding may require root",
                severity="warning",
            )
        self.dismiss((local, remote))

    def action_cancel(self) -> None:
        self.dismiss(None)


_LIST_CSS = """
ForwardListScreen {
    align: center middle;
}
ForwardListScreen > Vertical {
    width: 76;
    height: auto;
    max-height: 80%;
    border: heavy $primary;
    padding: 1 2;
    background: $surface;
}
ForwardListScreen .pf-title {
    text-style: bold;
}
ForwardListScreen .pf-hint {
    color: $text-muted;
}
ForwardListScreen OptionList {
    height: auto;
    max-height: 16;
    margin-top: 1;
}
"""

_EMPTY_ROW = "No active port-forwards — press shift+f on a pod or service"

#: How often the list re-polls the registry for dead forwards.
_REFRESH_SECONDS = 1.0


def forward_row(record: ForwardRecord) -> str:
    """One list row: id, liveness, and the local -> remote mapping.

    The target carries its kind (`pod/` or `service/`) — both kinds may
    legally share a namespace, name, and port, and ctrl+d / r act on the
    highlighted row, so the mapping alone must disambiguate them.
    """
    spec = record.spec
    prefix = FORWARDABLE_KINDS.get(spec.kind, spec.kind)
    return (
        f"#{record.id}  {record.status:<6}  "
        f"localhost:{spec.local_port} -> {spec.namespace}/{prefix}/{spec.name}:{spec.remote_port}"
    )


class ForwardListScreen(ModalScreen[None]):
    """`:pf` — active forwards with liveness, stop, and re-attach (issue #38).

    The screen polls the registry so a forward whose target pod died flips
    to ``broken`` while the user watches, instead of failing silently.
    Stop/re-attach effects (auditing, notifications) are injected by the app
    via callbacks — the widget stays free of core wiring.
    """

    CSS = _LIST_CSS

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("ctrl+d", "stop_forward", "Stop", show=True),
        Binding("r", "reattach_forward", "Re-attach", show=True),
    ]

    def __init__(
        self,
        registry: ForwardRegistry,
        *,
        on_stop: Callable[[ForwardRecord], None] | None = None,
        on_reattach: Callable[[ForwardRecord], None] | None = None,
        on_reattach_error: Callable[[ForwardRecord, OSError], None] | None = None,
        target_exists: Callable[[ForwardRecord], Awaitable[bool]] | None = None,
    ) -> None:
        super().__init__()
        self._registry = registry
        self._on_stop = on_stop
        self._on_reattach = on_reattach
        self._on_reattach_error = on_reattach_error
        self._target_exists = target_exists
        self._ids: list[int] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Port-forwards", classes="pf-title")
            yield Static(
                "Ctrl-D stops, r re-attaches a broken forward, Esc closes.",
                classes="pf-hint",
            )
            yield OptionList(id="pf-list")

    def on_mount(self) -> None:
        self._rebuild()
        self.set_interval(_REFRESH_SECONDS, self._poll)
        self.query_one(OptionList).focus()

    def _poll(self) -> None:
        self._registry.refresh()
        self._rebuild()

    def _rebuild(self) -> None:
        options = self.query_one(OptionList)
        highlighted = options.highlighted
        options.clear_options()
        records = self._registry.forwards()
        self._ids = [record.id for record in records]
        if not records:
            options.add_option(Option(_EMPTY_ROW, disabled=True))
            return
        for record in records:
            options.add_option(Option(forward_row(record)))
        if options.option_count:
            # Keep the previous highlight (clamped); default to the first row
            # so ctrl+d / r work immediately after opening.
            target = 0 if highlighted is None else min(highlighted, options.option_count - 1)
            options.highlighted = target

    def _highlighted_record(self) -> ForwardRecord | None:
        index = self.query_one(OptionList).highlighted
        if index is None or index >= len(self._ids):
            return None
        return self._registry.get(self._ids[index])

    def action_stop_forward(self) -> None:
        record = self._highlighted_record()
        if record is None:
            return
        stopped = self._registry.stop(record.id)
        if stopped is not None and self._on_stop is not None:
            self._on_stop(stopped)
        self._rebuild()

    async def action_reattach_forward(self) -> None:
        record = self._highlighted_record()
        if record is None:
            return
        if record.status != "broken":
            self.notify("Forward is still alive — nothing to re-attach", severity="warning")
            return
        if self._target_exists is not None and not await self._target_exists(record):
            # A Deployment replaces a dead pod under a new name; a Service
            # keeps its name, so the hint stays kind-appropriate.
            hint = (
                "its replacement has a new name; start a fresh forward with shift+f"
                if record.spec.kind == "pods"
                else "start a fresh forward with shift+f once it is recreated"
            )
            self.notify(
                f"{record.spec.name} no longer exists — {hint}",
                severity="warning",
            )
            return
        try:
            revived = self._registry.reattach(record.id)
        except OSError as exc:
            self.notify(f"Re-attach failed: {exc}", severity="error")
            if self._on_reattach_error is not None:
                # The app audits failed re-attaches like failed starts.
                self._on_reattach_error(record, exc)
            return
        if revived is not None and self._on_reattach is not None:
            self._on_reattach(revived)
        self._rebuild()

    def action_close(self) -> None:
        self.dismiss(None)
