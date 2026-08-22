"""Where an unresolved `:` command goes (issue #187 / Deep Task 9).

The command bar resolves what it can - a resource kind, `ns`, `ctx`, `q`.
Everything else arrives here as an `UnknownCommand`, and this is the single
place that decides which owner it belongs to: the agent session, the
optional integrations, the external-proposal review, the port-forward list,
or the operator catalog.

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


class CommandRouter:
    """Dispatches an unresolved `:` command to the owner that implements it."""

    def __init__(
        self,
        *,
        ui: UiSurface,
        agent: AgentCommands,
        integrations: IntegrationCommands,
        proposals: ProposalCommands,
        forwards: ForwardCommands,
        operators: CatalogCommands,
    ) -> None:
        self._ui = ui
        self._agent = agent
        self._integrations = integrations
        self._proposals = proposals
        self._forwards = forwards
        self._operators = operators

    def route(self, text: str) -> None:
        """Hand *text* to its owner, or report that nothing claims it."""
        parts = text.strip().split()
        head = parts[0] if parts else ""
        args = parts[1:]
        if head in {"ai", "agent"} and self._agent.available:
            self._agent.handle_command(args)
            return
        if head == "model" and self._agent.available:
            self._agent.handle_model_command(args)
            return
        if head == "mcp":
            self._integrations.handle_mcp_command(args)
            return
        if head in {"tp", "telepresence"} and not args:
            self._integrations.handle_telepresence_command()
            return
        if head == "proposals" and not args:
            self._proposals.open_review()
            return
        if head == "pf" and not args:
            self._forwards.open_list()
            return
        if head == "operators" and len(args) <= 1 and self._operators.explain_missing_catalog():
            return
        self._ui.notify(
            f"Unknown resource or command: {text}"
            " — not found in this cluster's API (CRD not installed?)",
            severity="warning",
            markup=False,
        )
