"""Direct tests for `CommandRouter` (issue #187 / Deep Task 9).

App-owned commands arrive with a canonical operation identity. Genuinely
unknown commands use a separate path that only checks the operator-catalog
special case before reporting them.
"""

from __future__ import annotations

from korvid.ui.command_router import CommandRouter
from korvid.ui.messages import BuiltinCommand, BuiltinOperation, UnknownCommand

from .test_write_coordinator import FakeUi


class FakeAgent:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.commands: list[list[str]] = []
        self.model_commands: list[list[str]] = []

    def handle_command(self, args: list[str]) -> None:
        self.commands.append(args)

    def handle_model_command(self, args: list[str]) -> None:
        self.model_commands.append(args)


class FakeIntegrations:
    def __init__(self) -> None:
        self.mcp: list[list[str]] = []
        self.telepresence = 0

    def handle_mcp_command(self, args: list[str]) -> None:
        self.mcp.append(args)

    def handle_telepresence_command(self) -> None:
        self.telepresence += 1


class FakeProposals:
    def __init__(self) -> None:
        self.reviews = 0

    def open_review(self) -> None:
        self.reviews += 1


class FakeForwards:
    def __init__(self) -> None:
        self.lists = 0

    def open_list(self) -> None:
        self.lists += 1


class FakeOperators:
    def __init__(self, *, catalog_missing: bool = False) -> None:
        self.catalog_missing = catalog_missing
        self.explanations = 0

    def explain_missing_catalog(self) -> bool:
        if not self.catalog_missing:
            return False
        self.explanations += 1
        return True


class Harness:
    def __init__(self, *, agent_available: bool = True, catalog_missing: bool = False) -> None:
        self.ui = FakeUi()
        self.agent = FakeAgent(agent_available)
        self.integrations = FakeIntegrations()
        self.proposals = FakeProposals()
        self.forwards = FakeForwards()
        self.operators = FakeOperators(catalog_missing=catalog_missing)
        self.router = CommandRouter(
            ui=self.ui,
            agent=self.agent,
            integrations=self.integrations,
            proposals=self.proposals,
            forwards=self.forwards,
            operators=self.operators,
        )


def _builtin(operation: BuiltinOperation, *arguments: str) -> BuiltinCommand:
    return BuiltinCommand(operation, arguments)


def test_ai_and_agent_reach_the_agent_owner() -> None:
    h = Harness()
    h.router.route_builtin(_builtin(BuiltinOperation.AI, "on"))
    h.router.route_builtin(_builtin(BuiltinOperation.AI, "off"))
    assert h.agent.commands == [["on"], ["off"]]
    assert h.ui.notifications == []


def test_model_reaches_the_agent_owner() -> None:
    h = Harness()
    h.router.route_builtin(_builtin(BuiltinOperation.MODEL, "list"))
    assert h.agent.model_commands == [["list"]]


def test_agent_commands_fall_through_when_the_agent_is_unavailable() -> None:
    """Without the [agent] extra there is no owner: the command is unknown,
    not silently swallowed."""
    h = Harness(agent_available=False)
    h.router.route_builtin(_builtin(BuiltinOperation.AI, "on"))
    h.router.route_builtin(_builtin(BuiltinOperation.MODEL, "list"))
    assert h.agent.commands == []
    assert h.agent.model_commands == []
    assert len(h.ui.notifications) == 2
    assert all("Unknown resource or command" in message for message in h.ui.messages())


def test_mcp_reaches_the_integration_owner() -> None:
    h = Harness()
    h.router.route_builtin(_builtin(BuiltinOperation.MCP, "follow", "on"))
    assert h.integrations.mcp == [["follow", "on"]]


def test_tp_and_telepresence_reach_the_integration_owner() -> None:
    h = Harness()
    h.router.route_builtin(_builtin(BuiltinOperation.TELEPRESENCE))
    h.router.route_builtin(_builtin(BuiltinOperation.TELEPRESENCE))
    assert h.integrations.telepresence == 2


def test_proposals_reaches_the_proposal_owner() -> None:
    h = Harness()
    h.router.route_builtin(_builtin(BuiltinOperation.PROPOSALS))
    assert h.proposals.reviews == 1


def test_pf_reaches_the_forward_owner() -> None:
    h = Harness()
    h.router.route_builtin(_builtin(BuiltinOperation.PORT_FORWARDS))
    assert h.forwards.lists == 1


def test_legacy_text_router_is_removed() -> None:
    assert not hasattr(Harness().router, "route")


def test_operators_without_a_discovered_catalog_is_explained_by_its_owner() -> None:
    h = Harness(catalog_missing=True)
    h.router.route_unknown(UnknownCommand("operators"))
    assert h.operators.explanations == 1
    assert h.ui.notifications == []


def test_operators_on_a_discovered_catalog_reports_the_syntax_error() -> None:
    """A syntax error on a discovered view (`:operators ns extra`) must not
    be reported as a missing API group."""
    h = Harness(catalog_missing=False)
    h.router.route_unknown(UnknownCommand("operators ns extra"))
    assert h.operators.explanations == 0
    assert any("Unknown resource or command" in message for message in h.ui.messages())


def test_malformed_operators_command_is_not_explained_as_a_missing_catalog() -> None:
    h = Harness(catalog_missing=True)
    h.router.route_unknown(UnknownCommand("operators ns extra"))
    assert h.operators.explanations == 0
    assert any("Unknown resource or command" in message for message in h.ui.messages())


def test_an_unknown_command_is_reported_verbatim() -> None:
    h = Harness()
    h.router.route_unknown(UnknownCommand("frobnicate [bold]everything[/bold]"))
    message = h.ui.messages()[0]
    assert "frobnicate [bold]everything[/bold]" in message
    assert "CRD not installed?" in message
    assert h.ui.notification_markup == [False]


def test_an_empty_command_is_reported_not_routed() -> None:
    h = Harness()
    h.router.route_unknown(UnknownCommand("   "))
    assert h.agent.commands == []
    assert h.integrations.mcp == []
    assert len(h.ui.notifications) == 1
