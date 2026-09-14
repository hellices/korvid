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
    CONTEXT_SWITCH_IN_PROGRESS,
    ActionAvailability,
    AvailabilityCode,
    UnavailableReason,
)
from korvid.ui.action_policy import ActionPolicy
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
    """An action with no owner resolver (most actions) is simply invokable."""
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
    assert availability.invokable is False


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
    assert availability.invokable is False


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


def test_interrupt_agent_stays_bound_but_is_not_invokable_while_idle() -> None:
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
    assert availability.invokable is False


def test_interrupt_agent_is_invokable_during_a_turn() -> None:
    policy = _policy(group="", plural="pods", agent_busy=lambda: True)
    assert policy.availability("interrupt_agent") == ActionAvailability.enabled()


def test_interrupt_agent_is_invokable_without_an_injected_busy_probe() -> None:
    """The default composition answer is "invokable": a policy built
    without an agent (tests, headless) must not grey out a bound key."""
    policy = _policy(group="", plural="pods")
    assert policy.availability("interrupt_agent").invokable is True


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
        reason_by_action={"interrupt_agent": lambda: None},  # explicit: always invokable
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
    assert availability.invokable is False


def test_palette_binding_defaults_to_enabled_without_injected_probes() -> None:
    """A policy composed without the app's surface probes (tests, headless)
    must not grey out a bound key it knows nothing about - the same default
    stance `agent_busy` takes."""
    policy = _policy(group="", plural="pods")
    assert policy.binding_enabled("open_action_palette") is True
