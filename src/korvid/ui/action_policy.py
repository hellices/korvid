"""The action binding policy: what `check_action` gates on (issue #114, #388).

Extracted from `KorvidApp.check_action` unchanged (issue #388 task 1): the
same view-identity routing for overloaded keys, log-pane gating and the helm
delete exception, now a plain class the palette (and tests) can consume
without composing the Textual app.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Mapping

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.k8s.portforward import FORWARDABLE_KINDS
from korvid.ui.action_availability import (
    AGENT_UNAVAILABLE,
    CONTEXT_SWITCH_IN_PROGRESS,
    ActionAvailability,
    AvailabilityCode,
    UnavailableReason,
)
from korvid.ui.view_state import ViewState
from korvid.ui.write_availability import RESTARTABLE, SCALABLE

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

#: The Action Palette's own open action (issue #388). It is the one action
#: gated on the *surface* rather than the view: its binding is `priority`,
#: so it fires over any screen and from inside any focused widget, and the
#: policy is therefore the only thing standing between `Ctrl-P` and an
#: approval dialog.
PALETTE_ACTION = "open_action_palette"

#: The actions each owner answers an *invocation* reason for. Declared
#: here, beside `_ACTION_VIEWS`, because "which actions does this owner
#: speak for" is the same action vocabulary this module already governs -
#: the composition root names the owners, not their action lists (#388).
_WRITE_REASON_ACTIONS: tuple[str, ...] = (
    "delete_resource",
    "edit_resource",
    "rollout_restart",
    "scale_resource",
    "resize_pod",
    "cordon_node",
    "uncordon_node",
    "drain_node",
)
_HELM_REASON_ACTIONS: tuple[str, ...] = (
    "helm_install",
    "helm_upgrade",
    "helm_history",
    "helm_rollback",
)
#: The actions `LogController` answers an invocation reason for: the two
#: keys that *open* a stream, plus `Ctrl-S`, whose own question is the
#: buffer behind the visible pane rather than the pane itself (#388 round
#: 6). The other pane-local display toggles need nothing beyond the pane
#: visibility `binding_enabled` already gates them on.
_LOG_REASON_ACTIONS: tuple[str, ...] = ("logs", "logs_multi", "log_save")

#: The read keys `ResourceInspectController` speaks for: its own describe
#: and hint-details flows, plus the `n`/`N` search step, whose first
#: question is the describe pane it owns (issue #388 final review).
_INSPECT_REASON_ACTIONS: tuple[str, ...] = (
    "describe",
    "hint_details",
    "log_search_next",
    "log_search_prev",
)

#: The keys `WorkspaceController` answers an invocation reason for: `g`,
#: plus the two metric sort keys, whose handler (`sort_by`) discards the
#: press without even a warning on a view that has no CPU/MEM column.
_WORKSPACE_REASON_ACTIONS: tuple[str, ...] = ("relationships", "sort_by_cpu", "sort_by_mem")


def _per_action(
    probe: Callable[[str], UnavailableReason | None], actions: Iterable[str]
) -> dict[str, Callable[[], UnavailableReason | None]]:
    """Bind one owner's per-action probe to each action it answers for."""
    return {action: functools.partial(probe, action) for action in actions}


