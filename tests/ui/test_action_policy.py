"""Characterization tests for the extracted action binding policy (#388).

`ActionPolicy` is a behavior-preserving extraction of `KorvidApp.check_action`
(issue #114): the same overloaded-key routing, log-pane gating and helm
synthetic-view exception, now testable without composing the Textual app.
"""

from collections.abc import Callable, Mapping

import pytest

from korvid.core.store import Summary
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.ui.action_availability import (
    AGENT_UNAVAILABLE,
    CONTEXT_SWITCH_IN_PROGRESS,
    ActionAvailability,
    AvailabilityCode,
    UnavailableReason,
)
from korvid.ui.action_policy import (
    _HELM_REASON_ACTIONS,
    _INSPECT_REASON_ACTIONS,
    _LOG_REASON_ACTIONS,
    _WORKSPACE_REASON_ACTIONS,
    _WRITE_REASON_ACTIONS,
    ActionPolicy,
    compose_action_reasons,
    compose_command_reasons,
)
from korvid.ui.app_bindings import APP_BINDINGS, as_binding, base_action
from korvid.ui.command import COMMANDS
from korvid.ui.read_availability import SEARCH_ACTIONS, SORT_ACTION_COLUMNS
from korvid.ui.view_state import ViewState


class FakeView(ViewState):
    def __init__(self, meta: ResourceMeta) -> None:
        self.meta = meta

    def current_kind(self) -> str:
        return self.meta.plural

    def current_scope(self) -> str:
        return "default"

    def canonical_kind(self, kind: str) -> str:
        return self.meta.plural

    def aliases(self) -> Mapping[str, ResourceMeta]:
        return {self.meta.plural: self.meta}

    def resources(self, kind: str, scope: str) -> list[Summary]:
        return []

    def readonly(self) -> bool:
        return False

    def default_namespace(self) -> str | None:
        return "default"

    def selected_ns_name(self, *, notify: bool = True) -> tuple[str | None, str | None]:
        return "default", "selected"

    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        return "uid-selected"

    def gvr_label(self, meta: ResourceMeta) -> str:
        return meta.plural

    def write_locus(self, namespace: str | None) -> str:
        return f"in namespace {namespace}" if namespace else "cluster-wide"


def _policy(
    *,
    group: str,
    plural: str,
    synthetic: bool = False,
    log_pane_open: Callable[[], bool] = lambda: False,
    agent_available: Callable[[], bool] = lambda: True,
    agent_busy: Callable[[], bool] | None = None,
    reason_by_action: Mapping[str, Callable[[], UnavailableReason | None]] | None = None,
) -> ActionPolicy:
    meta = ResourceMeta(
        kind=plural.removesuffix("s").title(),
        plural=plural,
        group=group,
        version="v1",
        namespaced=plural != "nodes",
        synthetic=synthetic,
    )
    return ActionPolicy(
        view=FakeView(meta),
        agent_available=agent_available,
        log_pane_open=log_pane_open,
        agent_busy=agent_busy,
        reason_by_action=reason_by_action,
    )


def test_binding_policy_routes_overloaded_actions_by_resource_identity() -> None:
    policy = _policy(group="", plural="pods")
    assert policy.binding_enabled("logs") is True
    assert policy.binding_enabled("hint_details") is True
    assert policy.binding_enabled("helm_install") is False
    assert policy.binding_enabled("cordon_node") is False


def test_binding_policy_uses_pane_and_composition_state() -> None:
    log_open = False
    agent_available = False
    policy = _policy(
        group="",
        plural="pods",
        log_pane_open=lambda: log_open,
        agent_available=lambda: agent_available,
    )
    assert policy.binding_enabled("log_wrap") is False
    assert policy.binding_enabled("toggle_agent") is False
    assert policy.binding_enabled("help") is True


def test_binding_policy_preserves_helm_delete_exception() -> None:
    policy = _policy(
        group=HELM_RELEASES_META.group,
        plural=HELM_RELEASES_META.plural,
        synthetic=True,
    )
    assert policy.binding_enabled("delete_resource") is True
    assert policy.binding_enabled("edit_resource") is False


def test_availability_is_fully_enabled_without_a_registered_reason() -> None:
    """An action with no owner resolver (most actions) is simply invocable."""
    policy = _policy(group="", plural="pods")
    assert policy.availability("help") == ActionAvailability.enabled()


