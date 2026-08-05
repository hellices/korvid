"""Lightweight console entrypoint for `korvid`."""

from __future__ import annotations

import argparse

from korvid import __version__


def main() -> None:
    """Handle fast-path CLI flags before importing the full app startup."""
    parser = argparse.ArgumentParser(prog="korvid", add_help=False, allow_abbrev=False)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_known_args()

    from korvid.__main__ import main as app_main

    app_main()
