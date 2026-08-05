#!/usr/bin/env python3
"""Shared release-version format gate for the release scripts.

The project version reaches shell interpolation, `$GITHUB_OUTPUT`, and release
artifact file names. Both the publication gate (`check_version.py`) and the dry
run gate (`check_dry_run.py`) validate its shape here so the two paths cannot
drift apart.
"""

from __future__ import annotations

import re

#: The only supported release version shape, e.g. `0.1.0`.
RELEASE_VERSION = re.compile(r"\A[0-9]+\.[0-9]+\.[0-9]+\Z")

#: Message used by both gates. It deliberately never echoes the rejected value.
UNSUPPORTED_VERSION = "project version is not a supported release version (expected X.Y.Z)"


def is_supported_release_version(version: str) -> bool:
    """Return whether *version* is a supported `X.Y.Z` release version."""
    return RELEASE_VERSION.match(version) is not None