def test_availability_explains_a_wrong_view_binding() -> None:
    """`logs` is pods-only; on `nodes` the binding is disabled and the
    palette needs a reason to explain the disabled entry, not just a bool."""
    policy = _policy(group="", plural="nodes")
    availability = policy.availability("logs")
    assert availability.binding_enabled is False
    assert availability.reason == UnavailableReason(
        AvailabilityCode.WRONG_VIEW, "Not available in this view"
    )
    assert availability.invocable is False


def test_availability_explains_a_synthetic_gated_binding() -> None:
    """`edit_resource` is gated off every synthetic view except the helm
    delete exception; the palette must say *why* it's greyed out there."""
    policy = _policy(
        group=HELM_RELEASES_META.group,
        plural=HELM_RELEASES_META.plural,
        synthetic=True,
    )
    availability = policy.availability("edit_resource")
    assert availability.binding_enabled is False
    assert availability.reason == UnavailableReason(
        AvailabilityCode.UNSUPPORTED_RESOURCE, "Helmrelease is a read-only view"
    )


def test_availability_explains_a_closed_log_pane() -> None:
    policy = _policy(group="", plural="pods", log_pane_open=lambda: False)
    availability = policy.availability("log_wrap")
    assert availability.binding_enabled is False
    assert availability.reason == UnavailableReason(
        AvailabilityCode.PANE_CLOSED, "Open the log pane first"
    )


def test_availability_explains_an_unavailable_agent() -> None:
    policy = _policy(group="", plural="pods", agent_available=lambda: False)
    availability = policy.availability("toggle_agent")
    assert availability.binding_enabled is False
    assert availability.reason == UnavailableReason(
        AvailabilityCode.MISSING_CAPABILITY, "Agent is not available"
    )


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (AvailabilityCode.NO_SELECTION, "Select a resource first"),
        (AvailabilityCode.READ_ONLY, "Read-only mode: cluster writes are disabled"),
        (AvailabilityCode.MISSING_CAPABILITY, "Writes disabled: no audit log configured"),
        (AvailabilityCode.UNSUPPORTED_RESOURCE, "Restart does not apply to pods"),
        (AvailabilityCode.UNSUPPORTED_RESOURCE, "Scale does not apply to pods"),
    ],
)
def test_availability_surfaces_the_owner_reason_for_a_generic_write(
    code: AvailabilityCode, message: str
) -> None:
    """`ActionPolicy` composes whatever reason its owner resolver returns
    verbatim - the resolver (`ResourceWriteController.unavailable_reason`)
    owns the check, `ActionPolicy` only wires it in (#388)."""
    reason = UnavailableReason(code, message)
    policy = _policy(
        group="",
        plural="pods",
        reason_by_action={"delete_resource": lambda: reason},
    )
    availability = policy.availability("delete_resource")
    assert availability.binding_enabled is True
    assert availability.reason == reason
    assert availability.invocable is False


def test_binding_enabled_stays_true_for_no_selection_and_read_only() -> None:
    """The palette shows *why* an entry can't run, but the key must stay
    dispatchable so the existing handler notification still fires when the
    user presses it directly (issue #114's overloaded-key contract)."""
    policy = _policy(
        group="",
        plural="pods",
        reason_by_action={
            "delete_resource": lambda: UnavailableReason(
                AvailabilityCode.NO_SELECTION, "Select a resource first"
            ),
        },
    )
    assert policy.binding_enabled("delete_resource") is True
    assert policy.availability("delete_resource").binding_enabled is True


def test_interrupt_agent_stays_bound_but_is_not_invocable_while_idle() -> None:
    """Ctrl-X is a priority binding that must keep working as a key (its
    visibility is deliberately unchanged), but there is nothing to
    interrupt while no turn is running - the palette says so (#388)."""
    policy = _policy(group="", plural="pods", agent_busy=lambda: False)
    availability = policy.availability("interrupt_agent")
    assert policy.binding_enabled("interrupt_agent") is True
    assert availability.binding_enabled is True
    assert availability.reason == UnavailableReason(
        AvailabilityCode.PROTECTED_UI, "No Agent turn is running"
    )
    assert availability.invocable is False


def test_interrupt_agent_is_invocable_during_a_turn() -> None:
    policy = _policy(group="", plural="pods", agent_busy=lambda: True)
    assert policy.availability("interrupt_agent") == ActionAvailability.enabled()


