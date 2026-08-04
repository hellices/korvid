"""Tests for shared platform helpers and CI workflow invariants."""

import os
from pathlib import Path

import pytest

from tests.platforms import POSIX, WINDOWS, posix_only


def test_windows_and_posix_flags_follow_os_name() -> None:
    assert WINDOWS is (os.name == "nt")
    assert POSIX is (os.name == "posix")


def test_posix_only_returns_a_skipif_mark_with_the_given_reason() -> None:
    mark = posix_only("POSIX permissions required")

    assert isinstance(mark, pytest.MarkDecorator)
    assert mark.mark.name == "skipif"
    assert mark.mark.args == (not POSIX,)
    assert mark.mark.kwargs == {"reason": "POSIX permissions required"}


def _ci_workflow() -> str:
    return (Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml").read_text()


def test_ci_workflow_defines_the_required_windows_test_job() -> None:
    workflow = _ci_workflow()
    test_job = workflow.index("\n  test:")
    windows_job = workflow.index("\n  windows-test:")
    pre_commit = workflow.index("\n  pre-commit:")
    assert test_job < windows_job < pre_commit
    segment = workflow[windows_job:pre_commit]
    assert "runs-on: windows-latest" in segment
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" in segment
    assert "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9" in segment
    assert 'python-version: "3.12"' in segment
    assert "uv sync --locked --dev --all-extras" in segment
    assert "uv run pytest -q" in segment
