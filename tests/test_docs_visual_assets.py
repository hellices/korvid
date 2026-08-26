"""Contracts for real, local product evidence used by the documentation site."""

from __future__ import annotations

import asyncio
import importlib.util
import math
import os
import re
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import ModuleType

import pytest

from korvid.agent.events import AgentEvent, TextDelta, ToolCallStarted, TurnComplete
from korvid.core.mcp import MCPControllerBase
from korvid.core.relationships import GraphResource, SummaryLike
from korvid.k8s.discovery import ResourceMeta
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
#: The bound the tape's typed readiness loop enforces: 600 iterations of
#: `sleep 0.1`, so 60 seconds at the outside.
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

    `docs/assets/mcp-follow-demo.gif` is 1280x711 with a 63:64 sample aspect
    ratio. Making the height even for `yuv420p` scales it to 710 and, unless
    the chain says otherwise, `scale` preserves the *display* aspect by
    rewriting the SAR to 2485:2528 — an encode that stores 1280x710 but that
    every browser lays out at 1258x710. The landing page reserves a box from
    the stored geometry, so the clip would then pillarbox inside its own
    reservation. Forcing square pixels is what makes the reservation true.
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
    assert "vhs docs/demo/mcp-follow.tape" in instructions
    assert "docs/assets/mcp-follow-demo.gif" in instructions


def test_mcp_capture_instructions_distinguish_public_landing_media_from_the_served_source_gif() -> (
    None
):
    """The raw reviewed GIF is a source asset, not the landing page's evidence.

    MkDocs still publishes `docs/assets/**` as static assets, so keeping the
    checked-in GIF for README/source provenance is not the same claim as a
    visitor-facing page embedding it. The instructions must say that
    distinction plainly, and the landing sources must keep using only the
    locally recorded MP4/poster pair.
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

    assert (
        "No official-site page embeds or uses the unredacted GIF as visitor-facing "
        "evidence." in normalized_mcp
    )
    assert "MkDocs still serves it at `assets/mcp-follow-demo.gif`." in normalized_mcp
    assert (
        "Sanitizing or re-recording that pre-existing README/source asset is a separate follow-up."
    ) in normalized_mcp
    assert (
        "The landing page uses only the locally recorded MP4/poster above, which is "
        "derived from no part of it." in normalized_mcp
    )
    assert not embeds, (
        "no official-site page should embed the raw MCP GIF as visitor-facing evidence; "
        f"found references in {embeds}"
    )
    assert "mcp-follow-demo.gif" not in landing
    assert 'src="assets/scenes/mcp-follow-demo.mp4"' in landing
    assert "assets/scenes/mcp-poster.png" in landing


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
    assert tape.count(f"rm -f {MCP_GATE_FILE}") >= 2, (
        f"{MCP_GATE_FILE} must be removed before the run starts and again after "
        "it ends, so an interrupted recording leaves nothing behind"
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
    keystroke path the capture's own provenance promises never happens.
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

    The fix is an ordering between two numbers that live in different files'
    worth of reasoning but in one tape: the loop is bounded at
    600 x 0.1 s = 60 s, so the hidden allowance is 65 s. Releasing early is
    harmless in the other direction — the client pane is blocked on the
    separate gate file, so no part of the story can run inside the hidden
    block.
    """
    lines = MCP_TAPE.read_text(encoding="utf-8").splitlines()

    wait_indices = [
        index
        for index, line in enumerate(lines)
        if MCP_READY_FILE in line and re.search(r"while .*-f |until .*-f ", line)
    ]
    assert wait_indices, f"the tape must wait for {MCP_READY_FILE} inside its hidden block"
    wait_line = lines[wait_indices[0]]

    bound = re.search(r"-lt (\d+)", wait_line)
    assert bound, f"the readiness wait must state its iteration bound: {wait_line!r}"
    step = re.search(r"sleep ([0-9.]+)", wait_line)
    assert step, f"the readiness wait must state its step: {wait_line!r}"
    wait_seconds = int(bound.group(1)) * float(step.group(1))
    assert wait_seconds == pytest.approx(MCP_READY_WAIT_SECONDS), (
        f"the readiness loop must stay bounded at {MCP_READY_WAIT_SECONDS} s; "
        f"{wait_line!r} bounds it at {wait_seconds} s"
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


def test_mcp_follow_tape_cleans_both_of_its_handshake_files() -> None:
    """Two scratch files now, both born of recording and neither committed."""
    tape = MCP_TAPE.read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for scratch in (MCP_GATE_FILE, MCP_READY_FILE):
        removals = [line for line in tape.splitlines() if "rm -f" in line and scratch in line]
        assert len(removals) >= 2, (
            f"{scratch} must be removed before the run starts and again after it "
            f"ends, so an interrupted recording leaves nothing behind; found {removals}"
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
        "vhs docs/demo/mcp-follow.tape",
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
