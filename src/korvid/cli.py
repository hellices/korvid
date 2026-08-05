"""Lightweight console entrypoint for `korvid`."""

from __future__ import annotations

import argparse
import sys

from korvid import __version__

#: The only invocation handled without importing the app. Anything else — even
#: `-n --version`, where the real parser consumes `--version` as the namespace
#: value — is delegated verbatim so this fast path can never diverge from
#: `korvid.__main__`'s parser.
_VERSION_ONLY = ["--version"]


def main() -> None:
    """Print the version without app startup, or delegate to the app."""
    if sys.argv[1:] == _VERSION_ONLY:
        parser = argparse.ArgumentParser(prog="korvid", add_help=False, allow_abbrev=False)
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
        parser.parse_args(_VERSION_ONLY)  # exits after printing

    from korvid.__main__ import main as app_main

    app_main()
