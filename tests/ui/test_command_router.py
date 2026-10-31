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


class FakePulse:
    def __init__(self) -> None:
        self.opens = 0

    def open_detail(self) -> None:
        self.opens += 1


class FakeKeybindings:
    def __init__(self) -> None:
        self.opens = 0

    def open_editor(self) -> None:
        self.opens += 1


class FakeSlots:
    def __init__(self) -> None:
        self.opens = 0

    def open_reallocation(self) -> None:
        self.opens += 1


class FakeOutcomes:
    def __init__(self) -> None:
        self.opens = 0

    def open_latest(self) -> None:
        self.opens += 1


class FakePropagation:
    def __init__(self) -> None:
        self.opens = 0

    def open_selected(self) -> None:
        self.opens += 1


class FakeApiResources:
    def __init__(self, *, settles: bool = True) -> None:
        self.settles = settles
        self.refreshes = 0
        self.asked: list[str] = []

    def refresh(self) -> None:
        self.refreshes += 1

    def resolve_unknown(self, text: str) -> bool:
        self.asked.append(text)
        return self.settles


class Harness:
    def __init__(
        self,
        *,
        agent_available: bool = True,
        catalog_missing: bool = False,
        outcomes: FakeOutcomes | None = None,
        propagation: FakePropagation | None = None,
        creation: FakePropagation | None = None,
        api_resources: FakeApiResources | None = None,
    ) -> None:
        self.ui = FakeUi()
        self.agent = FakeAgent(agent_available)
        self.integrations = FakeIntegrations()
        self.proposals = FakeProposals()
        self.forwards = FakeForwards()
        self.operators = FakeOperators(catalog_missing=catalog_missing)
        self.pulse = FakePulse()
        self.keybindings = FakeKeybindings()
        self.slots = FakeSlots()
        self.router = CommandRouter(
            ui=self.ui,
            agent=self.agent,
            integrations=self.integrations,
            proposals=self.proposals,
            forwards=self.forwards,
            operators=self.operators,
            pulse=self.pulse,
            keybindings=self.keybindings,
            slots=self.slots,
            outcomes=outcomes,
            propagation=propagation,
            creation=creation,
            api_resources=api_resources,
        )


def _builtin(operation: BuiltinOperation, *arguments: str) -> BuiltinCommand:
    return BuiltinCommand(operation, arguments)


def test_editor_reaches_its_owner_once() -> None:
    harness = Harness()
    harness.router.route_builtin(_builtin(BuiltinOperation.KEYBINDINGS))
    assert harness.keybindings.opens == 1
    assert harness.ui.notifications == []


def test_slot_reallocation_reaches_its_owner_once() -> None:
    harness = Harness()
    harness.router.route_builtin(_builtin(BuiltinOperation.NAMESPACE_SLOTS))
    assert harness.slots.opens == 1
    assert harness.ui.notifications == []


def test_pulse_reaches_its_owner_once() -> None:
    harness = Harness()
    harness.router.route_builtin(_builtin(BuiltinOperation.PULSE))
    assert harness.pulse.opens == 1
    assert harness.ui.notifications == []


def test_deployment_outcomes_reaches_its_owner_once() -> None:
    outcomes = FakeOutcomes()
    harness = Harness(outcomes=outcomes)

    harness.router.route_builtin(_builtin(BuiltinOperation.DEPLOYMENT_OUTCOMES))

    assert outcomes.opens == 1
    assert harness.ui.notifications == []


def test_deployment_outcomes_without_owner_warns_unavailable() -> None:
    harness = Harness()

    harness.router.route_builtin(_builtin(BuiltinOperation.DEPLOYMENT_OUTCOMES))

    assert harness.ui.notifications == [
        ("Deployment outcome tracking is unavailable in this session", "warning")
    ]


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


def test_configuration_propagation_reaches_its_owner_once() -> None:
    propagation = FakePropagation()
    harness = Harness(propagation=propagation)

    harness.router.route_builtin(_builtin(BuiltinOperation.CONFIG_PROPAGATION))

    assert propagation.opens == 1
    assert harness.ui.notifications == []


def test_configuration_propagation_without_owner_warns_unavailable() -> None:
    harness = Harness()

    harness.router.route_builtin(_builtin(BuiltinOperation.CONFIG_PROPAGATION))

    assert harness.ui.notifications == [
        ("Configuration propagation is unavailable in this session", "warning")
    ]


def test_creation_failures_reach_their_owner_once() -> None:
    creation = FakePropagation()
    harness = Harness(creation=creation)

    harness.router.route_builtin(_builtin(BuiltinOperation.CREATION_FAILURES))

    assert creation.opens == 1
    assert harness.ui.notifications == []


def test_creation_failures_without_owner_warn_unavailable() -> None:
    harness = Harness()

    harness.router.route_builtin(_builtin(BuiltinOperation.CREATION_FAILURES))

    assert harness.ui.notifications == [
        ("Workload creation failures are unavailable in this session", "warning")
    ]


def test_rediscover_reaches_its_owner_once() -> None:
    api_resources = FakeApiResources()
    harness = Harness(api_resources=api_resources)

    harness.router.route_builtin(_builtin(BuiltinOperation.REDISCOVER))

    assert api_resources.refreshes == 1
    assert harness.ui.notifications == []


def test_rediscover_without_owner_warns_unavailable() -> None:
    harness = Harness()

    harness.router.route_builtin(_builtin(BuiltinOperation.REDISCOVER))

    assert harness.ui.notifications == [
        ("Reading API resources is unavailable in this session", "warning")
    ]


def test_an_unknown_command_waits_for_the_api_resources_to_be_read_again() -> None:
    api_resources = FakeApiResources()
    harness = Harness(api_resources=api_resources, catalog_missing=True)

    harness.router.route_unknown(UnknownCommand("operators"))

    assert api_resources.asked == ["operators"]
    assert harness.operators.explanations == 0
    assert harness.ui.messages() == []


def test_a_command_still_unknown_after_reading_again_is_reported_not_read_again() -> None:
    api_resources = FakeApiResources()
    harness = Harness(api_resources=api_resources, catalog_missing=True)

    harness.router.route_unknown(UnknownCommand("widgets", rediscovered=True))
    harness.router.route_unknown(UnknownCommand("operators", rediscovered=True))

    assert api_resources.asked == []
    assert api_resources.refreshes == 0
    assert harness.operators.explanations == 1
    assert any("Unknown resource or command: widgets" in m for m in harness.ui.messages())


def test_an_unknown_command_no_read_will_settle_is_reported_now() -> None:
    api_resources = FakeApiResources(settles=False)
    harness = Harness(api_resources=api_resources)

    harness.router.route_unknown(UnknownCommand("widgets"))

    assert api_resources.asked == ["widgets"]
    assert api_resources.refreshes == 0
    assert any("Unknown resource or command: widgets" in m for m in harness.ui.messages())
