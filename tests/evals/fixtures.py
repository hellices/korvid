"""Shared typed starting interactions for the eval test suite.

Scenarios and journeys record the exact workspace a turn starts from
(issue #316 task 13), so a test that builds a `Scenario` or a journey by
hand needs one too. One helper, so a schema change lands in one place
instead of thirty literal blocks.
"""

from __future__ import annotations

from korvid.agent.interaction import InteractionContext, PaneContext, ResourceIdentity


def eval_interaction(
    *,
    kind: str = "pods",
    scope: str = "shop",
    kube_context: str = "eval-cluster",
    context_epoch: int = 1,
    selected: ResourceIdentity | None = None,
    filter_pattern: str | None = None,
) -> InteractionContext:
    """One focused pane, no secondary pane, no timeline cursor."""
    return InteractionContext(
        kube_context=kube_context,
        context_epoch=context_epoch,
        focused_pane=PaneContext(
            kind=kind, scope=scope, filter_pattern=filter_pattern, selected=selected
        ),
        secondary_pane=None,
        timeline_cursor=None,
    )


#: The default starting interaction for hand-built fixtures.
EVAL_INTERACTION = eval_interaction()
