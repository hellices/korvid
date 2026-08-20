"""README contract for the short MCP follow recording."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
ASSET = ROOT / "docs" / "assets" / "mcp-follow-demo.gif"
ASSET_URL = "https://raw.githubusercontent.com/hellices/korvid/main/docs/assets/mcp-follow-demo.gif"


def _gif_duration_centiseconds(payload: bytes) -> int:
    marker = b"\x21\xf9\x04"
    cursor = 0
    duration = 0
    frames = 0
    while (start := payload.find(marker, cursor)) >= 0:
        end = start + 8
        if end <= len(payload) and payload[end - 1] == 0:
            duration += int.from_bytes(payload[start + 4 : start + 6], "little")
            frames += 1
            cursor = end
        else:
            cursor = start + 1
    assert frames > 0
    return duration


def test_readme_embeds_mcp_follow_demo() -> None:
    readme = (ROOT / "README.md").read_text()
    assert "## Watch MCP follow" in readme
    assert f"]({ASSET_URL})" in readme
    assert "**One prompt. Korvid follows.**" in readme


def test_mcp_follow_demo_asset_fits_readme_budget() -> None:
    payload = ASSET.read_bytes()
    assert payload[:6] in {b"GIF87a", b"GIF89a"}
    assert int.from_bytes(payload[6:8], "little") == 1280
    assert _gif_duration_centiseconds(payload) <= 1500
    assert len(payload) <= 8 * 1024 * 1024
