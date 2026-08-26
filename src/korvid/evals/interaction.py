"""Typed starting interactions for eval fixtures (issue #316 task 13).

A scenario or journey used to describe the operator's screen in one prose
string (`screen: "resource view: pods, namespace jobs, selected: ..."`).
The agent no longer reads prose about the workspace: `AgentSession` reads
a typed `InteractionContext` from an `AgentUiBridge` at the start of every
turn, and `PromptHarness` encodes it. So a fixture that still shipped
prose would be measuring a screen the production prompt never renders.

This module is the eval half of that seam:

- `load_interaction` parses one YAML `interaction:` block into the exact
  Task-1 `InteractionContext` the TUI's own bridge would return. It is
  strict — a block that names no focused pane, or a selection with no
  name, is refused rather than silently defaulted, because a fixture that
  loads and describes nothing publishes a score for a run nobody can
  reconstruct.
- `EvalUiBridge` is a *mutable* workspace: `snapshot` answers the current
  context and `apply` moves it exactly as the corresponding screen action
  would, recording every action. Nothing here imports the TUI: an eval
  has no app, and the point of the bridge seam is that it does not need
  one.

Nothing in here derives an interaction from a question. The starting
workspace is authored fixture data, like the cluster manifests next to it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, Final

from korvid.agent.interaction import (
    AgentUiBridge,
    DrillDown,
    InteractionContext,
    Navigate,
    OpenDescribe,
    OpenLogs,
    PaneContext,
    ResourceIdentity,
    SetFilter,
    UiAction,
    UiActionResult,
)

#: How a pane that is not confined to one namespace spells its scope. The
#: TUI's own sentinel lives in `korvid.core.store`, which the eval layer
#: may not import; the string is the wire value, not a second concept.
ALL_NAMESPACES_SCOPE: Final[str] = "*"

#: Where an eval reports the screen actions a model made:
#: `(tool name, arguments, result text)`.
ActionSink = Callable[[str, dict[str, Any], str], None]

_INTERACTION_KEYS = frozenset(
    {"kube_context", "context_epoch", "focused_pane", "secondary_pane", "timeline_cursor"}
)
_PANE_KEYS = frozenset({"kind", "scope", "filter", "selected"})
_SELECTED_KEYS = frozenset({"kind", "namespace", "name", "uid"})


def _reject_unknown_keys(mapping: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(map(str, mapping)) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {unknown}")


def _required_str(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} field {key!r} must be a non-blank string")
    return value


def _optional_str(mapping: dict[str, Any], key: str, label: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} field {key!r} must be a non-blank string when present")
    return value


def _selected(raw: Any, label: str) -> ResourceIdentity | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{label} field 'selected' must be a mapping")
    _reject_unknown_keys(raw, _SELECTED_KEYS, f"{label} 'selected'")
    return ResourceIdentity(
        kind=_required_str(raw, "kind", f"{label} 'selected'"),
        namespace=_optional_str(raw, "namespace", f"{label} 'selected'"),
        name=_required_str(raw, "name", f"{label} 'selected'"),
        uid=_optional_str(raw, "uid", f"{label} 'selected'"),
    )


def _pane(raw: Any, label: str) -> PaneContext:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be a mapping of kind/scope")
    _reject_unknown_keys(raw, _PANE_KEYS, label)
    return PaneContext(
        kind=_required_str(raw, "kind", label),
        scope=_required_str(raw, "scope", label),
        filter_pattern=_optional_str(raw, "filter", label),
        selected=_selected(raw.get("selected"), label),
    )


def _context_epoch(raw: Any, label: str) -> int:
    # A YAML `true` is an int in Python; an epoch is a counter, and a
    # boolean one would compare equal to 1 and silently suppress the
    # handoff note the next turn owes.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"{label} field 'context_epoch' must be a non-negative integer")
    return raw


def load_interaction(raw: Any, label: str) -> InteractionContext:
    """Parse one `interaction:` block into a typed `InteractionContext`.

    Args:
        raw: The YAML mapping under the fixture's `interaction` key.
        label: Where the block came from, for error messages.

    Returns:
        The workspace snapshot a turn starts from.

    Raises:
        ValueError: The block is not a mapping, names an unknown key,
            omits the focused pane, or describes a selection the TUI
            could not identify (no kind or no name).
    """
    if not isinstance(raw, dict):
        raise ValueError(f"{label}: interaction must be a mapping")
    _reject_unknown_keys(raw, _INTERACTION_KEYS, label)
    if "focused_pane" not in raw:
        raise ValueError(f"{label} needs a 'focused_pane' mapping")
    secondary = raw.get("secondary_pane")
    return InteractionContext(
        kube_context=_optional_str(raw, "kube_context", label),
        context_epoch=_context_epoch(raw.get("context_epoch", 0), label),
        focused_pane=_pane(raw["focused_pane"], f"{label} 'focused_pane'"),
        secondary_pane=(
            None if secondary is None else _pane(secondary, f"{label} 'secondary_pane'")
        ),
        timeline_cursor=_optional_str(raw, "timeline_cursor", label),
    )


def _resource_payload(resource: ResourceIdentity | None) -> dict[str, str | None] | None:
    if resource is None:
        return None
    return {
        "kind": resource.kind,
        "namespace": resource.namespace,
        "name": resource.name,
        "uid": resource.uid,
    }


def _pane_payload(pane: PaneContext | None) -> dict[str, Any] | None:
    if pane is None:
        return None
    return {
        "kind": pane.kind,
        "scope": pane.scope,
        "filter": pane.filter_pattern,
        "selected": _resource_payload(pane.selected),
    }


def interaction_payload(interaction: InteractionContext) -> dict[str, Any]:
    """The JSON-ready record of the workspace a run started from.

    Reported in every eval artifact: a score for a diagnostic question is
    not reproducible without the screen the question was asked from.
    """
    return {
        "kube_context": interaction.kube_context,
        "context_epoch": interaction.context_epoch,
        "focused_pane": _pane_payload(interaction.focused_pane),
        "secondary_pane": _pane_payload(interaction.secondary_pane),
        "timeline_cursor": interaction.timeline_cursor,
    }


class EvalUiBridge(AgentUiBridge):
    """A workspace made of nothing but state, driven by typed actions.

    The production bridge translates a `UiAction` into calls on a live
    Textual app and then reports the resulting screen. An eval has no
    screen, but it does have the part that matters to the model: what the
    *next* turn's snapshot will say. So this bridge applies each action to
    its own context and answers with it, which is what lets a journey's
    later turns see where the model actually navigated.

    Args:
        context: The authored starting interaction.
    """

    def __init__(self, context: InteractionContext) -> None:
        self._context = context
        self._actions: list[UiAction] = []
        self._sink: ActionSink | None = None

    def record_into(self, sink: ActionSink) -> None:
        """Report each applied action as `(tool name, arguments, result)`.

        A screen action is not agent *evidence* — the production
        `ToolHarness` deliberately mints none for it — but it is exactly
        what the TUI-following journeys grade. Reporting into the eval's
        own record stream, in the order the model acted, is what lets a
        journey assert "and it put that on screen" without inventing a
        second notion of evidence inside the agent.
        """
        self._sink = sink

    @property
    def actions(self) -> tuple[UiAction, ...]:
        """Every action applied since construction or the last `reset`."""
        return tuple(self._actions)

    def reset(self, context: InteractionContext) -> None:
        """Put the workspace back to an authored interaction.

        Journeys use this between turns when the fixture states that the
        *operator* moved the screen; without such a statement the bridge
        keeps whatever the model navigated to, as a live session would.
        """
        self._context = context
        self._actions.clear()

    def snapshot(self) -> InteractionContext:
        """The current human-visible workspace state."""
        return self._context

    async def apply(self, action: UiAction) -> UiActionResult:
        """Apply one typed action and report the workspace it produced."""
        self._actions.append(action)
        ok, message, context = self._transition(action)
        self._context = context
        if self._sink is not None:
            name, arguments = _action_call(action)
            self._sink(name, arguments, message)
        return UiActionResult(ok=ok, message=message, context=context)

    def _transition(self, action: UiAction) -> tuple[bool, str, InteractionContext]:
        if isinstance(action, Navigate):
            return self._navigate(action)
        if isinstance(action, SetFilter):
            pane = replace(self._focused, filter_pattern=action.filter_pattern)
            return True, f"filter set to {action.filter_pattern or '(cleared)'}", self._focus(pane)
        if isinstance(action, OpenLogs):
            return self._display(
                ResourceIdentity(
                    kind="pods", namespace=action.namespace, name=action.pod, uid=None
                ),
                f"opened logs for {action.pod}",
                kind="pods",
                scope=action.namespace,
            )
        if isinstance(action, OpenDescribe):
            return self._display(
                ResourceIdentity(
                    kind=action.kind, namespace=action.namespace, name=action.name, uid=None
                ),
                f"opened describe for {action.name}",
                kind=action.kind,
                scope=action.namespace or ALL_NAMESPACES_SCOPE,
            )
        if isinstance(action, DrillDown):
            return self._select(
                ResourceIdentity(
                    kind=self._focused.kind,
                    # Drilling stays inside the pane, so the pane's scope
                    # is the namespace unless the pane spans all of them.
                    namespace=None
                    if self._focused.scope == ALL_NAMESPACES_SCOPE
                    else (self._focused.scope),
                    name=action.name,
                    uid=None,
                ),
                f"drilled into {action.name}",
            )
        # `UiAction` is a closed union of five members and every one is
        # handled above; the fall-through keeps the function total for
        # mypy without inventing a transition for an action korvid cannot
        # produce.
        raise ValueError(f"unhandled UI action {type(action).__name__}")

    @property
    def _focused(self) -> PaneContext:
        return self._context.focused_pane

    def _focus(self, pane: PaneContext) -> InteractionContext:
        return replace(self._context, focused_pane=pane)

    def _navigate(self, action: Navigate) -> tuple[bool, str, InteractionContext]:
        # The filter survives, exactly as it does in the TUI: navigating
        # changes the pane's kind and scope and never touches
        # `pane.filter_pattern`, which is why `agent_navigate` reports the
        # still-applied filter back to the model. Clearing it here would
        # score the model against a screen production never shows.
        #
        # The selection does not survive: it names a row of the list the
        # operator just left, and the new list has its own rows. Keeping
        # it would tell the next turn a resource is on screen that is not.
        pane = PaneContext(
            kind=action.view,
            scope=action.namespace or ALL_NAMESPACES_SCOPE,
            filter_pattern=self._focused.filter_pattern,
            selected=None,
        )
        return True, f"navigated to {action.view}", self._focus(pane)

    def _select(
        self, resource: ResourceIdentity, message: str
    ) -> tuple[bool, str, InteractionContext]:
        return True, message, self._focus(replace(self._focused, selected=resource))

    def _display(
        self,
        resource: ResourceIdentity,
        message: str,
        *,
        kind: str,
        scope: str,
    ) -> tuple[bool, str, InteractionContext]:
        pane = PaneContext(
            kind=kind,
            scope=scope,
            filter_pattern=None,
            selected=resource,
        )
        return True, message, self._focus(pane)


def _action_call(action: UiAction) -> tuple[str, dict[str, Any]]:
    """The `(tool name, arguments)` an applied action is recorded under."""
    if isinstance(action, Navigate):
        return "navigate", {"view": action.view, "namespace": action.namespace}
    if isinstance(action, SetFilter):
        return "set_filter", {"pattern": action.filter_pattern}
    if isinstance(action, OpenLogs):
        return "open_logs", {
            "pod": action.pod,
            "namespace": action.namespace,
            "container": action.container,
        }
    if isinstance(action, OpenDescribe):
        return "open_describe", {
            "kind": action.kind,
            "name": action.name,
            "namespace": action.namespace,
        }
    if isinstance(action, DrillDown):
        return "drill_down", {"name": action.name}
    # Every member of the closed union is named above. Falling through to
    # `drill_down` recorded an unhandled action as a drill into whatever
    # its `name` attribute held — a graded transcript describing a call
    # the model never made. The union's completeness is guarded in
    # `tests/test_agent_replacement_guard.py`; this is what happens if
    # something reaches here anyway.
    raise TypeError(f"unsupported UI action {type(action).__name__}")
