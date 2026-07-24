"""`:`  command grammar — familiar TUI conventions. UnknownCommand is the future agent fallthrough hook."""

from __future__ import annotations

from korvid.ui.messages import (
    FilterCommand,
    NavigateCommand,
    QuitCommand,
    ShowNamespacePicker,
    UnknownCommand,
)

_VIEWS = {"pods"}


def parse_command(
    text: str,
) -> NavigateCommand | FilterCommand | QuitCommand | ShowNamespacePicker | UnknownCommand:
    parts = text.strip().split()
    if not parts:
        return UnknownCommand(text)
    head, *rest = parts
    if head in {"q", "quit"}:
        return QuitCommand()
    if head in _VIEWS and not rest:
        return NavigateCommand(head)
    if head == "ns" and not rest:
        return ShowNamespacePicker()
    if head == "ns" and len(rest) == 1:
        return NavigateCommand("pods", namespace=rest[0])
    return UnknownCommand(text)
