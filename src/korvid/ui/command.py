"""`:`  command grammar — k9s conventions. UnknownCommand is the future agent fallthrough hook."""

from __future__ import annotations

from collections.abc import Callable

from korvid.core.store import ALL_NAMESPACES
from korvid.ui.messages import (
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ShowNamespacePicker,
    UnknownCommand,
)

_NS_KEYWORDS = {"ns", "namespaces"}
# Built-ins handled by the app's unknown-command hook; a cluster CRD alias
# (e.g. a `Model` resource) must never shadow them.  Help rows live beside
# the set so adding a builtin updates the overlay in the same review.
_BUILTIN_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    (":ai", "Open agent setup (also :agent)"),
    (":model [name]", "Show or switch the agent model"),
    (":mcp [on|off]", "Show MCP tool state, or toggle it live"),
    (":pf", "List port-forwards (Ctrl-D stop, r re-attach)"),
)
_RESERVED_BUILTINS = {"ai", "agent", "model", "mcp", "pf"}


def command_help() -> list[tuple[str, str]]:
    """``(command, description)`` rows for the help overlay (issue #41).

    Kept next to `parse_command` so grammar changes update the help text in
    the same review.
    """
    # Primary short form first, remaining aliases sorted for stability.
    ns = "|".join(["ns", *sorted(_NS_KEYWORDS - {"ns"})])
    return [
        (":q", "Quit (also :quit)"),
        (f":{ns}", "Namespace picker, or :ns <name> to switch"),
        (":<kind>", "Open a resource view (plural, singular, or alias)"),
        (":<kind> <ns>", "Open a view scoped to a namespace ('all' for every namespace)"),
        *_BUILTIN_COMMAND_HELP,
    ]


def parse_command(
    text: str,
    known: Callable[[str], str | None],
) -> NavigateCommand | FilterCommand | QuitCommand | ShowNamespacePicker | UnknownCommand:
    parts = text.strip().split()
    if not parts:
        return UnknownCommand(text)
    head, *rest = parts
    if head in {"q", "quit"}:
        return QuitCommand()
    if head in _NS_KEYWORDS and not rest:
        return ShowNamespacePicker()
    if head in _NS_KEYWORDS and len(rest) == 1:
        return NavigateCommand(view=None, namespace=rest[0])
    if head in _RESERVED_BUILTINS:
        return UnknownCommand(text)
    plural = known(head)
    if plural is None:
        return UnknownCommand(text)
    if not rest:
        return NavigateCommand(view=plural, namespace=None)
    if len(rest) == 1:
        ns = rest[0]
        return NavigateCommand(view=plural, namespace=ALL_NAMESPACES if ns == "all" else ns)
    return UnknownCommand(text)
