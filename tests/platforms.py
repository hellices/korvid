"""Shared test helpers for platform-specific expectations."""

import os

import pytest

WINDOWS = os.name == "nt"
POSIX = os.name == "posix"


def posix_only(reason: str) -> pytest.MarkDecorator:
    """Skip a test when POSIX-specific behavior is unavailable."""
    return pytest.mark.skipif(not POSIX, reason=reason)
