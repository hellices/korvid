"""Typed `:` command catalog, grammar, help, and completion metadata."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum

from korvid.core.store import ALL_NAMESPACES
from korvid.ui.messages import (
    BuiltinCommand,
    BuiltinOperation,
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ShowContextPicker,
    ShowNamespacePicker,
    SortCommand,
    SwitchContextCommand,
    UnknownCommand,
)


class ArgumentCompletion(Enum):
    """Dynamic word list used to complete a command argument."""

    NAMESPACE = "namespace"
    CONTEXT = "context"


class CommandParse(Enum):
    """Parsing behavior selected by a command descriptor."""

    QUIT = "quit"
    NAMESPACE = "namespace"
    CONTEXT = "context"
    SORT = "sort"


HelpRow = tuple[str, str]
CommandOperation = CommandParse | BuiltinOperation


@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    """Immutable definition shared by parsing, help, and completion."""

    aliases: tuple[str, ...]
    help: tuple[HelpRow, ...]
    operation: CommandOperation
    completion: ArgumentCompletion | None = None
    maximum_arguments: int | None = None


COMMANDS: tuple[CommandDescriptor, ...] = (
    CommandDescriptor(
        aliases=("q", "quit"),
        help=((":q", "Quit (also :quit)"),),
        operation=CommandParse.QUIT,
    ),
    CommandDescriptor(
        aliases=("ns", "namespaces"),
        help=((":ns|namespaces", "Namespace picker, or :ns <name> to switch"),),
        operation=CommandParse.NAMESPACE,
        completion=ArgumentCompletion.NAMESPACE,
    ),
    CommandDescriptor(
        aliases=("ctx", "context", "contexts"),
        help=((":ctx|:context|:contexts", "Context picker, or :ctx <name> to switch clusters"),),
        operation=CommandParse.CONTEXT,
        completion=ArgumentCompletion.CONTEXT,
    ),
    CommandDescriptor(
        aliases=("ai", "agent"),
        help=(
            (
                ":ai [off|payload]",
                "Agent setup; off disconnects, payload inspects (also :agent)",
            ),
            (":ai follow [on|off]", "Mirror the agent's cluster reads in the TUI (toggle)"),
        ),
        operation=BuiltinOperation.AI,
    ),
    CommandDescriptor(
        aliases=("model",),
        help=((":model [name]", "Show or switch the agent model"),),
        operation=BuiltinOperation.MODEL,
    ),
    CommandDescriptor(
        aliases=("mcp",),
        help=(
            (":mcp [on|off]", "Show MCP tool state, or toggle it live"),
            (":mcp follow [on|off]", "Mirror external MCP reads in the TUI (toggle)"),
        ),
        operation=BuiltinOperation.MCP,
    ),
    CommandDescriptor(
        aliases=("proposals",),
        help=((":proposals", "Review pending external write proposals"),),
        operation=BuiltinOperation.PROPOSALS,
        maximum_arguments=0,
    ),
    CommandDescriptor(
        aliases=("pf",),
        help=((":pf", "List port-forwards (Ctrl-D stop, r re-attach)"),),
        operation=BuiltinOperation.PORT_FORWARDS,
        maximum_arguments=0,
    ),
    CommandDescriptor(
        aliases=("tp", "telepresence"),
        help=((":tp", "Telepresence status panel (also :telepresence)"),),
        operation=BuiltinOperation.TELEPRESENCE,
        maximum_arguments=0,
    ),
    CommandDescriptor(
        aliases=("sort",),
        help=((":sort [column]", "Sort by a column (custom too); no argument clears"),),
        operation=CommandParse.SORT,
    ),
)

_COMMAND_BY_ALIAS = {alias: descriptor for descriptor in COMMANDS for alias in descriptor.aliases}
_RESOURCE_HELP: tuple[HelpRow, ...] = (
    (":<kind>", "Open a resource view (plural, singular, or alias)"),
    (":<kind> <ns>", "Open a view scoped to a namespace ('all' for every namespace)"),
)


def command_help(*, telepresence: bool = True) -> list[tuple[str, str]]:
    """Return ``(usage, description)`` rows for the help overlay.

    Telepresence stays reserved when unavailable, but its help row is hidden.
    """
    rows: list[HelpRow] = []
    for descriptor in COMMANDS:
        if descriptor.operation is BuiltinOperation.TELEPRESENCE and not telepresence:
            continue
        rows.extend(descriptor.help)
        if descriptor.operation is CommandParse.CONTEXT:
            rows.extend(_RESOURCE_HELP)
    return rows


def command_words(resource_aliases: Iterable[str]) -> list[str]:
    """Return sorted first-token completions for built-ins and resources."""
    return sorted({*_COMMAND_BY_ALIAS, *resource_aliases})


def argument_completion(
    command: str,
    known: Callable[[str], str | None],
) -> ArgumentCompletion | None:
    """Return the argument completion kind for a command token."""
    descriptor = _COMMAND_BY_ALIAS.get(command)
    if descriptor is not None:
        return descriptor.completion
    if known(command) is not None:
        return ArgumentCompletion.NAMESPACE
    return None


def _parse_builtin(
    descriptor: CommandDescriptor,
    rest: list[str],
    text: str,
) -> (
    QuitCommand
    | ShowNamespacePicker
    | ShowContextPicker
    | SwitchContextCommand
    | SortCommand
    | NavigateCommand
    | BuiltinCommand
    | UnknownCommand
):
    """Parse a catalogued command without consulting resource aliases."""
    operation = descriptor.operation
    if operation is CommandParse.QUIT:
        return QuitCommand()
    if operation is CommandParse.NAMESPACE:
        if not rest:
            return ShowNamespacePicker()
        if len(rest) == 1:
            return NavigateCommand(view=None, namespace=rest[0])
        return UnknownCommand(text)
    if operation is CommandParse.CONTEXT:
        if not rest:
            return ShowContextPicker()
        return SwitchContextCommand(text.strip().split(None, 1)[1])
    if operation is CommandParse.SORT:
        return SortCommand(rest[0] if rest else None) if len(rest) <= 1 else UnknownCommand(text)
    if descriptor.maximum_arguments is not None and len(rest) > descriptor.maximum_arguments:
        return UnknownCommand(text)
    return BuiltinCommand(operation, tuple(rest))


def parse_command(
    text: str,
    known: Callable[[str], str | None],
) -> (
    NavigateCommand
    | FilterCommand
    | QuitCommand
    | ShowNamespacePicker
    | ShowContextPicker
    | SwitchContextCommand
    | SortCommand
    | BuiltinCommand
    | UnknownCommand
):
    parts = text.strip().split()
    if not parts:
        return UnknownCommand(text)
    head, *rest = parts
    descriptor = _COMMAND_BY_ALIAS.get(head)
    if descriptor is not None:
        return _parse_builtin(descriptor, rest, text)
    plural = known(head)
    if plural is None:
        return UnknownCommand(text)
    if not rest:
        return NavigateCommand(view=plural, namespace=None)
    if len(rest) == 1:
        ns = rest[0]
        return NavigateCommand(view=plural, namespace=ALL_NAMESPACES if ns == "all" else ns)
    return UnknownCommand(text)