def test_interrupt_agent_is_invocable_without_an_injected_busy_probe() -> None:
    """The default composition answer is "invocable": a policy built
    without an agent (tests, headless) must not grey out a bound key."""
    policy = _policy(group="", plural="pods")
    assert policy.availability("interrupt_agent").invocable is True


def test_interrupt_agent_resolver_registered_via_reason_by_action_wins() -> None:
    """Regression (#388 task 4 review): `interrupt_agent`'s default resolver
    is folded into `_reason_by_action` at construction rather than checked
    as a second, hard-coded branch - one source of truth, so an explicit
    registration (a future caller composing it differently) overrides the
    `agent_busy` default instead of silently losing to it or racing it."""
    policy = _policy(
        group="",
        plural="pods",
        agent_busy=lambda: False,  # idle: the default would refuse
        reason_by_action={"interrupt_agent": lambda: None},  # explicit: always invocable
    )
    assert policy.availability("interrupt_agent") == ActionAvailability.enabled()


# ---------------------------------------------------------------------------
# The palette's own open binding: protected and transient surfaces (task 6)
# ---------------------------------------------------------------------------


def _palette_policy(
    *,
    screen_depth: Callable[[], int] = lambda: 1,
    inline_editor_open: Callable[[], bool] = lambda: False,
    switching: Callable[[], bool] = lambda: False,
    app_running: Callable[[], bool] = lambda: True,
) -> ActionPolicy:
    meta = ResourceMeta(
        kind="Pod", plural="pods", group="", version="v1", namespaced=True, synthetic=False
    )
    return ActionPolicy(
        view=FakeView(meta),
        agent_available=lambda: True,
        log_pane_open=lambda: False,
        screen_depth=screen_depth,
        inline_editor_open=inline_editor_open,
        switching=switching,
        app_running=app_running,
    )


def test_palette_binding_is_enabled_on_the_ordinary_workspace() -> None:
    policy = _palette_policy()
    assert policy.binding_enabled("open_action_palette") is True
    assert policy.availability("open_action_palette") == ActionAvailability.enabled()


@pytest.mark.parametrize(
    ("probes", "code", "message"),
    [
        ({"screen_depth": lambda: 2}, AvailabilityCode.PROTECTED_UI, "Close the open dialog first"),
        (
            {"inline_editor_open": lambda: True},
            AvailabilityCode.PROTECTED_UI,
            "Finish the command or filter entry first",
        ),
        (
            {"switching": lambda: True},
            AvailabilityCode.TRANSITION,
            CONTEXT_SWITCH_IN_PROGRESS.message,
        ),
        (
            {"app_running": lambda: False},
            AvailabilityCode.PROTECTED_UI,
            "korvid is shutting down",
        ),
    ],
)
def test_palette_binding_is_refused_on_every_protected_surface(
    probes: dict[str, Callable[[], object]], code: AvailabilityCode, message: str
) -> None:
    """`Ctrl-P` is a priority binding, so it fires over any screen: the
    policy is the only thing that keeps the palette off an approval dialog,
    a half-typed command, a context switch, or a shutting-down app (#388)."""
    policy = _palette_policy(**probes)  # type: ignore[arg-type]  # per-case probe override
    availability = policy.availability("open_action_palette")
    assert policy.binding_enabled("open_action_palette") is False
    assert availability.binding_enabled is False
    assert availability.reason == UnavailableReason(code, message)
    assert availability.invocable is False


def test_palette_binding_defaults_to_enabled_without_injected_probes() -> None:
    """A policy composed without the app's surface probes (tests, headless)
    must not grey out a bound key it knows nothing about - the same default
    stance `agent_busy` takes."""
    policy = _policy(group="", plural="pods")
    assert policy.binding_enabled("open_action_palette") is True


# ---------------------------------------------------------------------------
# `:` commands answer in their own namespace, never the action one (task 6
# review): a command's canonical text and an app action's name are different
# vocabularies that happen to be strings, so they get different APIs.
# ---------------------------------------------------------------------------


