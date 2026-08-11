"""Turning a citation into something the user can open (issue #192).

A reference is only worth having if selecting it puts the evidence on
screen. The mapping from an `Evidence` record to a view is pure and lives
here so it can be tested without a running app, and so the UI has one
place to ask "where does this citation go?".
"""

from __future__ import annotations

import pytest

from korvid.agent.evidence import Evidence
from korvid.agent.navigation import EvidenceTarget, target_for


def _evidence(
    tool: str,
    *,
    kind: str | None = None,
    name: str | None = None,
    namespace: str | None = None,
    container: str | None = None,
) -> Evidence:
    return Evidence(
        ref="E1",
        tool=tool,
        kind=kind,
        namespace=namespace,
        name=name,
        container=container,
        excerpt="…",
    )


def test_a_log_read_opens_the_log_pane_for_that_container() -> None:
    """The citation lands on the exact stream the claim came from."""
    target = target_for(
        _evidence("get_logs", kind="pods", name="api-1", namespace="prod", container="app")
    )

    assert target == EvidenceTarget(
        view="logs", kind="pods", name="api-1", namespace="prod", container="app"
    )


def test_a_resource_read_opens_describe() -> None:
    """A manifest read is best shown as the object it described."""
    target = target_for(_evidence("get_resource", kind="deployments", name="web", namespace="prod"))

    assert target is not None
    assert target.view == "describe"
    assert target.kind == "deployments"
    assert target.name == "web"


def test_an_events_read_opens_describe_too() -> None:
    """Events are rendered within describe; there is no separate view."""
    target = target_for(_evidence("get_events", kind="pods", name="api-1", namespace="prod"))

    assert target is not None
    assert target.view == "describe"


def test_a_listing_opens_the_list_rather_than_a_single_object() -> None:
    """`list_resources` has no single subject to describe."""
    target = target_for(_evidence("list_resources", kind="pods", namespace="prod"))

    assert target is not None
    assert target.view == "list"
    assert target.name is None


def test_a_diagnose_read_opens_the_object_it_diagnosed() -> None:
    """The locator already normalised `pvc`/`service` to kind and name."""
    target = target_for(_evidence("diagnose_pvc", kind="persistentvolumeclaims", name="data-0"))

    assert target is not None
    assert target.view == "describe"
    assert target.kind == "persistentvolumeclaims"


def test_a_single_object_view_with_no_object_is_not_navigable() -> None:
    """A read that names nothing cannot be opened, and says so.

    Better than guessing: a citation that opens the wrong object is worse
    than one that reports it cannot be followed.
    """
    assert target_for(_evidence("get_resource", kind="pods", namespace="prod")) is None
    assert target_for(_evidence("get_logs", namespace="prod")) is None


def test_an_unknown_tool_is_not_navigable() -> None:
    """A plugin read has to declare where it goes before it can be opened."""
    assert target_for(_evidence("some_plugin_read", kind="pods", name="api-1")) is None


def test_every_registered_read_is_classified() -> None:
    """A new cluster read must not silently become unnavigable.

    Fails when a `cluster_read` is added that the mapping does not know,
    which is the moment to decide what selecting its citation should do.
    """
    from korvid.agent.navigation import NAVIGABLE_TOOLS
    from korvid.tools.registry import TOOLS_BY_NAME

    unclassified = sorted(
        name
        for name, definition in TOOLS_BY_NAME.items()
        if definition.effect == "cluster_read" and name not in NAVIGABLE_TOOLS
    )

    assert unclassified == []


def test_a_target_needs_a_view() -> None:
    """An empty view would make the citation open nothing at all."""
    with pytest.raises(ValueError, match="view"):
        EvidenceTarget(view="", kind="pods", name="api-1", namespace=None, container=None)


def test_a_log_citation_keeps_the_container_the_read_defaulted_to() -> None:
    """An omitted container is resolved by the executor, not left open.

    `get_logs` without a container reads the pod's *first* container. If
    the citation opens every container, the user is shown streams that
    were not the evidence, and the cited one can be scrolled away
    (#192 review).
    """
    target = target_for(_evidence("get_logs", kind="pods", name="api-1", namespace="prod"))

    assert target is not None
    assert target.container is None
    assert target.needs_container_resolution is True


def test_an_events_citation_says_it_needs_events() -> None:
    """Describe fetches events for pods only.

    Routing a Deployment event citation to describe would open a manifest
    with none of the cited events on it while claiming otherwise.
    """
    pod = target_for(_evidence("get_events", kind="pods", name="api-1", namespace="prod"))
    deployment = target_for(_evidence("get_events", kind="deployments", name="web", namespace="p"))

    assert pod is not None
    assert deployment is not None
    assert pod.expects_events is True
    assert deployment.expects_events is True


def test_a_cluster_wide_listing_is_marked_as_such() -> None:
    """An omitted namespace means every namespace, not the current one."""
    target = target_for(_evidence("list_resources", kind="pods"))

    assert target is not None
    assert target.view == "list"
    assert target.all_namespaces is True


def test_a_scoped_listing_is_not_cluster_wide() -> None:
    target = target_for(_evidence("list_resources", kind="pods", namespace="prod"))

    assert target is not None
    assert target.all_namespaces is False


def test_the_fixed_kind_listings_resolve() -> None:
    """helm_list_releases and list_operators take no kind argument.

    They were in the table but could never reach it, because the locator
    leaves kind unset for them - a declaration that disagreed with the
    runtime (#192 review).
    """
    helm = target_for(_evidence("helm_list_releases", namespace="prod"))
    operators = target_for(_evidence("list_operators", namespace="prod"))

    assert helm is not None
    assert helm.kind == "helmreleases"
    assert operators is not None
    assert operators.kind == "operators"
