"""Typed routing for app-owned and genuinely unknown `:` commands.

The command parser assigns app-owned commands a canonical operation. This
router maps that identity to typed collaborators without reparsing command
text. Unknown commands only retain the operator-catalog special case before
being reported.

Deliberately thin. The router holds no feature state and performs no
feature work; each branch is one call on a typed collaborator, and the only
message it produces itself is the unknown-command report - the one thing
that is genuinely *its* job, because "no owner claimed this" is a routing
outcome. Anything more would recreate the integration hub the app was
decomposed to remove.

The collaborators are structural `Protocol`s rather than the concrete
controllers, so the router imports none of them.
"""

from __future__ import annotations

from typing import Protocol, assert_never

from korvid.ui.messages import BuiltinCommand, BuiltinOperation, UnknownCommand
from korvid.ui.ui_surface import UiSurface


class AgentCommands(Protocol):
    """The `:ai` / `:agent` / `:model` owner."""

    @property
    def available(self) -> bool:
        """False without the [agent] extra: the commands then have no owner."""

    def handle_command(self, args: list[str]) -> None: ...

    def handle_model_command(self, args: list[str]) -> None: ...


class IntegrationCommands(Protocol):
    """The `:mcp` / `:tp` owner."""

    def handle_mcp_command(self, args: list[str]) -> None: ...

    def handle_telepresence_command(self) -> None: ...


class ProposalCommands(Protocol):
    """The `:proposals` owner."""

    def open_review(self) -> None: ...


class ForwardCommands(Protocol):
    """The `:pf` owner."""

    def open_list(self) -> None: ...


class CatalogCommands(Protocol):
    """The `:operators` owner.

    Answers whether it *handled* the command: only the OLM owner can tell
    "the packages API was never discovered" (explain the absence) from "the
    view exists and the arguments were wrong" (fall through to the normal
    unknown-command report).
    """

    def explain_missing_catalog(self) -> bool: ...


class PulseCommands(Protocol):
    """The ambient attention detail owner."""

    def open_detail(self) -> None: ...


class KeybindingCommands(Protocol):
    """The staged `:keys` / `:keybindings` owner."""

    def open_editor(self) -> None: ...


class CommandRouter:
    """Dispatches typed commands to the owner that implements them."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        agent: AgentCommands,
        integrations: IntegrationCommands,
        proposals: ProposalCommands,
        forwards: ForwardCommands,
        operators: CatalogCommands,
        pulse: PulseCommands,
        keybindings: KeybindingCommands,
    ) -> None:
        self._ui = ui
        self._agent = agent
        self._integrations = integrations
        self._proposals = proposals
        self._forwards = forwards
        self._operators = operators
        self._pulse = pulse
        self._keybindings = keybindings

    def route_builtin(self, command: BuiltinCommand) -> None:
        """Dispatch an app-owned command by canonical operation identity."""
        arguments = list(command.arguments)
        operation = command.operation
        if operation in (BuiltinOperation.AI, BuiltinOperation.MODEL):
            self._route_agent(command)
            return
        if operation is BuiltinOperation.MCP:
            self._integrations.handle_mcp_command(arguments)
            return
        if operation is BuiltinOperation.TELEPRESENCE:
            self._integrations.handle_telepresence_command()
            return
        if operation is BuiltinOperation.PROPOSALS:
            self._proposals.open_review()
            return
        if operation is BuiltinOperation.PORT_FORWARDS:
            self._forwards.open_list()
            return
        if operation is BuiltinOperation.PULSE:
            self._pulse.open_detail()
            return
        if operation is BuiltinOperation.KEYBINDINGS:
            self._keybindings.open_editor()
            return
        assert_never(operation)

    def _route_agent(self, command: BuiltinCommand) -> None:
        if not self._agent.available:
            self._report_unknown(_builtin_text(command))
        elif command.operation is BuiltinOperation.AI:
            self._agent.handle_command(list(command.arguments))
        else:
            self._agent.handle_model_command(list(command.arguments))

    def route_unknown(self, command: UnknownCommand) -> None:
        """Explain a missing operator catalog or report a genuine unknown."""
        text = command.text
        parts = text.strip().split()
        head = parts[0] if parts else ""
        args = parts[1:]
        if head == "operators" and len(args) <= 1 and self._operators.explain_missing_catalog():
            return
        self._report_unknown(text)

    def _report_unknown(self, text: str) -> None:
        """Report command text that no owner claims."""
        self._ui.notify(
            f"Unknown resource or command: {text}"
            " — not found in this cluster's API (CRD not installed?)",
            severity="warning",
            markup=False,
        )


def _builtin_text(command: BuiltinCommand) -> str:
    """Render a typed command for the unavailable-owner warning."""
    return " ".join((command.operation.value, *command.arguments))