def _command_policy(
    *,
    plural: str = "pods",
    reason_by_action: Mapping[str, Callable[[], UnavailableReason | None]] | None = None,
    reason_by_command: Mapping[str, Callable[[], UnavailableReason | None]] | None = None,
) -> ActionPolicy:
    meta = ResourceMeta(
        kind=plural.removesuffix("s").title(),
        plural=plural,
        group="",
        version="v1",
        namespaced=plural != "nodes",
    )
    return ActionPolicy(
        view=FakeView(meta),
        agent_available=lambda: True,
        log_pane_open=lambda: False,
        reason_by_action=reason_by_action,
        reason_by_command=reason_by_command,
    )


def test_command_availability_is_enabled_without_an_owner_reason() -> None:
    """Most `:` commands have no capability to miss: they are simply runnable."""
    policy = _command_policy()
    assert policy.command_availability("pulse") == ActionAvailability.enabled()


def test_command_availability_surfaces_the_owner_reason() -> None:
    reason = UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, "Agent is not available")
    policy = _command_policy(reason_by_command={"ai": lambda: reason})
    availability = policy.command_availability("ai")
    assert availability.binding_enabled is True
    assert availability.reason == reason
    assert availability.invocable is False


def test_command_and_action_namespaces_cannot_collide() -> None:
    """A command whose canonical text equals an app action's name must not
    inherit that action's answer, in either direction."""
    action_reason = UnavailableReason(AvailabilityCode.NO_SELECTION, "Select a resource first")
    command_reason = UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, "No owner for this")
    policy = _command_policy(
        reason_by_action={"help": lambda: action_reason},
        reason_by_command={"help": lambda: command_reason},
    )
    assert policy.availability("help").reason == action_reason
    assert policy.command_availability("help").reason == command_reason


def test_command_availability_is_not_gated_on_the_current_view() -> None:
    """`logs` is a pods-only *action*; a `:logs`-shaped command name must not
    pick up that view gate, because commands are not bound keys."""
    policy = _command_policy(plural="nodes")
    assert policy.availability("logs").binding_enabled is False
    assert policy.command_availability("logs") == ActionAvailability.enabled()


def test_command_availability_ignores_the_palette_surface_probes() -> None:
    """The surface guard belongs to the palette's own open binding, not to
    every command row inside it."""
    policy = ActionPolicy(
        view=FakeView(
            ResourceMeta(kind="Pod", plural="pods", group="", version="v1", namespaced=True)
        ),
        agent_available=lambda: True,
        log_pane_open=lambda: False,
        screen_depth=lambda: 2,
        reason_by_command={"open_action_palette": lambda: None},
    )
    assert policy.binding_enabled("open_action_palette") is False
    assert policy.command_availability("open_action_palette") == ActionAvailability.enabled()


# ---------------------------------------------------------------------------
# Reason-map composition (#388 task 8)
# ---------------------------------------------------------------------------


def _recording_probe(
    calls: list[str], reason: UnavailableReason | None
) -> Callable[[str], UnavailableReason | None]:
    def probe(action: str) -> UnavailableReason | None:
        calls.append(action)
        return reason

    return probe


def test_composed_action_reasons_ask_each_owner_about_its_own_actions() -> None:
    """The composition root names the owners; this module owns *which*
    actions each of them answers for, so the action vocabulary lives next
    to `_ACTION_VIEWS` rather than being spelled out again at the wiring.

    Each resolver must also carry its own action: a bound resolver that
    dropped the name would ask its owner about the wrong write.
    """
    write_calls: list[str] = []
    helm_calls: list[str] = []
    log_calls: list[str] = []
    inspect_calls: list[str] = []
    workspace_calls: list[str] = []
    refused = UnavailableReason(AvailabilityCode.NO_SELECTION, "No resource selected")
    reasons = compose_action_reasons(
        writes=_recording_probe(write_calls, refused),
        helm=_recording_probe(helm_calls, None),
        logs=_recording_probe(log_calls, None),
        inspect=_recording_probe(inspect_calls, None),
        workspace=_recording_probe(workspace_calls, None),
        port_forward=lambda: None,
        transfer=lambda: None,
        shell=lambda: None,
        operator_install=lambda: None,
    )
    assert set(reasons) == {
        "delete_resource",
        "edit_resource",
        "rollout_restart",
        "scale_resource",
        "resize_pod",
        "cordon_node",
        "uncordon_node",
        "drain_node",
        "helm_install",
        "helm_upgrade",
        "helm_history",
        "helm_rollback",
        "logs",
        "logs_multi",
        "log_save",
        "describe",
        "hint_details",
        "log_search_next",
        "log_search_prev",
        "relationships",
        "sort_by_age",
        "sort_by_cpu",
        "sort_by_mem",
        "port_forward",
        "transfer",
        "shell",
        "operator_install",
    }
    assert reasons["scale_resource"]() == refused
    assert write_calls == ["scale_resource"]
    assert reasons["helm_rollback"]() is None
    assert helm_calls == ["helm_rollback"]
    assert reasons["logs_multi"]() is None
    assert log_calls == ["logs_multi"]
    assert reasons["log_search_prev"]() is None
    assert inspect_calls == ["log_search_prev"]
    assert reasons["relationships"]() is None
    assert reasons["sort_by_cpu"]() is None
    assert reasons["sort_by_age"]() is None
    assert workspace_calls == ["relationships", "sort_by_cpu", "sort_by_age"]


