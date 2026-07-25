"""NavigationStack: drill-down levels, parent-uid filter, and breadcrumb."""

from korvid.ui.navigation import DrillLevel, NavigationStack


def _deploy_level() -> DrillLevel:
    return DrillLevel(
        parent_kind="deployments",
        parent_name="web",
        parent_namespace="prod",
        parent_uid="dep-1",
        child_kind="replicasets",
    )


def _rs_level() -> DrillLevel:
    return DrillLevel(
        parent_kind="replicasets",
        parent_name="web-6d9f88",
        parent_namespace="prod",
        parent_uid="rs-1",
        child_kind="pods",
    )


class TestNavigationStack:
    def test_empty_stack_has_no_filter(self) -> None:
        stack = NavigationStack()
        assert not stack.active
        assert stack.parent_uid is None
        assert stack.breadcrumb() == ""

    def test_push_sets_filter_to_top_parent(self) -> None:
        stack = NavigationStack()
        stack.push(_deploy_level())
        assert stack.active
        assert stack.parent_uid == "dep-1"
        assert stack.child_kind == "replicasets"

    def test_pop_returns_parent_kind_to_display(self) -> None:
        stack = NavigationStack()
        stack.push(_deploy_level())
        stack.push(_rs_level())
        assert stack.parent_uid == "rs-1"
        popped = stack.pop()
        assert popped is not None
        assert popped.parent_kind == "replicasets"
        # Back at the replicasets level: filter is the deployment uid again.
        assert stack.parent_uid == "dep-1"

    def test_pop_empty_returns_none(self) -> None:
        assert NavigationStack().pop() is None

    def test_clear(self) -> None:
        stack = NavigationStack()
        stack.push(_deploy_level())
        stack.clear()
        assert not stack.active
        assert stack.parent_uid is None

    def test_breadcrumb_walks_levels(self) -> None:
        stack = NavigationStack()
        stack.push(_deploy_level())
        stack.push(_rs_level())
        assert stack.breadcrumb() == "deployments/web > replicasets/web-6d9f88 > pods"

    def test_breadcrumb_single_level(self) -> None:
        stack = NavigationStack()
        stack.push(_deploy_level())
        assert stack.breadcrumb() == "deployments/web > replicasets"
