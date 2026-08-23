"""Contracts for real, local product evidence used by the documentation site."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import re
import struct
import sys
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import ModuleType

from korvid.core.relationships import GraphResource, SummaryLike
from korvid.k8s.discovery import ResourceMeta
from korvid.ui.relationship_controller import RelationshipSnapshotLoader, graph_source_metas

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
SCENES = ROOT / "docs" / "assets" / "scenes"
INSTRUCTIONS = ROOT / "docs" / "demo" / "visual-storytelling.md"
LANDING = DOCS / "index.md"
AGENT_TAPE = ROOT / "docs" / "demo" / "agent.tape"
AGENT_PAGE = DOCS / "agent.md"
DEMO_HARNESS = ROOT / "docs" / "demo" / "demo.py"

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


def _decode_png_rgb(path: Path) -> tuple[int, int, list[bytearray]]:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG into scanlines.

    The repository ships no image dependency, and these captures are the
    product's own evidence, so the few lines of PNG plumbing live here
    rather than in the runtime.

    Args:
        path: A PNG file written by the capture pipeline.

    Returns:
        `(width, height, rows)` where each row holds `width * channels`
        bytes.
    """
    payload = path.read_bytes()
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


def _band_deviation(rows: list[bytearray], top: int, bottom: int, channels: int) -> int:
    """Largest per-channel distance from `#111111` inside the client pane."""
    left, right = MCP_CLIENT_PANE
    worst = 0
    for row in rows[top:bottom]:
        for value in row[left * channels : right * channels]:
            worst = max(worst, abs(value - 0x11))
    return worst


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


def test_storytelling_pngs_meet_their_declared_size_and_byte_budget() -> None:
    """Binary/dimension/byte contract only — it says nothing about content.

    The declared `width`/`height` attributes on the site must match the real
    intrinsic size or the reserved box is wrong, and the byte budget keeps
    the page light. Whether a capture is legible or shows the right screen
    is verified by looking at it, not here.
    """
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
    assert digest == "48a11a419d66b1732526387783abe25335484435e0fe51b9f83188de0d60a0f8", (
        "docs/assets/scenes/mcp-follow-demo.mp4 is a privacy-sensitive re-encode; "
        "review any byte change explicitly alongside the redaction recipe in "
        "docs/demo/visual-storytelling.md before updating this SHA-256 pin"
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
    assert any(edge.relation.value == "uses_config" for edge in dependencies), (
        f"the declared ConfigMap dependency must survive: {dependencies}"
    )
    assert dependents, "at least one resource must depend on the demo pod"