def test_composed_action_reasons_only_cover_owned_actions() -> None:
    """Actions nobody registered a reason for stay invocable once bound -
    composing the map must not invent an owner for them."""
    reasons = compose_action_reasons(
        writes=lambda _action: None,
        helm=lambda _action: None,
        logs=lambda _action: None,
        inspect=lambda _action: None,
        workspace=lambda _action: None,
        port_forward=lambda: None,
        transfer=lambda: None,
        shell=lambda: None,
        operator_install=lambda: None,
    )
    assert "open_action_palette" not in reasons
    assert "toggle_agent" not in reasons


def test_composed_command_reasons_route_the_picker_commands() -> None:
    """`:ns` and `:ctx` are typed commands whose handlers refuse without
    their collaborator - the namespace listing, and the kubeconfig
    listing/probe/switch trio - so each gets its own owner resolver rather
    than sharing one, and each is read live."""
    namespace_reason = UnavailableReason(
        AvailabilityCode.MISSING_CAPABILITY, "Namespace listing unavailable"
    )
    context_reason = UnavailableReason(
        AvailabilityCode.MISSING_CAPABILITY, "Context switching unavailable in this build"
    )
    listing = False
    reasons = compose_command_reasons(
        agent_available=lambda: True,
        mcp=lambda: None,
        telepresence=lambda: None,
        proposals=lambda: None,
        namespace=lambda: None if listing else namespace_reason,
        context=lambda: context_reason,
    )
    assert set(reasons) == {"ai", "model", "mcp", "tp", "proposals", "ns", "ctx"}
    assert reasons["ns"]() == namespace_reason
    assert reasons["ctx"]() == context_reason
    listing = True
    assert reasons["ns"]() is None
    assert reasons["ctx"]() == context_reason


def test_composed_command_reasons_share_one_agent_answer() -> None:
    """`:ai` and `:model` have no owner at all without the [agent] extra,
    which is the same absence the bound Ctrl-A key refuses with - so both
    answer with the one shared wording, and nothing else does."""
    reasons = compose_command_reasons(
        agent_available=lambda: False,
        mcp=lambda: None,
        telepresence=lambda: None,
        proposals=lambda: None,
        namespace=lambda: None,
        context=lambda: None,
    )
    assert set(reasons) == {"ai", "model", "mcp", "tp", "proposals", "ns", "ctx"}
    assert reasons["ai"]() == AGENT_UNAVAILABLE
    assert reasons["model"]() == AGENT_UNAVAILABLE


def test_composed_command_reasons_follow_a_late_agent() -> None:
    """The agent can be composed (or disconnected) after wiring, so the
    answer is read live rather than frozen when the map was built."""
    available = False
    reasons = compose_command_reasons(
        agent_available=lambda: available,
        mcp=lambda: None,
        telepresence=lambda: None,
        proposals=lambda: None,
        namespace=lambda: None,
        context=lambda: None,
    )
    assert reasons["ai"]() == AGENT_UNAVAILABLE
    available = True
    assert reasons["ai"]() is None
    assert reasons["model"]() is None


