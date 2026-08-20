"""README contract for the short MCP follow recording."""

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
ASSET = ROOT / "docs" / "assets" / "mcp-follow-demo.gif"
ASSET_URL = "https://raw.githubusercontent.com/hellices/korvid/main/docs/assets/mcp-follow-demo.gif"


def _require_gif_bytes(payload: bytes, cursor: int, size: int, block: str) -> None:
    if cursor + size > len(payload):
        raise ValueError(f"truncated GIF {block} at offset {cursor} of {len(payload)}")


def _skip_gif_sub_blocks(payload: bytes, cursor: int) -> int:
    while True:
        _require_gif_bytes(payload, cursor, 1, "sub-block length")
        size = payload[cursor]
        cursor += 1
        if size == 0:
            return cursor
        _require_gif_bytes(payload, cursor, size, "sub-block")
        cursor += size


def _read_gif_extension(payload: bytes, cursor: int, delays: list[int]) -> int:
    _require_gif_bytes(payload, cursor, 1, "extension label")
    label = payload[cursor]
    cursor += 1
    if label != 0xF9:
        return _skip_gif_sub_blocks(payload, cursor)

    _require_gif_bytes(payload, cursor, 6, "graphic control extension")
    if payload[cursor] != 4 or payload[cursor + 5] != 0:
        raise ValueError(f"invalid GIF graphic control extension at offset {cursor}")
    delays.append(int.from_bytes(payload[cursor + 2 : cursor + 4], "little"))
    return cursor + 6


def _skip_gif_image(payload: bytes, cursor: int) -> int:
    _require_gif_bytes(payload, cursor, 9, "image descriptor")
    packed = payload[cursor + 8]
    cursor += 9
    if packed & 0x80:
        table_size = 3 * (1 << ((packed & 0x07) + 1))
        _require_gif_bytes(payload, cursor, table_size, "local color table")
        cursor += table_size
    _require_gif_bytes(payload, cursor, 1, "LZW code size")
    return _skip_gif_sub_blocks(payload, cursor + 1)


def _gif_frame_delays_centiseconds(payload: bytes) -> list[int]:
    if payload[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("invalid GIF signature")
    _require_gif_bytes(payload, 0, 13, "logical screen descriptor")
    packed = payload[10]
    cursor = 13
    if packed & 0x80:
        table_size = 3 * (1 << ((packed & 0x07) + 1))
        _require_gif_bytes(payload, cursor, table_size, "global color table")
        cursor += table_size

    delays: list[int] = []
    while cursor < len(payload):
        introducer = payload[cursor]
        cursor += 1
        if introducer == 0x3B:
            break
        if introducer == 0x21:
            cursor = _read_gif_extension(payload, cursor, delays)
            continue
        if introducer == 0x2C:
            cursor = _skip_gif_image(payload, cursor)
            continue
        raise ValueError(
            f"unexpected GIF block introducer 0x{introducer:02x} at offset {cursor - 1}"
        )
    if not delays:
        raise ValueError("GIF contains no frame delays")
    return delays


def _gif_duration_centiseconds(payload: bytes) -> int:
    return sum(_gif_frame_delays_centiseconds(payload))


def test_readme_embeds_mcp_follow_demo() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "## Watch MCP follow" in readme
    assert f"]({ASSET_URL})" in readme
    assert "**One prompt. Korvid follows.**" in readme
    section = readme.split("## Watch MCP follow", 1)[1].split("## Status", 1)[0]
    assert section.index("<details open>") < section.index(ASSET_URL)
    assert section.index(ASSET_URL) < section.index("</details>")


def test_recording_tape_owns_prompt_entry() -> None:
    runbook = (ROOT / "docs" / "demo" / "mcp-follow.md").read_text()
    tape = (ROOT / "docs" / "demo" / "mcp-follow.tape").read_text()
    prompt = (
        "Use korvid MCP in order: list_resources shop pods → "
        "get_logs unhealthy one → helm_list_releases."
    )

    assert "Leave the Copilot pane focused at its empty prompt" in runbook
    assert "Do not enter the scenario prompt yourself" in runbook
    assert f'Type "{prompt}"' in tape


def test_recording_runbook_only_deletes_namespace_it_created() -> None:
    runbook = (ROOT / "docs" / "demo" / "mcp-follow.md").read_text()

    assert "Refusing to reuse existing namespace: shop" in runbook
    assert runbook.count("kind-*|k3d-*|minikube|docker-desktop") == 2
    assert "kubectl delete namespace shop --ignore-not-found" in runbook


def test_gif_duration_ignores_marker_bytes_inside_image_data() -> None:
    header = b"GIF89a\x01\x00\x01\x00\x00\x00\x00"
    real_control = b"\x21\xf9\x04\x00\x05\x00\x00\x00"
    image_descriptor = b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00"
    fake_control = b"\x21\xf9\x04\x00\xff\x7f\x00\x00"
    image_data = b"\x02\x08" + fake_control + b"\x00"

    assert (
        _gif_duration_centiseconds(header + real_control + image_descriptor + image_data + b"\x3b")
        == 5
    )


def test_gif_parser_reports_truncated_sub_block() -> None:
    payload = b"GIF89a\x01\x00\x01\x00\x00\x00\x00\x21\xfe\x04ab"

    with pytest.raises(ValueError, match=r"truncated GIF sub-block at offset \d+"):
        _gif_duration_centiseconds(payload)


def test_mcp_follow_demo_asset_fits_readme_budget() -> None:
    payload = ASSET.read_bytes()
    assert payload[:6] in {b"GIF87a", b"GIF89a"}
    assert int.from_bytes(payload[6:8], "little") == 1280
    delays = _gif_frame_delays_centiseconds(payload)
    assert min(delays) >= 6
    assert sum(delays) <= 1500
    assert len(payload) <= 8 * 1024 * 1024
