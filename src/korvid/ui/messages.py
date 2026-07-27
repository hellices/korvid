"""UI Bus vocabulary: user keystrokes and agent UI-control emit the same Messages."""

from __future__ import annotations

from textual.message import Message


class NavigateCommand(Message):
    """Command to navigate to a view, optionally in a specific namespace."""

    view: str | None
    # `namespace` is a ClassVar on Message used only in __init_subclass__ to derive
    # handler_name ("on_navigate_command") at class-definition time. Dispatch reads
    # message.handler_name (class var), never message.namespace at runtime, so this
    # instance-attribute shadow is safe. Mypy cannot model this; suppression required.
    namespace: str | None  # type: ignore[misc,assignment]

    def __init__(self, view: str | None, namespace: str | None = None) -> None:
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


class QuitCommand(Message):
    """Command to quit the application."""

    pass


class ShowNamespacePicker(Message):
    """Command to open the namespace picker (bare `:ns`)."""

    pass


class ShowContextPicker(Message):
    """Command to open the kubeconfig context picker (bare `:ctx`, issue #36)."""

    pass


class SwitchContextCommand(Message):
    """`:ctx <name>` — switch the session to another kubeconfig context (issue #36)."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name


class UnknownCommand(Message):
    """Command that was not recognised — future agent fallthrough hook."""

    def __init__(self, text: str) -> None:
        self.text = text
        super().__init__()


class SortCommand(Message):
    """`:sort <column>` — sort the current view; None clears the sort (issue #45)."""

    column: str | None

    def __init__(self, column: str | None) -> None:
        super().__init__()
        self.column = column


class AgentPromptSubmitted(Message):
    """Posted by AgentPanel when the user submits a non-empty prompt."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class TransferCancelRequested(Message):
    """Posted by TransferProgressScreen (bubbling to the app) when the user
    hits escape during a file transfer."""

    pass
