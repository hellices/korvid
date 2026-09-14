"""The action binding policy: what `check_action` gates on (issue #114, #388).

Extracted from `KorvidApp.check_action` unchanged (issue #388 task 1): the
same view-identity routing for overloaded keys, log-pane gating and the helm
delete exception, now a plain class the palette (and tests) can consume
without composing the Textual app.
"""

from __future__ import annotations

from collections.abc import Callable

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP
from korvid.k8s.portforward import FORWARDABLE_KINDS
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
    ) -> None:
        self._view = view
        self._agent_available = agent_available
        self._log_pane_open = log_pane_open

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

    def _current_meta(self) -> ResourceMeta | None:
        return self._view.aliases().get(self._view.canonical_kind(self._view.current_kind()))
