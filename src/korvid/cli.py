"""Lightweight console entrypoint for `korvid`."""

from __future__ import annotations

import argparse
import importlib.util
import sys

from korvid import __version__

#: Exact version and MCP subcommands avoid importing the app. Other arguments,
#: including `-n --version`, are delegated verbatim to the composition root.
_VERSION_ONLY = ["--version"]


def _positive_pid(value: str) -> int:
    try:
        pid = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("instance must be a positive process ID") from exc
    if pid <= 0:
        raise argparse.ArgumentTypeError("instance must be a positive process ID")
    return pid


def _mcp_main(arguments: list[str]) -> None:
    parser = argparse.ArgumentParser(prog="korvid mcp", description="Connect local MCP clients.")
    subcommands = parser.add_subparsers(dest="transport", required=True)
    stdio = subcommands.add_parser("stdio", help="Connect to an existing TUI over stdio.")
    stdio.add_argument("--instance", type=_positive_pid, help="PID of the running Korvid TUI.")
    args = parser.parse_args(arguments)
    if any(importlib.util.find_spec(package) is None for package in ("mcp", "httpx2", "anyio")):
        from korvid.agent.install_hint import isolated_install_hint

        parser.exit(
            1, f"korvid: MCP support is not installed; {isolated_install_hint(feature='mcp')}\n"
        )

    from korvid.mcp.registry import EndpointRegistryError
    from korvid.mcp.stdio import StdioBridgeError, run_stdio

    try:
        run_stdio(instance=args.instance)
    except (EndpointRegistryError, StdioBridgeError) as exc:
        parser.exit(1, f"korvid: {exc}\n")


def main() -> None:
    """Handle lightweight commands, or delegate TUI startup to the composition root."""
    if sys.argv[1:] == _VERSION_ONLY:
        parser = argparse.ArgumentParser(prog="korvid", add_help=False, allow_abbrev=False)
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
        parser.parse_args(_VERSION_ONLY)  # exits after printing

    if sys.argv[1:2] == ["mcp"]:
        _mcp_main(sys.argv[2:])
        return

    from korvid.__main__ import main as app_main

    app_main()
