"""Tests for owner-private, collision-safe text exports."""

from __future__ import annotations

from pathlib import Path

import pytest

from korvid.core.private_export import write_private_text
from tests.platforms import POSIX


def test_write_private_text_exclusively_creates_requested_name(tmp_path: Path) -> None:
    path = write_private_text(tmp_path, "korvid-agent-payload-20260805-094820", ".json", "{}\n")

    assert path == tmp_path / "korvid-agent-payload-20260805-094820.json"
    assert path.read_bytes() == b"{}\n"


def test_write_private_text_uses_collision_suffix_without_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "korvid-agent-payload-20260805-094820.json"
    existing.write_text("first\n", encoding="utf-8")

    path = write_private_text(
        tmp_path,
        "korvid-agent-payload-20260805-094820",
        ".json",
        "second\n",
    )

    assert path.name == "korvid-agent-payload-20260805-094820-1.json"
    assert existing.read_text(encoding="utf-8") == "first\n"
    assert path.read_text(encoding="utf-8") == "second\n"


def test_write_private_text_creates_directories_and_writes_utf8_lf(tmp_path: Path) -> None:
    directory = tmp_path / "nested" / "exports"

    path = write_private_text(directory, "payload", ".json", "한글 ✓\nnext\n")

    assert path.parent == directory
    assert path.read_bytes() == "한글 ✓\nnext\n".encode()


def test_write_private_text_file_is_private_or_exists_on_windows(tmp_path: Path) -> None:
    path = write_private_text(tmp_path, "payload", ".json", "{}\n")

    if POSIX:
        assert path.stat().st_mode & 0o077 == 0
    else:
        assert path.is_file()


@pytest.mark.parametrize(
    ("stem", "suffix"),
    [
        ("", ".json"),
        (".hidden", ".json"),
        ("..", ".json"),
        ("../payload", ".json"),
        ("nested/payload", ".json"),
        (r"nested\payload", ".json"),
        ("payload", ""),
        ("payload", "json"),
        ("payload", "..json"),
        ("payload", ".json/backup"),
        ("payload", r".json\backup"),
    ],
)
def test_write_private_text_rejects_unsafe_names(
    tmp_path: Path,
    stem: str,
    suffix: str,
) -> None:
    with pytest.raises(ValueError, match="safe filename"):
        write_private_text(tmp_path, stem, suffix, "content")
