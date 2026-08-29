"""README contract for the short MCP follow animation."""

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ASSET = ROOT / "docs" / "assets" / "mcp-follow-demo.gif"
SOURCE_CLIP = ROOT / "docs" / "assets" / "scenes" / "mcp-follow-demo.mp4"
ASSET_URL = "https://raw.githubusercontent.com/hellices/korvid/main/docs/assets/mcp-follow-demo.gif"
MAX_DURATION_CS = 1500
#: `docs/assets/scenes/mcp-follow-demo.mp4` runs 13.76s. The README GIF is a
#: palette-quantised copy of that approved capture, so it must tell the same
#: story end to end rather than a reframed excerpt of some other recording.
CLIP_DURATION_CS = 1376
DURATION_TOLERANCE_CS = 40
#: The clip's stored box, which the GIF inherits because nothing rescales it.
CLIP_SIZE = (1280, 710)
MAX_BYTES = 8 * 1024 * 1024
#: The retained, script-free derivation recipe for the reviewed README GIF.
REGENERATION_COMMAND = (
    "ffmpeg -y -i docs/assets/scenes/mcp-follow-demo.mp4 "
    '-lavfi "fps=12.5,split[a][b];'
    "[a]palettegen=max_colors=256:stats_mode=diff[p];"
    '[b][p]paletteuse=dither=none:diff_mode=rectangle" '
    "-loop 0 docs/assets/mcp-follow-demo.gif"
)
#: A derivation recipe only proves what a reader reproduces — it cannot stop a
#: different recording being dropped in at this path. This digest pins the
#: frames reviewed for leaked paths, branches, hostnames, model names and
#: token spend.
REVIEWED_SHA256 = "fc6a7022aa8068152e803fb6ab21533488e4a0aee09f9f91fddb69049b5a36a6"
#: The reviewed MP4 bytes from which the GIF above was generated. Pinning both
#: sides prevents a same-duration, same-size replacement clip from silently
#: changing the landing story while the README keeps the old reviewed frames.
REVIEWED_SOURCE_SHA256 = "67134ed7e6e33478087b6cb80d0c602c11fd31ab3fc82c1810563e631c1cd078"


def test_regeneration_recipe_derives_the_gif_from_the_reviewed_mp4() -> None:
    assert "docs/assets/scenes/mcp-follow-demo.mp4" in REGENERATION_COMMAND
    assert "palettegen=max_colors=256" in REGENERATION_COMMAND
    assert "paletteuse=dither=none:diff_mode=rectangle" in REGENERATION_COMMAND
    assert REGENERATION_COMMAND.endswith("-loop 0 docs/assets/mcp-follow-demo.gif")


def _require_bytes(payload: bytes, cursor: int, size: int, block: str) -> None:
    if cursor + size > len(payload):
        raise ValueError(f"truncated GIF {block} at offset {cursor} of {len(payload)}")


def _skip_sub_blocks(payload: bytes, cursor: int) -> int:
    while True:
        _require_bytes(payload, cursor, 1, "sub-block length")
        size = payload[cursor]
        cursor += 1
        if size == 0:
            return cursor
        _require_bytes(payload, cursor, size, "sub-block")
        cursor += size


def _read_extension(payload: bytes, cursor: int) -> tuple[int, int | None]:
    _require_bytes(payload, cursor, 1, "extension label")
    label = payload[cursor]
    cursor += 1
    if label != 0xF9:
        return _skip_sub_blocks(payload, cursor), None
    _require_bytes(payload, cursor, 6, "graphic control extension")
    if payload[cursor] != 4 or payload[cursor + 5] != 0:
        raise ValueError(f"invalid GIF graphic control extension at offset {cursor}")
    return cursor + 6, int.from_bytes(payload[cursor + 2 : cursor + 4], "little")


