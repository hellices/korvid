"""README contract for the short MCP follow recording."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
ASSET = ROOT / "docs" / "assets" / "mcp-follow-demo.gif"
ASSET_URL = "https://raw.githubusercontent.com/hellices/korvid/main/docs/assets/mcp-follow-demo.gif"


def _skip_gif_sub_blocks(payload: bytes, cursor: int) -> int:
    while True:
        assert cursor < len(payload)
        size = payload[cursor]
        cursor += 1
        if size == 0:
            return cursor
        cursor += size
        assert cursor <= len(payload)


def _gif_duration_centiseconds(payload: bytes) -> int:
    assert payload[:6] in {b"GIF87a", b"GIF89a"}
    assert len(payload) >= 13
    packed = payload[10]
    cursor = 13
    if packed & 0x80:
        cursor += 3 * (1 << ((packed & 0x07) + 1))

    duration = 0
    frames = 0
    while cursor < len(payload):
        introducer = payload[cursor]
        cursor += 1
        if introducer == 0x3B:
            break
        if introducer == 0x21:
            assert cursor < len(payload)
            label = payload[cursor]
            cursor += 1
            if label == 0xF9:
                assert cursor + 6 <= len(payload)
                assert payload[cursor] == 4
                assert payload[cursor + 5] == 0
                duration += int.from_bytes(payload[cursor + 2 : cursor + 4], "little")
                cursor += 6
                frames += 1
            else:
                cursor = _skip_gif_sub_blocks(payload, cursor)
            continue
        if introducer == 0x2C:
            assert cursor + 9 <= len(payload)
            packed = payload[cursor + 8]
            cursor += 9
            if packed & 0x80:
                cursor += 3 * (1 << ((packed & 0x07) + 1))
            assert cursor < len(payload)
            cursor += 1
            cursor = _skip_gif_sub_blocks(payload, cursor)
            continue
        raise AssertionError(f"unexpected GIF block introducer: 0x{introducer:02x}")
    assert frames > 0
    return duration


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


def test_mcp_follow_demo_asset_fits_readme_budget() -> None:
    payload = ASSET.read_bytes()
    assert payload[:6] in {b"GIF87a", b"GIF89a"}
    assert int.from_bytes(payload[6:8], "little") == 1280
    assert _gif_duration_centiseconds(payload) <= 1500
    assert len(payload) <= 8 * 1024 * 1024
