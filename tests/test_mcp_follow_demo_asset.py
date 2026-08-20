"""README contract for the short MCP follow recording."""

import re
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


def _read_gif_extension(payload: bytes, cursor: int) -> tuple[int, int | None]:
    _require_gif_bytes(payload, cursor, 1, "extension label")
    label = payload[cursor]
    cursor += 1
    if label != 0xF9:
        return _skip_gif_sub_blocks(payload, cursor), None

    _require_gif_bytes(payload, cursor, 6, "graphic control extension")
    if payload[cursor] != 4 or payload[cursor + 5] != 0:
        raise ValueError(f"invalid GIF graphic control extension at offset {cursor}")
    delay = int.from_bytes(payload[cursor + 2 : cursor + 4], "little")
    return cursor + 6, delay


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


def _read_gif_block(
    payload: bytes,
    cursor: int,
    pending_delay: int | None,
    delays: list[int],
) -> tuple[int, int | None, bool]:
    introducer_offset = cursor
    introducer = payload[cursor]
    cursor += 1
    if introducer == 0x3B:
        if pending_delay is not None:
            raise ValueError("GIF frame delay has no image")
        return cursor, None, True
    if introducer == 0x21:
        cursor, delay = _read_gif_extension(payload, cursor)
        if delay is None:
            return cursor, pending_delay, False
        if pending_delay is not None:
            raise ValueError(f"multiple GIF frame delays before image at offset {cursor}")
        return cursor, delay, False
    if introducer == 0x2C:
        delays.append(0 if pending_delay is None else pending_delay)
        return _skip_gif_image(payload, cursor), None, False
    raise ValueError(
        f"unexpected GIF block introducer 0x{introducer:02x} at offset {introducer_offset}"
    )


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
    pending_delay: int | None = None
    saw_trailer = False
    while cursor < len(payload):
        cursor, pending_delay, saw_trailer = _read_gif_block(payload, cursor, pending_delay, delays)
        if saw_trailer:
            break
    if not saw_trailer:
        raise ValueError("missing GIF trailer")
    if cursor != len(payload):
        raise ValueError(f"trailing bytes after GIF trailer at offset {cursor}")
    if not delays:
        raise ValueError("GIF contains no frame delays")
    return delays


def _gif_duration_centiseconds(payload: bytes) -> int:
    return sum(_gif_frame_delays_centiseconds(payload))


def _gif_effective_frame_rate(delays: list[int]) -> float:
    total = sum(delays)
    if total <= 0:
        raise ValueError("GIF has no positive frame delays")
    return len(delays) * 100 / total


def test_readme_embeds_mcp_follow_demo() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Watch MCP follow" in readme
    assert f"]({ASSET_URL})" in readme
    assert "**One prompt. Korvid follows.**" in readme
    section = readme.split("## Watch MCP follow", 1)[1].split("## Status", 1)[0]
    assert section.index("<details open>") < section.index(ASSET_URL)
    assert section.index(ASSET_URL) < section.index("</details>")
    assert "Show or hide the up-to-15-second MCP follow animation" in section


def test_recording_tape_owns_prompt_entry() -> None:
    runbook = (ROOT / "docs" / "demo" / "mcp-follow.md").read_text(encoding="utf-8")
    tape = (ROOT / "docs" / "demo" / "mcp-follow.tape").read_text(encoding="utf-8")
    prompt = (
        "Use korvid MCP in order: list_resources shop pods → "
        "get_logs unhealthy one → helm_list_releases."
    )

    assert "Leave the Copilot pane focused at its empty prompt" in runbook
    assert "Do not enter the scenario prompt yourself" in runbook
    assert "Start the visible capture with Enter" not in runbook
    assert f'Type "{prompt}"' in tape
    assert runbook.index("tmux new-session") < runbook.index("korvid --mcp")
    assert "tmux select-pane -t korvid-mcp-demo:0.1" in runbook
    assert "Sleep 45s" in tape
    assert "--available-tools=korvid-mcp-demo" in runbook
    assert "mcp-demo-registration" in runbook
    assert "Failed to write the recording MCP marker" in runbook
    assert runbook.index("mcp-demo-registration") < runbook.index("copilot mcp add")
    assert "tmux kill-session -t korvid-mcp-demo" in runbook
    assert "Refusing to reuse existing tmux session: korvid-mcp-demo" in runbook
    assert "if ! tmux new-session" in runbook
    assert 'if ! copilot mcp remove "$recording_server"; then' in runbook
    assert runbook.index("copilot mcp remove") < runbook.index("V1Preconditions")
    assert "helm uninstall" not in runbook
    for variable in ("idle_start", "idle_end", "demo_end"):
        assert re.search(rf"^{variable}=\d+(?:\.\d+)?$", runbook, re.MULTILINE)
        assert f"${{{variable}}}" in runbook
    assert "Output docs/assets/mcp-follow-demo.raw.gif" in tape
    assert "ffmpeg -y -i docs/assets/mcp-follow-demo.raw.gif" in runbook
    assert "Invalid trim timestamps" in runbook
    assert "Trimmed duration exceeds the 15s budget" in runbook


