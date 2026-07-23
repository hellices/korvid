"""UI Bus vocabulary: user keystrokes and agent UI-control emit the same Messages."""

from __future__ import annotations

from textual.message import Message


class NavigateCommand(Message):
    """Command to navigate to a view, optionally in a specific namespace."""

    view: str
    # `namespace` is a ClassVar on Message used only in __init_subclass__ to derive
    # handler_name ("on_navigate_command") at class-definition time. Dispatch reads
    # message.handler_name (class var), never message.namespace at runtime, so this
    # instance-attribute shadow is safe. Mypy cannot model this; suppression required.
    namespace: str | None  # type: ignore[misc,assignment]

    def __init__(self, view: str, namespace: str | None = None) -> None:
        super().__init__()
        self.view = view
        self.namespace = namespace


class FilterCommand(Message):
    """Command to apply a filter pattern."""

    pattern: str

    def __init__(self, pattern: str) -> None:
        super().__init__()
        self.pattern = pattern


class ClearFilter(Message):
    """Command to clear any active filter."""

    pass


class ResourcesUpdated(Message):
    """Message indicating that resources of a kind have been updated."""

    kind: str

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind


class ShowError(Message):
    """Message to display an error to the user."""

    title: str
    detail: str

    def __init__(self, title: str, detail: str) -> None:
        super().__init__()
        self.title = title
        self.detail = detail