def test_composed_command_reasons_route_the_integration_commands() -> None:
    """`:mcp`, `:tp` and `:proposals` keep their own owner's answer,
    unrelated to the agent - and each is read live, so an inbox that fills
    or empties after the wiring ran changes the answer."""
    mcp_reason = UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, "MCP is not installed")
    tp_reason = UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, "telepresence not found")
    proposals_reason = UnavailableReason(
        AvailabilityCode.NO_SELECTION, "No pending write proposals", severity="information"
    )
    pending = False
    reasons = compose_command_reasons(
        agent_available=lambda: True,
        mcp=lambda: mcp_reason,
        telepresence=lambda: tp_reason,
        proposals=lambda: None if pending else proposals_reason,
        namespace=lambda: None,
        context=lambda: None,
    )
    assert reasons["mcp"]() == mcp_reason
    assert reasons["tp"]() == tp_reason
    assert reasons["proposals"]() == proposals_reason
    pending = True
    assert reasons["proposals"]() is None


# ---------------------------------------------------------------------------
# The two maps, audited as a whole (#388 round 8)
# ---------------------------------------------------------------------------


def _bound_actions() -> set[str]:
    """Every action id `APP_BINDINGS` can dispatch, `--alt` ids collapsed."""
    return {
        base_action(as_binding(raw).action)
        for raw in APP_BINDINGS
        if "(" not in as_binding(raw).action
    }


def _palette_commands() -> set[str]:
    """Every canonical `:` text the palette can invoke."""
    return {
        descriptor.palette.canonical_text
        for descriptor in COMMANDS
        if descriptor.palette is not None
    }


def test_no_owner_silently_overwrites_another_in_the_action_map() -> None:
    """Every owner contributes its own actions and nobody's entry is lost.

    The map is built by merging one dict per owner, so two owners claiming
    the same action would leave the later one answering for both - with
    the earlier owner's probe never called again and no error anywhere.
    Counting the merged map against the declared action lists is what
    makes that collision visible.
    """
    owners = {
        "writes": _WRITE_REASON_ACTIONS,
        "helm": _HELM_REASON_ACTIONS,
        "logs": _LOG_REASON_ACTIONS,
        "inspect": _INSPECT_REASON_ACTIONS,
        "workspace": _WORKSPACE_REASON_ACTIONS,
    }
    declared = [action for actions in owners.values() for action in actions]
    declared += ["port_forward", "transfer", "shell", "operator_install"]
    assert len(declared) == len(set(declared))
    reasons = compose_action_reasons(
        writes=lambda _action: None,
        helm=lambda _action: None,
        logs=lambda _action: None,
        inspect=lambda _action: None,
        workspace=lambda _action: None,
        port_forward=lambda: None,
        transfer=lambda: None,
        shell=lambda: None,
        operator_install=lambda: None,
    )
    assert set(reasons) == set(declared)
    assert len(reasons) == len(declared)


def test_every_owned_action_is_really_a_bound_action() -> None:
    """An owner reason for an action nothing binds would never be asked:
    the palette derives its rows from `APP_BINDINGS`, so a typo here is a
    refusal that silently never happens."""
    reasons = compose_action_reasons(
        writes=lambda _action: None,
        helm=lambda _action: None,
        logs=lambda _action: None,
        inspect=lambda _action: None,
        workspace=lambda _action: None,
        port_forward=lambda: None,
        transfer=lambda: None,
        shell=lambda: None,
        operator_install=lambda: None,
    )
    assert set(reasons) <= _bound_actions()


def test_every_owned_command_is_really_a_palette_command() -> None:
    """Same for the command map, against its own vocabulary: a command
    reason keyed on an alias (`:namespaces`) or on an action name would
    never reach the row it was written for."""
    reasons = compose_command_reasons(
        agent_available=lambda: True,
        mcp=lambda: None,
        telepresence=lambda: None,
        proposals=lambda: None,
        namespace=lambda: None,
        context=lambda: None,
    )
    assert set(reasons) <= _palette_commands()


def test_every_refusable_sort_key_has_an_owner() -> None:
    """`read_availability` decides which sort columns can be refused;
    `action_policy` decides who is asked about them. A column added to one
    and not the other is a refusal the palette never shows - so the sort
    vocabulary is audited against the owner list, not just spelled twice.
    """
    assert set(SORT_ACTION_COLUMNS) <= set(_WORKSPACE_REASON_ACTIONS)


def test_every_search_key_has_an_owner() -> None:
    """The same audit for `n`/`N`: `SEARCH_ACTIONS` is the pane-search
    vocabulary, and the inspect owner is who answers for it."""
    assert set(SEARCH_ACTIONS) <= set(_INSPECT_REASON_ACTIONS)
