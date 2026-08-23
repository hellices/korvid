"""Contracts for real, local product evidence used by the documentation site."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import math
import re
import struct
import sys
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType

from korvid.agent.events import AgentEvent, TextDelta, TurnComplete
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
DEMO_HARNESS = DEMO_DIR / "demo.py"
VISUAL_STORYTELLING_PLAN = DOCS / "superpowers" / "plans" / "2026-08-22-visual-storytelling.md"

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
#: The MCP filter chain this repository published before square pixels were
#: forced. `docs/assets/mcp-follow-demo.gif` is 1280x711 with SAR 63:64, and
#: `scale`'s even-height correction (711 -> 710) preserves the display aspect
#: by rewriting the SAR to 2485:2528 instead of dropping it — so the encode
#: stored 1280x710 but displayed 1258x710. The contract below must reject it.
MCP_UNNORMALIZED_CHAIN = (
    "scale=trunc(iw/2)*2:trunc(ih/2)*2,"
    "trim=start_frame=36:end_frame=84,setpts=PTS-STARTPTS,"
    "drawbox=x=1000:y=22:w=280:h=320:color=0x111111:t=fill,"
    "drawbox=x=1000:y=578:w=280:h=132:color=0x111111:t=fill"
)
#: One `ffmpeg` invocation that reads the reviewed GIF and writes the
#: sanitised clip, without swallowing the commands published beside it.
MCP_MP4_COMMAND = re.compile(
    r"ffmpeg(?:(?!ffmpeg).)*?docs/assets/mcp-follow-demo\.gif"
    r"(?:(?!ffmpeg).)*?docs/assets/scenes/mcp-follow-demo\.mp4",
    re.S,
)

#: The MCP capture is a two-pane recording: korvid on the left of the divider
#: at x=999, the external MCP client on the right.
MCP_CLIENT_PANE = (1000, 1280)
#: Rows of the client pane the sanitising `drawbox` pass clears. Everything
#: outside the client's own prompt-and-tool-call exchange belongs to that
#: third-party session (its startup banner and tool inventory above, its
#: working directory, branch, token spend and model name below) and is
#: unrelated to what korvid does.
MCP_CLEARED_BANDS = ((22, 342), (578, 710))
#: The rows that must keep carrying the evidence, so a future "fix" cannot
#: satisfy the contract by blanking the whole pane.
MCP_EVIDENCE_BAND = (342, 578)


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


def _mcp_filter_chains(source: str, label: str) -> list[str]:
    """Every `-vf` chain the MCP sanitising command publishes in `source`."""
    chains = []
    for command in MCP_MP4_COMMAND.finditer(source):
        chain = re.search(r"-vf '([^']*)'", command.group(0))
        assert chain is not None, f"{label} publishes an MCP command with no -vf chain"
        chains.append(" ".join(chain.group(1).split()))
    assert chains, f"{label} must publish the MCP sanitising command"
    return chains


def _forces_square_pixels(chain: str) -> bool:
    """Does an ffmpeg filter chain drop a non-square SAR after its geometry?

    `setsar=1` only governs what follows it, so it has to come after the
    `scale`/`trim` pass that rewrites the sample aspect ratio in the first
    place. None of the MCP chain's filters take a comma-bearing argument, so
    splitting on commas is the filtergraph's own step separator here.
    """
    steps = [step.strip() for step in chain.split(",")]
    if "setsar=1" not in steps:
        return False
    geometry = [index for index, step in enumerate(steps) if step.startswith(("scale=", "trim="))]
    return steps.index("setsar=1") > max(geometry, default=-1)


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


def test_scripted_agent_runtime_reports_its_hard_coded_marker_as_unsupported() -> None:
    """The scripted turn mints no evidence, so its `[E1]` must not read as cited.

    `TurnComplete.cited` means "the ledger actually minted this reference".
    `ScriptedAgentRuntime` reads nothing, executes no tool and mints no
    evidence — its `[E1]` is a string in a hard-coded answer. Reporting it as
    `cited` made the panel treat an unsourced claim as a supported one and
    suppressed the very warning korvid ships for this case (issue #192), so
    the capture published a frame that contradicts the product's own
    behaviour. `uncited` is what the panel is owed, and what it renders as
    the yellow "unsupported citation" note.

    This runs the real runtime rather than reading its source: the event is
    the contract.
    """
    harness = _demo_harness()

    async def drain() -> list[AgentEvent]:
        return [
            event
            async for event in harness.ScriptedAgentRuntime().run_turn(
                "Why is the payment worker failing?", "selected: shop/payment-worker"
            )
        ]

    events = asyncio.run(drain())
    completions = [event for event in events if isinstance(event, TurnComplete)]
    assert len(completions) == 1, f"the scripted turn ends exactly once: {events}"
    complete = completions[0]

    assert complete.uncited == ("E1",), (
        "the scripted marker must be reported as unsupported, so the panel renders "
        f"its citation warning; found uncited={complete.uncited!r}"
    )
    assert complete.cited == (), (
        "nothing was read this turn, so no reference may be reported as minted "
        f"evidence; found cited={complete.cited!r}"
    )

    answer = "".join(event.text for event in events if isinstance(event, TextDelta))
    assert "[E1]" in answer, (
        "the warning is only meaningful beside the marker it flags, so the "
        f"scripted answer must keep it: {answer!r}"
    )


def test_scripted_agent_runtime_exposes_an_empty_evidence_ledger() -> None:
    """Opening the scripted `[E1]` must report missing evidence, not crash."""
    runtime = _demo_harness().ScriptedAgentRuntime()
    assert hasattr(runtime, "evidence"), "the Agent UI reads runtime.evidence directly"
    assert runtime.evidence.resolve("E1") is None


def test_scripted_agent_runtime_exposes_no_outbound_payload() -> None:
    """Opening the payload inspector must see an empty snapshot, not crash."""
    runtime = _demo_harness().ScriptedAgentRuntime()
    assert hasattr(runtime, "latest_outbound_payload"), (
        "the Agent UI reads runtime.latest_outbound_payload directly"
    )
    assert runtime.latest_outbound_payload is None


def test_visual_storytelling_plan_keeps_the_scripted_runtime_safety_contract() -> None:
    """Replaying the executable plan must preserve the demo runtime boundary."""
    plan = VISUAL_STORYTELLING_PLAN.read_text(encoding="utf-8")
    runtime = plan.split("class ScriptedAgentRuntime:", 1)[1].split("\n```", 1)[0]
    assert "self.evidence = EvidenceLedger()" in runtime
    assert "self.latest_outbound_payload = None" in runtime
    assert 'uncited=("E1",)' in runtime
    assert not any(line.strip().startswith('cited=("E1",)') for line in runtime.splitlines())


def test_agent_capture_copy_says_the_scripted_marker_is_flagged_unsupported() -> None:
    """Every surface that publishes this frame must explain the yellow note.

    The recording now shows korvid's own unsupported-citation warning under
    the scripted answer. A visitor who is not told why would read it as a
    product defect instead of the product working exactly as designed on an
    unsourced claim, so the provenance page states it and the embedding
    pages inherit it.
    """
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    section = instructions.split("## Embedded agent", 1)[1].split("\n## ", 1)[0]
    lowered = " ".join(section.lower().split())

    assert "uncited" in lowered, (
        "the provenance page must name the field the scripted runtime reports"
    )
    assert "unsupported citation" in lowered, (
        "and the warning the panel renders from it, so the frame is explainable"
    )
    for fact in ("no evidence", "yellow"):
        assert fact in lowered, (
            f"the provenance must say why the marker is unsupported and what the "
            f"capture shows; {fact!r} is missing"
        )
    assert "validated citation" not in lowered.replace("not validated", ""), (
        "the capture still validates nothing"
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
    assert "docs/assets/mcp-follow-demo.gif" in instructions


def test_mcp_capture_instructions_distinguish_public_landing_media_from_the_served_source_gif() -> (
    None
):
    """The raw reviewed GIF is a source asset, not the landing page's evidence.

    MkDocs still publishes `docs/assets/**` as static assets, so keeping the
    checked-in GIF for README/source provenance is not the same claim as a
    visitor-facing page embedding it. The instructions must say that
    distinction plainly, and the landing sources must keep using only the
    sanitised derived MP4/poster pair.
    """
    mcp = INSTRUCTIONS.read_text(encoding="utf-8").split("## MCP follow", 1)[1]
    normalized_mcp = " ".join(mcp.split())
    landing = LANDING.read_text(encoding="utf-8")
    public_pages = [
        path
        for path in DOCS.rglob("*.md")
        if "demo" not in path.parts and "superpowers" not in path.parts
    ]
    embeds = [
        str(path.relative_to(ROOT))
        for path in public_pages
        if "mcp-follow-demo.gif" in path.read_text(encoding="utf-8")
    ]

    assert (
        "No official-site page embeds or uses the unredacted GIF as visitor-facing "
        "evidence." in normalized_mcp
    )
    assert "MkDocs still serves it at `assets/mcp-follow-demo.gif`." in normalized_mcp
    assert (
        "Sanitizing or re-recording that pre-existing README/source asset is a separate follow-up."
    ) in normalized_mcp
    assert "The landing page uses only the sanitized derived MP4/poster." in normalized_mcp
    assert not embeds, (
        "no official-site page should embed the raw MCP GIF as visitor-facing evidence; "
        f"found references in {embeds}"
    )
    assert "mcp-follow-demo.gif" not in landing
    assert 'src="assets/scenes/mcp-follow-demo.mp4"' in landing
    assert "assets/scenes/mcp-poster.png" in landing


def test_mcp_landing_media_carries_no_third_party_session_internals() -> None:
    """The MCP tile publishes an unrelated assistant's window; only its work may ship.

    The recording's right-hand pane is a third-party MCP client. Its own
    session chrome — the startup banner and tool inventory above the
    exchange, and the working directory, branch, token spend and model name
    below it — has nothing to do with korvid, but the landing page promotes
    this frame to a scene poster and an evidence tile. The capture pipeline
    clears exactly those two bands, so they must hold nothing but the pane's
    own `#111111` background (codec noise aside), while the band between
    them must still carry the client's prompt and tool calls: a blank pane
    would prove nothing.
    """
    width, height, rows = _decode_png_rgb(SCENES / "mcp-poster.png")
    assert (width, height) == (1280, 710)
    channels = len(rows[0]) // width

    for top, bottom in MCP_CLEARED_BANDS:
        deviation = _band_deviation(rows, top, bottom, channels)
        assert deviation <= 16, (
            f"rows {top}-{bottom} of the external client's pane must be cleared to its "
            f"background; found a pixel {deviation} off #111111, which is legible "
            "content, not codec noise"
        )

    evidence = _band_deviation(rows, *MCP_EVIDENCE_BAND, channels)
    assert evidence > 100, (
        "the retained band must still show the external client's prompt and tool "
        f"calls; peak contrast {evidence} means the pane was blanked instead of framed"
    )


def test_mcp_capture_instructions_document_the_sanitising_pass() -> None:
    """The published command must reproduce the sanitised asset exactly."""
    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    mcp = instructions[instructions.index("## MCP follow") :]
    for fragment in (
        "trim=start_frame=36:end_frame=84",
        "drawbox=x=1000:y=22:w=280:h=320",
        "drawbox=x=1000:y=578:w=280:h=132",
        "select='eq(n\\,9)'",
    ):
        assert fragment in mcp, f"the MCP capture recipe must publish `{fragment}`"
    lowered = mcp.lower()
    assert "follow" in lowered
    for reason in ("model", "token", "directory"):
        assert reason in lowered, (
            "the recipe must say which third-party session details the cleared bands "
            f"remove; {reason!r} is missing"
        )


def test_mcp_follow_demo_mp4_sha256_matches_the_reviewed_sanitized_bytes() -> None:
    digest = hashlib.sha256((SCENES / "mcp-follow-demo.mp4").read_bytes()).hexdigest()
    assert digest == "f4fe95f9109655124d0a16a6d6132e7396aeb14b2b22b89788c629378c59f2d5", (
        "docs/assets/scenes/mcp-follow-demo.mp4 is a privacy-sensitive re-encode; "
        "review any byte change explicitly alongside the redaction recipe in "
        "docs/demo/visual-storytelling.md before updating this SHA-256 pin"
    )


def test_mcp_capture_recipe_forces_square_pixels_everywhere_it_is_published() -> None:
    """Every replayable copy of the recipe must normalise the GIF's SAR.

    The clip's bytes are pinned above, but a pin only proves the checked-in
    file was reviewed — it cannot stop the next regeneration from restoring a
    1258x710 display box. The command that produces those bytes is published
    twice (the provenance page and the executable plan a contributor
    replays), so `setsar=1` has to be in both, after the `scale`/`trim` pass
    that rewrites the sample aspect ratio.
    """
    assert not _forces_square_pixels(MCP_UNNORMALIZED_CHAIN), (
        "this contract must reject the chain that shipped the non-square encode, "
        "or it would pass without normalising anything"
    )
    assert not _forces_square_pixels(f"setsar=1,{MCP_UNNORMALIZED_CHAIN}"), (
        "a `setsar=1` placed before the geometry pass normalises nothing"
    )

    sources = {
        "docs/demo/visual-storytelling.md": INSTRUCTIONS,
        "docs/superpowers/plans/2026-08-22-visual-storytelling.md": VISUAL_STORYTELLING_PLAN,
    }
    for label, path in sources.items():
        for chain in _mcp_filter_chains(path.read_text(encoding="utf-8"), label):
            assert _forces_square_pixels(chain), (
                "the published MCP command leaves the GIF's 63:64 pixels in the "
                f"encode; it needs `setsar=1` after its geometry pass. {label} "
                f"has {chain!r}"
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


def test_agent_capture_provenance_states_what_the_scripted_runtime_proves() -> None:
    """`ScriptedAgentRuntime` proves korvid's panel, never its pipeline.

    The tape drives the product's real `AgentPanel`: VHS types the prompt into
    `#agent-input` and presses Enter, so the submission crosses the genuine
    `Input`/`on_input_submitted` path and the panel renders the turn itself.
    Everything behind that boundary is fabricated — the runtime discards the
    prompt text and the screen context it is handed, contacts no provider,
    executes no read tool, and yields hard-coded tool, text, citation, and
    token events.

    That distinction is what every embedding page has to inherit, so it is
    written down where the media's provenance lives instead of being
    rediscovered by the next reviewer.
    """
    harness = DEMO_HARNESS.read_text(encoding="utf-8")
    assert "del user_text, screen_context" in harness, (
        "this contract is written against a runtime that ignores its prompt and "
        "screen context; if the harness stopped discarding them the provenance "
        "wording has to be revisited rather than silently kept"
    )

    instructions = INSTRUCTIONS.read_text(encoding="utf-8")
    section = instructions.split("## Embedded agent", 1)[1].split("\n## ", 1)[0]
    lowered = " ".join(section.lower().split())

    assert "proves" in lowered, "the provenance section must state what the capture does prove"
    assert "does not prove" in lowered, "and must state the other half of the boundary explicitly"
    for proof in ("agentpanel", "input", "renders"):
        assert proof in lowered, f"the section must name what the capture does prove: {proof!r}"
    for limit in (
        "discards",
        "screen context",
        "contacts no provider",
        "executes no read tool",
        "hard-coded",
        "not validated",
    ):
        assert limit in lowered, f"the section must name what it does not prove: {limit!r}"
    assert "scripted agentpanel walkthrough" in lowered, (
        "the provenance page must give the embedding surfaces the label they use"
    )


def test_agent_page_capture_is_labelled_scripted_and_split_from_the_turn_flow() -> None:
    """`docs/agent.md` must not present the scripted frame as a real turn.

    The storyboard pairs one capture with korvid's production turn flow. That
    flow (context, bounded reads, validated citations, UI drive) documents
    what the shipped `AgentRuntime` does and stays exactly as strong; the
    capture beside it executes none of it, so its alt and caption identify the
    deterministic scripted walkthrough and the caption carries the note that
    no provider and no real read tool run.
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
    assert "scripted" in alt_text, f"the alt must identify the scripted capture: {alt_text!r}"
    assert "agentpanel" in alt_text or "agent panel" in alt_text, (
        f"the alt must name the panel the capture really shows: {alt_text!r}"
    )

    caption = re.search(r"<figcaption[^>]*>(.*?)</figcaption>", figure, re.DOTALL)
    assert caption is not None, "the storyboard keeps its figure caption"
    caption_text = " ".join(re.sub(r"<[^>]+>", " ", caption.group(1)).lower().split())
    assert "scripted" in caption_text, (
        f"the caption must identify the scripted capture: {caption_text!r}"
    )
    assert "no provider" in caption_text, (
        f"the capture note must say no provider runs: {caption_text!r}"
    )
    assert "no real read tool" in caption_text, (
        f"the capture note must say no real read tool runs: {caption_text!r}"
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