def test_recording_runbook_only_deletes_namespace_it_created() -> None:
    runbook = (ROOT / "docs" / "demo" / "mcp-follow.md").read_text(encoding="utf-8")

    assert "Refusing to reuse existing namespace: shop" in runbook
    assert "mcp-demo-context" in runbook
    assert "mcp-demo-kubeconfig" in runbook
    assert "mcp-demo-cluster-uid" in runbook
    assert "mcp-demo-namespace-uid" in runbook
    assert "Failed to create the demo state directory" in runbook
    assert "Failed to write the demo context marker" in runbook
    assert 'test "$cluster_uid" = "$prepared_cluster_uid"' in runbook
    assert '--context "$prepared_context"' in runbook
    assert "korvid.dev/demo=mcp-follow" in runbook
    assert "create namespace shop; then" in runbook
    assert "label namespace shop korvid.dev/demo=mcp-follow; then" in runbook
    assert "if ! helm upgrade --install shop-demo" in runbook
    assert "Failed to install the recording release" in runbook
    assert "Run the cleanup section before retrying" in runbook
    assert "-n shop get pods --watch" in runbook
    assert '--context "$context" -n shop get events' in runbook
    assert "Refusing MCP startup after cluster identity changed" in runbook
    assert "Refusing MCP startup after namespace identity changed" in runbook
    assert "Refusing MCP startup because namespace ownership changed" in runbook
    assert "Failed to create the isolated config directory" in runbook
    assert "Failed to write the isolated korvid config" in runbook
    assert "kube_context: %s" in runbook
    assert 'HOME=\\"$demo_home\\"' in runbook
    assert 'XDG_CONFIG_HOME=\\"$demo_home/.config\\"' in runbook
    assert '--kubeconfig "$demo_kubeconfig"' in runbook
    assert "Recording namespace is already absent; continuing cleanup" in runbook
    assert "V1Preconditions(" in runbook
    assert 'uid=os.environ["DEMO_NAMESPACE_UID"]' in runbook
    assert "from kubernetes_asyncio import client, config" in runbook
    assert "asyncio.run(delete_namespace())" in runbook
    assert "from kubernetes import client, config" not in runbook
    assert "delete namespace shop --ignore-not-found" in runbook


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


def test_gif_parser_requires_trailer() -> None:
    payload = b"GIF89a\x01\x00\x01\x00\x00\x00\x00\x21\xf9\x04\x00\x05\x00\x00\x00"

    with pytest.raises(ValueError, match="missing GIF trailer"):
        _gif_duration_centiseconds(payload)


def test_gif_parser_rejects_bytes_after_trailer() -> None:
    payload = (
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00"
        b"\x21\xf9\x04\x00\x05\x00\x00\x00"
        b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00"
        b"\x02\x01\x00\x00\x3b\x00"
    )

    with pytest.raises(ValueError, match=r"trailing bytes after GIF trailer at offset \d+"):
        _gif_duration_centiseconds(payload)


def test_gif_parser_records_zero_for_image_without_frame_delay() -> None:
    payload = (
        b"GIF89a\x01\x00\x01\x00\x00\x00\x00"
        b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00"
        b"\x02\x01\x00\x00\x3b"
    )

    assert _gif_frame_delays_centiseconds(payload) == [0]


def test_gif_effective_frame_rate_uses_encoded_delays() -> None:
    assert _gif_effective_frame_rate([8, 9, 8]) == pytest.approx(12.0)


def test_gif_effective_frame_rate_rejects_zero_delays() -> None:
    with pytest.raises(ValueError, match="GIF has no positive frame delays"):
        _gif_effective_frame_rate([0, 0])


def test_mcp_follow_demo_asset_fits_readme_budget() -> None:
    payload = ASSET.read_bytes()
    assert payload[:6] in {b"GIF87a", b"GIF89a"}
    assert int.from_bytes(payload[6:8], "little") == 1280
    delays = _gif_frame_delays_centiseconds(payload)
    assert min(delays) >= 6
    assert sum(delays) <= 1500
    assert 12 <= _gif_effective_frame_rate(delays) <= 15
    assert len(payload) <= 8 * 1024 * 1024
