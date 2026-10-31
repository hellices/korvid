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

from typing import Protocol

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


class SlotCommands(Protocol):
    """The `:slots` / `:ns-slots` namespace shortcut owner."""

    def open_reallocation(self) -> None: ...


class OutcomeCommands(Protocol):
    """The latest Deployment operation outcome owner."""

    def open_latest(self) -> None: ...


class PropagationCommands(Protocol):
    """The configuration propagation view's owner."""

    def open_selected(self) -> None: ...


class CreationCommands(Protocol):
    """The workload creation constraints view's owner."""

    def open_selected(self) -> None: ...


class ApiResourceCommands(Protocol):
    """The `:rediscover` owner, which also reads the API again for an unknown kind."""

    def refresh(self) -> None: ...

    def resolve_unknown(self, text: str) -> bool: ...


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
        slots: SlotCommands,
        outcomes: OutcomeCommands | None = None,
        propagation: PropagationCommands | None = None,
        creation: CreationCommands | None = None,
        api_resources: ApiResourceCommands | None = None,
    ) -> None:
        self._ui = ui
        self._agent = agent
        self._integrations = integrations
        self._proposals = proposals
        self._forwards = forwards
        self._operators = operators
        self._pulse = pulse
        self._keybindings = keybindings
        self._slots = slots
        self._outcomes = outcomes
        self._propagation = propagation
        self._creation = creation
        self._api_resources = api_resources

    def route_builtin(self, command: BuiltinCommand) -> None:
        """Dispatch an app-owned command by canonical operation identity."""
        arguments = list(command.arguments)
        operation = command.operation
        no_argument_handlers = {
            BuiltinOperation.PROPOSALS: self._proposals.open_review,
            BuiltinOperation.PORT_FORWARDS: self._forwards.open_list,
            BuiltinOperation.PULSE: self._pulse.open_detail,
            BuiltinOperation.KEYBINDINGS: self._keybindings.open_editor,
            BuiltinOperation.NAMESPACE_SLOTS: self._slots.open_reallocation,
            BuiltinOperation.DEPLOYMENT_OUTCOMES: self._open_outcomes,
            BuiltinOperation.CONFIG_PROPAGATION: self._open_propagation,
            BuiltinOperation.CREATION_FAILURES: self._open_creation,
            BuiltinOperation.REDISCOVER: self._rediscover,
        }
        handler = no_argument_handlers.get(operation)
        if handler is not None:
            handler()
            return
        if operation in (BuiltinOperation.AI, BuiltinOperation.MODEL):
            self._route_agent(command)
            return
        if operation is BuiltinOperation.MCP:
            self._integrations.handle_mcp_command(arguments)
            return
        if operation is BuiltinOperation.TELEPRESENCE:
            self._integrations.handle_telepresence_command()
            return
        raise AssertionError(f"Unhandled built-in operation: {operation}")

    def _open_outcomes(self) -> None:
        if self._outcomes is None:
            self._ui.notify(
                "Deployment outcome tracking is unavailable in this session",
                severity="warning",
            )
            return
        self._outcomes.open_latest()

    def _open_propagation(self) -> None:
        if self._propagation is None:
            self._ui.notify(
                "Configuration propagation is unavailable in this session", severity="warning"
            )
            return
        self._propagation.open_selected()

    def _open_creation(self) -> None:
        if self._creation is None:
            self._ui.notify(
                "Workload creation failures are unavailable in this session", severity="warning"
            )
            return
        self._creation.open_selected()

    def _rediscover(self) -> None:
        if self._api_resources is None:
            self._ui.notify(
                "Reading API resources is unavailable in this session", severity="warning"
            )
            return
        self._api_resources.refresh()

    def _route_agent(self, command: BuiltinCommand) -> None:
        if not self._agent.available:
            self._report_unknown(_builtin_text(command))
        elif command.operation is BuiltinOperation.AI:
            self._agent.handle_command(list(command.arguments))
        else:
            self._agent.handle_model_command(list(command.arguments))

    def route_unknown(self, command: UnknownCommand) -> None:
        """Read the API again for it, explain a missing operator catalog, or report it.

        A kind the cluster started serving since the last discovery resolves
        once the API resources are read again: the owner then posts the
        command again, marked `rediscovered` if it is still unknown.
        """
        text = command.text
        api_resources = self._api_resources
        if (
            not command.rediscovered
            and api_resources is not None
            and api_resources.resolve_unknown(text)
        ):
            return
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