def compose_action_reasons(
    *,
    writes: Callable[[str], UnavailableReason | None],
    helm: Callable[[str], UnavailableReason | None],
    logs: Callable[[str], UnavailableReason | None],
    inspect: Callable[[str], UnavailableReason | None],
    workspace: Callable[[str], UnavailableReason | None],
    port_forward: Callable[[], UnavailableReason | None],
    transfer: Callable[[], UnavailableReason | None],
    shell: Callable[[], UnavailableReason | None],
    operator_install: Callable[[], UnavailableReason | None],
) -> dict[str, Callable[[], UnavailableReason | None]]:
    """Build `ActionPolicy(reason_by_action=...)` from its owners.

    Every value is a live call into the owner that already refuses the
    keypress, so a probe and its keypress can never answer differently.
    Actions absent from the result have no owner reason and stay invocable
    once their binding is enabled.

    Args:
        writes: `ResourceWriteController.unavailable_reason`.
        helm: `HelmController.unavailable_reason`.
        logs: `LogController.unavailable_reason`.
        inspect: `ResourceInspectController.unavailable_reason` — the read
            keys `d`, `h` and the `n`/`N` search step.
        workspace: `WorkspaceController.unavailable_reason` — `g` and
            the two metric sort keys.
        port_forward: `ForwardController.unavailable_reason`.
        transfer: `TransferController.unavailable_reason`.
        shell: `ShellController.unavailable_reason`.
        operator_install: `OperatorController.unavailable_reason`.

    Returns:
        The action -> reason-resolver map, one entry per owned action.
    """
    return {
        **_per_action(writes, _WRITE_REASON_ACTIONS),
        **_per_action(helm, _HELM_REASON_ACTIONS),
        **_per_action(logs, _LOG_REASON_ACTIONS),
        **_per_action(inspect, _INSPECT_REASON_ACTIONS),
        **_per_action(workspace, _WORKSPACE_REASON_ACTIONS),
        "port_forward": port_forward,
        "transfer": transfer,
        "shell": shell,
        "operator_install": operator_install,
    }


