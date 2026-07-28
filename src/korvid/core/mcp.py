"""Layer-boundary contract for the MCP server controller.

The concrete controller lives in `korvid.mcp.server`, an optional extra
(issue #73). The UI and the composition root depend on this ABC so the
base TUI never imports the MCP adapter or its third-party dependencies;
the concrete controller is injected only when the `mcp` extra is
installed.
"""

from __future__ import annotations

import abc
import asyncio


class MCPControllerBase(abc.ABC):
    """Lifecycle contract for the MCP server controller."""

    @property
    @abc.abstractmethod
    def running(self) -> bool:
        """True while the MCP server is serving."""

    @abc.abstractmethod
    def status(self) -> str:
        """One-line human-readable server state for the status bar."""

    @abc.abstractmethod
    async def start(self) -> str:
        """Start the server; returns a user-facing status message."""

    @abc.abstractmethod
    async def stop(self) -> str:
        """Stop the server; returns a user-facing status message."""

    @abc.abstractmethod
    async def shutdown(self) -> asyncio.Task[None] | None:
        """Begin a graceful stop; returns any still-pending teardown task."""
