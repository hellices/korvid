"""`:`  command grammar — familiar TUI conventions. UnknownCommand is the future agent fallthrough hook."""

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
# (e.g. a `Model` resource) must never shadow them.
_RESERVED_BUILTINS = {"ai", "agent", "model"}


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
