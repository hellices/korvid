"""Shared test helpers for platform-specific expectations."""

import os
import re
from pathlib import Path

import pytest

WINDOWS = os.name == "nt"
POSIX = os.name == "posix"


def posix_only(reason: str) -> pytest.MarkDecorator:
    """Skip a test when POSIX-specific behavior is unavailable."""
    return pytest.mark.skipif(not POSIX, reason=reason)


def read_text_utf8(path: Path) -> str:
    """Read a repository text file with an explicit UTF-8 encoding."""
    return path.read_text(encoding="utf-8")


def assert_pinned_action_ref(workflow_text: str, action: str) -> str:
    """Require an action use-site to be pinned to a lowercase full commit SHA."""
    pattern = re.compile(
        rf"(?m)^\s*-\s+uses:\s+{re.escape(action)}@(?P<ref>[0-9a-f]{{40}})\s*(?:#.*)?$"
    )
    match = pattern.search(workflow_text)
    assert match is not None, f"expected {action}@<40 lowercase hex characters>"
    return match.group("ref")