def compose_command_reasons(
    *,
    agent_available: Callable[[], bool],
    mcp: Callable[[], UnavailableReason | None],
    telepresence: Callable[[], UnavailableReason | None],
    proposals: Callable[[], UnavailableReason | None],
    namespace: Callable[[], UnavailableReason | None],
    context: Callable[[], UnavailableReason | None],
) -> dict[str, Callable[[], UnavailableReason | None]]:
    """Build `ActionPolicy(reason_by_command=...)` from its owners.

    `:ai` and `:model` have no owner at all without the [agent] extra -
    the same absence the bound Ctrl-A key refuses with - so they share one
    composed answer here rather than becoming a method on a controller
    that may not exist (#388 task 6 review). It is read live: the agent can
    be built, rebuilt or disconnected long after the wiring ran.

    Args:
        agent_available: Whether an Agent is composed and usable now.
        mcp: `IntegrationController.mcp_unavailable_reason`.
        telepresence: `IntegrationController.telepresence_unavailable_reason`.
        proposals: `ProposalController.unavailable_reason` — the inbox
            `:proposals` reviews: the feature, then what is pending, then
            whether a review is already open.
        namespace: `WorkspaceController.namespace_picker_unavailable_reason`
            — whether `:ns` has a namespace listing to open its picker over.
        context: `ContextSwitchCoordinator.unavailable_reason` — whether
            this build has the kubeconfig collaborators `:ctx` needs.
            Separate owners, not one shared answer: the two pickers list
            different things through different seams, and a session can
            have either without the other.

    Returns:
        The canonical command text -> reason-resolver map.
    """

    def agent_reason() -> UnavailableReason | None:
        return None if agent_available() else AGENT_UNAVAILABLE

    return {
        "ai": agent_reason,
        "model": agent_reason,
        "mcp": mcp,
        "tp": telepresence,
        "proposals": proposals,
        "ns": namespace,
        "ctx": context,
    }


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
        #: as "invocable": a policy built without one must not grey out a
        #: bound key it knows nothing about.
        agent_busy: Callable[[], bool] | None = None,
        #: The app surfaces `open_action_palette` is gated on (issue #388
        #: task 6): how many screens are stacked, whether the `:` command
        #: bar or `/` filter bar is editing, whether a `:ctx` switch is in
        #: flight, and whether the app is still accepting input. Each is
        #: optional and defaults to "not blocking" for the same reason
        #: `agent_busy` does: a policy composed without the Textual shell
        #: (unit tests, headless callers) must not grey out a bound key it
        #: knows nothing about.
        screen_depth: Callable[[], int] | None = None,
        inline_editor_open: Callable[[], bool] | None = None,
        switching: Callable[[], bool] | None = None,
        app_running: Callable[[], bool] | None = None,
        reason_by_action: Mapping[str, Callable[[], UnavailableReason | None]] | None = None,
        #: Owner-supplied reasons for `:` *commands*, keyed by canonical
        #: command text. A separate map on purpose (#388 task 6 review):
        #: command texts and action names are different vocabularies that
        #: happen to be strings, so `:help`-the-command could otherwise
        #: inherit `help`-the-action's answer. `command_availability` is the
        #: only reader; nothing here is view- or binding-gated, because a
        #: command is typed, not bound.
        reason_by_command: Mapping[str, Callable[[], UnavailableReason | None]] | None = None,
    ) -> None:
        self._view = view
        self._agent_available = agent_available
        self._log_pane_open = log_pane_open
        self._agent_busy = agent_busy
        self._screen_depth = screen_depth
        self._inline_editor_open = inline_editor_open
        self._switching = switching
        self._app_running = app_running
        #: Owner-supplied reason resolvers for actions whose binding stays
        #: enabled but whose *invocation* may still be refused (the generic
        #: writes: `ResourceWriteController.unavailable_reason`; the
        #: capability owners: helm, forwards, transfer, shell, operators,
        #: logs). Actions absent from the map are always invocable once
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
        self._reason_by_command: Mapping[str, Callable[[], UnavailableReason | None]] = dict(
            reason_by_command if reason_by_command is not None else {}
        )

    def binding_enabled(self, action: str) -> bool:
        """Whether `action`'s binding is enabled in the current composition and view."""
        if action == PALETTE_ACTION:
            return self._palette_reason() is None
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

    def command_availability(self, command: str) -> ActionAvailability:
        """Whether the `:` command `command` can run right now (issue #388).

        Its own namespace, deliberately separate from `availability`: a
        command is typed rather than bound, so none of the binding, view or
        palette-surface gates apply to it - only the owner's synchronous,
        silent capability answer (`AgentUiController` for `:ai`/`:model`,
        `IntegrationController` for `:mcp`/`:tp`). A command with no
        registered owner reason is simply runnable.
        """
        resolver = self._reason_by_command.get(command)
        return ActionAvailability(
            binding_enabled=True, reason=None if resolver is None else resolver()
        )

    def _invocation_reason(self, action: str) -> UnavailableReason | None:
        """The owner's reason a *bound* action still can't run, or None -
        `_reason_by_action` is the one place that answers this."""
        resolver = self._reason_by_action.get(action)
        return None if resolver is None else resolver()

    def _palette_reason(self) -> UnavailableReason | None:
        """Why the Action Palette must not open right now, or None.

        One place answers this for both `binding_enabled` (so the priority
        `Ctrl-P` binding is skipped during dispatch and stays out of the
        legend) and `availability` (so the direct app action refuses with
        the same verdict). The order is the user's: an open dialog is the
        nearest surface, then a half-typed command or filter, then the
        cluster-wide `:ctx` transition, then a shutting-down app.
        """
        if self._screen_depth is not None and self._screen_depth() > 1:
            return UnavailableReason(AvailabilityCode.PROTECTED_UI, "Close the open dialog first")
        if self._inline_editor_open is not None and self._inline_editor_open():
            return UnavailableReason(
                AvailabilityCode.PROTECTED_UI, "Finish the command or filter entry first"
            )
        if self._switching is not None and self._switching():
            return CONTEXT_SWITCH_IN_PROGRESS
        if self._app_running is not None and not self._app_running():
            return UnavailableReason(AvailabilityCode.PROTECTED_UI, "korvid is shutting down")
        return None

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
        if action == PALETTE_ACTION:
            reason = self._palette_reason()
            if reason is not None:
                return reason
        if action == "toggle_agent" and not self._agent_available():
            return AGENT_UNAVAILABLE
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