def _skip_image(payload: bytes, cursor: int) -> int:
    _require_bytes(payload, cursor, 9, "image descriptor")
    packed = payload[cursor + 8]
    cursor += 9
    if packed & 0x80:
        table_size = 3 * (1 << ((packed & 0x07) + 1))
        _require_bytes(payload, cursor, table_size, "local color table")
        cursor += table_size
    _require_bytes(payload, cursor, 1, "LZW code size")
    return _skip_sub_blocks(payload, cursor + 1)


def _frame_delays(payload: bytes) -> list[int]:
    if payload[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("invalid GIF signature")
    _require_bytes(payload, 0, 13, "logical screen descriptor")
    cursor = 13
    packed = payload[10]
    if packed & 0x80:
        cursor += 3 * (1 << ((packed & 0x07) + 1))

    delays: list[int] = []
    pending_delay: int | None = None
    while cursor < len(payload):
        introducer = payload[cursor]
        cursor += 1
        if introducer == 0x3B:
            if cursor != len(payload):
                raise ValueError(f"trailing bytes after GIF trailer at offset {cursor}")
            if not delays:
                raise ValueError("GIF contains no frames")
            return delays
        if introducer == 0x21:
            cursor, delay = _read_extension(payload, cursor)
            if delay is not None:
                pending_delay = delay
            continue
        if introducer == 0x2C:
            delays.append(0 if pending_delay is None else pending_delay)
            pending_delay = None
            cursor = _skip_image(payload, cursor)
            continue
        raise ValueError(f"unexpected GIF block introducer 0x{introducer:02x}")
    raise ValueError("missing GIF trailer")


def _effective_fps(delays: list[int]) -> float:
    duration = sum(delays)
    if duration <= 0:
        raise ValueError("GIF has no positive frame delays")
    return len(delays) * 100 / duration


def test_readme_embeds_mcp_follow_demo() -> None:
    """The README's animation and the copy around it must match the capture.

    Round-8 review: the shipped GIF was an unrelated third-party session
    whose right-hand pane carried that host's working directory, branch,
    token spend and model name. It is replaced by a copy of
    `docs/assets/scenes/mcp-follow-demo.mp4` — this repository's own MCP SDK
    client calling four read-only tools against the synthetic in-memory
    fixture — so the copy may no longer credit an external assistant or a
    disposable cluster, and must name the read-only sequence it shows.
    """
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Watch MCP follow", 1)[1].split("## Status", 1)[0]
    lowered = section.lower()

    assert "**One client. Korvid follows.**" in section
    assert f"]({ASSET_URL})" in section
    assert section.index("<details open>") < section.index(ASSET_URL)
    assert section.index(ASSET_URL) < section.index("</details>")

    for stale in ("github copilot", "copilot cli", "disposable", "one prompt"):
        assert stale not in lowered, (
            f"the capture is korvid's own MCP SDK client against a synthetic "
            f"fixture; the README may not still claim {stale!r}"
        )
    assert "mcp sdk client" in lowered, "the copy must name what actually drove the calls"
    assert "synthetic" in lowered, "the copy must name the synthetic in-memory fixture"
    assert "read-only" in lowered, "the recorded sequence is read-only; say so"
    for tool in ("list_resources", "diagnose_pod", "get_logs", "helm_list_releases"):
        assert tool in section, f"the follow sequence must name {tool}"
    assert "no external client session metadata" in lowered, (
        "the README must state the privacy property the new capture has and the "
        "replaced one did not"
    )


def test_gif_parser_ignores_marker_bytes_inside_image_data() -> None:
    header = b"GIF89a\x01\x00\x01\x00\x00\x00\x00"
    control = b"\x21\xf9\x04\x00\x05\x00\x00\x00"
    descriptor = b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    image_data = b"\x02\x08\x21\xf9\x04\x00\xff\x7f\x00\x00\x00"

    assert _frame_delays(header + control + descriptor + image_data + b"\x3b") == [5]


def test_gif_parser_reports_truncated_data() -> None:
    payload = b"GIF89a\x01\x00\x01\x00\x00\x00\x00\x21\xfe\x04ab"

    with pytest.raises(ValueError, match=r"truncated GIF sub-block at offset \d+"):
        _frame_delays(payload)


def test_gif_parser_requires_clean_trailer() -> None:
    payload = b"GIF89a\x01\x00\x01\x00\x00\x00\x00"

    with pytest.raises(ValueError, match="missing GIF trailer"):
        _frame_delays(payload)


def test_mcp_follow_demo_asset_fits_readme_budget() -> None:
    """The GIF must be the approved clip's whole story, at a readable rate.

    Its geometry and running time are the clip's own — anything else means
    the README is showing a different recording than the one the landing
    page and `docs/demo/visual-storytelling.md` document.
    """
    payload = ASSET.read_bytes()
    delays = _frame_delays(payload)
    size = (
        int.from_bytes(payload[6:8], "little"),
        int.from_bytes(payload[8:10], "little"),
    )
    effective_fps = _effective_fps(delays)
    total_cs = sum(delays)

    assert SOURCE_CLIP.is_file(), "the GIF is derived from the checked-in MP4"
    assert size == CLIP_SIZE, (
        f"the GIF stores {size}, not the approved clip's {CLIP_SIZE}; it must be "
        "derived from docs/assets/scenes/mcp-follow-demo.mp4 without rescaling"
    )
    assert abs(total_cs - CLIP_DURATION_CS) <= DURATION_TOLERANCE_CS, (
        f"the GIF runs {total_cs / 100:.2f}s; the approved four-read follow story "
        f"runs {CLIP_DURATION_CS / 100:.2f}s and the GIF must carry all of it"
    )
    assert total_cs <= MAX_DURATION_CS
    assert min(delays) >= 2
    assert 150 <= len(delays) <= 220, (
        f"{len(delays)} frames cannot hold a {CLIP_DURATION_CS / 100:.2f}s story at "
        "a readable 12-15 fps"
    )
    assert 11.5 <= effective_fps <= 15.5, effective_fps
    assert len(payload) <= MAX_BYTES


def test_shipped_gif_is_the_reviewed_artifact() -> None:
    """A README asset is public evidence; pin the bytes that were reviewed.

    Regenerating with the documented command is expected — when it happens,
    re-review the frames for leaked paths, branches, hostnames, model names
    and token spend, then update this digest in the same change.
    """
    digest = hashlib.sha256(ASSET.read_bytes()).hexdigest()
    assert digest == REVIEWED_SHA256, (
        f"docs/assets/mcp-follow-demo.gif is {digest}, not the reviewed "
        f"{REVIEWED_SHA256}; re-review its frames for external client session "
        "metadata before updating this pin"
    )


def test_source_clip_is_the_reviewed_gif_source() -> None:
    digest = hashlib.sha256(SOURCE_CLIP.read_bytes()).hexdigest()

    assert digest == REVIEWED_SOURCE_SHA256, (
        f"{SOURCE_CLIP} is not the reviewed GIF source: got {digest}. "
        "Re-review the MP4 and derived GIF together before updating both pins"
    )


def test_readme_animation_length_claim_matches_the_shipped_gif() -> None:
    """The disclosure summary states a length; the asset has to honour it."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Watch MCP follow", 1)[1].split("## Status", 1)[0]
    claimed = re.search(r"(\d+)-second MCP follow animation", section)
    assert claimed is not None, (
        "the collapsible summary must keep stating how long the animation runs"
    )

    seconds = sum(_frame_delays(ASSET.read_bytes())) / 100
    promised = int(claimed.group(1))
    assert seconds <= promised, f"the GIF runs {seconds:.2f}s but the README promises {promised}s"
    assert promised - seconds < 1, (
        f"the README promises {promised}s for a {seconds:.2f}s animation; round to the "
        "length a visitor actually waits"
    )
