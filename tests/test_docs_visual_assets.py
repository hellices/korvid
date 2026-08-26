"""Contracts for real, local product evidence used by the documentation site."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import importlib.util
import math
import os
import re
import struct
import subprocess
import sys
import tomllib
import zlib
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

from korvid.agent.events import AgentEvent, TextDelta, ToolCallStarted, TurnComplete
from korvid.core.mcp import MCPControllerBase
from korvid.core.relationships import GraphResource, SummaryLike
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import format_age
from korvid.tools.structured import ERROR_PREFIX
from korvid.ui.relationship_controller import RelationshipSnapshotLoader, graph_source_metas

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
SCENES = ROOT / "docs" / "assets" / "scenes"
DEMO_DIR = ROOT / "docs" / "demo"
INSTRUCTIONS = DEMO_DIR / "visual-storytelling.md"
LANDING = DOCS / "index.md"
EXTRA_CSS = DOCS / "stylesheets" / "extra.css"
AGENT_TAPE = DEMO_DIR / "agent.tape"
AGENT_PAGE = DOCS / "agent.md"
MCP_PAGE = DOCS / "mcp.md"
DEMO_HARNESS = DEMO_DIR / "demo.py"
VISUAL_STORYTELLING_PLAN = DOCS / "superpowers" / "plans" / "2026-08-22-visual-storytelling.md"
LANDING_VIDEO_PLAN = DOCS / "superpowers" / "plans" / "2026-08-26-landing-video-experience.md"
LANDING_VIDEO_DESIGN = (
    DOCS / "superpowers" / "specs" / "2026-08-26-landing-video-experience-design.md"
)
AGENT_STORY = DEMO_DIR / "agent_story.py"
AGENT_PANEL = ROOT / "src" / "korvid" / "ui" / "widgets" / "agent_panel.py"
#: The synthetic label the harness hands `KorvidApp` for the Agent scene, and
#: therefore the label the shipped frame renders in the panel header.
DEMO_MODEL_LABEL = "korvid-demo"
_MARKDOWN_FENCE = re.compile(
    r"^(?P<fence>`{3,}|~{3,}).*?^(?P=fence)",
    re.DOTALL | re.MULTILINE,
)
_MARKDOWN_INLINE_CODE = re.compile(r"(?P<ticks>`+)[^\n]*?(?P=ticks)")
_EXCLUDED_DOC_PREFIXES = ("overrides/", "dev/plans/", "superpowers/")

#: Every tape that starts the TUI through `uv run` before it records.
TUI_TAPES = ("demo.tape", "agent.tape", "relationships.tape")
#: The cold-start allowance each of them hides behind `Hide`. `uv run` may
#: resolve and install the project on a cold cache before the TUI paints its
#: first frame, and any keystroke sent in the meantime lands in the shell.
COLD_START_SLEEP = "Sleep 20s"
#: Where that reason is written down once, instead of in three tapes.
COLD_START_REFERENCE = "docs/demo/visual-storytelling.md"

#: The synthetic pod the relationship tape opens the graph on.
DEMO_ROOT = GraphResource(
    group="",
    kind="Pod",
    namespace="shop",
    name="payment-worker-6c9f7d-b3xnq",
    uid="pod-payment",
)

PNG_ASSETS = {
    "cockpit-poster.png": (1280, 720, 720),
    "agent-poster.png": (1280, 720, 720),
    "mcp-poster.png": (1280, 710, 710),
    "relationship-graph.png": (1280, 720, 720),
    "diagnosis.png": (1280, 720, 720),
    "merged-logs.png": (1280, 720, 720),
}
MP4_ASSETS = {
    "agent-demo.mp4",
    "mcp-follow-demo.mp4",
    "relationship-demo.mp4",
}
#: The pixel box each clip must both store and display. A browser lays a
#: `<video>` out at its *display* size — stored size scaled by the sample
#: aspect ratio — while the landing CSS reserves a box from the stored size,
#: so a non-square SAR silently makes the two disagree and pillarboxes the
#: media inside its own reservation.
MP4_GEOMETRY = {
    "agent-demo.mp4": (1280, 720),
    "mcp-follow-demo.mp4": (1280, 710),
    "relationship-demo.mp4": (1280, 720),
}
#: The MCP capture is a two-pane recording, split by the divider column the
#: tape's `-p 45` puts at x=704: korvid's TUI to its left, the external MCP
#: client to its right. Both columns are named because the clip's whole
#: claim is that the two agree.
MCP_TUI_PANE = (0, 700)
MCP_CLIENT_PANE = (708, 1280)
#: The reproducible MCP follow capture: a real `KorvidMCPServer` on
#: `MCP_DEMO_URL`, driven by a clean MCP SDK client, recorded beside the TUI
#: it makes follow.
MCP_CLIENT = DEMO_DIR / "mcp_client.py"
MCP_TAPE = DEMO_DIR / "mcp-follow.tape"
MCP_DEMO_URL = "http://127.0.0.1:7878/mcp"
#: The repository-local file the tape uses to hold the client back until the
#: recorded timeline starts. It never leaves the checkout being recorded.
MCP_GATE_FILE = ".korvid-mcp-demo-go"
#: The repository-local file the `mcp` scene publishes once its MCP server is
#: bound *and* the TUI has mounted. The tape waits for it before it arms the
#: gate above: a fixed sleep releases the client whether or not port 7878 is
#: listening, so a slow cold checkout would open the story on a connection
#: error instead of the follow story.
MCP_READY_FILE = ".korvid-mcp-demo-ready"
#: The tmux session the tape composes and attaches. Named here because the
#: fail-closed contract below is precisely that `attach-session` never runs
#: without the readiness signal.
MCP_TMUX_SESSION = "korvid-mcp-demo"
#: The two repository-local status files the client pane publishes, and the
#: only channel that carries its verdict back to the tape. VHS records for a
#: fixed window no matter what the pane does, so a client that raised
#: mid-story would otherwise still produce a complete-looking asset.
MCP_CLIENT_OK_FILE = ".korvid-mcp-demo-client-ok"
MCP_CLIENT_FAILED_FILE = ".korvid-mcp-demo-client-failed"
#: The external promotion boundary. A tape's own `exit 1` cannot reject a
#: recording: VHS renders the timeline it was given and exits 0 whatever the
#: shell it typed into did, so the canonical clip was already overwritten by
#: the time any in-tape check ran. The verdict therefore lives outside VHS —
#: the tape writes a candidate, and this wrapper promotes it only on a run
#: the client pane certified.
MCP_RECORDER = DEMO_DIR / "record-mcp-follow.sh"
MCP_RECORDER_COMMAND = "docs/demo/record-mcp-follow.sh"
#: The only file the tape may write. Hidden, beside the published clip, and
#: never committed.
MCP_CANDIDATE_CLIP = "docs/assets/scenes/.mcp-follow-demo.candidate.mp4"
#: The published clip. In the whole recording chain this name may appear only
#: as the wrapper's promotion target.
MCP_FINAL_CLIP = "docs/assets/scenes/mcp-follow-demo.mp4"
#: The line the wrapper prints when it publishes nothing.
MCP_RECORDER_REJECTION = "record-mcp-follow.sh: rejecting this recording"
#: The other reviewed clips that live in the published clip's own directory.
#: A tape's second `Output` may name any of them, and none of their basenames
#: is what the byte guard looks for, so the preflight's contract is wider than
#: the MCP clip: an `Output` VHS would obey may not reach *any* approved asset.
MCP_SIBLING_CLIPS = ("agent-demo.mp4", "relationship-demo.mp4")
#: The environment overrides the wrapper honours, so a contract can drive the
#: whole boundary against a fake VHS in a temporary directory without touching
#: the checkout, its scratch files or its published media.
MCP_RECORDER_ENV = {
    "vhs": "KORVID_MCP_VHS_BIN",
    "tape": "KORVID_MCP_TAPE",
    "candidate": "KORVID_MCP_CANDIDATE",
    "final": "KORVID_MCP_FINAL",
    "ok": "KORVID_MCP_CLIENT_OK",
    "failed": "KORVID_MCP_CLIENT_FAILED",
    "ready": "KORVID_MCP_READY",
    "go": "KORVID_MCP_GO",
}
#: The bytes a previously approved clip is represented by in those contracts.
#: A rejected run must leave them byte-identical.
MCP_PUBLISHED_BYTES = b"previously approved clip"
#: The bytes a fake VHS renders into the candidate, so a promotion is visible
#: as a byte change rather than as a timestamp.
MCP_CANDIDATE_BYTES = b"freshly recorded candidate"
#: The bound the tape's typed readiness loop enforces: a wall-clock deadline
#: on bash's own `SECONDS` builtin, reset with `SECONDS=0` and checked with
#: `[ $SECONDS -lt 60 ]`. Counting `sleep 0.1` iterations instead assumes
#: each one costs exactly 0.1s, but fork+exec of `sleep` is not free — 600
#: iterations measured ~69s of real time here, already past the tape's own
#: 65s hidden allowance. `SECONDS` is immune to that overhead.
MCP_READY_WAIT_SECONDS = 60.0
#: VHS's `Sleep` runs on VHS's own clock. It does *not* observe the shell it
#: typed into finishing, so it advances while the readiness loop is still
#: spinning. The hidden allowance must therefore outlast the loop's own bound
#: with margin, or VHS types the release and reaches `Show` mid-wait and the
#: composing shell lands in the captured frames.
MCP_HIDDEN_ALLOWANCE = "Sleep 65s"
#: How far the hidden allowance must exceed the bounded wait. A margin, not
#: an equality: VHS starts its `Sleep` a keystroke before the shell starts
#: its loop, so an exact match would still be a race.
MCP_HIDDEN_ALLOWANCE_MARGIN_SECONDS = 5.0
#: The story the external client tells, in the order korvid must mirror it:
#: the pod table, the failing pod's diagnosis, its logs, then the release
#: that owns it.
MCP_CLIENT_CALLS = ("list_resources", "diagnose_pod", "get_logs", "helm_list_releases")
#: The three calls whose answer is read through `_tail` rather than
#: `_sections` (`diagnose_pod` is the odd one out).
MCP_CLIENT_TAIL_CALLS = ("list_resources", "get_logs", "helm_list_releases")
#: Rows both panes must carry legible content across. The poster is cut
#: mid-story, so the client's calls and korvid's mirrored view are on screen
#: together; a blanked or half-drawn pane would prove nothing.
MCP_EVIDENCE_BAND = (0, 690)
MCP_EVIDENCE_MIN_PIXELS = 5_000
#: How long `docs/demo/mcp_client.py` holds on the `diagnose_pod` answer
#: before it issues `get_logs`. `docs/demo/demo.py`'s `MCP_DESCRIBE_HOLD`
#: (2.2 s) dismisses the follow-opened describe modal well inside this beat,
#: so the shipped user-priority guard is never actually asked to refuse a
#: mirror in the captured timeline.
MCP_DIAGNOSE_HOLD = "3.2"
#: Indicative refusal verbs — a claim that something *was* refused. The bare
#: infinitive ("would refuse", "never asked to refuse") is deliberately absent:
#: a counterfactual is exactly what this page is allowed to say.
_INDICATIVE_REFUSAL = re.compile(
    r"\b(refuses|refused|refusing|blocks|blocked|rejects|rejected|denies|denied"
    r"|(?:does|did|do) (?:refuse|block|reject|deny))\b",
    re.IGNORECASE,
)
#: The two moods that keep such a clause truthful about this recording: a
#: conditional (the guard *would* refuse) or a denial (it never did).
_UNASSERTED_MOOD = re.compile(r"\b(would|if|were|had|never|not|no|nothing)\b", re.IGNORECASE)


def _prose_sentences(markdown: str) -> list[str]:
    """Split markdown prose into sentences without cutting decimals or paths.

    A naive split on `.` would cut `2.2`, `00:00:08.5` and `mcp_client.py`
    into fragments and hide a claim across two of them, so the period must be
    preceded by a non-digit and followed by whitespace to end a sentence.

    Args:
        markdown: Markdown text, fences and tables included.

    Returns:
        Whitespace-collapsed sentences in document order.
    """
    flat = " ".join(markdown.split())
    return [part.strip() for part in re.split(r"(?<=[^0-9])\.(?=\s)", flat) if part.strip()]


def _prose_clauses(markdown: str) -> list[str]:
    """Split prose further, at every comma, semicolon, colon and em dash.

    A whole-sentence scan lets an unrelated denial elsewhere in the sentence
    excuse a claim — "the guard refuses the next two calls, but no keystroke
    is sent" would pass on the strength of its second clause. Grading each
    clause on its own removes that laundering.

    Args:
        markdown: Markdown text, fences and tables included.

    Returns:
        Whitespace-collapsed clauses in document order.
    """
    return [
        clause.strip()
        for sentence in _prose_sentences(markdown)
        for clause in re.split(r"[,;:—]", sentence)
        if clause.strip()
    ]


def _png_size(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", payload[16:24])


def _png_pixel_aspect(path: Path) -> tuple[int, int, int]:
    """The pixel aspect ratio a PNG declares, reduced to lowest terms.

    A `pHYs` chunk with unit specifier 0 declares an aspect ratio rather than
    a physical density (RFC 2083 section 4.2.4.2); unit 1 declares a physical
    density in pixels per metre instead. Either form scales the rendered
    image the same way as long as the horizontal and vertical values are
    equal, so `(2835, 2835, unit=1)` (72 dpi) and `(2, 2, unit=0)` are both
    square, exactly like the default. ffmpeg copies the decoded frame's
    sample aspect ratio into it, so a poster cut from a non-square-pixel clip
    inherits the same disagreement with its own `width`/`height`. An absent
    chunk means square pixels.

    Returns:
        `(horizontal, vertical, unit)` with `horizontal`/`vertical` divided
        by their GCD, so square declarations always reduce to `(1, 1, ...)`
        regardless of the raw density or aspect-ratio values used to state
        them. `unit` is `0` when the chunk is absent, matching an aspect
        ratio's own "no physical unit" meaning.
    """
    for kind, body in _png_chunks(path.read_bytes()):
        if kind == b"pHYs":
            horizontal, vertical, unit = struct.unpack(">IIB", body)
            divisor = math.gcd(horizontal, vertical) or 1
            return horizontal // divisor, vertical // divisor, unit
    return 1, 1, 0


def _png_chunks(payload: bytes) -> Iterator[tuple[bytes, bytes]]:
    """Yield `(type, data)` for every chunk in a PNG stream."""
    cursor = 8
    while cursor < len(payload):
        (length,) = struct.unpack(">I", payload[cursor : cursor + 4])
        kind = payload[cursor + 4 : cursor + 8]
        yield kind, payload[cursor + 8 : cursor + 8 + length]
        cursor += 12 + length


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
    if distances[0] <= distances[1] and distances[0] <= distances[2]:
        return left
    return above if distances[1] <= distances[2] else upper_left


def _unfilter(kind: int, line: bytearray, previous: bytes, stride: int) -> bytearray:
    """Reverse one PNG scanline filter in place (RFC 2083 section 6)."""
    for index in range(len(line)):
        left = line[index - stride] if index >= stride else 0
        above = previous[index]
        upper_left = previous[index - stride] if index >= stride else 0
        if kind == 1:
            line[index] = (line[index] + left) & 0xFF
        elif kind == 2:
            line[index] = (line[index] + above) & 0xFF
        elif kind == 3:
            line[index] = (line[index] + (left + above) // 2) & 0xFF
        elif kind == 4:
            line[index] = (line[index] + _paeth(left, above, upper_left)) & 0xFF
        elif kind != 0:
            raise AssertionError(f"unsupported PNG filter type {kind}")
    return line


def _decode_png_bytes(payload: bytes, source: str) -> tuple[int, int, list[bytearray]]:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG stream into scanlines.

    The repository ships no image dependency, and these captures are the
    product's own evidence, so the few lines of PNG plumbing live here
    rather than in the runtime.

    Args:
        payload: The raw PNG stream.
        source: What produced `payload`, used in assertion messages.

    Returns:
        `(width, height, rows)` where each row holds `width * channels`
        bytes.
    """
    path = source
    assert payload[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    chunks = dict[bytes, bytes]()
    data = bytearray()
    for kind, body in _png_chunks(payload):
        if kind == b"IDAT":
            data += body
        else:
            chunks[kind] = body
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    assert (depth, interlace) == (8, 0), f"{path} must be 8-bit non-interlaced"
    assert colour in (2, 6), f"{path} must be RGB or RGBA, found colour type {colour}"
    stride = 3 if colour == 2 else 4
    raw = zlib.decompress(bytes(data))
    rows: list[bytearray] = []
    previous = bytes(width * stride)
    for index in range(height):
        start = index * (width * stride + 1)
        line = bytearray(raw[start + 1 : start + 1 + width * stride])
        row = _unfilter(raw[start], line, previous, stride)
        rows.append(row)
        previous = bytes(row)
    return width, height, rows


def _decode_png_rgb(path: Path) -> tuple[int, int, list[bytearray]]:
    """Decode a PNG capture written by the pipeline. See `_decode_png_bytes`."""
    return _decode_png_bytes(path.read_bytes(), str(path))


def _mp4_boxes(payload: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    """Yield `(type, body_start, box_end)` for every ISO-BMFF box in a span.

    Args:
        payload: The whole MP4 stream.
        start: First byte of the first box header in the span.
        end: Byte the span stops before.

    Yields:
        The four-character box type, the offset its payload starts at, and
        the offset the box ends at.
    """
    cursor = start
    while cursor + 8 <= end:
        (size,) = struct.unpack(">I", payload[cursor : cursor + 4])
        kind = payload[cursor + 4 : cursor + 8]
        body = cursor + 8
        if size == 1:
            (size,) = struct.unpack(">Q", payload[body : body + 8])
            body += 8
        elif size == 0:
            size = end - cursor
        assert size >= 8, f"malformed {kind!r} box of size {size}"
        yield kind, body, cursor + size
        cursor += size


def _find_mp4_box(payload: bytes, path: Sequence[bytes]) -> tuple[int, int]:
    """Resolve a container path such as `moov/trak/mdia` to `(body, end)`."""
    start, end = 0, len(payload)
    for name in path:
        for kind, body, stop in _mp4_boxes(payload, start, end):
            if kind == name:
                start, end = body, stop
                break
        else:
            raise AssertionError(f"no {name!r} box under {b'/'.join(path).decode()}")
    return start, end


def _mp4_geometry(path: Path) -> tuple[tuple[int, int], tuple[float, float], tuple[int, int]]:
    """Read a video track's stored size, display size and pixel aspect ratio.

    The repository ships no media dependency, so the three ISO-BMFF fields a
    browser lays a `<video>` out from are read here directly: `tkhd`'s fixed
    16.16 display box, the `avc1` sample entry's stored pixel box, and the
    optional `pasp` box that scales one into the other. An absent `pasp`
    means square pixels (ISO/IEC 14496-12), so it is reported as `(1, 1)`.

    Args:
        path: The MP4 to read.

    Returns:
        `(stored, displayed, pixel_aspect)`.
    """
    payload = path.read_bytes()
    trak, trak_end = _find_mp4_box(payload, (b"moov", b"trak"))
    tkhd, _ = _find_mp4_box(payload[trak:trak_end], (b"tkhd",))
    header = payload[trak + tkhd :]
    offset = 88 if header[0] == 1 else 76
    raw_width, raw_height = struct.unpack(">II", header[offset : offset + 8])
    displayed = (raw_width / 65536, raw_height / 65536)

    stsd, stsd_end = _find_mp4_box(payload[trak:trak_end], (b"mdia", b"minf", b"stbl", b"stsd"))
    _, entry, entry_end = next(_mp4_boxes(payload, trak + stsd + 8, trak + stsd_end))
    stored = struct.unpack(">HH", payload[entry + 24 : entry + 28])

    pixel_aspect = (1, 1)
    for kind, body, _ in _mp4_boxes(payload, entry + 78, entry_end):
        if kind == b"pasp":
            pixel_aspect = struct.unpack(">II", payload[body : body + 8])
    return stored, displayed, pixel_aspect


def _band_deviation(rows: list[bytearray], top: int, bottom: int, channels: int) -> int:
    """Largest colour distance from `#111111` inside the client pane.

    Only the red, green and blue samples of each pixel are compared. An
    RGBA capture stores an opaque pixel's alpha as `0xFF`, which is 238
    away from `0x11` as a raw byte: counting it would make a perfectly
    cleared band read as legible content, and — worse — would let a fully
    blanked pane clear the retained-evidence floor on opacity alone,
    turning both halves of the redaction contract into noise.

    Args:
        rows: Decoded scanlines, each `width * channels` bytes.
        top: First row of the band, inclusive.
        bottom: Row the band stops before.
        channels: Samples per pixel — 3 for RGB, 4 for RGBA.

    Returns:
        The largest absolute distance from `0x11` over every RGB sample in
        the band's slice of the client pane.
    """
    left, right = MCP_CLIENT_PANE
    worst = 0
    for row in rows[top:bottom]:
        for pixel in range(left, right):
            base = pixel * channels
            for value in row[base : base + 3]:
                worst = max(worst, abs(value - 0x11))
    return worst


def _band_contrast_pixels(
    rows: list[bytearray],
    top: int,
    bottom: int,
    channels: int,
    *,
    minimum_deviation: int,
    columns: tuple[int, int] = MCP_CLIENT_PANE,
) -> int:
    """Count pixels in one pane's band whose RGB contrast exceeds the floor."""
    left, right = columns
    count = 0
    for row in rows[top:bottom]:
        for pixel in range(left, right):
            base = pixel * channels
            if max(abs(value - 0x11) for value in row[base : base + 3]) > minimum_deviation:
                count += 1
    return count


def _without_markdown_code(text: str) -> str:
    """Remove fenced and inline code before scanning visitor-facing prose."""
    return _MARKDOWN_INLINE_CODE.sub("", _MARKDOWN_FENCE.sub("", text))


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    """Frame one PNG chunk with its length and CRC (RFC 2083 section 3.2)."""
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    )


def _encode_png(width: int, rows: Sequence[Sequence[int]], *, colour: int) -> bytes:
    """Encode 8-bit non-interlaced rows as a PNG stream (filter type 0).

    Args:
        width: Pixels per row.
        rows: One sequence of `width * channels` samples per scanline.
        colour: PNG colour type — 2 for RGB, 6 for RGBA.

    Returns:
        The PNG stream `_decode_png_bytes` reads back.
    """
    header = struct.pack(">IIBBBBB", width, len(rows), 8, colour, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _png_with_pixel_aspect(*, horizontal: int, vertical: int, unit: int) -> bytes:
    """A minimal valid 1x1 RGB PNG carrying a `pHYs` chunk before its `IDAT`.

    Used to prove `_png_pixel_aspect` reads declared axes/unit correctly
    without depending on any real asset in `docs/assets/scenes`.
    """
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    raw = b"\x00\xff\xff\xff"
    phys = struct.pack(">IIB", horizontal, vertical, unit)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"pHYs", phys)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _demo_harness() -> ModuleType:
    """Import the documentation-only capture harness as a module.

    It lives outside the package on purpose (it is never shipped), so it
    has to be loaded by path rather than imported by name.
    """
    module = sys.modules.get("korvid_docs_demo_harness")
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location("korvid_docs_demo_harness", DEMO_HARNESS)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["korvid_docs_demo_harness"] = module
    spec.loader.exec_module(module)
    return module


def _mcp_client_module() -> ModuleType:
    """Import the checked-in MCP follow-capture client as a module.

    Like the demo harness, it lives outside the package on purpose (it is
    never shipped), so it has to be loaded by path rather than imported by
    name.
    """
    module = sys.modules.get("korvid_docs_mcp_client")
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location("korvid_docs_mcp_client", MCP_CLIENT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["korvid_docs_mcp_client"] = module
    spec.loader.exec_module(module)
    return module


class _DemoLister:
    """The `Lister` shape `RelationshipSnapshotLoader` consumes."""

    def __init__(self, harness: ModuleType) -> None:
        self._harness = harness

    async def list_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> Sequence[SummaryLike]:
        objects = await self._harness.list_relationship_objects(meta, namespace)
        return list(objects)


def test_band_deviation_ignores_the_alpha_channel_of_a_cleared_rgba_capture() -> None:
    """An opaque RGBA re-encode must still read as cleared, not as content.

    `_decode_png_rgb` accepts colour type 6, so the capture pipeline may
    hand this contract an RGBA poster at any time. In RGBA an opaque pixel
    stores `0xFF` alpha, which is 238 away from the `0x11` background as a
    raw byte — comparing it would report a perfectly cleared band as
    legible third-party content and fail the redaction contract on a frame
    that redacts everything it claims to.
    """
    width = 1280
    left, _right = MCP_CLIENT_PANE
    rows = []
    for _ in range(8):
        row = bytearray()
        for pixel in range(width):
            # korvid's own pane keeps bright content; the client pane is
            # cleared to its `#111111` background and fully opaque.
            row += b"\xd0\xd0\xd0\xff" if pixel < left else b"\x11\x11\x11\xff"
        rows.append(row)

    decoded_width, height, decoded = _decode_png_bytes(
        _encode_png(width, rows, colour=6), "synthetic opaque RGBA capture"
    )
    channels = len(decoded[0]) // decoded_width
    assert (decoded_width, height, channels) == (width, 8, 4)

    assert _band_deviation(decoded, 0, height, channels) <= 16, (
        "an opaque alpha sample is not legible content; the cleared-band check "
        "must compare colour only"
    )


def test_band_deviation_cannot_be_satisfied_by_alpha_variation_alone() -> None:
    """Alpha must never stand in for the retained band's evidence.

    The retained band's floor (`> 100`) exists so a future "fix" cannot
    satisfy the redaction contract by blanking the whole pane. If alpha
    counted, an RGBA capture whose client pane was wiped to a flat
    background would still clear that floor purely on varying opacity, and
    the contract would pass on a frame with no prompt and no tool calls
    left in it.
    """
    width = 1280
    left, _right = MCP_CLIENT_PANE
    rows = []
    for index in range(8):
        row = bytearray()
        for pixel in range(width):
            if pixel < left:
                row += b"\xd0\xd0\xd0\xff"
            else:
                # Blanked colour, wildly varying opacity.
                row += bytes((0x11, 0x11, 0x11, (pixel * 7 + index * 31) % 256))
        rows.append(row)

    deviation = _band_deviation(rows, 0, len(rows), 4)
    assert deviation <= 16, (
        "a blanked pane must not reach the retained-evidence floor through its "
        f"alpha channel; got {deviation}"
    )
    assert not deviation > 100, "alpha alone must never satisfy the evidence contract"


def _hidden_launch_block(tape: str, source: str) -> tuple[str, list[str]]:
    """Split a tape into its launch line and the hidden block containing it.

    Args:
        tape: The full VHS script.
        source: The tape's name, used in assertion messages.

    Returns:
        `(launch_line, hidden_lines)` — the `Type "uv run …"` line and every
        stripped line from the `Hide` that precedes it up to the next
        `Show`. Everything in that block is executed while VHS records
        nothing, so it cannot change the captured timeline.
    """
    lines = [line.strip() for line in tape.splitlines()]
    launches = [index for index, line in enumerate(lines) if line.startswith('Type "uv run')]
    assert len(launches) == 1, f"{source} must launch the harness exactly once"
    launch = launches[0]
    hides = [index for index, line in enumerate(lines[:launch]) if line == "Hide"]
    assert hides, f"{source} must launch the harness inside a Hide block"
    shows = [index for index, line in enumerate(lines) if index > launch and line == "Show"]
    assert shows, f"{source} must reveal the terminal with Show once the TUI is up"
    return lines[launch], lines[hides[-1] : shows[0]]


def test_every_tui_tape_hides_the_same_frozen_cold_start_allowance() -> None:
    """All three tapes start the TUI the same way, or one of them records a shell.

    `uv run` can spend tens of seconds resolving and installing the project
    on a cold cache before korvid paints its first frame; whatever the tape
    types before then lands in the shell. `demo.tape` allowed six seconds
    while `agent.tape` and `relationships.tape` allowed twenty, so the same
    cold cache that the other two tolerate produced a recording of bash
    echoing `0`, `/payment`, `d`, `l` and `:deploy`. The allowance sits
    inside the tape's `Hide` block, so raising it costs the captured
    animation nothing — the output timeline still starts at `Show`, and the
    poster/`ffmpeg -ss` offsets cut from it are unaffected.

    `--frozen` is part of the same contract: recording a screenshot must
    never re-resolve and rewrite `uv.lock` as a side effect.
    """
    for name in TUI_TAPES:
        tape = (DEMO_DIR / name).read_text(encoding="utf-8")
        launch, hidden = _hidden_launch_block(tape, name)

        assert launch.startswith('Type "uv run --frozen python docs/demo/demo.py'), (
            f"{name} must launch the harness with `uv run --frozen`, or recording a "
            f"capture can re-resolve and rewrite uv.lock: {launch!r}"
        )
        sleeps = [line for line in hidden if line.startswith("Sleep")]
        assert sleeps == [COLD_START_SLEEP], (
            f"{name} must hide exactly the shared cold-start allowance "
            f"{COLD_START_SLEEP!r} before Show; found {sleeps}"
        )
        assert COLD_START_REFERENCE in tape, (
            f"{name} must point at {COLD_START_REFERENCE} for the reason behind the "
            "allowance instead of restating it, so the three tapes cannot drift apart"
        )


def test_cold_start_allowance_is_documented_once_in_the_provenance_page() -> None:
    """The reason lives in one place; the tapes reference it.

    A per-tape comment is how the tapes drifted to two different allowances
    in the first place. The provenance page states the allowance, the reason
    and the fact that hiding it keeps the recorded timeline unchanged, and
    must not describe any tape as needing less.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    section = instructions.split("## Running the tapes reproducibly", 1)[1].split("\n## ", 1)[0]
    lowered = " ".join(section.lower().split())

    assert "uv run --frozen" in section
    assert "**20 seconds**" in section, (
        "the provenance page must state the single cold-start allowance every tape hides"
    )
    for tape in TUI_TAPES:
        assert tape in section, f"the shared allowance must name {tape}"
    for reason in ("cold", "resolv", "shell", "hide"):
        assert reason in lowered, (
            f"the centralized reason must explain the allowance; {reason!r} is missing"
        )
    assert "starts at `show`" in lowered, (
        "the page must say the recorded timeline still starts at Show, so the "
        "hidden allowance cannot be read as padding the captured duration"
    )
    assert "allows 6" not in lowered, "no tape may still document a shorter allowance"


def test_visual_storytelling_plan_tape_snippets_match_the_shipped_cold_start() -> None:
    """The plan is an executable recipe, so its tapes must be the shipped tapes.

    A contributor replaying `Step 4` verbatim would otherwise recreate the
    unfrozen `uv run` and the six-second allowance this change removes.
    """
    plan = VISUAL_STORYTELLING_PLAN.read_text(encoding="utf-8")
    for name in ("agent.tape", "relationships.tape"):
        marker = f"Create `docs/demo/{name}`:"
        assert marker in plan, f"the plan must still create {name}"
        snippet = plan.split(marker, 1)[1].split("```", 2)[1]
        snippet = snippet.split("\n", 1)[1]
        shipped = (DEMO_DIR / name).read_text(encoding="utf-8")
        assert snippet.strip() == shipped.strip(), (
            f"the plan's {name} snippet must be the shipped tape, cold-start "
            "allowance and `--frozen` included"
        )
    assert "Sleep 6s" not in plan, (
        "no plan snippet may still ship the six-second cold-start allowance"
    )
    assert 'Type "uv run python docs/demo/demo.py' not in plan, (
        "no plan snippet may launch the harness without `--frozen`"
    )


def test_demo_agent_turn_uses_real_tools_and_mints_citations() -> None:
    """The captured turn must be korvid's own loop, not a replayed transcript.

    The Agent capture used to inject hard-coded panel events, so the frame
    proved the `AgentPanel` and nothing behind it. The harness now builds the
    shipped `AgentRuntime` over the real `ToolExecutor` and the synthetic
    fixture, so the tool calls are dispatched, the results are read, and every
    `[E]` marker in the answer is a reference the real `EvidenceLedger` minted
    for a read that happened. `uncited == ()` is the load-bearing half: it can
    only hold when the ledger recognises both markers.
    """
    harness = _demo_harness()
    runtime = harness.build_demo_agent_runtime()

    async def drain() -> list[AgentEvent]:
        return [
            event
            async for event in runtime.run_turn(
                "Why is the payment worker failing?",
                "view=pods ns=shop selected=payment-worker-6c9f7d-b3xnq",
            )
        ]

    events = asyncio.run(drain())
    started = [event.name for event in events if isinstance(event, ToolCallStarted)]
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, f"the grounded turn ends exactly once: {events}"
    complete = completions[0]
    answer = "".join(event.text for event in events if isinstance(event, TextDelta))

    assert started == ["diagnose_pod", "get_logs"], (
        f"the capture's story is a diagnosis followed by its logs; found {started}"
    )
    assert complete.cited == ("E1", "E2"), (
        f"both markers must resolve to minted evidence; found cited={complete.cited!r}"
    )
    assert complete.uncited == (), (
        "a grounded answer leaves nothing unsupported, so the panel renders no "
        f"citation warning; found uncited={complete.uncited!r}"
    )
    assert runtime.evidence.resolve("E1") is not None
    assert runtime.evidence.resolve("E2") is not None
    assert "[E1]" in answer, (
        f"the rendered answer must carry the first validated marker: {answer!r}"
    )
    assert "[E2]" in answer, (
        f"the rendered answer must carry the second validated marker: {answer!r}"
    )


def test_demo_agent_provider_is_answered_with_real_tool_results() -> None:
    """The capture cannot be satisfied by injecting panel events.

    Panel events can be forged; a conversation cannot. The deterministic
    provider is called once per iteration, and the second and third calls must
    carry `role="tool"` messages whose text is what the real executor read out
    of the synthetic fixture — the pod's own identity and its log lines. If the
    harness ever went back to emitting events directly, no tool message would
    exist to find.
    """
    harness = _demo_harness()
    story = harness.load_agent_story()
    provider = story.DemoAgentProvider()
    runtime = story.build_demo_agent_runtime(
        harness.DemoReadOps(), harness.ALIASES, provider=provider
    )

    async def drain() -> None:
        async for _event in runtime.run_turn(
            "Why is the payment worker failing?",
            "view=pods ns=shop selected=payment-worker-6c9f7d-b3xnq",
        ):
            pass

    asyncio.run(drain())

    assert len(provider.seen_messages) == 3, (
        f"one provider call per iteration: diagnosis, logs, answer; found "
        f"{len(provider.seen_messages)}"
    )
    assert not [m for m in provider.seen_messages[0] if m.get("role") == "tool"], (
        "nothing has been read before the first call, so it must carry no tool result"
    )
    for index in (1, 2):
        results = [m for m in provider.seen_messages[index] if m.get("role") == "tool"]
        assert results, (
            f"provider call {index + 1} must be answered with the real tool results; "
            f"roles were {[m.get('role') for m in provider.seen_messages[index]]}"
        )
    diagnosis = "".join(
        str(m.get("content") or "") for m in provider.seen_messages[1] if m.get("role") == "tool"
    )
    assert "payment-worker-6c9f7d-b3xnq" in diagnosis, (
        f"the diagnosis result must come from the synthetic pod: {diagnosis!r}"
    )
    logs = "".join(
        str(m.get("content") or "") for m in provider.seen_messages[2] if m.get("role") == "tool"
    )
    assert "gateway" in logs, f"the log result must be the fixture's log stream: {logs!r}"


def test_visual_storytelling_plan_no_longer_ships_the_injected_agent_runtime() -> None:
    """The historical plan must not re-create the event injector it replaced.

    That plan is still an executable recipe, so a contributor replaying it
    would otherwise restore a demo runtime that mints no evidence and reports
    its own `[E1]` as unsupported. Its Agent snippets are pinned to the shipped
    harness instead; the rest of the recipe (scene app, auto-open timer) is
    unchanged and still checked here.
    """
    plan = VISUAL_STORYTELLING_PLAN.read_text(encoding="utf-8")
    assert "class ScriptedAgentRuntime:" not in plan, (
        "the plan must not recreate the injected runtime this change removed"
    )
    assert 'uncited=("E1",)' not in plan, (
        "no plan snippet may hard-code an unsupported citation into the capture"
    )
    assert "build_demo_agent_runtime" in plan, (
        "the plan must build the grounded runtime the harness ships"
    )
    assert "class DemoKorvidApp(KorvidApp):" in plan
    assert "self.set_timer(0.2, self.action_toggle_agent)" in plan
    assert "app = DemoKorvidApp(" in plan
    assert "demo_scene=scene" in plan


def test_agent_capture_copy_states_the_grounded_path_and_its_offline_limit() -> None:
    """The provenance page must say exactly which parts of the turn are real.

    The capture now runs korvid's own runtime, executor and evidence ledger
    over a synthetic fixture, and the only scripted participant is the
    model's side of the conversation. Both halves have to be published: a
    reader who is told "deterministic" without being told the tools are real
    would discount the frame, and a reader told "real agent turn" without the
    offline limit would read it as a model-quality claim.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    section = instructions.split("## Embedded agent", 1)[1].split("\n## ", 1)[0]
    lowered = " ".join(section.lower().split())

    for real in ("agentruntime", "toolexecutor", "evidenceledger", "diagnose_pod", "get_logs"):
        assert real.lower() in lowered, (
            f"the provenance must name the shipped component the capture runs; {real!r} is missing"
        )
    for limit in ("deterministic", "offline", "synthetic", "no credential", "opens no socket"):
        assert limit in lowered, (
            f"the provenance must state the capture's limit; {limit!r} is missing"
        )
    assert "unsupported citation" in lowered, (
        "the page must say why the frame carries no citation warning, since the "
        "previous capture did"
    )
    assert "quality" in lowered, (
        "a deterministic provider says nothing about answer quality, and the page "
        "must refuse that claim explicitly"
    )


def test_agent_provenance_credits_the_successful_log_mirror_not_a_refusal() -> None:
    """The `get_logs` follow mirror succeeds; nothing refuses it in this capture.

    `agent_open_describe`'s guard (`AgentScreens.describe_screen_open`) only
    trips for a pushed modal `DescribeScreen`. With `AgentPanel` expanded,
    `diagnose_pod`'s mirror shares the non-modal `DescribePane` instead of
    pushing one, so the app screen is never a `DescribeScreen` and
    `agent_open_logs`'s identical guard never blocks the second mirror:
    both the `diagnose_pod` describe and the `get_logs` mirror succeed, and
    both fire their own success toast. The log pane really opens — it is
    only invisible in the frame because the docked describe pane (60%
    width) and the docked `AgentPanel` (40% width) already fill the screen
    between them, leaving the undocked log pane no room. The old claim that
    a "user-priority guard refuses" the `get_logs` mirror described the MCP
    scene's modal-dismissal choreography, not this capture, and must not
    appear here.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    section = instructions.split("## Embedded agent", 1)[1].split("\n## ", 1)[0]
    lowered = " ".join(section.lower().split())

    assert "guard refuses" not in lowered, (
        "the get_logs mirror is not refused by any guard in this capture — "
        "describe_screen_open() never trips while the shared pane is open"
    )
    assert "user-priority guard" not in lowered, (
        "the user-priority guard does not gate this capture's get_logs mirror; "
        "it only trips for a pushed modal DescribeScreen, which this capture "
        "never pushes"
    )
    assert "both" in lowered, "the page must state plainly that both follow mirrors succeed"
    assert "succeed" in lowered, "the page must state plainly that both follow mirrors succeed"
    assert "toast" in lowered, "the page must say both mirrors fire their own success toast"
    assert "log pane" in lowered, "the page must say the log pane really opens"
    assert " opens" in lowered, "the page must say the log pane really opens"
    assert "not visible" in lowered or "invisible" in lowered, (
        "the page must explain the log pane opens but is not visible in the frame"
    )


def _mp4_duration(path: Path) -> float:
    """Seconds of presentation time, read from the movie header.

    The repository ships no media dependency and the contracts already read
    ISO-BMFF boxes directly, so `mvhd`'s timescale and duration are read the
    same way rather than shelling out to `ffprobe`.

    Args:
        path: The MP4 to read.

    Returns:
        The movie duration in seconds.
    """
    payload = path.read_bytes()
    mvhd, _ = _find_mp4_box(payload, (b"moov", b"mvhd"))
    version = payload[mvhd]
    if version == 1:
        timescale, duration = struct.unpack(">IQ", payload[mvhd + 20 : mvhd + 32])
    else:
        timescale, duration = struct.unpack(">II", payload[mvhd + 12 : mvhd + 20])
    assert timescale, f"{path.name} declares a zero timescale"
    return float(duration) / float(timescale)


def test_agent_clip_runs_long_enough_to_read_the_whole_grounded_turn() -> None:
    """The Agent story needs time on screen, and no more than that.

    The turn dispatches two real read tools before it answers, so a clip cut
    at the old eight-second hold ended while the answer was still streaming.
    The upper bound is the other half of the contract: a landing clip that
    outruns a visitor's attention is padding, not evidence.
    """
    duration = _mp4_duration(SCENES / "agent-demo.mp4")
    assert 12.0 <= duration <= 15.0, (
        f"agent-demo.mp4 runs {duration:.2f}s; the grounded story must settle "
        "on screen within a 12-15s clip"
    )


def test_storytelling_pngs_meet_their_declared_size_and_byte_budget() -> None:
    """Binary/dimension/byte contract only — it says nothing about content.

    The declared `width`/`height` attributes on the site must match the real
    intrinsic size — and a frame only lays out at that size while its pixels
    are square — or the reserved box is wrong, and the byte budget keeps the
    page light. Whether a capture is legible or shows the right screen is
    verified by looking at it, not here.
    """
    for name, (width, min_height, max_height) in PNG_ASSETS.items():
        path = SCENES / name
        assert path.is_file(), f"{path} is required by the visual narrative"
        actual_width, actual_height = _png_size(path)
        assert actual_width == width
        assert min_height <= actual_height <= max_height
        assert path.stat().st_size <= 900_000
        horizontal, vertical, unit = _png_pixel_aspect(path)
        assert horizontal == vertical, (
            f"{name} declares non-square pixels {horizontal}:{vertical} "
            f"(pHYs unit={unit}), so the {actual_width}x{actual_height} box the "
            "site reserves is not the box a browser draws; cut the frame from a "
            "square-pixel source instead of widening the reservation"
        )


def test_png_pixel_aspect_accepts_square_pixels_declared_as_a_density(
    tmp_path: Path,
) -> None:
    """A 72 dpi `pHYs` chunk (unit=1, physical density) is still square.

    `(2835, 2835)` pixels-per-metre is the common 72 dpi density ffmpeg (and
    many other encoders) writes for "no particular density"; it must not be
    mistaken for a non-square declaration just because the raw values are
    not literally `1`.
    """
    path = tmp_path / "dpi-square.png"
    path.write_bytes(_png_with_pixel_aspect(horizontal=2835, vertical=2835, unit=1))

    assert _png_pixel_aspect(path) == (1, 1, 1), (
        f"{_png_pixel_aspect(path)} should reduce a 72 dpi density to a square ratio"
    )


def test_png_pixel_aspect_accepts_square_pixels_declared_as_a_ratio(
    tmp_path: Path,
) -> None:
    """A small `(2, 2, unit=0)` aspect-ratio declaration is also square."""
    path = tmp_path / "ratio-square.png"
    path.write_bytes(_png_with_pixel_aspect(horizontal=2, vertical=2, unit=0))

    assert _png_pixel_aspect(path) == (1, 1, 0), (
        f"{_png_pixel_aspect(path)} should reduce a (2, 2) aspect ratio to (1, 1)"
    )


def test_png_pixel_aspect_rejects_a_genuinely_non_square_declaration(
    tmp_path: Path,
) -> None:
    """The regression this contract exists for: unequal axes must still fail."""
    path = tmp_path / "non-square.png"
    path.write_bytes(_png_with_pixel_aspect(horizontal=2485, vertical=2528, unit=0))

    horizontal, vertical, unit = _png_pixel_aspect(path)
    assert horizontal != vertical, (
        f"{horizontal}:{vertical} (unit={unit}) is genuinely non-square and must "
        "not be reported as square"
    )


def test_png_pixel_aspect_defaults_to_square_without_a_phys_chunk(
    tmp_path: Path,
) -> None:
    """A PNG with no `pHYs` chunk at all is the implicit square-pixel case."""
    path = tmp_path / "no-phys.png"
    path.write_bytes(_encode_png(1, [[255, 255, 255]], colour=2))
    assert not any(kind == b"pHYs" for kind, _ in _png_chunks(path.read_bytes())), (
        "this fixture must not carry a pHYs chunk for the default to be exercised"
    )

    assert _png_pixel_aspect(path) == (1, 1, 0)


def test_storytelling_motion_assets_are_local_mp4_files_with_a_size_budget() -> None:
    for name in MP4_ASSETS:
        path = SCENES / name
        assert path.is_file()
        payload = path.read_bytes()
        assert payload[4:8] == b"ftyp"
        assert len(payload) <= 3 * 1024 * 1024


def test_storytelling_motion_assets_store_square_pixels() -> None:
    """A published clip must display at exactly the box it stores.

    The two-pane MCP capture is recorded at 1280x711. Making the height even
    for `yuv420p` scales it to 710 and, unless the chain says otherwise,
    `scale` preserves the *display* aspect by rewriting the SAR to 2485:2528
    — an encode that stores 1280x710 but that every browser lays out at
    1258x710. The landing page reserves a box from the stored geometry, so
    the clip would then pillarbox inside its own reservation. Forcing square
    pixels is what makes the reservation true.
    """
    for name, expected in MP4_GEOMETRY.items():
        stored, displayed, pixel_aspect = _mp4_geometry(SCENES / name)
        assert stored == expected, f"{name} stores {stored}, not the reviewed {expected}"
        assert pixel_aspect[0] == pixel_aspect[1], (
            f"{name} carries a non-square pixel aspect ratio {pixel_aspect[0]}:"
            f"{pixel_aspect[1]}; add `setsar=1` to its filter chain and re-encode "
            "rather than letting the CSS box disagree with the layout"
        )
        assert displayed == (float(stored[0]), float(stored[1])), (
            f"{name} displays at {displayed} but stores {stored}; the reserved "
            "box would pillarbox or crop it"
        )

    clip, _, _ = _mp4_geometry(SCENES / "mcp-follow-demo.mp4")
    assert clip == _png_size(SCENES / "mcp-poster.png"), (
        "the MCP poster stands in for the clip until it plays, so both must lay "
        f"out in one box; clip is {clip}, poster is {_png_size(SCENES / 'mcp-poster.png')}"
    )


def test_storytelling_capture_instructions_name_every_generated_asset() -> None:
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    for name in PNG_ASSETS.keys() | MP4_ASSETS:
        assert f"docs/assets/scenes/{name}" in instructions
    assert "synthetic" in instructions.lower()
    assert "vhs docs/demo/agent.tape" in instructions
    assert "vhs docs/demo/relationships.tape" in instructions
    assert MCP_RECORDER_COMMAND in instructions
    assert "docs/assets/mcp-follow-demo.gif" in instructions


def test_mcp_capture_instructions_derive_the_readme_gif_from_the_approved_clip() -> None:
    """Round-8 review: the README GIF must be this repository's own capture.

    The GIF that used to ship here was an unrelated recording whose
    right-hand pane was a third-party MCP client, carrying that session's
    working directory, branch, token spend and model name — and README.md
    embeds it by raw URL, so it was public evidence, not a dormant source
    asset. It is replaced by a palette-quantised copy of
    `docs/assets/scenes/mcp-follow-demo.mp4`, and the derivation has to be
    written down exactly, the way every other generated asset here is, so a
    later reader can reproduce it instead of trusting the checked-in bytes.
    """
    mcp = INSTRUCTIONS.read_text(encoding="utf-8").split("## MCP follow", 1)[1]
    normalized_mcp = " ".join(mcp.split())
    landing = LANDING.read_text(encoding="utf-8")
    public_pages = []
    for path in DOCS.rglob("*.md"):
        relative = path.relative_to(DOCS).as_posix()
        if not relative.startswith(_EXCLUDED_DOC_PREFIXES):
            public_pages.append(path)
    published_labels = {path.relative_to(ROOT).as_posix() for path in public_pages}
    assert "docs/demo/visual-storytelling.md" in published_labels
    embeds = [
        str(path.relative_to(ROOT))
        for path in public_pages
        if "mcp-follow-demo.gif" in _without_markdown_code(path.read_text(encoding="utf-8"))
    ]

    probe = (
        "`assets/mcp-follow-demo.gif`\n"
        "```text\nassets/mcp-follow-demo.gif\n```\n"
        "![visitor-facing raw capture](assets/mcp-follow-demo.gif)"
    )
    assert _without_markdown_code(probe).count("mcp-follow-demo.gif") == 1

    for stale in (
        "older, unrelated capture",
        "third-party MCP client",
        "separate follow-up",
        "unredacted",
    ):
        assert stale not in normalized_mcp, (
            f"the GIF is now derived from the approved clip; {stale!r} describes the "
            "asset it replaced"
        )

    assert (
        "`docs/assets/mcp-follow-demo.gif` is the README's animated copy of that same "
        "capture, derived from the MP4 above and from nothing else:" in normalized_mcp
    )
    for fragment in (
        "ffmpeg -y -i docs/assets/scenes/mcp-follow-demo.mp4",
        "palettegen=max_colors=256:stats_mode=diff",
        "paletteuse=dither=none:diff_mode=rectangle",
        "-loop 0 docs/assets/mcp-follow-demo.gif",
    ):
        assert fragment in mcp, f"the GIF derivation command must state {fragment!r}"
    assert (
        "The README GIF therefore contains no external client session metadata." in normalized_mcp
    )
    assert "No official-site page embeds it." in normalized_mcp
    assert "MkDocs still serves it at `assets/mcp-follow-demo.gif`." in normalized_mcp
    assert "The landing page uses only the locally recorded MP4/poster above." in normalized_mcp
    assert not embeds, (
        "no official-site page should embed the MCP GIF as visitor-facing evidence; "
        f"found references in {embeds}"
    )
    assert "mcp-follow-demo.gif" not in landing
    assert 'src="assets/scenes/mcp-follow-demo.mp4"' in landing
    assert "assets/scenes/mcp-poster.png" in landing


def test_readme_gif_inherits_the_clips_geometry_and_documented_frame_rate() -> None:
    """The documented recipe and the checked-in GIF must agree.

    Nothing in the derivation rescales, so the GIF stores exactly the box the
    clip stores; and the `fps` the command names is the rate the README
    animation actually plays at. If either drifts, the instructions describe
    an asset that is no longer the one shipped.
    """
    gif = (DOCS / "assets" / "mcp-follow-demo.gif").read_bytes()
    assert gif[:6] in {b"GIF87a", b"GIF89a"}
    gif_size = (
        int.from_bytes(gif[6:8], "little"),
        int.from_bytes(gif[8:10], "little"),
    )
    clip_size = _mp4_geometry(SCENES / "mcp-follow-demo.mp4")[0]
    assert gif_size == clip_size, (
        f"the README GIF stores {gif_size} but its documented source stores "
        f"{clip_size}; the derivation must not rescale the capture"
    )

    mcp = INSTRUCTIONS.read_text(encoding="utf-8").split("## MCP follow", 1)[1]
    rate = re.search(r"-lavfi \"fps=([0-9.]+),split", mcp)
    assert rate is not None, "the GIF derivation must name the frame rate it samples at"
    assert 12.0 <= float(rate.group(1)) <= 15.0, (
        f"the README animation samples at {rate.group(1)} fps; the reviewed range for "
        "a readable follow story is 12-15 fps"
    )


def test_mcp_client_calls_the_follow_story_tools_in_order() -> None:
    """The capture's right-hand pane must be a real MCP SDK client.

    The published clip claims an external host drove korvid over MCP, so
    the thing driving it has to be an actual `ClientSession` speaking
    Streamable HTTP to the running server — not a printer that fakes the
    exchange. The four calls are the story itself: the pod table, the
    failing pod's diagnosis, its logs, then the release that owns it, and
    korvid's follow mirror is only legible if they happen in that order.
    """
    client = MCP_CLIENT.read_text(encoding="utf-8")
    positions = [client.index(f'call_tool("{name}"') for name in MCP_CLIENT_CALLS]

    assert positions == sorted(positions), (
        f"the client must call {list(MCP_CLIENT_CALLS)} in order; found offsets {positions}"
    )
    assert "from mcp import ClientSession" in client, (
        "the pane must hold the real MCP SDK client session, not a transcript"
    )
    assert "streamable_http_client" in client, (
        "the capture claims Streamable HTTP; the client must speak it"
    )
    assert MCP_DEMO_URL in client, f"the client must connect to the harness endpoint {MCP_DEMO_URL}"


def test_mcp_client_publishes_nothing_but_korvids_own_work() -> None:
    """Everything the recorded pane prints is authored here, and bounded.

    This pane is promoted to a landing scene and a poster, so whatever it
    writes ships. A client that echoed its endpoint file, its process, its
    working directory, a model name, a token count — or an unbounded tool
    result — would publish session internals that have nothing to do with
    korvid, which is exactly what the previous third-party capture had to
    redact after the fact. Authoring the pane is what removes the redaction
    pass; this contract is what keeps it authored.
    """
    client = MCP_CLIENT.read_text(encoding="utf-8")
    for leak in (
        "getcwd",
        "Path.cwd",
        "os.environ",
        "getpid",
        "sys.argv",
        "gethostname",
        "mcp-endpoint",
        "capability_token",
        "model",
    ):
        assert leak not in client, (
            f"the recorded client pane must not publish session internals; found {leak!r}"
        )
    assert "TAIL_LINES" in client, (
        "tool results are unbounded text; the pane must print a fixed tail of "
        "each one so a long result cannot scroll the story off screen"
    )


def test_mcp_client_sections_keeps_only_the_named_headers_in_order() -> None:
    """`_sections` collects each named header and its indented body, in order.

    `diagnose_pod` answers with far more than the pane can hold; the
    `CURRENT HEALTH` / `CONTAINERS` beat must survive intact and in the
    order asked, regardless of what other sections surround it.
    """
    module = _mcp_client_module()
    answer = "\n".join(
        [
            "CURRENT HEALTH",
            "  status: CrashLoopBackOff",
            "CONTAINERS",
            "  app: restarting",
            "RECENT LOGS",
            "  ...500 lines the pane must never see...",
        ]
    )

    kept = module._sections(answer, "CURRENT HEALTH", "CONTAINERS")

    assert kept == [
        "CURRENT HEALTH",
        "  status: CrashLoopBackOff",
        "CONTAINERS",
        "  app: restarting",
    ]


def test_mcp_client_sections_fails_closed_when_no_named_header_is_found() -> None:
    """A capture must fail loudly, not silently sleep past a missing beat.

    If `diagnose_pod`'s headers drift or the tool call errors, none of the
    requested section names match anything in the answer, and `_sections`
    used to return `[]`. The caller then printed nothing and held for
    3.2s anyway, publishing a clip whose central verdict beat is blank
    but looks like normal pacing. `_sections` must instead raise, naming
    the sections it was asked for — never echoing the (unbounded, possibly
    sensitive) tool text it failed to find them in.
    """
    module = _mcp_client_module()
    secret_error = "Traceback: leaked-token=abc123 at /home/whoever/.kube/config"

    with pytest.raises(RuntimeError, match=r"CURRENT HEALTH.*CONTAINERS") as excinfo:
        module._sections(secret_error, "CURRENT HEALTH", "CONTAINERS")

    assert secret_error not in str(excinfo.value), (
        "the failure must name the missing sections, not echo the tool output "
        "it failed to find them in"
    )


def test_mcp_client_sections_matches_a_header_exactly_not_by_prefix() -> None:
    """Round-9 (comment 3859789128): `startswith` swallowed sibling headers.

    `_sections` decided a header line by `line.startswith(name)`, so asking
    for `CONTAINERS` also opened `CONTAINERS SUMMARY` — and every indented
    line under it — turning a bounded verdict beat into whatever the next
    section happens to hold. A header is the whole line (the shipped
    `ToolExecutor` emits `CURRENT HEALTH` and `CONTAINERS` verbatim), so the
    comparison must be equality, tolerating only a trailing colon.
    """
    module = _mcp_client_module()
    answer = "\n".join(
        [
            "CURRENT HEALTH:",
            "  status: CrashLoopBackOff",
            "CONTAINERS",
            "  app: restarting",
            "CONTAINERS SUMMARY",
            "  ...a sibling section this beat never asked for...",
        ]
    )

    kept = module._sections(answer, "CURRENT HEALTH", "CONTAINERS")

    assert kept == [
        "CURRENT HEALTH:",
        "  status: CrashLoopBackOff",
        "CONTAINERS",
        "  app: restarting",
    ], "a trailing colon still matches; a longer header is a different section"
    assert "CONTAINERS SUMMARY" not in kept
    assert not any("sibling section" in line for line in kept), (
        "no line of an unrequested sibling header may reach the pane"
    )


def test_mcp_client_sections_requests_the_headers_the_executor_emits() -> None:
    """Exact matching is only safe if the asked names are the shipped titles.

    `ToolExecutor._diagnose_pod` builds its report from literal section
    titles. Tightening `_sections` to equality would silently blank the
    capture's verdict beat if the client asked for anything but those exact
    strings, so the two are pinned together here.
    """
    executor = (
        Path(__file__).parent.parent / "src" / "korvid" / "tools" / "executor.py"
    ).read_text(encoding="utf-8")

    for name in ("CURRENT HEALTH", "CONTAINERS"):
        assert f'("{name}", ' in executor, (
            f"docs/demo/mcp_client.py asks for the exact header {name!r}; "
            "the shipped diagnose report must still emit it as a whole title"
        )


def test_mcp_client_sections_does_not_repeat_a_duplicated_name() -> None:
    """A repeated name must not print the same section twice.

    The pane is a handful of lines tall. Collecting one section per entry in
    `names` meant a duplicated (or aliased) request doubled its lines and
    pushed the rest of the beat out of frame, so names are deduplicated
    while keeping the order they were asked in.
    """
    module = _mcp_client_module()
    answer = "\n".join(
        [
            "CURRENT HEALTH",
            "  status: CrashLoopBackOff",
            "CONTAINERS",
            "  app: restarting",
        ]
    )

    kept = module._sections(answer, "CONTAINERS", "CURRENT HEALTH", "CONTAINERS")

    assert kept == [
        "CONTAINERS",
        "  app: restarting",
        "CURRENT HEALTH",
        "  status: CrashLoopBackOff",
    ], "each section appears once, in the order it was first asked for"


def test_mcp_client_sections_is_bounded_like_the_tail_is() -> None:
    """Round-9 (comment 3859789128): `_sections` had no line bound at all.

    `_tail` clips to `TAIL_LINES` precisely because a result that overflows
    the pane scrolls the story out of frame. `_sections` returned every
    indented line it matched, and `CONTAINERS` grows with the container
    count, so a wider fixture would silently overflow exactly the beat the
    clip exists to show. The bound must be a constant tied to the pane.
    """
    module = _mcp_client_module()
    assert module.SECTION_MAX_LINES == module.TAIL_LINES * 2, (
        "the section bound must stay derived from the pane-height constant"
    )
    body = [f"  container-{index}: restarting" for index in range(module.SECTION_MAX_LINES + 4)]
    answer = "\n".join(["CONTAINERS", *body])

    kept = module._sections(answer, "CONTAINERS")

    assert len(kept) == module.SECTION_MAX_LINES, (
        f"the pane holds {module.SECTION_MAX_LINES} lines; got {len(kept)}"
    )
    assert kept == ["CONTAINERS", *body[: module.SECTION_MAX_LINES - 1]], (
        "the bound truncates the tail of the section, keeping its header and order"
    )


def test_mcp_client_sections_budgets_each_requested_section_separately() -> None:
    """Round-11 (comment 3861056625): a long section erased the next one.

    `_sections` concatenated every requested section and clipped the
    *total* to `SECTION_MAX_LINES` at the very end, so a `CURRENT HEALTH`
    that grew past the pane budget consumed all ten lines and `CONTAINERS`
    reached no frame at all — while `kept` stayed non-empty, so nothing
    raised and the wrapper promoted a clip whose verdict beat is half
    printed. Each requested section must get its own share of the budget,
    which is the same argument that put a bound on this helper at all.
    """
    module = _mcp_client_module()
    budget = module.SECTION_MAX_LINES
    health = [f"  probe-{index}: failing" for index in range(budget + 3)]
    answer = "\n".join(["CURRENT HEALTH", *health, "CONTAINERS", "  app: restarting"])

    kept = module._sections(answer, "CURRENT HEALTH", "CONTAINERS")

    per_section = max(1, budget // 2)
    assert kept[:per_section] == ["CURRENT HEALTH", *health[: per_section - 1]], (
        "the first section keeps its header and is clipped to its own share"
    )
    assert kept[per_section:] == ["CONTAINERS", "  app: restarting"], (
        "the second requested section must still reach the pane, header first"
    )
    assert len(kept) <= budget, f"the pane holds {budget} lines; got {len(kept)}"


def test_mcp_client_sections_budget_counts_a_repeated_name_once() -> None:
    """Deduplication must shape the budget too, not just the output.

    A name asked twice is still one section on screen, so dividing the pane
    budget by the raw `names` length would halve a single section's share
    for no reason and clip a beat that fits.
    """
    module = _mcp_client_module()
    budget = module.SECTION_MAX_LINES
    body = [f"  container-{index}: restarting" for index in range(budget)]
    answer = "\n".join(["CONTAINERS", *body])

    kept = module._sections(answer, "CONTAINERS", "CONTAINERS")

    assert kept == ["CONTAINERS", *body[: budget - 1]], (
        "one unique name owns the whole budget, however often it was asked for"
    )


@pytest.mark.parametrize(
    ("present", "missing"),
    [("CURRENT HEALTH", "CONTAINERS"), ("CONTAINERS", "CURRENT HEALTH")],
)
def test_mcp_client_sections_fails_closed_when_any_requested_section_is_missing(
    present: str, missing: str
) -> None:
    """Round-11 (comment 3861056625): one found section is not the beat.

    The old guard only refused an answer in which *none* of the requested
    headers appeared. A `diagnose_pod` report that dropped or renamed one
    of them therefore published a half-evidence beat that reads as normal
    pacing — exactly the failure the guard exists to stop. Every requested
    section must be present, and the refusal must name the missing ones
    without echoing the (unbounded, possibly sensitive) answer.
    """
    module = _mcp_client_module()
    secret = "leaked-token=abc123 at /home/whoever/.kube/config"
    answer = "\n".join([present, f"  status: {secret}"])

    with pytest.raises(RuntimeError, match=re.escape(missing)) as excinfo:
        module._sections(answer, "CURRENT HEALTH", "CONTAINERS")

    message = str(excinfo.value)
    assert secret not in message, (
        "the failure must name the missing sections, not echo the tool output"
    )
    assert answer not in message, "no raw answer text may travel in the failure message"


def test_mcp_client_sections_prints_the_real_diagnose_answer_whole() -> None:
    """The budget must not clip the answer the capture actually records.

    The per-section share is a guard against a wider fixture, not a new
    edit of this clip: the shipped `diagnose_pod` report gives
    `CURRENT HEALTH` one line and `CONTAINERS` one line per container
    status, and the demo pod has a single container. Running the *real*
    executor answer through `_sections` therefore has to yield both
    sections complete — header and body — exactly as the published clip
    shows them.
    """
    module = _mcp_client_module()
    harness = _demo_harness()
    story = harness.load_agent_story()
    provider = story.DemoAgentProvider()
    runtime = story.build_demo_agent_runtime(
        harness.DemoReadOps(), harness.ALIASES, provider=provider
    )

    async def drain() -> None:
        async for _event in runtime.run_turn(
            "Why is the payment worker failing?",
            "view=pods ns=shop selected=payment-worker-6c9f7d-b3xnq",
        ):
            pass

    asyncio.run(drain())
    diagnosis = "".join(
        str(message.get("content") or "")
        for message in provider.seen_messages[1]
        if message.get("role") == "tool"
    )
    assert "CURRENT HEALTH" in diagnosis, (
        f"this test is only meaningful over the real report: {diagnosis!r}"
    )

    kept = module._sections(diagnosis, "CURRENT HEALTH", "CONTAINERS")

    lines = diagnosis.splitlines()
    health = lines.index("CURRENT HEALTH")
    containers = lines.index("CONTAINERS")
    whole = [
        *lines[health : health + 2],
        *lines[containers : containers + 2],
    ]
    assert kept == whole, (
        "the recorded beat is unchanged: both sections reach the pane in full, "
        f"header first; got {kept}"
    )
    assert len(kept) <= module.SECTION_MAX_LINES


def test_mcp_client_tail_keeps_the_last_n_lines_in_order() -> None:
    """The happy path is unchanged: the last `TAIL_LINES` lines, in order."""
    module = _mcp_client_module()
    answer = "\n".join(f"line-{index}" for index in range(module.TAIL_LINES + 3))

    tail = module._tail(answer, "get_logs")

    assert tail == [f"line-{index}" for index in range(3, module.TAIL_LINES + 3)]


@pytest.mark.parametrize("answer", ["", "   ", "\n\n\t \n"])
def test_mcp_client_tail_fails_closed_on_empty_or_whitespace_text(answer: str) -> None:
    """A blank answer must abort the capture, not hold on a silent beat.

    `_tail` used to return `[]` for an empty or whitespace-only answer, and
    the caller (`_answered`) then printed nothing and slept for the full
    hold anyway — a blank evidence beat that looks like normal pacing in
    the recorded pane. `_tail` must instead raise, naming only the call
    that produced the blank answer; it must never echo the (unbounded,
    possibly sensitive) answer text — not even the raw `repr` of the blank
    text itself.
    """
    module = _mcp_client_module()

    with pytest.raises(RuntimeError, match=r"helm_list_releases") as excinfo:
        module._tail(answer, "helm_list_releases")

    message = str(excinfo.value)
    assert repr(answer) not in message, (
        "the failure must name the call, not echo the answer text it refused"
    )


def test_mcp_client_tail_is_called_with_its_call_name_for_every_tail_based_answer() -> None:
    """Every tail-printed answer must be traceable back to the call that made it."""
    client = MCP_CLIENT.read_text(encoding="utf-8")
    for name in MCP_CLIENT_TAIL_CALLS:
        assert re.search(rf'_tail\(\s*_text\(\s*\w+,\s*"{name}"\s*\),\s*"{name}"\s*\)', client), (
            f'the {name} answer must be read through `_tail(_text(..., "{name}"), "{name}")`'
        )
    assert client.count("await _answered(_tail(") == len(MCP_CLIENT_TAIL_CALLS), (
        "every `_tail` call must be one of the three story answers, each naming its call — "
        "an extra or bare invocation would tail an answer without a traceable failure name"
    )


def test_mcp_client_url_tracks_the_demo_scenes_own_port() -> None:
    """Round-7 advisory: two processes, two hand-written copies of one port.

    `docs/demo/demo.py` binds `KorvidMCPServer` on `MCP_DEMO_PORT` and
    `docs/demo/mcp_client.py` dials a literal `URL`. The client must *not*
    import the harness to learn the port — it runs in its own process, and
    importing `demo.py` there would drag korvid's whole TUI stack into a
    plain SDK client and defeat the point of the capture. So the two stay
    independent literals, and this test is the joint that keeps them
    synchronized: editing either one alone fails here rather than at
    record time, where the symptom is a connection error captured on film.
    """
    port = _demo_harness().MCP_DEMO_PORT
    assert isinstance(port, int), "the scene must bind a concrete port"

    served = f"http://127.0.0.1:{port}/mcp"
    dialled = _mcp_client_module().URL
    assert dialled == served, (
        "docs/demo/mcp_client.py's URL must address the port docs/demo/demo.py binds"
    )
    assert served == MCP_DEMO_URL, (
        "this module's MCP_DEMO_URL is the third copy; it must agree with both processes"
    )


def test_mcp_follow_tape_composes_the_real_server_with_the_clean_client() -> None:
    """The clip is regenerated by one command, from files in this repository.

    The MCP scene is two processes — the TUI serving `KorvidMCPServer` and
    the external client driving it — so the tape composes them in tmux and
    records the pair. Naming both entry points here is what makes the clip
    reproducible instead of a reviewed artefact nobody can rebuild.
    """
    tape = MCP_TAPE.read_text(encoding="utf-8")

    assert "tmux" in tape, "the two-pane story needs a multiplexer"
    assert "docs/demo/demo.py --scene mcp" in tape, (
        "the left pane must be the real TUI serving the real MCP server"
    )
    assert "docs/demo/mcp_client.py" in tape, "the right pane must be the checked-in MCP SDK client"
    assert MCP_HIDDEN_ALLOWANCE in tape, (
        f"the tape must hide its composition behind {MCP_HIDDEN_ALLOWANCE!r} before "
        "Show, an allowance sized to outlast its own bounded readiness wait"
    )
    assert COLD_START_REFERENCE in tape, (
        f"the tape must point at {COLD_START_REFERENCE} for the reason behind the "
        "allowance instead of restating it"
    )
    assert "uv run --frozen" in tape, (
        "recording a capture must never re-resolve and rewrite uv.lock"
    )
    assert tape.count("uv run") == tape.count("uv run --frozen"), (
        "every launch in the tape must be frozen, not just the first"
    )


def _project_optional_extras() -> set[str]:
    """The extras `pyproject.toml` declares, which `uv run` does not install."""
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(manifest["project"]["optional-dependencies"])


def test_mcp_follow_tape_launches_both_panes_with_the_optional_mcp_extra() -> None:
    """Round-4 review: neither pane can start from a clean checkout without `[mcp]`.

    `mcp` is an optional extra in `pyproject.toml`, not a dependency group,
    so `uv run --frozen` alone syncs an environment without it. The left
    pane's scene lazily imports `korvid.mcp.server` (`docs/demo/demo.py`)
    and the right pane imports the MCP SDK itself
    (`docs/demo/mcp_client.py`), so both launches must ask for the extra or
    the "reproducible from this repository alone" claim fails on the first
    clean checkout that tries it. `--extra mcp` adds nothing to a frame:
    the panes run the same code and print the same output.
    """
    tape = MCP_TAPE.read_text(encoding="utf-8")
    plan = LANDING_VIDEO_PLAN.read_text(encoding="utf-8")
    launches = [
        line for line in tape.splitlines() if not line.lstrip().startswith("#") and "uv run" in line
    ]

    assert "mcp" in _project_optional_extras(), (
        "this guard exists because the MCP stack is an extra, not a default group"
    )
    assert len(launches) == 2, f"the tape composes exactly two launches; found {launches}"
    assert all("uv run --frozen --extra mcp python" in line for line in launches), (
        f"both the demo scene and the SDK client need the `[mcp]` extra enabled: {launches}"
    )
    for pane in ("docs/demo/demo.py --scene mcp", "docs/demo/mcp_client.py"):
        matched = [line for line in launches if pane in line]
        assert len(matched) == 1, f"the {pane} pane must be launched exactly once"
    assert plan.count("uv run --frozen --extra mcp python") == 2, (
        "the plan's executable tape snippet must carry the same two launches"
    )


def test_mcp_capture_provenance_names_the_extra_the_recording_needs() -> None:
    """A reproducibility claim has to name what the reproduction requires."""
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]

    assert "--extra mcp" in mcp, (
        "the provenance section must publish the extra both panes are launched with"
    )
    assert re.search(r"extra|optional", mcp, re.I), (
        "the page must say the MCP stack is optional, which is why the flag exists"
    )


def test_mcp_follow_tape_records_no_host_identity_and_leaves_no_scratch_file() -> None:
    """A screen capture of a terminal publishes whatever the terminal shows.

    tmux's status line carries the host name and the local date, and the
    gate file that holds the client back until the visible timeline starts
    is a side effect of recording. Neither belongs in a published clip or
    in a contributor's checkout, so the tape turns the status line off
    before it attaches and removes the gate file on both sides of the run.
    """
    tape = MCP_TAPE.read_text(encoding="utf-8")

    assert "status off" in tape, (
        "tmux's status line publishes the recording host's name and date; the "
        "tape must turn it off before it attaches"
    )
    assert "/tmp" not in tape, (
        "the gate file must live in the checkout being recorded, not in a "
        "shared world-writable directory"
    )
    assert tape.count(f"rm -f {MCP_GATE_FILE}") >= 1, (
        f"{MCP_GATE_FILE} must be cleared before the run starts, so a signal left by an "
        "interrupted recording cannot release this one's client"
    )
    assert "kill-session" in tape, "the tape must tear its own tmux session down"


class _RecordingController(MCPControllerBase):
    """A controller shaped like the real one, without binding a socket.

    Only the ordering matters here: whether the readiness file exists when
    the server binds, when the TUI mounts, and when the run tears down.
    """

    def __init__(self, log: list[str], ready: Path, *, binds: bool = True) -> None:
        self._log = log
        self._ready = ready
        self._binds = binds
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def status(self) -> str:
        return "MCP on :7878" if self._running else "MCP off"

    async def start(self) -> str:
        self._log.append(f"bind ready={self._ready.exists()}")
        if not self._binds:
            # Exactly how `MCPController.start` reports a bind failure.
            return "ERROR: MCP failed to start (port in use?)"
        self._running = True
        return self.status()

    async def stop(self) -> str:
        self._running = False
        self._log.append(f"stop ready={self._ready.exists()}")
        return "MCP off"

    async def shutdown(self) -> asyncio.Task[None] | None:
        return None


class _MountRecordingApp:
    """The two surfaces `run_mcp_demo` touches: the mount hook and the run."""

    def __init__(self, log: list[str], ready: Path) -> None:
        self._log = log
        self._ready = ready
        self.on_mcp_ready: Callable[[], None] | None = None

    async def run_async(self) -> None:
        self._log.append(f"mount ready={self._ready.exists()}")
        assert self.on_mcp_ready is not None, "run_mcp_demo must arm the mount hook"
        self.on_mcp_ready()
        self._log.append(f"story ready={self._ready.exists()}")


def test_mcp_scene_publishes_readiness_only_after_the_server_bound_and_the_tui_mounted(
    tmp_path: Path,
) -> None:
    """The signal the tape waits for must mean both halves of the scene are up.

    A file touched when the process starts would be exactly the timer the
    tape replaced. `run_mcp_demo` clears any signal an interrupted run left
    behind, starts the server, and only arms the mount hook once the
    controller reports it bound — so the file appears when the TUI mounts,
    which is the first moment the client can connect *and* be mirrored.
    """
    harness = _demo_harness()
    ready = tmp_path / MCP_READY_FILE
    ready.write_text("stale run", encoding="utf-8")
    log: list[str] = []

    controller = _RecordingController(log, ready)
    app = _MountRecordingApp(log, ready)
    asyncio.run(harness.run_mcp_demo(app, controller, ready_file=ready))

    assert log == [
        "bind ready=False",
        "mount ready=False",
        "story ready=True",
        "stop ready=False",
    ], f"readiness must be published at mount and cleared at teardown; got {log}"
    assert not ready.exists(), "a finished recording must leave no readiness file behind"


def test_mcp_scene_publishes_no_readiness_when_the_server_never_binds(
    tmp_path: Path,
) -> None:
    """A failed bind must fail the recording, never release the client.

    `MCPController.start` reports a bind failure by returning an error line
    rather than raising, so a scene that ignored it would run a TUI with no
    server and publish readiness anyway — the exact connection-error capture
    this handshake exists to prevent.
    """
    harness = _demo_harness()
    ready = tmp_path / MCP_READY_FILE
    log: list[str] = []
    controller = _RecordingController(log, ready, binds=False)
    app = _MountRecordingApp(log, ready)

    with pytest.raises(RuntimeError, match="MCP"):
        asyncio.run(harness.run_mcp_demo(app, controller, ready_file=ready))

    assert not ready.exists(), "an unbound server must never publish readiness"
    assert "mount ready=False" not in log, "the TUI must not run without its server"


class _ZombieTaskController(MCPControllerBase):
    """Mirrors the real controller's brief post-timeout window.

    `MCPController.running` means "the server task is alive", not "the
    server is bound". After a start timeout that `start()` failed to reap
    within its own deadline, the task can still be running for a moment
    even though `start()` has already returned its `ERROR:` line. A
    readiness gate that only checks `controller.running` — and ignores the
    status `start()` returned — would arm the mount hook and publish
    readiness over a server that never bound.
    """

    def __init__(self, log: list[str], ready: Path) -> None:
        self._log = log
        self._ready = ready

    @property
    def running(self) -> bool:
        return True

    def status(self) -> str:
        return "ERROR: MCP failed to start (port in use?)"

    async def start(self) -> str:
        self._log.append(f"bind ready={self._ready.exists()}")
        return "ERROR: MCP failed to start (port in use?)"

    async def stop(self) -> str:
        self._log.append(f"stop ready={self._ready.exists()}")
        return "MCP off"

    async def shutdown(self) -> asyncio.Task[None] | None:
        return None


def test_mcp_scene_fails_closed_when_start_errors_despite_a_zombie_running_task(
    tmp_path: Path,
) -> None:
    """A start-time error must fail the recording even if `running` is True.

    The real controller can leave its task alive for a moment after a start
    timeout it did not manage to reap in time, so `running` alone cannot be
    trusted as the readiness gate. `run_mcp_demo` must also reject a status
    that begins with the `ERROR:` line `start()` uses to report a bind
    failure, arm nothing, and still stop the controller during cleanup.
    """
    harness = _demo_harness()
    ready = tmp_path / MCP_READY_FILE
    log: list[str] = []
    controller = _ZombieTaskController(log, ready)
    app = _MountRecordingApp(log, ready)

    with pytest.raises(RuntimeError, match="MCP"):
        asyncio.run(harness.run_mcp_demo(app, controller, ready_file=ready))

    assert app.on_mcp_ready is None, "the mount hook must never be armed on a failed start"
    assert "mount ready=False" not in log, "the TUI must not run without a bound server"
    assert not ready.exists(), "a failed start must never publish readiness"
    assert log == [
        "bind ready=False",
        "stop ready=False",
    ], f"cleanup must still stop a controller left running after a failed start; got {log}"


class _ExplodingStartController(MCPControllerBase):
    """A controller whose `start()` raises instead of returning a status line.

    `MCPController.start` reports the failures it catches itself by
    returning an `ERROR:` line, but that is not the only way it ends. A
    cancellation while binding, or a failure creating the server task,
    leaves the exception in place — and by then part of the server may
    already hold the port. If `start()` is called outside the cleanup
    block, that partially started server survives until the process exits
    and the next take fails to bind.
    """

    def __init__(self, log: list[str], ready: Path) -> None:
        self._log = log
        self._ready = ready

    @property
    def running(self) -> bool:
        return False

    def status(self) -> str:
        return "MCP off"

    async def start(self) -> str:
        self._log.append(f"bind ready={self._ready.exists()}")
        raise RuntimeError("cancelled while binding the MCP server")

    async def stop(self) -> str:
        self._log.append(f"stop ready={self._ready.exists()}")
        return "MCP off"

    async def shutdown(self) -> asyncio.Task[None] | None:
        return None


def test_mcp_scene_stops_a_controller_whose_start_raised(tmp_path: Path) -> None:
    """A raising `start()` must still reach the cleanup that stops the server.

    `start()` announces the failures it catches by returning an `ERROR:`
    line, so the readiness gate reads that line. It is not the only exit:
    cancellation during the bind, or a failure creating the internal task,
    propagates as an exception. Started outside the `try`, that exception
    skips the cleanup entirely — nothing calls `stop()`, a half-started
    server can keep port 7878 for the life of the process, and the next
    take of the recording fails to bind for a reason that has nothing to do
    with the take.
    """
    harness = _demo_harness()
    ready = tmp_path / MCP_READY_FILE
    ready.write_text("stale run", encoding="utf-8")
    log: list[str] = []
    controller = _ExplodingStartController(log, ready)
    app = _MountRecordingApp(log, ready)

    with pytest.raises(RuntimeError, match="binding the MCP server"):
        asyncio.run(harness.run_mcp_demo(app, controller, ready_file=ready))

    assert log == [
        "bind ready=False",
        "stop ready=False",
    ], f"a raising start must still be stopped during cleanup; got {log}"
    assert app.on_mcp_ready is None, "the mount hook must never be armed after a raising start"
    assert not ready.exists(), "a failed start must leave no readiness file behind"


def test_demo_ui_bridge_proxy_composes_not_ready_from_the_shared_error_prefix() -> None:
    """The harness's degraded answer must be the product's error contract.

    `run_mcp_demo` already judges the controller's status with the imported
    `ERROR_PREFIX`, so the same module stating the same contract as a
    literal is drift waiting to happen: if the product's prefix changed,
    this return value alone would stop being an error line and the external
    MCP host would read "UI not ready" as an ordinary text answer.
    """
    harness = _demo_harness()
    proxy = harness._UIBridgeProxy()

    assert f"{ERROR_PREFIX} UI not ready" == proxy._NOT_READY
    assert asyncio.run(proxy.agent_navigate("pods")).startswith(ERROR_PREFIX)

    source = DEMO_HARNESS.read_text(encoding="utf-8")
    assert '_NOT_READY = f"{ERROR_PREFIX} UI not ready"' in source, (
        "the harness must compose its not-ready line from the imported constant"
    )
    assert '"ERROR: UI not ready"' not in source, (
        "a hardcoded prefix drifts silently from korvid.tools.structured.ERROR_PREFIX"
    )


def test_every_demo_fixture_row_carries_a_relative_creation_timestamp() -> None:
    """Each row the capture can show must render an AGE, and a relative one.

    The fixture's ages are relative on purpose: a capture made today and
    one made a year later must render identically, and no frame may carry
    a calendar date. An empty `created` breaks both halves of that — the
    row renders korvid's "-" placeholder while every neighbour renders a
    real age, so the one blank cell reads as missing data in the frame and
    in any external host's `list_resources` answer.
    """
    harness = _demo_harness()
    rows = [*harness.PODS, *(row for group in harness.EXTRA.values() for row in group)]
    assert len(rows) > 10, "the fixture should still hold every scene's rows"

    for row in rows:
        created = getattr(row, "created", "")
        assert created, f"{row.kind} {row.namespace}/{row.name} ships without a creation timestamp"
        moment = datetime.fromisoformat(created)
        assert moment.tzinfo is not None, f"{row.name}'s timestamp must carry its UTC offset"
        assert format_age(created) != "-", (
            f"{row.kind} {row.namespace}/{row.name} renders an empty AGE column"
        )

    configmap = harness.EXTRA["configmaps"][0]
    age = datetime.now(UTC) - datetime.fromisoformat(configmap.created)
    assert timedelta(hours=11) < age < timedelta(hours=13), (
        f"the ConfigMap must sit at the documented ~12h offset; it is {age} old"
    )
    assert format_age(configmap.created) == "12h"


async def test_mcp_scene_signals_readiness_from_the_real_textual_mount() -> None:
    """Reading the hook proves its shape; mounting the app proves it fires.

    The recording's whole guarantee is that the file appears when korvid is
    on screen, so the contract is taken from `DemoKorvidApp`'s real Textual
    mount rather than from a call to the handler.
    """
    harness = _demo_harness()
    app = harness.build_demo_app("mcp", None)
    published: list[str] = []
    app.on_mcp_ready = lambda: published.append("ready")

    async with app.run_test():
        pass

    assert published == ["ready"], (
        "the mcp scene must publish its readiness signal exactly once, at mount"
    )


def test_mcp_capture_provenance_publishes_the_readiness_handshake() -> None:
    """The published recipe must describe the handshake it actually runs.

    The page told contributors about one scratch file and called it "the
    handshake file the tape uses to release the client", which described a
    release driven by a timer. There are two files now, and the point of the
    second one is that the release is not a timer at all: the scene publishes
    readiness from its Textual mount, over a server it has already bound, and
    only then may the client be let go.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    lowered = mcp.lower()

    for scratch in (MCP_READY_FILE, MCP_GATE_FILE):
        assert scratch in mcp, f"the provenance page must name {scratch}"
    assert "created and removed inside the checkout" in lowered, (
        "both handshake files stay in the checkout being recorded; the page must say so"
    )
    assert "bound" in lowered, (
        "the page must state the first half of readiness: the MCP server is bound"
    )
    assert "mount" in lowered, (
        "the page must state the second half of readiness: the TUI has mounted"
    )
    assert re.search(r"not (released )?on a timer|never (released )?on a timer", lowered), (
        "the page must say the client is not released by a timer, since that is the "
        "failure mode the readiness file removes"
    )
    assert "connection error" in lowered, (
        "the page must name what the handshake prevents: a capture that opens on the "
        "client's connection error"
    )


def test_landing_video_plan_ships_the_recorded_mcp_tape_not_a_timer_gate() -> None:
    """This plan is new and executable, so its tape must be the shipped tape.

    Replaying its `Step 5` verbatim would otherwise recreate a gate file in
    the shared world-writable `/tmp`, a client released by a fixed sleep, and
    a `Ctrl+B :run-shell` trigger typed *into the attached session* — the
    keystroke path the capture's own provenance promises never happens. It
    would also recreate a tape that renders straight onto the published
    clip, which is the one thing no failed recording may be able to do.
    """
    plan = LANDING_VIDEO_PLAN.read_text(encoding="utf-8")
    marker = "Create `docs/demo/mcp-follow.tape`:"
    assert marker in plan, "the plan must still create the MCP tape"
    snippet = plan.split(marker, 1)[1].split("```", 2)[1].split("\n", 1)[1]
    assert snippet.strip() == MCP_TAPE.read_text(encoding="utf-8").strip(), (
        "the plan's mcp-follow.tape snippet must be the shipped tape, readiness "
        "handshake and repo-local scratch files included"
    )
    assert "/tmp/korvid-mcp-demo" not in plan, (
        "no plan snippet may put the recording's handshake in a shared world-writable directory"
    )
    assert MCP_READY_FILE in plan, (
        "the plan must describe the readiness signal the release now waits for"
    )
    assert MCP_RECORDER_COMMAND in plan, (
        "the plan's own regeneration step must run the wrapper that owns the published clip"
    )


def test_mcp_follow_tape_releases_its_client_on_readiness_not_on_a_timer() -> None:
    """A fixed sleep is an allowance, not a signal.

    The client pane blocks on a gate file, but the gate used to be dropped by
    a background timer that started as soon as the shared 20-second cold-start
    allowance elapsed. On a cold checkout — `uv` resolving the project, the
    first watch, uvicorn binding — that allowance can expire before port 7878
    is listening, and the "reproducible" capture then opens on the client's
    connection error. The allowance stays (it is what keeps the composition
    off screen), but the release itself must wait for the scene's own
    readiness file, and the wait must be bounded so a server that never binds
    fails the recording loudly instead of hanging forever.
    """
    tape = MCP_TAPE.read_text(encoding="utf-8")
    lines = tape.splitlines()

    releases = [line for line in lines if f"touch {MCP_GATE_FILE}" in line]
    assert len(releases) == 1, f"exactly one line may arm the client gate; found {releases}"
    release = releases[0]
    assert MCP_READY_FILE in release, (
        f"the gate must be armed only after {MCP_READY_FILE} exists, not by a timer alone"
    )
    assert release.index(MCP_READY_FILE) < release.index(f"touch {MCP_GATE_FILE}"), (
        "the readiness check must guard the release inside the same command, so no "
        "shell scheduling can reorder them"
    )
    assert "attach-session" in release, (
        "the attach must ride the same command as the release, so the recorded "
        "timeline and the client start from one decision"
    )

    waits = [
        index
        for index, line in enumerate(lines)
        if MCP_READY_FILE in line and re.search(r"while .*-f |until .*-f ", line)
    ]
    assert waits, f"the tape must actually wait for {MCP_READY_FILE} before it releases"
    wait_index, release_index = waits[0], lines.index(release)
    assert wait_index < release_index, "the wait must come before the release"
    assert re.search(r"-lt \d+|-le \d+|seq \d+", lines[wait_index]), (
        "the readiness wait must be bounded, so a server that never binds fails the "
        f"recording instead of hanging the tape forever: {lines[wait_index]!r}"
    )

    cold_start = [index for index, line in enumerate(lines) if line.strip() == MCP_HIDDEN_ALLOWANCE]
    assert cold_start, f"the tape must still hide the {MCP_HIDDEN_ALLOWANCE!r} allowance"
    assert cold_start[-1] < release_index, (
        f"the hidden {MCP_HIDDEN_ALLOWANCE!r} allowance must still be hidden before the "
        "release, with the readiness wait layered on top of it"
    )
    assert all("Show" not in line for line in lines[:release_index]), (
        "the whole handshake must stay inside the hidden composition block"
    )


def _mcp_typed_command(needle: str) -> str:
    """The one shell command the tape types that contains `needle`.

    The tape drives a real bash session, so its contracts are about shell
    semantics rather than about substrings sharing a line. Pulling the exact
    command out lets a test hand it to bash and watch what runs.
    """
    typed = [
        line
        for line in MCP_TAPE.read_text(encoding="utf-8").splitlines()
        if line.startswith('Type "') and needle in line
    ]
    assert len(typed) == 1, f"exactly one typed command may contain {needle!r}; found {typed}"
    command = typed[0]
    assert command.endswith('"'), f"a typed command must be a closed VHS string: {command!r}"
    return command[len('Type "') : -1]


def _run_mcp_release(workdir: Path, *, ready: bool) -> tuple[int, str, str]:
    """Run the tape's release command against stubbed `tmux`/`clear`.

    Returns the exit status, everything the stubs recorded, and everything
    the command printed. `tmux` and `clear` are shell stubs on `PATH`, so no
    multiplexer, terminal or korvid process is involved.
    """
    stub_dir = workdir / "stubs"
    stub_dir.mkdir()
    invocations = workdir / "invocations.log"
    for name in ("tmux", "clear"):
        stub = stub_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{invocations}"\n', encoding="utf-8")
        stub.chmod(0o755)

    if ready:
        (workdir / MCP_READY_FILE).touch()

    environment = dict(os.environ)
    environment["PATH"] = f"{stub_dir}{os.pathsep}{environment.get('PATH', '')}"
    completed = subprocess.run(
        ["bash", "-c", _mcp_typed_command(f"touch {MCP_GATE_FILE}")],
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    recorded = invocations.read_text(encoding="utf-8") if invocations.exists() else ""
    return completed.returncode, recorded, completed.stdout + completed.stderr


def test_mcp_follow_tape_never_attaches_without_the_readiness_signal(tmp_path: Path) -> None:
    """The guard must be a branch bash honours, not two clauses on one line.

    The release used to read
    `[ -f READY ] && ( sleep 0.7; touch GO ) & clear; tmux attach-session ...`.
    Bash parses `&` as a list terminator, not as an operand of `&&`: the whole
    `and_or` list — readiness test included — is what gets backgrounded, and
    `clear; tmux attach-session` is a *separate* command that runs
    unconditionally. So the tape attached and recorded the story even when
    readiness never arrived, which is exactly the connection-error capture the
    handshake exists to prevent.

    This contract runs the shipped command under bash with stubbed `tmux` and
    `clear`, so it proves the guard by observing whether `attach-session` is
    reached — not by observing that a substring shares a line with it.
    """
    absent = tmp_path / "readiness-absent"
    absent.mkdir()
    status, recorded, output = _run_mcp_release(absent, ready=False)

    assert "attach-session" not in recorded, (
        "without the readiness signal the tape must not attach; bash reached "
        f"attach-session anyway: {recorded!r}"
    )
    assert status != 0, (
        "a recording whose scene never became ready must fail loudly rather than "
        f"capture whatever the terminal happened to show; exit status was {status}"
    )
    assert output.strip(), (
        "the missing-readiness branch must print why the capture was abandoned; it printed nothing"
    )
    assert not (absent / MCP_GATE_FILE).exists(), (
        "the client gate must stay shut when readiness never arrived"
    )

    present = tmp_path / "readiness-present"
    present.mkdir()
    status, recorded, output = _run_mcp_release(present, ready=True)

    assert status == 0, f"the readiness-success branch must succeed; it exited {status}: {output!r}"
    assert f"attach-session -t {MCP_TMUX_SESSION}" in recorded, (
        f"with readiness published the tape must attach {MCP_TMUX_SESSION}; the stubs "
        f"recorded {recorded!r}"
    )
    assert (present / MCP_GATE_FILE).exists(), (
        "the readiness-success branch must also arm the client gate, from the same branch"
    )


def test_mcp_follow_tape_hidden_allowance_outlasts_its_bounded_readiness_wait() -> None:
    """VHS's clock and the shell's clock are independent; only one is typed into.

    `Type` puts the readiness loop into the shell and returns; the following
    `Sleep` then runs on VHS's own timeline. VHS never learns that the loop
    finished, so an allowance shorter than the loop's own bound lets VHS type
    the release and reach `Show` while the shell is still spinning — the
    composing shell, the release command and an unattached prompt would all
    land in the captured frames.

    The bound itself must be wall-clock, not an iteration count. Counting
    `sleep 0.1` iterations up to a fixed total (`waited -lt 600`) assumes each
    iteration costs exactly its `sleep` argument, but fork+exec of `sleep`
    itself is not free: 600 iterations measured ~69 s of real time, already
    past this tape's own 65 s hidden allowance. The loop instead resets
    bash's own `SECONDS` builtin (`SECONDS=0`) and checks `$SECONDS` directly,
    so the deadline is ~60 s of real time regardless of how much the loop's
    own bookkeeping costs. The fix is then an ordering between two numbers
    that live in different files' worth of reasoning but in one tape: the
    loop is bounded at a 60 s wall-clock deadline, so the hidden allowance is
    65 s — at least 5 s of margin. Releasing early is harmless in the other
    direction — the client pane is blocked on the separate gate file, so no
    part of the story can run inside the hidden block.
    """
    tape_text = MCP_TAPE.read_text(encoding="utf-8")
    assert "600" not in tape_text, (
        "no comment or command may still bound the readiness wait by counting "
        "600 iterations of sleep 0.1; the bound is wall-clock now"
    )
    lines = tape_text.splitlines()

    wait_indices = [
        index
        for index, line in enumerate(lines)
        if MCP_READY_FILE in line and re.search(r"while .*-f |until .*-f ", line)
    ]
    assert wait_indices, f"the tape must wait for {MCP_READY_FILE} inside its hidden block"
    wait_line = lines[wait_indices[0]]

    assert not re.search(r"waited=|waited\+", wait_line), (
        f"the wait must no longer count iterations into a hand-rolled counter: {wait_line!r}"
    )

    assert re.search(r"\bSECONDS=0\b", wait_line), (
        f"the readiness wait must reset bash's own SECONDS builtin before looping, "
        f"so the deadline is wall-clock time rather than an iteration count: {wait_line!r}"
    )
    assert re.search(r"SECONDS=0.*while ", wait_line), (
        f"SECONDS must be reset before the loop starts, not after: {wait_line!r}"
    )

    deadline = re.search(r"\$SECONDS -lt (\d+)", wait_line)
    assert deadline, f"the readiness wait must bound itself on bash's own $SECONDS: {wait_line!r}"
    wait_seconds = float(deadline.group(1))
    assert wait_seconds == pytest.approx(MCP_READY_WAIT_SECONDS), (
        f"the readiness loop must stay bounded at a {MCP_READY_WAIT_SECONDS} s wall-clock "
        f"deadline; {wait_line!r} bounds it at {wait_seconds} s"
    )

    allowances = [
        (index, float(match.group(1)))
        for index, line in enumerate(lines)
        if index > wait_indices[0]
        and (match := re.fullmatch(r"Sleep (\d+(?:\.\d+)?)s", line.strip()))
    ]
    assert allowances, "the tape must hide the bounded wait behind a VHS Sleep"
    allowance_index, allowance_seconds = allowances[0]

    assert allowance_seconds >= wait_seconds + MCP_HIDDEN_ALLOWANCE_MARGIN_SECONDS, (
        f"VHS advances independently of the shell, so the hidden allowance must cover "
        f"the whole {wait_seconds} s bound with at least "
        f"{MCP_HIDDEN_ALLOWANCE_MARGIN_SECONDS} s of margin; it is {allowance_seconds} s"
    )
    assert lines[allowance_index].strip() == MCP_HIDDEN_ALLOWANCE, (
        f"the allowance covering the bounded wait must be {MCP_HIDDEN_ALLOWANCE!r}; "
        f"found {lines[allowance_index]!r}"
    )

    show = [index for index, line in enumerate(lines) if line.strip() == "Show"]
    assert show, "the tape must still reveal a captured story"
    assert allowance_index < show[0], (
        "the allowance must run inside the hidden composition block, so raising it "
        "costs the captured story nothing"
    )


def test_mcp_provenance_states_the_allowance_covers_the_bounded_wait() -> None:
    """The page must not claim VHS watches the shell finish; it cannot.

    The published recipe described the readiness wait as running "inside the
    hidden cold-start allowance", which reads as if VHS resumed when the loop
    returned. It does not: the allowance is a fixed sleep on VHS's own clock,
    and the only thing that makes it safe is that it is longer than the bound
    the loop enforces. A contributor who trusted the old wording would size
    the allowance to a typical start and reintroduce the race.

    The page must also describe the bound itself as wall-clock, on bash's own
    `SECONDS` builtin, rather than as a count of `sleep 0.1` iterations —
    that iteration count measured ~69 s of real fork/exec overhead against
    this tape's 65 s hidden allowance, which is exactly the race this
    provenance exists to rule out.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    lowered = " ".join(mcp.lower().split())

    assert "65" in mcp, "the page must publish the hidden allowance that covers the bounded wait"
    assert "60 s" in mcp, "the page must publish the bound the readiness loop enforces"
    assert re.search(r"own clock|independent|does not (wait|observe)", lowered), (
        "the page must say VHS advances on its own clock rather than on the shell's, "
        "since that is why the allowance has to outlast the bound"
    )
    assert not re.search(r"vhs (waits|resumes|continues) (for|when|once|until)", lowered), (
        "VHS never observes the typed shell finishing; the page may not say it does"
    )
    assert re.search(r"without attach|not attach|never attach", lowered), (
        "the page must state the fail-closed half: no readiness signal, no attach"
    )
    assert "wall-clock" in lowered, (
        "the page must describe the readiness bound as wall-clock, not an iteration count"
    )
    assert "seconds" in lowered, "the page must name bash's SECONDS builtin by name"
    assert "600" not in mcp, (
        "the page may not still describe the bound as 600 iterations of sleep 0.1; "
        "that arithmetic is what the wall-clock SECONDS bound replaces"
    )


def test_mcp_follow_tape_cleans_both_of_its_handshake_files() -> None:
    """Two scratch files now, both born of recording and neither committed."""
    tape = MCP_TAPE.read_text(encoding="utf-8")
    script = MCP_RECORDER.read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for scratch in (MCP_GATE_FILE, MCP_READY_FILE):
        cleared = [line for line in tape.splitlines() if "rm -f" in line and scratch in line]
        assert cleared, (
            f"{scratch} must be cleared before the run starts, so an interrupted "
            f"recording cannot hand its signals to the next one; found {cleared}"
        )
        assert scratch in script, (
            f"{scratch} outlives an aborted tape, so {MCP_RECORDER_COMMAND} must remove it "
            "on every exit path"
        )
        assert re.search(rf"^{re.escape(scratch)}$", gitignore, re.MULTILINE), (
            f"{scratch} is a recording side effect; it must never be committable"
        )


def test_mcp_clip_duration_holds_the_whole_follow_story() -> None:
    """Four mirrored reads need time on screen, and no more than that.

    The previous clip was a four-second reframe of someone else's
    recording: korvid moved three times with no room to read any of it.
    The story is only evidence if the log pane is still legible when the
    Helm view replaces it, and only a landing clip if it ends before a
    visitor's attention does.
    """
    duration = _mp4_duration(SCENES / "mcp-follow-demo.mp4")
    assert 12 <= duration <= 15, (
        f"mcp-follow-demo.mp4 runs {duration:.2f}s; the four-read follow story "
        "must settle on screen within a 12-15s clip"
    )


def test_mcp_clip_geometry_matches_the_reserved_landing_box() -> None:
    """The landing page reserves 1280x710 for this clip; it must store it."""
    stored = _mp4_geometry(SCENES / "mcp-follow-demo.mp4")[0]
    assert stored == (1280, 710), (
        f"mcp-follow-demo.mp4 stores {stored}, not the reserved (1280, 710) box"
    )


def test_mcp_landing_poster_shows_both_panes_of_korvids_own_capture() -> None:
    """The MCP tile's whole claim is that two panes agree; both must be legible.

    The poster is cut mid-story, while the external client's calls and the
    view korvid mirrored for them are on screen together. It stands in for
    the clip until the clip plays, so a poster cut from a frame where either
    pane is blank, half-drawn or scrolled away would advertise the follow
    story without showing it. Nothing here is cleared or redrawn: the
    capture is korvid's own work end to end, so the contract asks for
    evidence in both columns rather than for the absence of someone else's.
    """
    width, height, rows = _decode_png_rgb(SCENES / "mcp-poster.png")
    assert (width, height) == (1280, 710)
    channels = len(rows[0]) // width

    top, bottom = MCP_EVIDENCE_BAND
    for label, columns in (("korvid", MCP_TUI_PANE), ("the MCP client", MCP_CLIENT_PANE)):
        pixels = _band_contrast_pixels(
            rows,
            top,
            bottom,
            channels,
            minimum_deviation=100,
            columns=columns,
        )
        assert pixels >= MCP_EVIDENCE_MIN_PIXELS, (
            f"{label}'s pane must carry legible content in the poster; only "
            f"{pixels} high-contrast pixels remain in columns {columns[0]}-{columns[1]}"
        )


def test_mcp_evidence_coverage_rejects_a_single_bright_pixel() -> None:
    """Peak contrast alone must not make a blank evidence band pass."""
    left, right = MCP_CLIENT_PANE
    rows = [bytearray([0x11] * right * 3)]
    rows[0][left * 3] = 0xFF
    assert _band_deviation(rows, 0, 1, 3) > 100
    assert _band_contrast_pixels(rows, 0, 1, 3, minimum_deviation=100) < MCP_EVIDENCE_MIN_PIXELS


def test_mcp_capture_instructions_publish_the_whole_reproducible_recording() -> None:
    """A contributor must be able to re-cut this clip from the repository alone.

    The MCP tile used to be a re-encode of a third-party capture, kept
    honest by a byte pin and a redaction recipe because nobody else could
    reproduce it. It is now recorded here, so the provenance page has to
    publish the three commands that make it — the tape, the poster cut and
    the probe that proves the geometry — and name the two sources the tape
    composes, or the pin's job falls to nothing.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    for fragment in (
        MCP_RECORDER_COMMAND,
        "docs/demo/mcp_client.py",
        "--scene mcp",
        "docs/assets/scenes/mcp-poster.png",
        "ffprobe",
    ):
        assert fragment in mcp, f"the MCP capture instructions must publish `{fragment}`"
    lowered = mcp.lower()
    assert "follow" in lowered
    for reason in ("loopback", "synthetic", "read-only"):
        assert reason in lowered, (
            f"the instructions must say why the capture is safe to publish; {reason!r} is missing"
        )


def test_mcp_recording_recipe_is_the_wrapper_and_never_a_bare_vhs_run() -> None:
    """A published recipe that runs VHS directly re-opens the whole hole.

    `vhs docs/demo/mcp-follow.tape` renders whatever the tape produced and
    exits 0. Before the boundary moved out of the tape that command
    overwrote the approved clip on its way to reporting a failure it could
    not enforce; it now leaves an unpromoted candidate and no verdict at
    all. Either way it is not the recipe, so no page, plan or tape header
    may offer it as one.

    Every place that documents how the clip is made must name the wrapper,
    the candidate the tape renders to, and the rule that decides promotion.
    """
    sources = {
        "docs/demo/visual-storytelling.md": INSTRUCTIONS.read_text(encoding="utf-8"),
        "docs/demo/mcp-follow.tape": MCP_TAPE.read_text(encoding="utf-8"),
        "docs/superpowers/plans/2026-08-22-visual-storytelling.md": VISUAL_STORYTELLING_PLAN.read_text(
            encoding="utf-8"
        ),
        "docs/superpowers/plans/2026-08-26-landing-video-experience.md": LANDING_VIDEO_PLAN.read_text(
            encoding="utf-8"
        ),
        MCP_RECORDER_COMMAND: MCP_RECORDER.read_text(encoding="utf-8"),
    }

    for label, text in sources.items():
        assert "vhs docs/demo/mcp-follow.tape" not in text, (
            f"{label} still offers a bare VHS run as the recipe; VHS cannot fail a "
            f"recording, so the published command must be {MCP_RECORDER_COMMAND}"
        )

    for label in (
        "docs/demo/visual-storytelling.md",
        "docs/superpowers/plans/2026-08-22-visual-storytelling.md",
        "docs/superpowers/plans/2026-08-26-landing-video-experience.md",
    ):
        text = sources[label]
        assert MCP_RECORDER_COMMAND in text, f"{label} must publish {MCP_RECORDER_COMMAND}"
        assert MCP_CANDIDATE_CLIP in text, (
            f"{label} must publish the candidate the tape renders to, or the promotion "
            "step reads as an unexplained extra file"
        )

    mcp = sources["docs/demo/visual-storytelling.md"]
    mcp = mcp[mcp.index("## MCP follow") :]
    lowered = " ".join(mcp.split()).lower()
    assert "exit status" in lowered, (
        "the page must explain why the wrapper exists: a tape's own exit status decides "
        "nothing about what VHS already rendered"
    )
    assert re.search(r"promot\w+", lowered), (
        "the page must name the promotion step that turns the candidate into the published clip"
    )
    assert re.search(r"byte-identical|unchanged|untouched", lowered), (
        "the page must state what a failed run leaves behind: the previously approved "
        "clip, exactly as it was"
    )


def test_mcp_provenance_publishes_the_token_scan_and_the_same_directory_rule() -> None:
    """The recipe's two preconditions belong on the page that publishes it.

    A reader who regenerates this clip has to know what the wrapper will
    refuse before it starts VHS, because both rules constrain how a tape
    and an override may be written. The first is the token scan: VHS reads
    whitespace-separated tokens, so `Output` is counted wherever it appears
    and the one that survives has to be a directive on its own line naming
    the candidate. The second is the same-directory rule: promotion is a
    rename, which is only atomic inside one directory, so the candidate is
    rendered beside the published clip and any override has to keep them
    there.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    lowered = " ".join(mcp.split()).lower()

    assert re.search(r"token|field", lowered), (
        "the page must say the tape is read as tokens, not as lines; that is the whole "
        "reason a second `Output` cannot hide behind `Hide`"
    )
    assert re.search(r"rename", lowered), (
        "the page claims promotion is atomic; it must name the rename that makes it so"
    )
    assert re.search(r"same directory|one directory", lowered), (
        "a rename is atomic only inside one directory; the page must state the "
        "precondition it depends on"
    )
    assert re.search(r"override", lowered), (
        "the overrides are the one way that invariant can be broken, so the page must "
        "state that they have to preserve it"
    )


def test_mcp_capture_instructions_disclose_the_documentation_only_describe_dismissal() -> None:
    """The one piece of choreography in the capture must be named, not hidden.

    `diagnose_pod` opens a modal `DescribeScreen` through korvid's own follow
    bridge, and the shipped user-priority guard *would* refuse to mirror any
    later call while that screen stayed up — the user is reading it.
    `DemoKorvidApp`, a documentation-only harness, stands in for the Esc a
    watching operator would press by closing that modal after
    `MCP_DESCRIBE_HOLD = 2.2` s, which lands inside the client's 3.2 s
    `diagnose_pod` beat and therefore before `get_logs` is ever issued: no
    call in the recording is actually refused. The provenance page must
    disclose this plainly: the dismissal is documentation-only, it happens
    after 2.2 seconds, no keystroke is sent to the TUI, and the shipped guard
    itself is not weakened.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    lowered = mcp.lower()
    assert "documentation-only" in lowered, (
        "the provenance page must call the describe-dismissal harness documentation-only"
    )
    assert "2.2" in mcp, "the provenance page must state the 2.2 second describe hold"
    assert "demokorvidapp" in lowered, (
        "the provenance page must name the harness that closes the modal"
    )
    assert "get_logs" in mcp, "the provenance page must name the mirror the dismissal protects"
    assert "helm" in lowered, (
        "the provenance page must name the second mirror the dismissal protects"
    )
    assert "no keystroke is sent" in lowered or "no tui keystroke is sent" in lowered, (
        "the provenance page must state that no keystroke is sent to the TUI"
    )
    assert "guard" in lowered, "the provenance page must name the shipped guard"
    assert "not weakened" in lowered or "is not weakened" in lowered, (
        "the provenance page must state that the shipped guard is not weakened"
    )


def test_mcp_provenance_states_the_guard_refusal_only_as_a_counterfactual() -> None:
    """The recording contains no refusal, so the page may not claim one.

    The page used to say the shipped user-priority guard "then correctly
    refuses to mirror the next two calls" while the follow-opened describe
    modal is up. Nothing in the captured timeline does that:
    `MCP_DESCRIBE_HOLD` dismisses the modal 2.2 s into the client's 3.2 s
    `diagnose_pod` beat, so the modal is already gone when `get_logs` is
    issued and all four mirrors succeed. Reading that sentence, a visitor
    would look for two refused mirrors in a clip that shows four accepted
    ones.

    The truthful shape is counterfactual: the guard *would* refuse a later
    mirror if the modal stayed up, which is exactly why the
    documentation-only harness closes it first. So every indicative refusal
    claim in this section must be either conditional or denied *in its own
    clause*, and the page must publish the beat the dismissal lands inside,
    the ordering it buys, and the fact that no mirror is refused.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    for clause in _prose_clauses(mcp):
        if not _INDICATIVE_REFUSAL.search(clause):
            continue
        assert _UNASSERTED_MOOD.search(clause), (
            "the captured timeline holds no refusal — the documentation-only harness "
            f"dismisses the describe modal at 2.2 s, inside the client's "
            f"{MCP_DIAGNOSE_HOLD} s `diagnose_pod` beat and before `get_logs` is issued — "
            f"so a refusal may only be stated as a counterfactual: {clause!r}"
        )

    lowered = " ".join(mcp.lower().split())
    assert "would refuse" in lowered, (
        "the page must keep the shipped guard's rule visible, in the conditional: "
        "it would refuse a later mirror if the modal stayed up"
    )
    assert MCP_DIAGNOSE_HOLD in mcp, (
        f"the page must publish the {MCP_DIAGNOSE_HOLD} s `diagnose_pod` beat that the "
        "2.2 s dismissal lands inside, or the ordering is unverifiable"
    )
    ordering = [
        sentence
        for sentence in _prose_sentences(mcp)
        if "get_logs" in sentence and "before" in sentence.lower()
    ]
    assert ordering, (
        "the page must state the ordering the dismissal buys: the modal closes "
        "before `get_logs` is issued"
    )
    assert "succeed" in lowered, (
        "the page must say what the capture actually shows — every mirror succeeding"
    )


def test_mcp_landing_ratio_override_matches_the_clips_display_geometry() -> None:
    """`aspect-ratio: 1280 / 710` is only truthful for a square-pixel clip.

    The landing CSS reserves the MCP box from the stored pixel geometry the
    markup declares. That reservation is a claim about layout, and layout
    uses the *display* box — so the override is correct exactly while the
    clip's sample aspect ratio is 1:1. The reason is kept next to the rule so
    a later reader does not have to re-derive it from an ffprobe dump.
    """
    stored, displayed, pixel_aspect = _mp4_geometry(SCENES / "mcp-follow-demo.mp4")
    css = EXTRA_CSS.read_text(encoding="utf-8")
    rule = ".md-typeset .scene-panel video.mcp-media"
    assert rule in css, "the MCP ratio override must still exist"

    assert pixel_aspect == (1, 1)
    assert displayed == (float(stored[0]), float(stored[1]))
    assert f"aspect-ratio: {stored[0]} / {stored[1]}" in css[css.index(rule) :], (
        f"the override must reserve the clip's real display box {displayed}"
    )

    reason = css[css.rindex("/*", 0, css.index(rule)) : css.index(rule)].lower()
    for fragment in ("square", "setsar=1", "sample aspect"):
        assert fragment in reason, (
            "the rule's comment must say the reservation holds because the clip is "
            f"normalised to square pixels; {fragment!r} is missing from {reason!r}"
        )


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


def test_agent_tape_closes_the_focused_panel_before_quitting() -> None:
    """The final `q` must reach the app binding instead of the Agent input."""
    commands = [
        line.strip()
        for line in AGENT_TAPE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert commands[-3:] == ["Ctrl+A", "Sleep 1s", 'Type "q"'], (
        "the Agent input still owns printable keys after the turn; close the "
        "panel and wait for focus handoff before typing q"
    )


def test_relationship_tape_closes_the_modal_before_quitting() -> None:
    """The first `q` closes RelationshipScreen; a second must quit the app."""
    commands = [
        line.strip()
        for line in (DEMO_DIR / "relationships.tape").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert commands[-3:] == ['Type "q"', "Sleep 1s", 'Type "q"'], (
        "RelationshipScreen consumes the first q; wait for dismissal before "
        "sending the app-level quit key"
    )


def test_demo_harness_never_synthesizes_the_agent_prompt_submission() -> None:
    """The documentation-only harness may auto-open/focus the AgentPanel, but
    it must drive the prompt through real keyboard input recorded by VHS —
    never by calling the message handler or posting the message directly."""
    source = DEMO_HARNESS.read_text(encoding="utf-8")
    assert "AgentPromptSubmitted" not in source
    assert "on_agent_prompt_submitted(" not in source


def test_agent_capture_provenance_states_what_the_grounded_turn_proves() -> None:
    """The provenance must draw the boundary where the code now draws it.

    The tape drives the product's real `AgentPanel`, and behind it the real
    `AgentRuntime` executes the real read tools against a synthetic fixture —
    so the capture proves the pipeline, and the only thing it cannot speak
    for is a live model or a live cluster. The harness is read here as well as
    the page: if the demo ever stopped handing the prompt and screen context
    to a real runtime, this wording would have to be revisited rather than
    silently kept.
    """
    harness = DEMO_HARNESS.read_text(encoding="utf-8")
    assert "del user_text, screen_context" not in harness, (
        "this contract is written against a harness that hands the prompt and "
        "screen context to a real AgentRuntime; a demo runtime that discards "
        "them needs the provenance wording revisited"
    )
    assert "build_demo_agent_runtime()" in harness, (
        "the agent scene must be wired to the grounded runtime builder"
    )

    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    section = instructions.split("## Embedded agent", 1)[1].split("\n## ", 1)[0]
    lowered = " ".join(section.lower().split())

    assert "proves" in lowered, "the provenance section must state what the capture does prove"
    assert "does not prove" in lowered, "and must state the other half of the boundary explicitly"
    for proof in ("agentpanel", "input", 'role="tool"', "e1", "e2"):
        assert proof in lowered, f"the section must name what the capture does prove: {proof!r}"
    for limit in (
        "live model",
        "live cluster",
        "deterministic",
        "synthetic",
        "no network",
    ):
        assert limit in lowered, f"the section must name what it does not prove: {limit!r}"
    assert "deterministic synthetic-cluster walkthrough" in lowered, (
        "the provenance page must give the embedding surfaces the label they use"
    )


def test_landing_video_privacy_rule_bans_provider_and_spend_evidence_not_korvids_own_chrome() -> (
    None
):
    """Round-9 review: criterion 7 banned what the shipped Agent frame renders.

    The Agent scene is korvid's own UI, and its panel header always carries
    the model label the app was given plus `↑… ↓… tok`. The harness gives
    it the synthetic label `korvid-demo`, and korvid estimates the counters
    itself because the offline demo provider reports no usage — so a
    criterion that forbade *any* model name or token count could only be
    satisfied by not shipping the Agent capture at all.

    The rule the privacy criterion is actually for is real external
    identity and spend: whose provider, whose bill, whose checkout, whose
    cluster. This pins the scoped rule in the design and in the plan that
    tracks it, so the next edit cannot quietly widen it back into a
    contradiction.
    """
    design = LANDING_VIDEO_DESIGN.read_text(encoding="utf-8")
    criteria = design.split("## Acceptance criteria", 1)[1]
    plan = LANDING_VIDEO_PLAN.read_text(encoding="utf-8")

    for label, text in (("the design's criteria", criteria), ("the landing-video plan", plan)):
        flat = " ".join(text.split()).lower()
        for banned in ("model name, or token count", "model name, token count"):
            assert banned not in flat, (
                f"{label} still bans what korvid's own panel renders ({banned!r}); the "
                "shipped Agent frame cannot satisfy it"
            )
        for required in ("provider identity", "token-spend", "credential", "real cluster"):
            assert required in flat, f"{label} must still forbid {required!r}"
        assert DEMO_MODEL_LABEL in flat, (
            f"{label} must name the synthetic demo label it allows, so the allowance is "
            "documented rather than assumed"
        )
        assert "product rendering" in flat, (
            f"{label} must say the label and counters are product rendering, not evidence"
        )
        assert re.search(r"not (?:live-provider|an external provider|cost)", flat), (
            f"{label} must say what the allowed chrome is *not*: live-provider or cost evidence"
        )

    boundary = design.split("### Execution boundary", 1)[1].split("\n## ", 1)[0]
    lowered_boundary = " ".join(boundary.split()).lower()
    chrome_reason = "the Agent story's own boundary must state which chrome the frame keeps"
    assert DEMO_MODEL_LABEL in lowered_boundary, chrome_reason
    assert "tok" in lowered_boundary, chrome_reason

    mcp_story = design.split("## MCP story", 1)[1]
    mcp_framing = mcp_story.split("### Framing and privacy", 1)[1].split("\n## ", 1)[0]
    lowered_framing = " ".join(mcp_framing.split()).lower()
    assert "region" in lowered_framing, (
        "the client-side ban must be scoped to the external client region; the frame also "
        "contains korvid's own panel"
    )


def test_agent_provenance_explains_the_panel_headers_label_and_estimated_counters() -> None:
    """The frame's header must be sourced, not left to look like provider data.

    `⚡ korvid-demo · ~↑… ↓… tok` is the one part of the Agent frame that
    could be mistaken for evidence about an external provider or a bill.
    Both halves are korvid's: the label is the harness's synthetic
    `agent_model_name`, and the counters are korvid's own token estimate
    derived from character length, which is what the leading `~` marks —
    `DemoAgentProvider` emits no usage event at all. The page has to say so,
    and the two
    structural facts behind the sentence are asserted here so the prose
    cannot outlive them.
    """
    harness = DEMO_HARNESS.read_text(encoding="utf-8")
    story = AGENT_STORY.read_text(encoding="utf-8")
    panel = AGENT_PANEL.read_text(encoding="utf-8")

    assert f'agent_model_name="{DEMO_MODEL_LABEL}" if scene == "agent" else None' in harness, (
        "the documented label must be the one the harness actually passes"
    )
    assert '"usage"' not in story, (
        "the provenance says korvid estimates the counters; a demo provider that reported "
        "usage would make that sentence false"
    )
    header_reason = "the provenance describes the shipped header format; the panel must render it"
    assert "tok" in panel, header_reason
    assert 'prefix = "~" if estimated else ""' in panel, header_reason

    section = INSTRUCTIONS.read_text(encoding="utf-8").split("## Embedded agent", 1)[1]
    section = section.split("\n## ", 1)[0]
    lowered = " ".join(section.split()).lower()

    assert DEMO_MODEL_LABEL in lowered, "the section must name the label the header renders"
    assert "agent_model_name" in lowered, "and where that label comes from"
    assert "tok" in lowered, "the section must account for the token counters on screen"
    assert "token estimate" in lowered, "and say korvid computes estimated tokens itself"
    assert "character length" in lowered, "the estimate's character-length basis must be explicit"
    assert re.search(r"no (?:usage|provider usage)", lowered), (
        "the section must say the demo provider reports no usage, which is why the counters "
        "are estimated"
    )
    for denial in ("billing", "spend"):
        assert denial in lowered, (
            f"the section must refuse the cost reading explicitly; {denial!r} is missing"
        )


def test_agent_page_capture_states_the_grounded_walkthrough_beside_the_turn_flow() -> None:
    """`docs/agent.md` must describe the frame it ships, and only that frame.

    The storyboard pairs one capture with korvid's production turn flow.
    That flow (context, bounded reads, validated citations, UI drive)
    documents what the shipped `AgentRuntime` does and stays exactly as
    strong. The capture beside it now runs that same runtime, executor and
    evidence ledger over a synthetic fixture behind a deterministic offline
    provider — so its alt and caption identify a deterministic
    synthetic-cluster walkthrough, refuse the live-model claim, and must no
    longer say that no provider or no read tool runs.
    """
    page = AGENT_PAGE.read_text(encoding="utf-8")
    storyboard = page[
        page.index('<section class="docs-storyboard"') : page.index("</section>")
        + len("</section>")
    ]
    figure = storyboard[storyboard.index("<figure>") : storyboard.index("</figure>")]

    alt = re.search(r'<img[^>]*alt="([^"]+)"', figure)
    assert alt is not None, "the storyboard keeps a described capture"
    alt_text = alt.group(1).lower()
    assert "deterministic synthetic-cluster walkthrough" in alt_text, (
        f"the alt must carry the shared label for this media: {alt_text!r}"
    )
    assert "agentpanel" in alt_text or "agent panel" in alt_text, (
        f"the alt must name the panel the capture really shows: {alt_text!r}"
    )
    assert "scripted" not in alt_text, f"the capture is no longer scripted: {alt_text!r}"

    caption = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", figure, re.DOTALL)
    assert caption is not None, "the storyboard keeps its figure caption"
    caption_text = " ".join(re.sub(r"<[^>]+>", " ", caption.group(1)).lower().split())
    assert "deterministic synthetic-cluster walkthrough" in caption_text, (
        f"the caption must identify the capture: {caption_text!r}"
    )
    assert "not a live-model quality claim" in caption_text, (
        f"the capture note must refuse the live-model claim: {caption_text!r}"
    )
    for stale in ("scripted", "no provider", "no real read tool", "unsupported citation"):
        assert stale not in caption_text, (
            f"the capture note must not keep the replaced story: {stale!r} in {caption_text!r}"
        )

    ordered_list = storyboard[storyboard.index("<ol>") :]
    assert storyboard.index("</figure>") < storyboard.index("<ol>"), (
        "the production turn flow must sit outside the captured figure, not read "
        "as a description of the frame"
    )
    assert "scripted" not in ordered_list.lower(), (
        "the production turn flow describes the real AgentRuntime and must not be "
        "weakened into a description of the capture"
    )
    for production_fact in (
        "Bounded tools gather manifests, events, logs, or diagnoses.",
        "Evidence references remain selectable and validated.",
        "Navigation can change; writes still stop at confirmation.",
    ):
        assert production_fact in ordered_list, (
            f"the documented production behaviour must survive verbatim: {production_fact!r}"
        )


def test_agent_page_recording_note_names_the_real_pipeline_and_the_follow_mirror() -> None:
    """The guide's recording note is the longest-lived description of the clip.

    It must name the four shipped components the capture runs, say that the
    screen change beside the panel is `agent.follow` mirroring a read rather
    than a UI-drive tool call or a write, and keep the offline/no-quality
    limit. The old note claimed the opposite of all of that.
    """
    page = AGENT_PAGE.read_text(encoding="utf-8")
    note = page.split("## What the recording demonstrates", 1)[1]
    lowered = " ".join(note.lower().split())

    for component in ("agentpanel", "agentruntime", "toolexecutor", "evidenceledger"):
        assert component in lowered.replace("`", ""), (
            f"the note must name the shipped component the capture runs: {component!r}"
        )
    for read in ("diagnose_pod", "get_logs"):
        assert read in lowered, f"the note must name the read the turn dispatches: {read!r}"
    assert "agent.follow" in lowered, (
        "the note must credit follow for the describe pane beside the panel"
    )
    assert "mirror" in lowered, "and must call it a mirror of the read"
    assert "not a ui drive" in lowered or "not a ui-drive" in lowered, (
        "the note must deny the UI-drive reading of that mirror"
    )
    assert "not a write" in lowered, "and must deny the write reading of it too"
    for limit in ("deterministic", "offline", "synthetic", "live model", "quality"):
        assert limit in lowered, f"the note must keep the capture's limit: {limit!r}"
    assert "no unsupported-citation warning" in lowered, (
        "the note must explain the absence of the warning the replaced capture "
        "carried, rather than leave a reader to wonder"
    )
    assert "scripted" not in lowered, "the replaced panel-only story must be gone"


def test_mcp_guide_capture_note_stays_compact_and_truthful() -> None:
    """`docs/mcp.md` gets a capture note, not a recording manual.

    The guide documents the product; the clip's full provenance lives on the
    provenance page. So the note has to be short, name the real SDK client,
    the loopback Streamable HTTP endpoint, the read-only boundary and follow,
    and link onwards rather than restate the tape.
    """
    page = MCP_PAGE.read_text(encoding="utf-8")
    matches = re.findall(r"(?m)^The landing clip[^\n]*(?:\n(?!\n)[^\n]*)*", page)
    assert len(matches) == 1, f"docs/mcp.md must carry exactly one capture note; found {matches}"
    note = matches[0]
    lowered = " ".join(note.lower().split())

    assert len(note.split()) <= 60, (
        f"the capture note must stay compact; found {len(note.split())} words"
    )
    for fact in ("mcp sdk", "streamable http", "read-only", "follow", "synthetic"):
        assert fact in lowered, f"the capture note must state {fact!r}; found {lowered!r}"
    assert "demo/visual-storytelling.md" in note, (
        "the note must link to the full provenance instead of restating it"
    )


def test_mcp_capture_instructions_publish_the_visible_two_pane_composition() -> None:
    """The provenance must describe the frame a visitor actually sees.

    The clip is two tmux panes at a fixed split, a fixed tool order and
    fixed holds, and a right pane that never clears — so the log excerpt
    `get_logs` returned is still on screen under the Helm beat and the
    closing summary. Those are the facts a reader needs to check the frame
    against the recipe, and they are also what stops the page drifting back
    to a derivation of somebody else's capture.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    lowered = " ".join(mcp.lower().split())

    assert "tmux" in lowered, "the provenance must name the compositor"
    assert "-p 45" in mcp, "and publish the exact pane split the tape asks tmux for"
    assert "139" in mcp, "and the exact terminal width the panes are laid out over"
    assert "42" in mcp, "and the exact terminal height"

    assert "streamable http" in lowered, "the transport must be named"
    assert "127.0.0.1:7878/mcp" in mcp, "and the exact loopback endpoint the client speaks to"
    assert "mcp sdk" in lowered, "and the official SDK the client is built on"

    for call, hold in (
        ("list_resources", "2.2"),
        ("diagnose_pod", "3.2"),
        ("get_logs", "3.6"),
        ("helm_list_releases", "2.4"),
    ):
        assert call in mcp, f"the provenance must publish the call {call!r}"
        assert hold in mcp, f"and the hold {hold!r}s that call is read at"

    assert "the logs remain visible" in lowered, (
        "the provenance must state that the client pane never clears, so the log "
        "excerpt is still readable beside the closing beats"
    )
    assert "read-only" in lowered, "the read-only boundary stays stated"
    assert "no keystroke is sent" in lowered, "and so does the follow boundary"


def test_relationship_demo_serves_every_kind_the_real_loader_asks_for() -> None:
    """The captured relationship screen must not be a list of missing kinds.

    `RelationshipSnapshotLoader` LISTs a fixed catalog plus any discovered
    Gateway API resources, and reports each kind the discovery aliases do
    not offer as `unavailable` *before* any LIST runs. The harness used to
    publish four kinds, so the real screen devoted most of its height to an
    amber "Coverage: incomplete" panel naming fourteen absent kinds — an
    accurate render of a deliberately thin fixture, and a bad claim about
    the product. An empty synthetic list is a complete answer; a missing
    alias is not.
    """
    harness = _demo_harness()
    _metas, missing = graph_source_metas(DEMO_ROOT, "shop", harness.RELATIONSHIP_ALIASES)
    unavailable = sorted(f"{spec.group or 'core'}/{spec.plural}" for spec in missing)
    assert missing == (), (
        "every snapshot source must resolve against the relationship scene's "
        f"discovery aliases; unavailable: {unavailable}"
    )


def test_configmap_discovery_is_scoped_to_the_relationship_scene() -> None:
    """Base demos must not expose a kind whose manifest they cannot describe."""
    harness = _demo_harness()
    for alias in ("configmaps", "configmap", "cm"):
        assert alias not in harness.ALIASES
        assert harness.RELATIONSHIP_ALIASES[alias].kind == "ConfigMap"

    manifest = asyncio.run(harness.get_manifest("configmaps", "shop", "payment-config"))
    assert manifest["apiVersion"] == "v1"
    assert manifest["kind"] == "ConfigMap"
    assert manifest["metadata"]["name"] == "payment-config"


def test_every_relationship_kind_describes_a_matching_manifest() -> None:
    """Known empty fixture kinds must never fall back to a Pod manifest."""
    harness = _demo_harness()
    metas = {meta.plural: meta for meta in harness.RELATIONSHIP_ALIASES.values()}
    for plural, meta in metas.items():
        namespace = "shop" if meta.namespaced else None
        manifest = asyncio.run(harness.get_manifest(plural, namespace, f"demo-{plural}"))
        expected_api_version = f"{meta.group}/{meta.version}" if meta.group else meta.version
        assert manifest["apiVersion"] == expected_api_version
        assert manifest["kind"] == meta.kind
        assert manifest["metadata"]["name"] == f"demo-{plural}"
        if namespace is not None:
            assert manifest["metadata"]["namespace"] == namespace


def test_payment_relationship_facts_come_from_the_described_pod_manifest() -> None:
    """The capture's evidence path must be one the production extractor emits."""
    harness = _demo_harness()
    extracted = harness.extract_relationship_facts("Pod", "", "v1", harness.POD_MANIFEST)
    config_refs = [ref for ref in extracted.references if ref.relation.value == "uses_config"]
    assert len(config_refs) == 1
    config_ref = config_refs[0]
    assert config_ref in harness._PAYMENT_RELATIONSHIPS.references
    assert config_ref.target.kind == "ConfigMap"
    assert config_ref.target.name == "payment-config"
    assert config_ref.field == "spec.volumes[0].configMap"


def test_visual_storytelling_plan_uses_the_extracted_configmap_fact() -> None:
    """The executable recipe must recreate the truthful relationship fixture."""
    plan = VISUAL_STORYTELLING_PLAN.read_text(encoding="utf-8")
    assert "RELATIONSHIP_ALIASES[_alias] = _CONFIGMAP_META" in plan
    assert "ALIASES[alias] = _CONFIGMAP_META" not in plan
    assert '"configMap": {"name": "payment-config"}' in plan
    assert re.search(
        r'extract_relationship_facts\(\s*"Pod",\s*"",\s*"v1",\s*POD_MANIFEST\s*\)',
        plan,
    )
    assert 'RELATIONSHIP_ALIASES if scene == "relationships" else ALIASES' in plan
    assert 'field="spec.volumes[0].configMap.name"' not in plan


def test_relationship_demo_graph_is_complete_with_both_directions_populated() -> None:
    """The frame has to show a real dependency *and* a real dependent.

    Both grouped sections of the relationship screen are part of the claim
    the caption makes ("the two sections separate dependencies from
    dependents"), so the fixture must produce at least one resolved edge in
    each direction through the genuine loader — not a screenshot of two
    empty headings.
    """
    harness = _demo_harness()
    loader = RelationshipSnapshotLoader(_DemoLister(harness))
    graph = asyncio.run(loader.load(DEMO_ROOT, "shop", harness.RELATIONSHIP_ALIASES))

    assert not graph.incomplete, (
        "the demo snapshot must report complete coverage; incomplete records: "
        f"{[(record.resource, record.state.value) for record in graph.coverage if record.detail]}"
    )
    root = next(
        node
        for node in graph.nodes
        if node.kind == "Pod" and node.name == DEMO_ROOT.name and node.namespace == "shop"
    )
    dependencies = graph.dependencies_of(root)
    dependents = graph.dependents_of(root)
    observed = {
        (
            edge.relation.value,
            edge.subject.kind,
            edge.subject.namespace,
            edge.subject.name,
            edge.target.kind,
            edge.target.namespace,
            edge.target.name,
        )
        for edge in (*dependencies, *dependents)
    }
    expected = {
        (
            "uses_config",
            "Pod",
            "shop",
            DEMO_ROOT.name,
            "ConfigMap",
            "shop",
            "payment-config",
        ),
        (
            "selects",
            "Service",
            "shop",
            "payment-worker",
            "Pod",
            "shop",
            DEMO_ROOT.name,
        ),
    }
    assert expected <= observed, (
        "the capture must keep its promised ConfigMap dependency and Service "
        f"dependent; missing: {expected - observed}"
    )


class _FakeBlock:
    """One text content block of a fake `tools/call` answer."""

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeCallToolResult:
    """The shape of the MCP SDK's `CallToolResult` this pane reads."""

    def __init__(self, text: str, *, is_error: bool = False) -> None:
        self.content = [_FakeBlock(text)]
        self.is_error = is_error


class _FakeSession:
    """A `ClientSession` stand-in that answers the follow story's calls."""

    def __init__(self, answers: dict[str, _FakeCallToolResult]) -> None:
        self._answers = answers
        self.calls: list[str] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def initialize(self) -> None:
        return None

    async def call_tool(self, name: str, arguments: dict[str, object]) -> _FakeCallToolResult:
        self.calls.append(name)
        return self._answers[name]


def test_mcp_client_text_reads_a_successful_answer() -> None:
    """The happy path is unchanged: text blocks joined, blanks dropped."""
    module = _mcp_client_module()
    result = _FakeCallToolResult("CURRENT HEALTH\n  status: CrashLoopBackOff")

    assert module._text(result, "diagnose_pod") == "CURRENT HEALTH\n  status: CrashLoopBackOff"


def test_mcp_client_text_fails_closed_when_the_sdk_flags_an_error() -> None:
    """An `is_error` answer must abort the capture, not be printed as evidence.

    The MCP SDK signals a failed `tools/call` with `CallToolResult.is_error`
    and still fills `content` — with the server's error text. Joining that
    content published a failure as though it were korvid's answer, and for
    `list_resources`, `get_logs` and `helm_list_releases` (which print a
    plain tail) the clip would have ended on "investigation complete"
    regardless. The client must raise instead, naming the call it made and
    never echoing the result content, which is unbounded and may hold
    sensitive cluster text.
    """
    module = _mcp_client_module()
    sentinel = "leaked-token=abc123 from /home/whoever/.kube/config"
    result = _FakeCallToolResult(sentinel, is_error=True)

    with pytest.raises(RuntimeError, match=r"helm_list_releases") as excinfo:
        module._text(result, "helm_list_releases")

    assert sentinel not in str(excinfo.value), (
        "the failure must name the call, not echo the tool result it refused"
    )


def test_mcp_client_checks_the_error_flag_on_every_follow_story_answer() -> None:
    """All four calls fail closed, not just the one with a section parser."""
    client = MCP_CLIENT.read_text(encoding="utf-8")
    for name in MCP_CLIENT_CALLS:
        assert re.search(rf'_text\(\s*\w+,\s*"{name}"\s*\)', client), (
            f"the {name} answer must be read through the error-checking helper"
        )
    assert not re.search(r"_text\(\s*\w+\s*\)", client), (
        "no answer may be read without naming the call that produced it"
    )


def test_mcp_client_never_closes_the_story_on_a_failed_call(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed first call ends the run before the closing card is printed."""
    module = _mcp_client_module()
    session = _FakeSession(
        {"list_resources": _FakeCallToolResult("boom: forbidden", is_error=True)}
    )

    @contextlib.asynccontextmanager
    async def _fake_transport(url: str) -> AsyncIterator[tuple[object, object]]:
        assert url == module.URL
        yield (object(), object())

    monkeypatch.setattr(module, "streamable_http_client", _fake_transport)
    monkeypatch.setattr(module, "ClientSession", lambda _read, _write: session)

    with pytest.raises(RuntimeError, match=r"list_resources"):
        asyncio.run(module.main())

    printed = capsys.readouterr().out
    assert "investigation complete" not in printed, (
        "a failed call must never be followed by the success card"
    )
    assert "boom: forbidden" not in printed, "the failed result must not be published"
    assert session.calls == ["list_resources"], "the story must stop at the failed call"


def test_demo_manifest_covers_the_mcp_scenes_helm_releases() -> None:
    """Round-3 review: `get_manifest` resolved relationship aliases only.

    The `mcp` scene discovers through `MCP_ALIASES`, which is what makes
    the Helm browser reachable when an external host calls
    `helm_list_releases`. Describing a release then went through
    `DemoReadOps.get_object` into `get_manifest`, which only knew
    `RELATIONSHIP_ALIASES` and raised `KeyError` for `helmreleases` — a
    hard failure mid-capture. The lookup must span both scenes' aliases
    without reviving the unsafe unknown-kind fallback that used to answer
    a Pod manifest for anything it did not recognise.
    """
    harness = _demo_harness()
    for alias in ("helmreleases", "helmrelease", "helm"):
        manifest = asyncio.run(harness.get_manifest(alias, "shop", "shop"))
        assert manifest["kind"] == "HelmRelease"
        assert manifest["apiVersion"] == "v1"
        assert manifest["metadata"] == {"name": "shop", "namespace": "shop"}

    with pytest.raises(KeyError, match="unknown demo kind"):
        asyncio.run(harness.get_manifest("widgets", "shop", "whatever"))


def test_demo_manifest_union_does_not_widen_either_scenes_discovery() -> None:
    """Manifests span both scenes; discovery surfaces stay as they were."""
    harness = _demo_harness()
    for alias in ("helmreleases", "helmrelease", "helm"):
        assert alias not in harness.RELATIONSHIP_ALIASES, (
            "the relationship scene must not gain a Helm view from the manifest union"
        )
    for alias in ("configmaps", "configmap", "cm"):
        assert alias not in harness.MCP_ALIASES, (
            "the mcp scene must not gain the relationship scene's ConfigMap view"
        )
    assert harness.RELATIONSHIP_ALIASES["configmaps"].kind == "ConfigMap"
    assert asyncio.run(harness.get_manifest("configmaps", "shop", "payment-config"))["kind"] == (
        "ConfigMap"
    )


def test_demo_manifest_isolates_every_nested_branch_from_the_fixture() -> None:
    """Round-9 (comment 3859789137): a shallow copy shared `spec`/`status`.

    `get_manifest` copied only the top level and `metadata`, so `spec`,
    `status` and `data` were the module-global `POD_MANIFEST`,
    `DEPLOY_MANIFEST`, `SVC_MANIFEST` and `CONFIGMAP_MANIFEST` objects
    themselves. The TUI's describe path and `DemoReadOps.get_object` (an
    external MCP host's `tools/call`) both return them, so one consumer
    touching `manifest["status"]` in place would corrupt every later frame
    and tool answer. Each answer must be its own object.
    """
    harness = _demo_harness()
    pristine = copy.deepcopy(harness.POD_MANIFEST)

    try:
        first = asyncio.run(harness.get_manifest("pods", "shop", "payment-worker-6c9f7d-b3xnq"))
        first["status"]["phase"] = "Succeeded"
        first["status"]["containerStatuses"][0]["restartCount"] = 0
        first["spec"]["containers"][0]["image"] = "tampered:0"
        first["spec"]["volumes"][0]["configMap"]["name"] = "tampered-config"

        second = asyncio.run(harness.get_manifest("pods", "shop", "payment-worker-6c9f7d-b3xnq"))

        assert second["status"]["phase"] == "Running", (
            "a later describe must not see an earlier consumer's in-place edit"
        )
        assert second["status"]["containerStatuses"][0]["restartCount"] == 17
        assert second["spec"]["containers"][0]["image"].endswith("payment-worker:2.4.1")
        assert second["spec"]["volumes"][0]["configMap"]["name"] == "payment-config"
        assert pristine == harness.POD_MANIFEST, (
            "the module fixture every scene reads must survive a mutated answer"
        )
    finally:
        harness.POD_MANIFEST.clear()
        harness.POD_MANIFEST.update(copy.deepcopy(pristine))


def test_demo_manifest_isolates_the_generic_helm_answer_too() -> None:
    """The alias union's synthesised manifests must be isolated as well.

    `helmreleases` has no fixture, so `get_manifest` builds a bare manifest
    for it. That path shares nothing today, but the deep copy is what keeps
    the guarantee uniform: a consumer that edits a Helm answer's metadata
    must not be able to reach the next one.
    """
    harness = _demo_harness()

    first = asyncio.run(harness.get_manifest("helm", "shop", "shop"))
    first["metadata"]["labels"] = {"tampered": "yes"}

    second = asyncio.run(harness.get_manifest("helmreleases", "shop", "shop"))

    assert second["kind"] == "HelmRelease"
    assert second["apiVersion"] == "v1"
    assert second["metadata"] == {"name": "shop", "namespace": "shop"}, (
        "the synthesised Helm manifest must not carry an earlier answer's edit"
    )


def test_demo_manifest_configmap_data_is_not_shared_between_answers() -> None:
    """`data` is the ConfigMap's whole payload; it must be copied too."""
    harness = _demo_harness()
    pristine = copy.deepcopy(harness.CONFIGMAP_MANIFEST)

    try:
        first = asyncio.run(harness.get_manifest("configmaps", "shop", "payment-config"))
        first["data"]["gateway"] = "tampered.example.com"

        second = asyncio.run(harness.get_manifest("configmaps", "shop", "payment-config"))

        assert second["data"] == {"gateway": "pay.example.com"}
        assert pristine == harness.CONFIGMAP_MANIFEST
    finally:
        harness.CONFIGMAP_MANIFEST.clear()
        harness.CONFIGMAP_MANIFEST.update(copy.deepcopy(pristine))


#: One `await asyncio.sleep(...)` the client took, as
#: `(seconds, ok-marker exists, failure-marker exists, output since the
#: previous hold)`. Recording the markers *and* the output at each beat is
#: what makes the ordering of the status handshake observable: a success file
#: published one line too early shows up on the wrong beat.
_ClientHold = tuple[float, bool, bool, str]


def _mcp_visible_window_seconds() -> float:
    """The wall-clock length of the tape's captured block.

    Everything between `Show` and the following `Hide` reaches the asset,
    and VHS runs that clock itself — it never observes the client pane. So
    a failed client has to outlive this window rather than exit inside it.
    """
    lines = [line.strip() for line in MCP_TAPE.read_text(encoding="utf-8").splitlines()]
    show = lines.index("Show")
    hide = next(index for index in range(show, len(lines)) if lines[index] == "Hide")
    total = 0.0
    for line in lines[show:hide]:
        match = re.fullmatch(r"Sleep (\d+(?:\.\d+)?)(ms|s)", line)
        if match is not None:
            total += float(match.group(1)) / (1000 if match.group(2) == "ms" else 1)
    return total


def _mcp_client_answers() -> dict[str, _FakeCallToolResult]:
    """One well-formed answer per call of the follow story."""
    return {
        "list_resources": _FakeCallToolResult("NAME READY\npayment-worker-6c9f7d-b3xnq 0/1"),
        "diagnose_pod": _FakeCallToolResult(
            "CURRENT HEALTH\n  status: CrashLoopBackOff\nCONTAINERS\n  app: restarting"
        ),
        "get_logs": _FakeCallToolResult("connection refused\nconnection refused"),
        "helm_list_releases": _FakeCallToolResult("NAME REVISION\nshop 4"),
    }


def _drive_mcp_client(
    module: ModuleType,
    session: _FakeSession,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    holds: list[_ClientHold],
) -> None:
    """Point the client at `session` and record every hold it takes."""

    @contextlib.asynccontextmanager
    async def _fake_transport(url: str) -> AsyncIterator[tuple[object, object]]:
        assert url == module.URL
        yield (object(), object())

    async def _fake_sleep(seconds: float) -> None:
        captured = capsys.readouterr()
        holds.append(
            (
                seconds,
                module.OK_FILE.exists(),
                module.FAILED_FILE.exists(),
                captured.out + captured.err,
            )
        )

    monkeypatch.setattr(module, "streamable_http_client", _fake_transport)
    monkeypatch.setattr(module, "ClientSession", lambda _read, _write: session)
    monkeypatch.setattr(module.asyncio, "sleep", _fake_sleep)


def test_mcp_client_publishes_success_only_after_the_whole_story_is_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The success file is the tape's evidence that the story actually ran.

    VHS records a fixed window and stops; it cannot tell a complete run from
    a client that connected, printed two beats and died. The success file is
    what carries that verdict out, so it may only appear once all four calls
    and the closing card have been printed — published one beat early it
    would certify exactly the truncated story it exists to reject.
    """
    module = _mcp_client_module()
    monkeypatch.chdir(tmp_path)
    session = _FakeSession(_mcp_client_answers())
    holds: list[_ClientHold] = []
    _drive_mcp_client(module, session, monkeypatch, capsys, holds)

    asyncio.run(module.main())

    assert session.calls == list(MCP_CLIENT_CALLS), "the whole story must have run"
    assert len(holds) == len(MCP_CLIENT_CALLS) + 1, (
        f"four answered beats and one closing hold; recorded {len(holds)}"
    )
    for seconds, ok, failed, _printed in holds[:-1]:
        assert not ok, f"the success file may not exist during the {seconds}s answer beat"
        assert not failed, f"a healthy run may not publish a failure at the {seconds}s beat"

    seconds, ok, failed, printed = holds[-1]
    assert seconds == module.CLOSING_HOLD, (
        f"the last hold must be the existing closing hold, not a new one: {seconds}"
    )
    assert ok, "the success file must be published before the closing hold, not after it"
    assert not failed, "a completed run must not publish the failure file"
    assert "investigation complete" in printed, (
        "the success file must be published after the closing summary is printed"
    )


def test_mcp_client_entry_point_holds_a_failed_pane_and_publishes_no_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed call must reject the recording, not decorate it.

    Unwrapped, an exception here printed a traceback into a pane that is
    being recorded and then closed it — which also reflows the TUI to full
    width inside the last captured frames — while VHS went on recording and
    produced an apparently finished asset. The entry point therefore catches
    the failure, publishes the failure file, never publishes success, prints
    no traceback and no error or result text, and holds the pane past the
    tape's own visible window so the composition it was recorded in survives
    to the teardown that rejects it.
    """
    module = _mcp_client_module()
    monkeypatch.chdir(tmp_path)
    sentinel = "leaked-token=abc123 from /home/whoever/.kube/config"
    answers = _mcp_client_answers()
    answers["get_logs"] = _FakeCallToolResult(sentinel, is_error=True)
    session = _FakeSession(answers)
    holds: list[_ClientHold] = []
    _drive_mcp_client(module, session, monkeypatch, capsys, holds)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(module.run())

    assert excinfo.value.code != 0, "a failed run must leave a non-zero status behind"
    assert module.FAILED_FILE.exists(), "the failure must be published for the tape to read"
    assert not module.OK_FILE.exists(), "a failed run may never publish success"
    assert session.calls == ["list_resources", "diagnose_pod", "get_logs"], (
        "the story must stop at the failed call"
    )

    seconds, ok, failed, _printed = holds[-1]
    assert failed, "the pane must be held *after* the failure was published"
    assert not ok, "the held pane may not also carry a success marker"
    assert seconds == module.FAILURE_HOLD, f"the hold must be the bounded failure hold: {seconds}"
    visible = _mcp_visible_window_seconds()
    assert visible < module.FAILURE_HOLD, (
        f"the failure hold ({module.FAILURE_HOLD}s) must outlast the tape's visible "
        f"window ({visible}s), or the pane closes inside the capture"
    )
    assert math.isfinite(module.FAILURE_HOLD), "the hold must be bounded, not indefinite"

    published = "".join(chunk for *_rest, chunk in holds)
    assert sentinel not in published, "the failed result may not reach the recorded pane"
    for leak in ("Traceback", "RuntimeError", "Error:"):
        assert leak not in published, f"the pane must publish no {leak!r}"
    assert "investigation complete" not in published, (
        "a failed run must never print the success card"
    )

    client = MCP_CLIENT.read_text(encoding="utf-8")
    for formatter in ("import traceback", "print_exc", "format_exc"):
        assert formatter not in client, f"the client may not format a traceback: {formatter!r}"
    assert not re.search(r"print\([^)]*\bexc\b", client), (
        "the caught exception may not be printed; it is unbounded and may hold "
        "sensitive cluster text"
    )


def test_mcp_client_run_clears_stale_markers_before_the_story_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-11 (comment 3861056637): only this run's markers may be graded.

    The module documented markers "removed on both sides of a run" but
    cleared neither itself, leaving the whole guarantee to whatever invoked
    it. `docs/demo/record-mcp-follow.sh` already pre-cleans both markers
    before it starts VHS, and the tape's own `rm -f` pre-cleans them again,
    so under the shipped recording flow a stale marker from an interrupted
    run is already gone before this client's own story starts — this test
    does not exercise a hole in that flow. What it pins down is client-local
    ownership as defence in depth: this module invoked directly (as this
    test does, with no wrapper or tape involved) or an external pre-clean
    skipped or removed by a future edit must not be able to leave a stale
    `OK_FILE` around to be graded. `run()` owns its markers now, exactly as
    `run_mcp_demo` owns `MCP_READY_FILE` through `clear_mcp_ready`.
    """
    module = _mcp_client_module()
    monkeypatch.chdir(tmp_path)
    module.OK_FILE.touch()
    module.FAILED_FILE.touch()
    session = _FakeSession(_mcp_client_answers())
    holds: list[_ClientHold] = []
    _drive_mcp_client(module, session, monkeypatch, capsys, holds)

    asyncio.run(module.run())

    assert session.calls == list(MCP_CLIENT_CALLS), "the whole story must have run"
    first_seconds, first_ok, first_failed, _printed = holds[0]
    assert not first_ok, (
        f"a stale success marker must be gone before the {first_seconds}s beat, or it "
        "certifies a run that never published one"
    )
    assert not first_failed, "a stale failure marker must be gone before the story too"
    assert module.OK_FILE.exists(), "this run's own success must still be published"
    assert not module.FAILED_FILE.exists(), "a completed run leaves no failure behind"


def test_mcp_client_run_clears_a_stale_success_before_a_failing_story(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing run must not inherit an earlier run's success marker.

    The shipped wrapper's own grading order already survives this case —
    `record-mcp-follow.sh` checks its failure marker first, so a stale
    `OK_FILE` next to the `FAILED_FILE` this run publishes is still a
    rejection. The invariant this test pins is client-local instead: after
    a failed story only the failure marker may exist, which is what keeps
    the module correct when there is no wrapper's ordering to fall back on
    (this module invoked directly, or an external pre-clean bypassed).
    """
    module = _mcp_client_module()
    monkeypatch.chdir(tmp_path)
    module.OK_FILE.touch()
    answers = _mcp_client_answers()
    answers["get_logs"] = _FakeCallToolResult("boom: forbidden", is_error=True)
    session = _FakeSession(answers)
    holds: list[_ClientHold] = []
    _drive_mcp_client(module, session, monkeypatch, capsys, holds)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(module.run())

    assert excinfo.value.code != 0, "a failed run must leave a non-zero status behind"
    assert not module.OK_FILE.exists(), (
        "the stale success must be gone; a failed run may never leave one behind"
    )
    assert module.FAILED_FILE.exists(), "the failure this run produced must be published"


def test_mcp_client_run_fails_closed_when_a_stale_marker_cannot_be_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A marker that cannot be removed must reject the run, not start it.

    If the stale `OK_FILE` survives — a read-only checkout, a permission
    error — then running the story anyway would let the wrapper read that
    old success. The clearing therefore happens inside the run's own
    failure channel: the failure marker is published (which rejects the
    candidate whatever else is on disk), the story never starts, and the
    pane still publishes no traceback into the frames it is recorded in.
    """
    module = _mcp_client_module()
    monkeypatch.chdir(tmp_path)
    module.OK_FILE.touch()
    session = _FakeSession(_mcp_client_answers())
    holds: list[_ClientHold] = []
    _drive_mcp_client(module, session, monkeypatch, capsys, holds)

    real_unlink = module.Path.unlink

    def _refusing_unlink(self: Path, *, missing_ok: bool = False) -> None:
        if self.name == module.OK_FILE.name:
            raise PermissionError(13, "Permission denied")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(module.Path, "unlink", _refusing_unlink)

    with pytest.raises(SystemExit) as excinfo:
        asyncio.run(module.run())

    assert excinfo.value.code != 0, "an unclearable marker must fail the run"
    assert session.calls == [], "the story may not start on markers this run cannot own"
    assert module.FAILED_FILE.exists(), (
        "the failure marker is what rejects the candidate; it must be published even "
        "though the stale success could not be removed"
    )
    published = "".join(chunk for *_rest, chunk in holds)
    for leak in ("Traceback", "PermissionError", "Permission denied"):
        assert leak not in published, f"the recorded pane must publish no {leak!r}"


def test_mcp_client_status_files_are_repo_local_and_never_committable() -> None:
    """Two more recording side effects, held to the handshake files' rules.

    The client's own prose is part of this joint. Its markers used to be
    read by the tape, and the tape's `exit 1` used to claim it published or
    rejected the capture — a claim VHS never honoured. Now that the verdict
    lives in the wrapper, a comment here that still names the tape as the
    reader or the publisher would send the next contributor to a file with
    no authority over the asset, so the client source is graded alongside
    the tape and the wrapper.
    """
    module = _mcp_client_module()
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    tape = MCP_TAPE.read_text(encoding="utf-8")
    script = MCP_RECORDER.read_text(encoding="utf-8")
    client = MCP_CLIENT.read_text(encoding="utf-8")

    for path, name in (
        (module.OK_FILE, MCP_CLIENT_OK_FILE),
        (module.FAILED_FILE, MCP_CLIENT_FAILED_FILE),
    ):
        assert str(path) == name, f"the client must publish {name}, not {path}"
        assert not Path(path).is_absolute(), (
            f"{name} must live in the checkout being recorded, not at an absolute path"
        )
        assert re.search(rf"^{re.escape(name)}$", gitignore, re.MULTILINE), (
            f"{name} is a recording side effect; it must never be committable"
        )
        cleared = [line for line in tape.splitlines() if "rm -f" in line and name in line]
        assert cleared, (
            f"{name} must be cleared before the panes start, so a stale marker from an "
            f"interrupted recording cannot certify the next one; found {cleared}"
        )
        assert name in script, (
            f"{name} outlives the tape now: {MCP_RECORDER_COMMAND} is what grades it and "
            "what removes it afterwards"
        )

    assert MCP_RECORDER_COMMAND in client, (
        "the client publishes these markers for one reader; it must name "
        f"{MCP_RECORDER_COMMAND} as the thing that grades them"
    )
    prose = " ".join(client.split())
    stale = re.findall(
        r"\btape\b[\s`'\u2019]*(?:\w+\s+){0,3}?"
        r"(?:publish\w*|reject\w*|promot\w*|grad\w*|read\w*|sees\b|decid\w*)",
        prose,
    )
    assert not stale, (
        "the tape neither reads these markers nor decides publication — it records and "
        f"leaves them in place; the client still claims otherwise: {stale}"
    )


class _RecorderRun(NamedTuple):
    """What a wrapper run left behind, collected once it had exited.

    Attributes:
        status: The wrapper's own exit status.
        output: Its combined stdout and stderr.
        published: The bytes at the canonical path, or `None` if none exist.
        candidate_left: Whether the candidate recording survived the run.
        scratch_left: The scratch markers still present, by role.
        tmux: Every `tmux` invocation the stub recorded.
        vhs_ran: Whether the fake VHS was invoked at all.
    """

    status: int
    output: str
    published: bytes | None
    candidate_left: bool
    scratch_left: tuple[str, ...]
    tmux: str
    vhs_ran: bool


def _write_fake_vhs(
    path: Path,
    *,
    log: Path,
    candidate: Path,
    markers: dict[str, Path],
    status: int,
    writes_candidate: bool,
    publishes_ok: bool,
    publishes_failed: bool,
) -> None:
    """Write a VHS stand-in that leaves exactly the side effects asked for.

    A real run leaves four things behind: the rendered file at the tape's
    `Output` path, the handshake pair the composition used, and whichever
    verdict marker the client pane published. The stub reproduces that set
    so the wrapper is graded on the same evidence it will see in a
    recording, without ttyd, ffmpeg, tmux or korvid taking part.
    """
    body = [
        "#!/bin/sh",
        f'echo "vhs $*" >> "{log}"',
        f'touch "{markers["ready"]}" "{markers["go"]}"',
    ]
    if writes_candidate:
        rendered = MCP_CANDIDATE_BYTES.decode()
        body.append(f"printf '%s' '{rendered}' > \"{candidate}\"")
    if publishes_ok:
        body.append(f'touch "{markers["ok"]}"')
    if publishes_failed:
        body.append(f'touch "{markers["failed"]}"')
    body.append(f"exit {status}")
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _run_recorder(
    workdir: Path,
    *,
    vhs_status: int = 0,
    writes_candidate: bool = True,
    publishes_ok: bool = True,
    publishes_failed: bool = False,
    published: bytes | None = MCP_PUBLISHED_BYTES,
    tape_body: str = "Output {candidate}\nSleep 1s\n",
    candidate_dir: str = "scenes",
    final_dir: str = "scenes",
    scenes_alias: str | None = None,
) -> _RecorderRun:
    """Run the shipped wrapper against a fake VHS inside `workdir`.

    Every path the wrapper touches is redirected into `workdir` through the
    documented overrides, so the contract exercises the real script — its
    real trap, its real promotion and its real cleanup — while the
    checkout's own media and scratch stay untouched. `tmux` is a stub on
    `PATH`, so no multiplexer is involved either.

    Args:
        workdir: The directory every path the wrapper touches is redirected
            into.
        vhs_status: The exit status the fake VHS returns.
        writes_candidate: Whether the fake VHS renders the candidate.
        publishes_ok: Whether the fake VHS leaves the client's success marker.
        publishes_failed: Whether the fake VHS leaves the client's failure
            marker.
        published: The bytes to seed the canonical path with, or `None` for a
            checkout where nothing was ever approved.
        tape_body: The tape the wrapper is handed, with `{candidate}` and
            `{final}` filled in — the whitespace of its `Output` directives is
            what the preflight is graded on.
        candidate_dir: The `workdir`-relative directory the candidate is
            rendered into. The wrapper creates it, exactly as it does in a
            checkout, so a directory that does not exist yet is a valid case.
        final_dir: The `workdir`-relative directory the published clip lives
            in. Only `published` creates it: the wrapper may not.
        scenes_alias: A symbolic link to create beside `scenes`, pointing at
            it, so a directory can be named by two spellings that resolve to
            one physical place.
    """
    scenes = workdir / "scenes"
    scenes.mkdir(parents=True)
    if scenes_alias is not None:
        (workdir / scenes_alias).symlink_to(scenes, target_is_directory=True)
    candidate = workdir / candidate_dir / Path(MCP_CANDIDATE_CLIP).name
    final = workdir / final_dir / Path(MCP_FINAL_CLIP).name
    if published is not None:
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(published)

    markers = {
        role: workdir / f".korvid-mcp-demo-{role}" for role in ("ok", "failed", "ready", "go")
    }
    tape = workdir / "fake.tape"
    tape.write_text(tape_body.format(candidate=candidate, final=final), encoding="utf-8")

    stub_dir = workdir / "stubs"
    stub_dir.mkdir()
    log = workdir / "invocations.log"
    tmux = stub_dir / "tmux"
    tmux.write_text(f'#!/bin/sh\necho "tmux $*" >> "{log}"\n', encoding="utf-8")
    tmux.chmod(0o755)
    _write_fake_vhs(
        workdir / "fake-vhs",
        log=log,
        candidate=candidate,
        markers=markers,
        status=vhs_status,
        writes_candidate=writes_candidate,
        publishes_ok=publishes_ok,
        publishes_failed=publishes_failed,
    )

    environment = dict(os.environ)
    environment["PATH"] = f"{stub_dir}{os.pathsep}{environment.get('PATH', '')}"
    environment[MCP_RECORDER_ENV["vhs"]] = str(workdir / "fake-vhs")
    environment[MCP_RECORDER_ENV["tape"]] = str(tape)
    environment[MCP_RECORDER_ENV["candidate"]] = str(candidate)
    environment[MCP_RECORDER_ENV["final"]] = str(final)
    for role in ("ok", "failed", "ready", "go"):
        environment[MCP_RECORDER_ENV[role]] = str(markers[role])

    completed = subprocess.run(
        [str(MCP_RECORDER)],
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return _RecorderRun(
        status=completed.returncode,
        output=completed.stdout + completed.stderr,
        published=final.read_bytes() if final.exists() else None,
        candidate_left=candidate.exists(),
        scratch_left=tuple(role for role, path in markers.items() if path.exists()),
        tmux=log.read_text(encoding="utf-8") if log.exists() else "",
        vhs_ran=log.exists()
        and any(line.startswith("vhs ") for line in log.read_text(encoding="utf-8").splitlines()),
    )


def test_mcp_recorder_promotes_only_a_completed_run(tmp_path: Path) -> None:
    """The success path: record to the candidate, then publish it.

    This is the half that has to keep working after the boundary moved out
    of the tape. VHS returns, the client pane's success marker is present
    and its failure marker is absent, so the candidate becomes the
    published clip in one rename and every scratch file the recording made
    is gone by the time the wrapper exits.
    """
    run = _run_recorder(tmp_path / "accepted")

    assert run.status == 0, (
        f"a completed run must be published; the wrapper exited {run.status}: {run.output!r}"
    )
    assert run.published == MCP_CANDIDATE_BYTES, (
        "the published clip must be the candidate this run rendered, promoted in place of "
        f"the previous one; it holds {run.published!r}"
    )
    assert not run.candidate_left, (
        "the candidate is scratch: promoting it must move it, not copy it"
    )
    assert run.scratch_left == (), (
        f"the wrapper owns cleanup now; it left {run.scratch_left} behind"
    )
    assert f"kill-session -t {MCP_TMUX_SESSION}" in run.tmux, (
        f"the wrapper must tear down its own named session; the stub recorded {run.tmux!r}"
    )
    assert "kill-server" not in run.tmux, (
        f"only the demo session may be killed, never the user's whole server: {run.tmux!r}"
    )


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("client-published-failure", {"publishes_failed": True}),
        ("client-never-reported-success", {"publishes_ok": False}),
        ("vhs-itself-failed", {"vhs_status": 3}),
        ("no-candidate-despite-success", {"writes_candidate": False}),
    ],
)
def test_mcp_recorder_publishes_nothing_on_a_failed_recording(
    tmp_path: Path, case: str, overrides: dict[str, object]
) -> None:
    """The half a tape could never enforce: a failed run publishes nothing.

    An `exit 1` typed into a recorded pane cannot stop VHS — it renders its
    timeline and returns 0 with the canonical clip already overwritten. So
    each way a recording can fail is graded here on the artefact that
    matters: the previously approved clip must survive byte-identical, the
    candidate and every scratch marker must be gone, and the wrapper must
    say why it published nothing on stderr.
    """
    run = _run_recorder(tmp_path / case, **overrides)  # type: ignore[arg-type]  # per-case override map

    assert run.status != 0, f"{case} must fail the recording; the wrapper exited {run.status}"
    assert run.published == MCP_PUBLISHED_BYTES, (
        f"{case} must leave the approved clip byte-identical; it now holds {run.published!r}"
    )
    assert not run.candidate_left, f"{case} must remove the candidate it rejected"
    assert run.scratch_left == (), f"{case} left scratch behind: {run.scratch_left}"
    assert MCP_RECORDER_REJECTION in run.output, (
        f"{case} must print why nothing was published: {run.output!r}"
    )


def test_mcp_recorder_creates_no_clip_where_none_was_approved(tmp_path: Path) -> None:
    """A rejected first recording must not leave a half-story at the canonical path."""
    run = _run_recorder(tmp_path / "first-run", publishes_ok=False, published=None)

    assert run.status != 0, "a run without the client's success marker must fail"
    assert run.published is None, (
        "nothing was ever approved here; a rejected run must not create the published "
        f"clip, yet it holds {run.published!r}"
    )
    assert not run.candidate_left, "the rejected candidate must be removed"


def test_mcp_recorder_owns_the_canonical_path_and_the_tape_never_writes_it() -> None:
    """One writer for the published clip, and it is not VHS.

    The tape's `Output` is the only thing VHS obeys, so as long as it names
    the canonical clip, any failure — a client that died on its second
    call, a scene that never bound — has already overwritten the approved
    asset by the time anything can complain. The tape therefore renders to
    a candidate, and the canonical name appears exactly once in the chain:
    as the target the wrapper promotes to.
    """
    tape = MCP_TAPE.read_text(encoding="utf-8")
    outputs = [line.strip() for line in tape.splitlines() if line.strip().startswith("Output ")]

    assert outputs == [f"Output {MCP_CANDIDATE_CLIP}"], (
        f"the tape must render to the candidate and to nothing else; it declares {outputs}"
    )
    assert MCP_FINAL_CLIP not in tape, (
        "the published clip's path may not appear in the tape at all: VHS would write it "
        "before any check could run"
    )
    assert Path(MCP_FINAL_CLIP).name not in tape, (
        "the wrapper refuses any tape whose bytes contain the published clip's basename, "
        "under any spelling of the path and in comments too, so the shipped tape may not "
        "name it even in passing"
    )

    script = MCP_RECORDER.read_text(encoding="utf-8")
    promotions = [line.strip() for line in script.splitlines() if MCP_FINAL_CLIP in line]
    assert promotions, f"the wrapper must own {MCP_FINAL_CLIP}"
    assert all(line.startswith("final=") or line.lstrip().startswith("#") for line in promotions), (
        "the canonical path belongs in the wrapper's promotion target alone, so a reader "
        f"can see every writer of it at once; found {promotions}"
    )
    assert re.search(r"^\s*mv .*\"\$candidate\" \"\$final\"", script, re.MULTILINE), (
        "promotion must be a single rename of the candidate onto the published clip, so a "
        "reader never observes a half-written asset"
    )


def test_mcp_recorder_derives_its_canonical_needle_and_says_it_is_strict() -> None:
    """The byte guard must read the promotion target, not a second copy of it.

    Two independent checks stand in front of VHS: the `Output` shape check,
    which reads the tape the way VHS splits a directive, and a guard that
    refuses the published clip's basename anywhere in the tape's bytes. The
    second one is the fail-closed net, so its needle has to be derived from
    the very path the wrapper promotes to — a hard-coded second spelling
    would go stale the day the clip is renamed and quietly guard nothing.
    Being byte-level, it also rejects the name in a comment, which VHS would
    ignore; that is a deliberate trade the script has to state, because the
    next reader will otherwise file it as a bug and loosen it.
    """
    script = MCP_RECORDER.read_text(encoding="utf-8")
    commentary = "\n".join(
        line for line in script.splitlines() if line.lstrip().startswith("#")
    ).lower()

    assert re.search(r'^\s*final_name=\$\(basename -- "\$final"\)', script, re.MULTILINE), (
        'the guard\'s needle must be `basename -- "$final"`, so it always names the path '
        "this wrapper promotes to"
    )
    assert re.search(r'grep -qF -- "\$final_name" "\$tape"', script), (
        "the guard must look for that basename literally, anywhere in the tape's bytes — "
        "any parse of the tape is a parse VHS may not share"
    )
    assert "stricter than vhs" in commentary, (
        "the wrapper rejects tapes VHS would render, comments included; a reader who is "
        "not told that is a reader who will loosen it"
    )
    assert "comment" in commentary, (
        "the false positive this guard accepts is the canonical name in a comment; the "
        "script must name it rather than leave it to be discovered"
    )


def test_mcp_recorder_counts_every_output_token_not_only_a_line_s_first_field() -> None:
    """The shape check must read fields, and it must say what that costs.

    A reader who finds `$1 == "Output"` here will assume VHS reads lines.
    It does not: its lexer emits whitespace-separated tokens, so a second
    `Output` can hide behind any directive that takes no argument. The
    source has to show the scan — every field of every non-comment line —
    and it has to name the two edges of that decision, because both look
    like bugs to someone who does not know why they are there: a full-line
    comment is skipped (VHS ignores it, and the shipped tape says the word
    `Output` in its own prose), while an `Output` inside a `Type` string is
    refused even though VHS would type it as text.
    """
    script = MCP_RECORDER.read_text(encoding="utf-8")
    commentary = "\n".join(
        line for line in script.splitlines() if line.lstrip().startswith("#")
    ).lower()

    assert '$1 == "Output"' not in script, (
        "judging a line by its first field is the bypass itself: `Hide Output <clip>` is "
        "two directives VHS obeys and one line whose first field is `Hide`"
    )
    assert re.search(r"for \(i = 1; i <= NF; i\+\+\)", script), (
        "every whitespace-separated field must be visited, since that is the unit VHS's "
        "lexer works in"
    )
    assert re.search(r'\$i (?:==|!=) "Output"', script), (
        "each field must be compared to the token `Output` itself, not to a prefix or a "
        "pattern that a path could satisfy"
    )
    assert "type" in commentary, (
        "refusing `Output` inside a `Type` string is stricter than VHS; a reader who is "
        "not told will file it as a bug"
    )
    assert "comment" in commentary, (
        "skipping full-line comments is the other half of that decision, and the shipped "
        "tape depends on it"
    )


def test_mcp_recorder_binds_promotion_to_one_physical_directory() -> None:
    """The atomicity claim has a precondition; the script must enforce it.

    `mv` is `rename(2)` only inside one directory. `KORVID_MCP_CANDIDATE`
    and `KORVID_MCP_FINAL` are independent, so two overrides can put them
    on different filesystems, where `mv` becomes copy-then-unlink and a
    reader can observe a half-written clip. The wrapper resolves both
    parents physically — `cd -P` then `pwd -P`, so a symlinked spelling of
    one directory still counts as one — and refuses before VHS renders
    anything, since a recording that cannot be promoted atomically should
    never be made.
    """
    script = MCP_RECORDER.read_text(encoding="utf-8")
    commands = [
        line for line in script.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    commentary = "\n".join(
        line for line in script.splitlines() if line.lstrip().startswith("#")
    ).lower()

    assert script.count("pwd -P") >= 2, (
        "both parents must be resolved physically, or a symlinked spelling of one "
        "directory reads as two"
    )
    assert re.search(r'cd -P -- "\$\(dirname -- "\$candidate"\)"', script), (
        "the candidate's own parent is what the rename leaves from"
    )
    assert re.search(r'cd -P -- "\$\(dirname -- "\$final"\)"', script), (
        "the published clip's parent is what the rename arrives in, and it may only be "
        "resolved, never created"
    )
    compared = next(
        (
            index
            for index, line in enumerate(commands)
            if re.search(r"\$\w+_parent.*\$\w+_parent", line)
        ),
        None,
    )
    assert compared is not None, (
        "the two resolved parents must be compared; nothing else makes the overrides agree"
    )
    rendered = next(
        (index for index, line in enumerate(commands) if '"$vhs_bin" "$tape"' in line),
        None,
    )
    assert rendered is not None, "the wrapper must invoke VHS"
    assert compared < rendered, (
        "the comparison belongs in front of VHS: a render that cannot be promoted "
        "atomically is a render nobody should pay for"
    )
    assert re.search(r"override|korvid_mcp", commentary), (
        "the overrides are what can break the invariant; the script must say they have to "
        "preserve it"
    )


def test_mcp_recorder_is_a_strict_fail_closed_shell_script() -> None:
    """The boundary is only as good as the shell it is written in."""
    assert os.access(MCP_RECORDER, os.X_OK), (
        f"{MCP_RECORDER_COMMAND} is the published recipe; it must be executable"
    )
    script = MCP_RECORDER.read_text(encoding="utf-8")
    lines = script.splitlines()
    commands = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]

    assert lines[0] == "#!/usr/bin/env bash", f"the wrapper must be bash; it starts {lines[0]!r}"
    assert "set -euo pipefail" in script, (
        "an unset variable or an unchecked command in a promotion boundary is a published "
        "clip nobody reviewed"
    )
    assert re.search(r"^trap .* EXIT", script, re.MULTILINE), (
        "cleanup must run on every exit path, including the ones the script does not take itself"
    )
    assert 'kill-session -t "$session"' in script, (
        "teardown must name the demo session; killing a server or matching a pattern would "
        "reach sessions the recording never created"
    )
    for destructive in ("rm -rf", "kill-server", "rm -r "):
        offenders = [line.strip() for line in commands if destructive in line]
        assert not offenders, f"{destructive!r} has no place in this wrapper: {offenders}"

    removals = [line.strip() for line in commands if "rm -f" in line]
    assert removals, "the wrapper must clear its own scratch"
    assert all("*" not in line and "?" not in line for line in removals), (
        f"every removal must name the files it removes literally, never a glob: {removals}"
    )


def test_mcp_recorder_defaults_are_repository_relative_and_quoted() -> None:
    """Overrides exist for the contracts above; the defaults are the recipe.

    A contributor runs the wrapper with no environment at all, so each
    default has to be the repository-relative path the provenance page
    publishes. And every expansion of a path has to be quoted, because a
    checkout is allowed to live behind a directory with a space in its
    name — an unquoted one would split into two arguments and either miss
    the file or delete a neighbour.
    """
    script = MCP_RECORDER.read_text(encoding="utf-8")
    defaults = {
        MCP_RECORDER_ENV["vhs"]: "vhs",
        MCP_RECORDER_ENV["tape"]: "docs/demo/mcp-follow.tape",
        MCP_RECORDER_ENV["candidate"]: MCP_CANDIDATE_CLIP,
        MCP_RECORDER_ENV["final"]: MCP_FINAL_CLIP,
        MCP_RECORDER_ENV["ok"]: MCP_CLIENT_OK_FILE,
        MCP_RECORDER_ENV["failed"]: MCP_CLIENT_FAILED_FILE,
        MCP_RECORDER_ENV["ready"]: MCP_READY_FILE,
        MCP_RECORDER_ENV["go"]: MCP_GATE_FILE,
    }
    for variable, default in defaults.items():
        assert f"${{{variable}:-{default}}}" in script, (
            f"{variable} must default to {default!r}, the path the published recipe uses"
        )

    for name in ("vhs_bin", "tape", "candidate", "final", "ok_marker", "failed_marker"):
        occurrences = re.findall(rf".?\${name}\b.?", script)
        assert occurrences, f"the wrapper must expand ${name}"
        assert all(found.startswith('"') and found.endswith('"') for found in occurrences), (
            f"every expansion of ${name} must be quoted; found {occurrences}"
        )


class _HostileTapeRun(NamedTuple):
    """What a preflight refusal left behind.

    Attributes:
        status: The wrapper's own exit status.
        output: Its combined stdout and stderr.
        published: The bytes at the canonical path once the wrapper exited.
        reviewed: The bytes of every other approved clip beside it, by name.
        vhs_ran: Whether the fake VHS was invoked at all.
    """

    status: int
    output: str
    published: bytes
    reviewed: dict[str, bytes]
    vhs_ran: bool


def _run_hostile_tape(workdir: Path, tape_body: str) -> _HostileTapeRun:
    """Hand the wrapper `tape_body` behind a VHS that would overwrite the clip.

    The stand-in overwrites every approved clip in the scene directory the
    moment it runs — the published MCP capture and the two reviewed clips
    beside it — so a tape the preflight lets through is visible twice over:
    as changed bytes at an asset nobody re-recorded and as an invocation log
    the wrapper should never have created.

    Args:
        workdir: The directory every redirected path lives in.
        tape_body: The tape text. `{candidate}` and `{final}` are absolute
            paths; `{final_relative}`, `{final_dotted}` and `{final_updir}`
            are the same published clip spelled relative to the working
            directory, through `./` and through `../`. `{agent_demo}`,
            `{agent_demo_relative}` and `{relationship_demo}` are the
            reviewed clips beside it, and `{unreviewed}` is a path in the
            working directory no asset lives at.
    """
    workdir.mkdir(parents=True)
    scenes = workdir / "scenes"
    scenes.mkdir()
    candidate = scenes / Path(MCP_CANDIDATE_CLIP).name
    final = scenes / Path(MCP_FINAL_CLIP).name
    final.write_bytes(MCP_PUBLISHED_BYTES)
    siblings = {name: scenes / name for name in MCP_SIBLING_CLIPS}
    for name, path in siblings.items():
        path.write_bytes(f"previously approved {name}".encode())
    tape = workdir / "hostile.tape"
    tape.write_text(
        tape_body.format(
            candidate=candidate,
            final=final,
            final_relative=f"scenes/{final.name}",
            final_dotted=f"./scenes/{final.name}",
            final_updir=f"../{workdir.name}/scenes/{final.name}",
            agent_demo=siblings["agent-demo.mp4"],
            agent_demo_relative="scenes/agent-demo.mp4",
            relationship_demo=siblings["relationship-demo.mp4"],
            unreviewed=workdir / "somewhere-else.mp4",
        ),
        encoding="utf-8",
    )
    log = workdir / "invocations.log"
    vhs = workdir / "fake-vhs"
    clobbered = "\n".join(
        f"""printf '%s' 'rendered anyway' > "{path}\"""" for path in (final, *siblings.values())
    )
    vhs.write_text(
        f"""#!/bin/sh\necho "vhs $*" >> "{log}"\n{clobbered}\nexit 0\n""",
        encoding="utf-8",
    )
    vhs.chmod(0o755)

    environment = dict(os.environ)
    environment[MCP_RECORDER_ENV["vhs"]] = str(vhs)
    environment[MCP_RECORDER_ENV["tape"]] = str(tape)
    environment[MCP_RECORDER_ENV["candidate"]] = str(candidate)
    environment[MCP_RECORDER_ENV["final"]] = str(final)
    for role in ("ok", "failed", "ready", "go"):
        environment[MCP_RECORDER_ENV[role]] = str(workdir / f".korvid-mcp-demo-{role}")
    completed = subprocess.run(
        [str(MCP_RECORDER)],
        cwd=workdir,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return _HostileTapeRun(
        status=completed.returncode,
        output=completed.stdout + completed.stderr,
        published=final.read_bytes(),
        reviewed={name: path.read_bytes() for name, path in siblings.items()},
        vhs_ran=log.exists(),
    )


@pytest.mark.parametrize(
    ("case", "tape_body"),
    [
        ("a-second-output-on-its-own-line", "Output {candidate}\nOutput {final}\n"),
        ("a-second-output-indented-with-spaces", "Output {candidate}\n  Output {final}\n"),
        ("a-second-output-indented-with-a-tab", "Output {candidate}\n\tOutput {final}\n"),
        ("a-second-output-separated-by-a-tab", "Output {candidate}\nOutput\t{final}\n"),
        (
            "a-second-output-indented-and-tab-separated",
            "Output {candidate}\n \tOutput\t{final}\n",
        ),
        ("the-candidate-declared-twice", "Output {candidate}\nOutput {candidate}\n"),
        ("a-path-followed-by-a-second-field", "Output {candidate} {final}\n"),
        ("a-path-followed-by-a-stray-word", "Output {candidate} extra\n"),
        ("an-output-with-no-path-at-all", "Output {candidate}\nOutput\n"),
        ("the-published-clip-alone", "Output {final}\n"),
        ("the-published-clip-indented", "  Output {final}\n"),
        ("the-published-clip-tab-separated", "Output\t{final}\n"),
        ("no-output-at-all", "Sleep 1s\n"),
        ("an-output-sharing-a-line-with-hide", "Output {candidate}\nHide Output {final}\n"),
        ("an-output-sharing-a-line-with-show", "Output {candidate}\nShow Output {final}\n"),
        ("an-output-sharing-a-line-with-sleep", "Output {candidate}\nSleep 1s Output {final}\n"),
        ("an-output-sharing-a-line-with-enter", "Output {candidate}\nEnter Output {final}\n"),
        (
            "an-output-sharing-a-line-by-a-relative-path",
            "Output {candidate}\nHide Output {final_relative}\n",
        ),
        (
            "an-output-sharing-a-line-by-a-dot-slash-path",
            "Output {candidate}\nSleep 1s Output {final_dotted}\n",
        ),
        (
            "an-output-sharing-a-line-through-a-parent-directory",
            "Output {candidate}\nEnter Output {final_updir}\n",
        ),
        ("the-canonical-name-in-a-comment", "Output {candidate}\n# never {final}\n"),
    ],
)
def test_mcp_recorder_refuses_a_tape_that_would_write_the_published_clip(
    tmp_path: Path, case: str, tape_body: str
) -> None:
    """The boundary checks the tape it is about to run, not just the run.

    Editing `Output` back to the canonical path — or adding a second
    `Output` beside the candidate, which VHS honours — would put the
    published clip back under VHS's pen. Line shape is not enough to see
    that: VHS's grammar is whitespace-separated tokens, not lines, so
    `Hide Output <clip>`, `Sleep 1s Output <clip>` and `Enter Output <clip>`
    are each two directives VHS obeys on one line, and a line-based reader
    that only looks at a line's first field sees `Hide`, `Sleep` or `Enter`
    and lets them through. So the published clip's own basename may not
    appear anywhere in the tape's bytes — under any spelling of its path,
    absolute, repository-relative, `./` or through `../`, and in a comment
    too. Refusing means the approved clip is still byte-identical and VHS
    was never invoked at all.
    """
    run = _run_hostile_tape(tmp_path / case, tape_body)

    assert run.status != 0, f"{case} must not be recorded; the wrapper exited {run.status}"
    assert not run.vhs_ran, (
        f"{case} must be refused before VHS is invoked, not after it has already rendered"
    )
    assert run.published == MCP_PUBLISHED_BYTES, (
        f"{case} must leave the approved clip byte-identical; it now holds {run.published!r}"
    )
    assert MCP_RECORDER_REJECTION in run.output, f"the refusal must say why: {run.output!r}"


@pytest.mark.parametrize(
    ("case", "tape_body"),
    [
        (
            "a-second-output-behind-hide-naming-the-agent-clip",
            "Output {candidate}\nHide Output {agent_demo}\n",
        ),
        (
            "a-second-output-behind-show-naming-the-relationship-clip",
            "Output {candidate}\nShow Output {relationship_demo}\n",
        ),
        (
            "a-second-output-behind-sleep-naming-the-agent-clip-relatively",
            "Output {candidate}\nSleep 1s Output {agent_demo_relative}\n",
        ),
        (
            "a-second-output-behind-enter-naming-an-unreviewed-path",
            "Output {candidate}\nEnter Output {unreviewed}\n",
        ),
        (
            "a-second-output-sharing-the-candidate-s-own-line",
            "Output {candidate} Output {agent_demo}\n",
        ),
        (
            "a-second-output-behind-hide-with-no-path-at-all",
            "Output {candidate}\nHide Output\n",
        ),
        (
            "the-candidate-declared-behind-hide-and-again-behind-show",
            "Hide Output {candidate}\nShow Output {candidate}\n",
        ),
    ],
)
def test_mcp_recorder_refuses_a_second_output_that_names_another_asset(
    tmp_path: Path, case: str, tape_body: str
) -> None:
    """The byte guard knows one name; the shape check must know the grammar.

    Refusing the published clip's basename anywhere in the tape's bytes
    stops a second `Output` aimed at *this* clip, whatever VHS's lexer
    does. It says nothing about a second `Output` aimed at
    `agent-demo.mp4`, at `relationship-demo.mp4` or at any other path — no
    approved clip's name is in that needle, and a shape check that judges a
    line by its first field sees `Hide`, `Show`, `Sleep` or `Enter` and
    waves it through. VHS obeys both directives on that line, so a run that
    this wrapper later rejects has already overwritten a reviewed asset it
    does not even own.

    So every whitespace-separated field of every non-comment line is read,
    every `Output` token is counted, and exactly one standalone
    `Output <candidate>` is the only tape that reaches VHS. Refusing means
    VHS was never invoked and every approved clip in that directory — the
    MCP capture and both of its neighbours — is byte-identical.
    """
    run = _run_hostile_tape(tmp_path / case, tape_body)

    assert run.status != 0, f"{case} must not be recorded; the wrapper exited {run.status}"
    assert not run.vhs_ran, (
        f"{case} is a second directive VHS obeys; it must be refused before VHS is invoked"
    )
    assert run.published == MCP_PUBLISHED_BYTES, (
        f"{case} must leave the MCP clip byte-identical; it now holds {run.published!r}"
    )
    for name in MCP_SIBLING_CLIPS:
        assert run.reviewed[name] == f"previously approved {name}".encode(), (
            f"{case} aims VHS at an approved clip this recording does not own; {name} "
            f"must be byte-identical, yet it holds {run.reviewed[name]!r}"
        )
    assert MCP_RECORDER_REJECTION in run.output, f"the refusal must say why: {run.output!r}"


@pytest.mark.parametrize(
    ("case", "tape_body"),
    [
        ("plain", "Output {candidate}\nSleep 1s\n"),
        ("indented-with-spaces", "  Output {candidate}\nSleep 1s\n"),
        ("indented-with-a-tab", "\tOutput {candidate}\nSleep 1s\n"),
        ("separated-by-a-tab", "Output\t{candidate}\nSleep 1s\n"),
        ("separated-by-several-spaces", "Output   {candidate}\nSleep 1s\n"),
        ("trailing-whitespace", "Output {candidate}  \nSleep 1s\n"),
    ],
)
def test_mcp_recorder_reads_the_candidate_in_every_whitespace_form_vhs_accepts(
    tmp_path: Path, case: str, tape_body: str
) -> None:
    """The candidate's own directive must survive every whitespace form.

    The wrapper is deliberately stricter than VHS about one string — the
    published clip's basename, which it refuses anywhere in the tape — but
    it may not be stricter about the directive it is meant to accept. A
    check that only recognised one spelling would reject a tape VHS renders
    perfectly well, and would tempt the next edit back towards a laxer
    match. The wrapper normalises whitespace instead: every form below
    declares exactly one `Output`, it names the candidate, it names the
    published clip nowhere, and the run is published.
    """
    run = _run_recorder(tmp_path / case, tape_body=tape_body)

    assert run.status == 0, (
        f"{case} is a directive VHS obeys; the wrapper exited {run.status}: {run.output!r}"
    )
    assert run.published == MCP_CANDIDATE_BYTES, (
        f"{case} must promote the candidate this run rendered; it holds {run.published!r}"
    )


def test_mcp_recorder_accepts_the_shipped_tape_with_all_of_its_prose(tmp_path: Path) -> None:
    """The strictness has to stop at the tape that is actually shipped.

    A scan that reads every field is only useful if the reviewed tape still
    passes it, and `docs/demo/mcp-follow.tape` is not a bare list of
    directives: it opens with a page of commentary, one line of which
    explains that "VHS has already rendered its Output", and it types real
    shell commands through `Type`. This drives the shipped tape itself —
    every comment, every `Type`, every `Sleep` — through the real wrapper,
    with only its `Output` argument repointed at the temporary candidate.
    If the preflight ever stops distinguishing a full-line comment from a
    directive, this is where it is caught, instead of the next time someone
    tries to regenerate the clip.
    """
    shipped = MCP_TAPE.read_text(encoding="utf-8")
    body = shipped.replace(f"Output {MCP_CANDIDATE_CLIP}", "Output {candidate}")

    assert body != shipped, (
        f"the shipped tape must declare `Output {MCP_CANDIDATE_CLIP}`, or this contract "
        "is grading a tape it did not repoint"
    )
    assert "Output" in body.replace("Output {candidate}", ""), (
        "this contract exists because the shipped tape says `Output` outside its "
        "directive; if it stops doing so, drop it rather than let it pass vacuously"
    )

    run = _run_recorder(tmp_path / "shipped-tape", tape_body=body)

    assert run.status == 0, (
        f"the reviewed tape must still record; the wrapper exited {run.status}: {run.output!r}"
    )
    assert run.published == MCP_CANDIDATE_BYTES, (
        f"the shipped tape must promote its candidate; the clip holds {run.published!r}"
    )


def test_mcp_recorder_requires_the_candidate_s_output_to_stand_alone(tmp_path: Path) -> None:
    """A single `Output` sharing its line is refused, and that is deliberate.

    `Hide Output <candidate>` renders the candidate and nothing else, so
    VHS would treat it as a perfectly good tape. The wrapper still refuses
    it: the whole reason the shape check reads tokens is that it cannot
    know what the other directives on that line do, and the only way to
    count `Output`s without re-implementing VHS's parser is to require the
    one it accepts to be a directive on its own — first field, one
    argument, the candidate. That costs a tape author a newline; it buys a
    rule with no line in it left to hide behind. The refusal happens before
    VHS runs, so nothing is rendered either way.
    """
    run = _run_recorder(tmp_path / "output-behind-hide", tape_body="Hide Output {candidate}\n")

    assert run.status != 0, (
        f"an `Output` sharing its line must be refused; the wrapper exited {run.status}"
    )
    assert not run.vhs_ran, "the refusal must land before VHS is invoked"
    assert run.published == MCP_PUBLISHED_BYTES, (
        f"a refused tape leaves the approved clip alone; it holds {run.published!r}"
    )
    assert MCP_RECORDER_REJECTION in run.output, f"the refusal must say why: {run.output!r}"


def test_mcp_recorder_refuses_to_promote_across_directories(tmp_path: Path) -> None:
    """Promotion is a rename, and a rename only holds inside one directory.

    `mv` is atomic because it is `rename(2)` — but only while both paths
    share a directory, and therefore a filesystem. Across two of them `mv`
    degrades to copy-then-unlink, and the published clip is observable
    half-written: exactly the torn asset this whole boundary exists to
    prevent. `KORVID_MCP_CANDIDATE` and `KORVID_MCP_FINAL` are set
    independently, so nothing but a check makes them agree.

    The check therefore runs before VHS, not before the `mv`: a recording
    that cannot be promoted atomically is one that should never have been
    made. Refusing costs nothing — no render, the approved clip untouched,
    and the run's own scratch cleared behind it.
    """
    run = _run_recorder(tmp_path / "another-directory", candidate_dir="scratch")

    assert run.status != 0, (
        f"a candidate outside the clip's directory must be refused; exited {run.status}"
    )
    assert not run.vhs_ran, (
        "a promotion that could not be atomic must be refused before VHS renders anything"
    )
    assert run.published == MCP_PUBLISHED_BYTES, (
        f"the approved clip must be untouched; it holds {run.published!r}"
    )
    assert not run.candidate_left, "the run's own scratch must be cleared, wherever it was put"
    assert run.scratch_left == (), f"the refusal left scratch behind: {run.scratch_left}"
    assert MCP_RECORDER_REJECTION in run.output, f"the refusal must say why: {run.output!r}"


def test_mcp_recorder_requires_the_published_clip_s_directory_to_exist(tmp_path: Path) -> None:
    """The wrapper creates the candidate's directory, never the clip's.

    Rendering needs somewhere to write, so the candidate's parent is
    created exactly as a checkout expects. The published clip's parent is
    different: it is a reviewed directory that already exists in every
    checkout, and creating it would mean the same-directory check could be
    satisfied by the wrapper itself — it would happily invent the
    destination it was about to compare against.
    """
    run = _run_recorder(tmp_path / "no-such-directory", final_dir="not-made-yet", published=None)

    assert run.status != 0, (
        f"a published clip with no directory must be refused; exited {run.status}"
    )
    assert not run.vhs_ran, "the refusal must land before VHS is invoked"
    assert run.published is None, "the wrapper may not create the directory it promotes into"
    assert run.scratch_left == (), f"the refusal left scratch behind: {run.scratch_left}"
    assert MCP_RECORDER_REJECTION in run.output, f"the refusal must say why: {run.output!r}"


def test_mcp_recorder_promotes_through_a_second_spelling_of_one_directory(
    tmp_path: Path,
) -> None:
    """Same directory means the same *physical* directory, not the same text.

    A checkout reached through a symlink — a worktree behind `/var` on
    macOS, a home directory behind an automounter — spells one directory
    two ways. Comparing the strings would reject a promotion that is a
    plain rename, so the wrapper compares what `cd -P` and `pwd -P`
    resolve to instead. Here the candidate is named through a link to the
    scene directory the published clip lives in: one physical directory,
    one rename, one published clip.
    """
    run = _run_recorder(
        tmp_path / "through-a-link",
        candidate_dir="scenes-by-another-name",
        scenes_alias="scenes-by-another-name",
    )

    assert run.status == 0, (
        f"one physical directory named twice is still one rename; exited {run.status}: "
        f"{run.output!r}"
    )
    assert run.published == MCP_CANDIDATE_BYTES, (
        f"the candidate must be promoted; the clip holds {run.published!r}"
    )
    assert not run.candidate_left, "promotion must move the candidate, not copy it"


def test_mcp_follow_tape_leaves_the_verdict_to_the_wrapper() -> None:
    """The tape may compose and tear down; it may not decide publication.

    Its teardown used to end in an `if ... else ... exit 1` that read the
    client's markers. Nothing consumed that status: VHS had already
    rendered the canonical clip and exited 0, so the check announced a
    rejection it could not carry out. The markers now survive the tape and
    the wrapper grades them.
    """
    lines = [line.rstrip() for line in MCP_TAPE.read_text(encoding="utf-8").splitlines()]
    last_hide = max(index for index, line in enumerate(lines) if line.strip() == "Hide")
    teardown = lines[last_hide:]
    typed = [line for line in teardown if line.startswith('Type "')]

    assert not any("rejecting this recording" in line for line in lines), (
        "the tape can neither reject nor publish a recording; that claim belongs to "
        f"{MCP_RECORDER_COMMAND}"
    )
    for marker in (MCP_CLIENT_OK_FILE, MCP_CLIENT_FAILED_FILE):
        assert all(marker not in line for line in typed), (
            f"{marker} must survive the tape untouched — it is the evidence the wrapper "
            f"grades: {typed}"
        )
    assert any(f"kill-session -t {MCP_TMUX_SESSION}" in line for line in typed), (
        f"the tape must still tear its own session down: {typed}"
    )
    assert all("exit 1" not in line for line in teardown), (
        "an exit status after the capture changes nothing VHS does; the tape may not "
        f"pretend otherwise: {teardown}"
    )

    start = next(index for index, line in enumerate(lines) if "new-session" in line)
    cleared = [
        index
        for index, line in enumerate(lines)
        if "rm -f" in line and MCP_CLIENT_OK_FILE in line and MCP_CLIENT_FAILED_FILE in line
    ]
    assert any(index < start for index in cleared), (
        "both status files must still be removed before the panes are launched, so a "
        "marker left by an earlier run cannot decide this one"
    )


def test_mcp_candidate_recording_is_scratch_and_never_committable() -> None:
    """The candidate is a recording side effect, like every other one here."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert re.search(rf"^{re.escape(MCP_CANDIDATE_CLIP)}$", gitignore, re.MULTILINE), (
        f"{MCP_CANDIDATE_CLIP} is written by every recording attempt, successful or not; "
        "it must never be committable"
    )
    assert not (ROOT / MCP_CANDIDATE_CLIP).exists(), (
        "a candidate in the checkout is an interrupted recording; the wrapper removes it "
        "on every exit path"
    )
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", MCP_CANDIDATE_CLIP],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert tracked.returncode != 0, "the candidate recording must not be tracked"


def test_mcp_capture_provenance_publishes_the_client_status_handshake() -> None:
    """The published recipe must describe the verdict channel it depends on.

    The page already documents how the capture *starts* fail-closed. It
    must document how it *ends* fail-closed too, because the reason a
    reader can trust the shipped clip is no longer only that the client
    was released on a real signal — it is also that a client which failed
    could not have produced this asset.
    """
    module = _mcp_client_module()
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    lowered = " ".join(mcp.split()).lower()

    for status in (MCP_CLIENT_OK_FILE, MCP_CLIENT_FAILED_FILE):
        assert status in mcp, f"the provenance page must name {status}"
    assert "created and removed inside the checkout" in lowered, (
        "the status files are recording side effects like the handshake pair; say so"
    )
    assert re.search(r"absent[^.]{0,120}present", lowered), (
        "the page must state the acceptance rule: failure absent *and* success present"
    )
    assert "traceback" in lowered, (
        "the page must state that a failure publishes no traceback into the frames"
    )
    assert f"{module.FAILURE_HOLD:g} s" in lowered, (
        f"the page must publish the bounded failure hold the client ships ({module.FAILURE_HOLD}s)"
    )
    assert re.search(r"closing card[^.]{0,120}closing hold", lowered), (
        "the page must state when success is published: after the closing card, before "
        "the closing hold"
    )
    assert re.search(r"clears both[^.]{0,160}start", lowered), (
        "the page must state that the client owns these markers — it clears both at the "
        "start of a run, so a marker an earlier run left cannot grade this one"
    )
