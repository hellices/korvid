"""`:`  command grammar. UnknownCommand is the future agent fallthrough hook."""

from __future__ import annotations

from collections.abc import Callable

from korvid.core.store import ALL_NAMESPACES
from korvid.ui.messages import (
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ShowContextPicker,
    ShowNamespacePicker,
    SortCommand,
    SwitchContextCommand,
    UnknownCommand,
)

_NS_KEYWORDS = {"ns", "namespaces"}
_CTX_KEYWORDS = {"ctx", "context", "contexts"}
# Built-ins handled by the app's unknown-command hook; a cluster CRD alias
# (e.g. a `Model` resource) must never shadow them.  Help rows live beside
# the set so adding a builtin updates the overlay in the same review.
_BUILTIN_COMMAND_HELP: tuple[tuple[str, str], ...] = (
    (":ai", "Open agent setup (also :agent)"),
    (":model [name]", "Show or switch the agent model"),
    (":mcp [on|off]", "Show MCP tool state, or toggle it live"),
    (":mcp follow [on|off]", "Mirror external MCP reads in the TUI (toggle)"),
    (":proposals", "Review pending external write proposals"),
    (":pf", "List port-forwards (Ctrl-D stop, r re-attach)"),
    (":tp", "Telepresence status panel (also :telepresence)"),
    (":sort [column]", "Sort by a column (custom too); no argument clears"),
)
_RESERVED_BUILTINS = {
    "ai",
    "agent",
    "model",
    "mcp",
    "proposals",
    "pf",
    "sort",
    "tp",
    "telepresence",
} | _CTX_KEYWORDS


def command_help(*, telepresence: bool = True) -> list[tuple[str, str]]:
    """``(command, description)`` rows for the help overlay (issue #41).

    Kept next to `parse_command` so grammar changes update the help text in
    the same review. `telepresence=False` drops the `:tp` row - an absent
    binary (or the kill-switch) means "no UI", and the help overlay must
    not advertise a command that only answers with a warning (the name
    stays reserved either way so a CRD alias cannot shadow it).
    """
    # Primary short form first, remaining aliases sorted for stability.
    ns = "|".join(["ns", *sorted(_NS_KEYWORDS - {"ns"})])
    ctx = "|".join([":ctx", *(f":{k}" for k in sorted(_CTX_KEYWORDS - {"ctx"}))])
    builtins = [
        row for row in _BUILTIN_COMMAND_HELP if telepresence or not row[0].startswith(":tp")
    ]
    return [
        (":q", "Quit (also :quit)"),
        (f":{ns}", "Namespace picker, or :ns <name> to switch"),
        (ctx, "Context picker, or :ctx <name> to switch clusters"),
        (":<kind>", "Open a resource view (plural, singular, or alias)"),
        (":<kind> <ns>", "Open a view scoped to a namespace ('all' for every namespace)"),
        *builtins,
    ]


def _parse_builtin(
    head: str, rest: list[str], text: str
) -> (
    QuitCommand
    | ShowNamespacePicker
    | ShowContextPicker
    | SwitchContextCommand
    | SortCommand
    | NavigateCommand
    | UnknownCommand
    | None
):
    """Handle reserved words; None means *head* is not a builtin.

    Reserved builtins never fall through to alias navigation: a malformed
    `:ctx`/`:sort` (or a cluster CRD named after one) must not open a view.
    """
    if head in {"q", "quit"}:
        return QuitCommand()
    if head in _NS_KEYWORDS:
        if not rest:
            return ShowNamespacePicker()
        if len(rest) == 1:
            return NavigateCommand(view=None, namespace=rest[0])
        return UnknownCommand(text)
    if head in _CTX_KEYWORDS:
        if not rest:
            return ShowContextPicker()
        # Context names are arbitrary strings (spaces included): the whole
        # remainder is the name; unknown names are validated at switch time.
        return SwitchContextCommand(text.strip().split(None, 1)[1])
    if head == "sort":
        return SortCommand(rest[0] if rest else None) if len(rest) <= 1 else UnknownCommand(text)
    if head in _RESERVED_BUILTINS:
        return UnknownCommand(text)
    return None


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
    | UnknownCommand
):
    parts = text.strip().split()
    if not parts:
        return UnknownCommand(text)
    head, *rest = parts
    builtin = _parse_builtin(head, rest, text)
    if builtin is not None:
        return builtin
    plural = known(head)
    if plural is None:
        return UnknownCommand(text)
    if not rest:
        return NavigateCommand(view=plural, namespace=None)
    if len(rest) == 1:
        ns = rest[0]
        return NavigateCommand(view=plural, namespace=ALL_NAMESPACES if ns == "all" else ns)
    return UnknownCommand(text)
