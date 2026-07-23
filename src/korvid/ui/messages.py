"""UI Bus vocabulary: user keystrokes and agent UI-control emit the same Messages."""

from __future__ import annotations

from textual.message import Message


class NavigateCommand(Message):
    """Command to navigate to a view, optionally in a specific namespace."""

    view: str
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
