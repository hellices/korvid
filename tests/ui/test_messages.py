from korvid.ui.messages import FilterCommand, NavigateCommand, ResourcesUpdated


def test_navigate_carries_view_and_namespace() -> None:
    msg = NavigateCommand("pods", namespace="prod")
    assert msg.view == "pods"
    assert msg.namespace == "prod"


def test_filter_carries_pattern() -> None:
    assert FilterCommand("check").pattern == "check"


def test_resources_updated_carries_kind() -> None:
    assert ResourcesUpdated("pods").kind == "pods"
