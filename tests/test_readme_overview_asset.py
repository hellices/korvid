"""Contracts for the reviewed, 28-second HyperFrames README overview."""

from __future__ import annotations

import hashlib
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ASSETS = ROOT / "docs" / "assets"
GIF_URL = "https://raw.githubusercontent.com/hellices/korvid/main/docs/assets/overview.gif"
MP4_URL = "https://hellices.github.io/korvid/assets/overview.mp4"

# Pin the approved pair, not just their dimensions: changes to these bytes
# require reviewing the footage and the synthetic/scripted disclosures again.
REVIEWED_MEDIA = (
    (
        "overview.gif",
        "3fd8eeaab06d6e46a5f0aca1fa8ffc302a067755fa42fbd8dc04b050cce419ee",
        4_000_000,
    ),
    (
        "overview.mp4",
        "aa6975e2f5a213a4225bec19b4a0de2ad9bde2298c767f043c2c577672b54f20",
        7_000_000,
    ),
)


def _readme_hero() -> str:
    return (ROOT / "README.md").read_text(encoding="utf-8").split("## Why korvid", 1)[0]


def test_readme_overview_thumbnail_links_to_hd_video() -> None:
    hero = _readme_hero()
    thumbnail = re.search(
        r"\[!\[(?P<alt>[^\]]+)\]\((?P<image>[^)]+)\)\]\((?P<video>[^)]+)\)",
        hero,
    )

    assert thumbnail is not None, "the overview GIF must be a clickable MP4 preview"
    assert thumbnail.group("image") == GIF_URL
    assert thumbnail.group("video") == MP4_URL
    for driver in ("keyboard", "agent", "mcp"):
        assert driver in thumbnail.group("alt").lower()
    assert (
        "(https://raw.githubusercontent.com/hellices/korvid/main/docs/assets/demo.gif)" not in hero
    )


def test_readme_overview_has_a_text_link_and_truthful_disclosures() -> None:
    hero = _readme_hero()
    text_link = f"[Watch the 28-second overview in HD (MP4)]({MP4_URL})"
    assert text_link in hero

    lowered = " ".join(hero.lower().split())
    for disclosure in (
        "real tui",
        "synthetic cluster",
        "edited excerpts",
        "scripted model response",
        "real read tools",
        "local sdk client",
        "streamable http",
        "read-only",
        "approval policy",
        "no cluster write is shown",
    ):
        assert disclosure in lowered, f"the overview caption must disclose {disclosure!r}"


@pytest.mark.parametrize(("name", "digest", "max_bytes"), REVIEWED_MEDIA)
def test_overview_media_matches_the_reviewed_artifact(
    name: str, digest: str, max_bytes: int
) -> None:
    asset = ASSETS / name
    assert asset.is_file(), f"missing approved overview asset: {asset}"
    payload = asset.read_bytes()

    assert len(payload) <= max_bytes, f"{name} exceeds its README delivery budget"
    assert hashlib.sha256(payload).hexdigest() == digest, (
        f"{name} is not the approved 28-second overview; review both media files "
        "and their README disclosures before updating the digest"
    )


def test_overview_gif_keeps_the_approved_readme_resolution() -> None:
    asset = ASSETS / "overview.gif"
    assert asset.is_file(), "the README needs the approved 960px GIF, not the larger render"
    payload = asset.read_bytes()

    assert payload[:6] in {b"GIF87a", b"GIF89a"}
    assert struct.unpack("<HH", payload[6:10]) == (960, 540)


def test_superseded_readme_gif_is_not_shipped() -> None:
    assert not (ASSETS / "demo.gif").exists()
