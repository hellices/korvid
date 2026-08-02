"""MCP follow mode (issue #153): mirror external cluster reads in the TUI.

External MCP hosts overwhelmingly call the ``cluster_read`` tools (those
return the data they need), which historically moved nothing on screen -
the TUI sat idle while the assistant inspected pods "behind its back".
With follow mode on, each read is mirrored through the *same* ``UIBridge``
methods the ``ui_only`` tools use, so the screen tracks what the external
host is reading.

Mirroring is strictly best-effort and fire-and-forget: `mirror_read` never
raises, and the MCP response never waits on it. The bridge methods carry
their own safety guards (they refuse to cover a describe screen the user
is reading, or an approval dialog awaiting keystrokes).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from korvid.tools.executor import UIBridge

logger = logging.getLogger(__name__)

#: Reads with a mirror mapping below.
FOLLOWABLE_TOOLS: frozenset[str] = frozenset(
    {
        "list_resources",
        "get_resource",
        "get_logs",
        "get_events",
        "list_operators",
        "diagnose_pod",
    }
)

#: Reads deliberately left unmirrored. The pairing test in
#: tests/tools/test_follow.py forces every cluster_read on the MCP surface
#: into exactly one of these sets, so a new read tool cannot silently
#: become invisible again.
UNMIRRORED_TOOLS: frozenset[str] = frozenset()


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def read_summary(tool: str, args: Mapping[str, Any]) -> str:
    """One activity-feed line for a cluster read.

    E.g. `get_logs api-1 (ns prod)`, `list_resources pods (ns all)` - short
    enough for a transient toast, specific enough to know what an external
    host just looked at.
    """
    kind = _str_or_none(args.get("kind"))
    target = _str_or_none(args.get("pod")) or _str_or_none(args.get("name"))
    namespace = _str_or_none(args.get("namespace"))
    parts = [tool]
    if kind and target:
        parts.append(f"{kind}/{target}")
    elif kind or target:
        parts.append(kind or target or "")
    parts.append(f"(ns {namespace or 'all'})")
    return " ".join(parts)


async def mirror_read(ui: UIBridge, tool: str, args: Mapping[str, Any]) -> str | None:
    """Mirror one MCP cluster read in the TUI; None when nothing mirrored.

    Never raises: mirroring is cosmetic and runs as a fire-and-forget task,
    so any failure (bad arguments, bridge refusal, UI teardown) is logged
    at debug level and swallowed.
    """
    try:
        return await _mirror(ui, tool, args)
    except Exception:
        logger.debug("follow mirror for %s failed", tool, exc_info=True)
        return None


async def _mirror(ui: UIBridge, tool: str, args: Mapping[str, Any]) -> str | None:
    namespace = _str_or_none(args.get("namespace"))
    if tool == "list_resources":
        kind = _str_or_none(args.get("kind"))
        if kind is None:
            return None
        # An omitted namespace lists cluster-wide: mirror the same scope.
        return await ui.agent_navigate(kind, namespace or "all")
    if tool in ("get_resource", "get_events"):
        kind = _str_or_none(args.get("kind"))
        name = _str_or_none(args.get("name"))
        if kind is None or name is None:
            return None
        return await ui.agent_open_describe(kind, name, namespace)
    if tool == "get_logs":
        pod = _str_or_none(args.get("pod"))
        if pod is None or namespace is None:
            return None
        return await ui.agent_open_logs(pod, namespace, _str_or_none(args.get("container")))
    if tool == "diagnose_pod":
        name = _str_or_none(args.get("name"))
        if name is None:
            return None
        return await ui.agent_open_describe("pods", name, namespace)
    if tool == "list_operators":
        return await ui.agent_navigate("subscriptions", namespace or "all")
    return None
