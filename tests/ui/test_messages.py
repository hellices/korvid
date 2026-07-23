from textual.message import Message

from korvid.ui.messages import (
    ClearFilter,
    FilterCommand,
    NavigateCommand,
    ResourcesUpdated,
    ShowError,
)


def test_navigate_carries_view_and_namespace() -> None:
    msg = NavigateCommand("pods", namespace="prod")
    assert msg.view == "pods"
    assert msg.namespace == "prod"


def test_filter_carries_pattern() -> None:
    assert FilterCommand("check").pattern == "check"


def test_resources_updated_carries_kind() -> None:
    assert ResourcesUpdated("pods").kind == "pods"


def test_show_error_carries_title_and_detail() -> None:
    msg = ShowError("RBAC error", "no permission")
    assert msg.title == "RBAC error"
    assert msg.detail == "no permission"


def test_clear_filter_is_instantiable() -> None:
    msg = ClearFilter()
    assert isinstance(msg, Message)
