"""Contracts for real, local product evidence used by the documentation site."""

from __future__ import annotations

import struct
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCENES = ROOT / "docs" / "assets" / "scenes"
INSTRUCTIONS = ROOT / "docs" / "demo" / "visual-storytelling.md"
AGENT_TAPE = ROOT / "docs" / "demo" / "agent.tape"
DEMO_HARNESS = ROOT / "docs" / "demo" / "demo.py"

PNG_ASSETS = {
    "cockpit-poster.png": (1280, 720, 720),
    "agent-poster.png": (1280, 720, 720),
    "mcp-poster.png": (1280, 690, 730),
    "relationship-graph.png": (1280, 720, 720),
    "diagnosis.png": (1280, 720, 720),
    "merged-logs.png": (1280, 720, 720),
}
MP4_ASSETS = {
    "agent-demo.mp4",
    "mcp-follow-demo.mp4",
    "relationship-demo.mp4",
}


def _png_size(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def test_storytelling_pngs_are_real_readable_terminal_captures() -> None:
    for name, (width, min_height, max_height) in PNG_ASSETS.items():
        path = SCENES / name
        assert path.is_file(), f"{path} is required by the visual narrative"
        actual_width, actual_height = _png_size(path)
        assert actual_width == width
        assert min_height <= actual_height <= max_height
        assert path.stat().st_size <= 900_000


def test_storytelling_motion_assets_are_local_mp4_files_with_a_size_budget() -> None:
    for name in MP4_ASSETS:
        path = SCENES / name
        assert path.is_file()
        payload = path.read_bytes()
        assert payload[4:8] == b"ftyp"
        assert len(payload) <= 3 * 1024 * 1024


def test_storytelling_capture_instructions_name_every_generated_asset() -> None:
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    for name in PNG_ASSETS.keys() | MP4_ASSETS:
        assert f"docs/assets/scenes/{name}" in instructions
    assert "synthetic" in instructions.lower()
    assert "vhs docs/demo/agent.tape" in instructions
    assert "vhs docs/demo/relationships.tape" in instructions
    assert "docs/assets/mcp-follow-demo.gif" in instructions


def test_agent_tape_types_the_real_prompt_and_presses_enter() -> None:
    """The recording must type into the real AgentPanel input, not synthesize it.

    `docs/demo/agent.tape` is the VHS script VHS itself executes; if it never
    types the prompt and sends Enter, no keystrokes ever reach the real
    `#agent-input` widget and the recording would just show a scripted
    response with no evidence of a working input path.
    """
    tape = AGENT_TAPE.read_text(encoding="utf-8")
    prompt_line_index = next(
        i
        for i, line in enumerate(tape.splitlines())
        if 'Type "Why is the payment worker failing?"' in line
    )
    following_lines = tape.splitlines()[prompt_line_index + 1 :]
    assert following_lines, "Enter must follow the typed prompt"
    assert following_lines[0].strip() == "Enter"


def test_demo_harness_never_synthesizes_the_agent_prompt_submission() -> None:
    """The documentation-only harness may auto-open/focus the AgentPanel, but
    it must drive the prompt through real keyboard input recorded by VHS —
    never by calling the message handler or posting the message directly."""
    source = DEMO_HARNESS.read_text(encoding="utf-8")
    assert "AgentPromptSubmitted" not in source
    assert "on_agent_prompt_submitted(" not in source
