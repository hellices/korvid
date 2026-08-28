"""Contracts for media that the public documentation actually renders."""

from __future__ import annotations

import struct
from pathlib import Path

ROOT = Path(__file__).parents[1]
DOCS = ROOT / "docs"
SCENES = DOCS / "assets" / "scenes"

PUBLIC_SCENES = {
    "agent-demo.mp4",
    "agent-poster.png",
    "cockpit-poster.png",
    "mcp-follow-demo.mp4",
    "mcp-poster.png",
    "relationship-graph.png",
}
PNG_GEOMETRY = {
    "agent-poster.png": (1280, 720),
    "cockpit-poster.png": (1280, 720),
    "mcp-poster.png": (1280, 710),
    "relationship-graph.png": (1280, 720),
}


def _png_size(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    assert payload[12:16] == b"IHDR"
    return struct.unpack(">II", payload[16:24])


def test_only_publicly_rendered_scene_media_is_checked_in() -> None:
    assert {
        path.name for path in SCENES.iterdir() if not path.name.startswith(".")
    } == PUBLIC_SCENES


def test_public_scene_media_is_referenced_by_operator_facing_pages() -> None:
    sources = [
        ROOT / "README.md",
        ROOT / "mkdocs.yml",
        *(
            source
            for source in DOCS.rglob("*.md")
            if not {"superpowers", "dev"}.intersection(source.relative_to(DOCS).parts)
        ),
    ]
    text = "\n".join(source.read_text(encoding="utf-8") for source in sources)

    for name in PUBLIC_SCENES:
        assert name in text, f"{name} is checked in but no public page renders it"


def test_public_scene_media_keeps_reviewed_geometry_and_budget() -> None:
    for name, expected in PNG_GEOMETRY.items():
        assert _png_size(SCENES / name) == expected

    for name in ("agent-demo.mp4", "mcp-follow-demo.mp4"):
        payload = (SCENES / name).read_bytes()
        assert payload[4:8] == b"ftyp"
        assert len(payload) <= 3 * 1024 * 1024


def test_recording_toolchain_is_not_shipped_with_the_docs() -> None:
    assert not (DOCS / "demo").exists()
