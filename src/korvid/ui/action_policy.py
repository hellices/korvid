"""The action binding policy: what `check_action` gates on (issue #114, #388).

Extracted from `KorvidApp.check_action` unchanged (issue #388 task 1): the
same view-identity routing for overloaded keys, log-pane gating and the helm
delete exception, now a plain class the palette (and tests) can consume
without composing the Textual app.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.k8s.portforward import FORWARDABLE_KINDS
from korvid.ui.action_availability import ActionAvailability, AvailabilityCode, UnavailableReason
from korvid.ui.resource_write_controller import RESTARTABLE, SCALABLE
from korvid.ui.view_state import ViewState

#: Resource identities — (group, plural) — where each view-specific action
#: applies; actions absent from the map work on every view. `ActionPolicy`
#: consults this so the footer legend shows only the current view's keys
#: and overloaded keys (i/u/r) dispatch to the binding whose view is on
#: screen (issue #114). Identity, not the kind string: a foreign CRD
#: claiming a bare plural (e.g. `packagemanifests`) must not surface
#: another view's actions. `log_search_next`/`log_search_prev` stay
#: unlisted on purpose: they also serve the describe pane's search (any
#: view) and the sort-by-name fallback. Fail-closed by design: a kind
#: missing from `aliases` (e.g. mid-discovery) hides every listed action
#: until its identity is known.
_ACTION_VIEWS: dict[str, frozenset[tuple[str, str]]] = {
    "shell": frozenset({("", "pods"), ("", "nodes")}),
    "logs": frozenset({("", "pods")}),
    "logs_multi": frozenset({("", "pods")}),
    "hint_details": frozenset({("", "pods")}),
    "resize_pod": frozenset({("", "pods")}),
    "transfer": frozenset({("", "pods")}),
    # Core-group identities of FORWARDABLE_KINDS (pods, services).
    "port_forward": frozenset(("", plural) for plural in FORWARDABLE_KINDS),
    "cordon_node": frozenset({("", "nodes")}),
    "uncordon_node": frozenset({("", "nodes")}),
    "drain_node": frozenset({("", "nodes")}),
    "rollout_restart": RESTARTABLE,
    "scale_resource": SCALABLE,
    "operator_install": frozenset(
        {(PACKAGES_GROUP, "packagemanifests"), (OPERATORS_GROUP, "installplans")}
    ),
    # The synthetic helm views (group "", client-side plurals).
    "helm_install": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
    "helm_upgrade": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
    "helm_history": frozenset({(HELM_RELEASES_META.group, HELM_RELEASES_META.plural)}),
    "helm_rollback": frozenset({(HELM_REVISIONS_META.group, HELM_REVISIONS_META.plural)}),
}

#: Actions that operate on the visible log pane, not the focused view: the
#: split workflow tails logs from one pane while the other shows a
#: different kind, so these gate on pane visibility (review of #114).
_LOG_PANE_ACTIONS: frozenset[str] = frozenset(
    {"log_format", "log_wrap", "log_timestamps", "log_save", "log_previous"}
)

#: Generic write actions that `WriteCoordinator.write_target` rejects on
#: synthetic (client-side, read-only) views such as the helm browser:
#: advertising them there would be a lie (review of #114). The dedicated
#: helm write actions stay available through `_ACTION_VIEWS`.
_SYNTHETIC_GATED_ACTIONS: frozenset[str] = frozenset({"delete_resource", "edit_resource"})


class ActionPolicy:
    """Gate bindings on composition availability and the current view.

    A binding evaluating to False both hides it from the footer and skips
    it during key dispatch, so overloaded keys fall through to the binding
    whose view is on screen (issue #114).
    """

    def __init__(
        self,
        *,
        view: ViewState,
        agent_available: Callable[[], bool],
        log_pane_open: Callable[[], bool],
        #: Whether an Agent turn is running. None (no agent composed) reads
        #: as "invokable": a policy built without one must not grey out a
        #: bound key it knows nothing about.
        agent_busy: Callable[[], bool] | None = None,
        reason_by_action: Mapping[str, Callable[[], UnavailableReason | None]] | None = None,
    ) -> None:
        self._view = view
        self._agent_available = agent_available
        self._log_pane_open = log_pane_open
        self._agent_busy = agent_busy
        #: Owner-supplied reason resolvers for actions whose binding stays
        #: enabled but whose *invocation* may still be refused (the generic
        #: writes: `ResourceWriteController.unavailable_reason`; the
        #: capability owners: helm, forwards, transfer, shell, operators,
        #: logs). Actions absent from the map are always invokable once
        #: bound. `interrupt_agent`'s resolver is folded in here too (unless
        #: a caller already supplied one) rather than left as a second,
        #: hard-coded lookup path in `_invocation_reason` - one source of
        #: truth, so a future caller that *does* register `interrupt_agent`
        #: explicitly overrides the default instead of silently competing
        #: with it (#388 task 4 review).
        reasons: dict[str, Callable[[], UnavailableReason | None]] = dict(
            reason_by_action if reason_by_action is not None else {}
        )
        if agent_busy is not None:
            reasons.setdefault("interrupt_agent", self._interrupt_agent_reason)
        self._reason_by_action: Mapping[str, Callable[[], UnavailableReason | None]] = reasons

    def binding_enabled(self, action: str) -> bool:
        """Whether `action`'s binding is enabled in the current composition and view."""
        if action == "toggle_agent" and not self._agent_available():
            return False
        if action in _LOG_PANE_ACTIONS:
            return self._log_pane_open()
        if action in _SYNTHETIC_GATED_ACTIONS:
            meta = self._current_meta()
            if (
                action == "delete_resource"
                and meta is not None
                and (meta.group, meta.plural)
                == (HELM_RELEASES_META.group, HELM_RELEASES_META.plural)
            ):
                # Ctrl+D on the release browser is `helm uninstall`
                # (issue #117) - the one synthetic view where delete works.
                return True
            # Unknown kinds keep the keys: the handler's own guards decide.
            return meta is None or not meta.synthetic
        views = _ACTION_VIEWS.get(action)
        if views is None:
            return True
        meta = self._current_meta()
        return meta is not None and (meta.group, meta.plural) in views

    def availability(self, action: str) -> ActionAvailability:
        """Whether the palette should let `action` run right now, and why
        not if it shouldn't (issue #388). `binding_enabled` alone stays the
        keybinding contract (a disabled binding is skipped during dispatch,
        so an overloaded key falls through to the view actually on screen);
        this composes it with the *invocation* reasons an enabled binding's
        owner (`ResourceWriteController` for the generic writes, and the
        capability owners - helm, forwards, transfer, shell, operators,
        logs - for their own synchronous guards) can still refuse, without
        changing either owner's own notification path."""
        if not self.binding_enabled(action):
            return ActionAvailability(binding_enabled=False, reason=self._wrong_view_reason(action))
        return ActionAvailability(binding_enabled=True, reason=self._invocation_reason(action))

    def _invocation_reason(self, action: str) -> UnavailableReason | None:
        """The owner's reason a *bound* action still can't run, or None -
        `_reason_by_action` is the one place that answers this."""
        resolver = self._reason_by_action.get(action)
        return None if resolver is None else resolver()

    def _interrupt_agent_reason(self) -> UnavailableReason | None:
        """The default `interrupt_agent` resolver: Ctrl-X is a priority
        binding that must stay dispatchable (its visibility is deliberately
        unchanged), but with no turn in flight there is nothing for the
        palette to interrupt. Only registered when `agent_busy` was
        injected (see `__init__`); the `None` check below is defensive
        narrowing for mypy, not a reachable branch."""
        if self._agent_busy is None or self._agent_busy():
            return None
        return UnavailableReason(AvailabilityCode.PROTECTED_UI, "No Agent turn is running")

    def _wrong_view_reason(self, action: str) -> UnavailableReason:
        """Explain a disabled binding, for a palette entry that stays
        searchable but greyed out with its cause attached."""
        if action == "toggle_agent" and not self._agent_available():
            return UnavailableReason(AvailabilityCode.MISSING_CAPABILITY, "Agent is not available")
        if action in _LOG_PANE_ACTIONS and not self._log_pane_open():
            return UnavailableReason(AvailabilityCode.PANE_CLOSED, "Open the log pane first")
        if action in _SYNTHETIC_GATED_ACTIONS:
            meta = self._current_meta()
            if meta is not None and meta.synthetic:
                return UnavailableReason(
                    AvailabilityCode.UNSUPPORTED_RESOURCE, f"{meta.kind} is a read-only view"
                )
        return UnavailableReason(AvailabilityCode.WRONG_VIEW, "Not available in this view")

    def _current_meta(self) -> ResourceMeta | None:
        return self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
