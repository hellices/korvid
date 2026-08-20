"""README contract for the short MCP follow animation."""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ASSET = ROOT / "docs" / "assets" / "mcp-follow-demo.gif"
ASSET_URL = "https://raw.githubusercontent.com/hellices/korvid/main/docs/assets/mcp-follow-demo.gif"
MAX_DURATION_CS = 1500
MAX_BYTES = 8 * 1024 * 1024


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
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Watch MCP follow", 1)[1].split("## Status", 1)[0]

    assert "**One prompt. Korvid follows.**" in section
    assert f"]({ASSET_URL})" in section
    assert section.index("<details open>") < section.index(ASSET_URL)
    assert section.index(ASSET_URL) < section.index("</details>")


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
    payload = ASSET.read_bytes()
    delays = _frame_delays(payload)
    height = int.from_bytes(payload[8:10], "little")
    effective_fps = _effective_fps(delays)

    assert int.from_bytes(payload[6:8], "little") == 1280
    assert 690 <= height <= 730
    assert 800 <= sum(delays) <= MAX_DURATION_CS
    assert len(delays) >= 90
    assert 11.5 <= effective_fps <= 15.5, effective_fps
    assert len(payload) <= MAX_BYTES
