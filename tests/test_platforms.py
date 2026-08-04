"""Tests for shared platform helpers and CI workflow invariants."""

import os
from pathlib import Path

import pytest

from tests.platforms import (
    POSIX,
    WINDOWS,
    assert_pinned_action_ref,
    posix_only,
    read_text_utf8,
)


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
    return read_text_utf8(Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml")


def test_assert_pinned_action_ref_accepts_full_lowercase_commit_shas() -> None:
    workflow = "- uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5  # v4.3.1"

    assert (
        assert_pinned_action_ref(workflow, "actions/checkout")
        == "34e114876b0b11c390a56381ad16ebd13914f8d5"
    )


@pytest.mark.parametrize("bad_ref", ["v4", "v4.3.1", "34E114876B0B11C390A56381AD16EBD13914F8D5"])
def test_assert_pinned_action_ref_rejects_tags_and_non_lowercase_refs(bad_ref: str) -> None:
    workflow = f"- uses: astral-sh/setup-uv@{bad_ref}"

    with pytest.raises(
        AssertionError, match="expected astral-sh/setup-uv@<40 lowercase hex characters>"
    ):
        assert_pinned_action_ref(workflow, "astral-sh/setup-uv")


def test_read_text_utf8_uses_utf8_encoding(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "ci.yml"
    captured: dict[str, object] = {}

    def fake_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        captured["path"] = self
        captured["encoding"] = encoding
        captured["errors"] = errors
        return "name: CI"

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert read_text_utf8(path) == "name: CI"
    assert captured == {"path": path, "encoding": "utf-8", "errors": None}


def test_ci_workflow_defines_the_required_windows_test_job() -> None:
    workflow = _ci_workflow()
    test_job = workflow.index("\n  test:")
    windows_job = workflow.index("\n  windows-test:")
    pre_commit = workflow.index("\n  pre-commit:")
    assert test_job < windows_job < pre_commit
    segment = workflow[windows_job:pre_commit]
    assert "runs-on: windows-latest" in segment
    assert_pinned_action_ref(segment, "actions/checkout")
    assert_pinned_action_ref(segment, "astral-sh/setup-uv")
    assert 'python-version: "3.12"' in segment
    assert "uv sync --locked --dev --all-extras" in segment
    assert "uv run pytest -q" in segment
