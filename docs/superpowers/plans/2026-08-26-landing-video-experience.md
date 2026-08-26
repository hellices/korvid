# Landing Video Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the landing videos play like lightweight GIFs and replace the abbreviated Agent and MCP clips with complete, reproducible synthetic-cluster stories.

**Architecture:** Extend the existing dependency-free landing controller so it owns visibility-aware playback for the hero and selected scene while preserving poster and no-JavaScript fallbacks. Replace the documentation-only Agent event injector with a deterministic `LLMProvider` driving the real `AgentRuntime` and `ToolExecutor`, and record MCP through a clean terminal client using the real MCP SDK and Streamable HTTP server. Keep every capture local, synthetic, read-only, and reproducible through VHS.

**Tech Stack:** MkDocs Material, plain JavaScript, Node's built-in `vm` test harness, Python 3.11+, Textual, Korvid `AgentRuntime`, Korvid `ToolExecutor`, MCP Python SDK, VHS, tmux, ffmpeg/ffprobe, pytest.

## Global Constraints

- Do not run the repository-wide quality gate.
- Hero and only the selected scene start automatically when visible.
- No programmatic playback occurs when `prefers-reduced-motion: reduce` matches.
- A rejected `HTMLMediaElement.play()` promise leaves the poster and native controls usable.
- Inactive scene videos must not download bytes before their first selection.
- Agent capture duration is 12–15 seconds and uses no external provider, credential, network service, or live cluster.
- Agent tool results and citation identifiers must come from the real runtime, executor, and evidence ledger.
- MCP capture duration is 12–15 seconds and uses real read-only Streamable HTTP MCP requests plus real follow navigation.
- MCP logs remain visible for at least two seconds before the Helm releases view.
- No real cluster identifier, credential, user path, branch, model name, token count, or unrelated client chrome may appear.
- Visitor-facing media remains local MP4 with local PNG posters, 1280-pixel width, and square pixels.
- Existing media URLs remain stable.
- Use `uv run --frozen` for every Python command; never re-lock or modify `uv.lock`.

## File Structure

- `docs/assets/javascripts/visual-storytelling.js`
  - Owns landing media visibility, motion preference, deferred source promotion, and scene selection.
- `tests/js/scene_switcher_harness.mjs`
  - Executes the real controller against a dependency-free DOM/media/intersection harness.
- `docs/index.md`
  - Declares hero autoplay intent, deferred scene sources, and truthful scene copy.
- `tests/test_docs_landing_design.py`
  - Pins landing markup, no-JavaScript fallback, bandwidth, and playback contracts.
- `docs/demo/agent_story.py`
  - Contains the deterministic documentation provider and a builder for the real grounded Agent runtime.
- `docs/demo/demo.py`
  - Supplies synthetic `ReadOps`, app composition, and Agent/MCP scene lifecycle.
- `docs/demo/agent.tape`
  - Records the grounded Agent story through the real AgentPanel input.
- `docs/demo/mcp_client.py`
  - Calls the running local MCP endpoint through the official SDK and renders only story-relevant output.
- `docs/demo/mcp-follow.tape`
  - Records the real TUI and real MCP client in a deterministic tmux split.
- `docs/assets/scenes/agent-demo.mp4`, `docs/assets/scenes/agent-poster.png`
  - Replacement Agent capture and settled final frame.
- `docs/assets/scenes/mcp-follow-demo.mp4`, `docs/assets/scenes/mcp-poster.png`
  - Replacement MCP capture and readable log/follow frame.
- `docs/demo/visual-storytelling.md`
  - Exact regeneration, provenance, privacy, timing, and truthfulness contract.
- `tests/test_docs_visual_assets.py`
  - Runtime, tape, duration, geometry, privacy, and provenance contracts for both captures.
- `docs/agent.md`, `docs/mcp.md`
  - Updated capture disclosures; production behavior remains separate from recording proof.

---

### Task 1: Visibility-aware landing playback

**Files:**
- Modify: `tests/js/scene_switcher_harness.mjs`
- Modify: `tests/test_docs_landing_design.py`
- Modify: `docs/index.md`
- Modify: `docs/assets/javascripts/visual-storytelling.js`

**Interfaces:**
- Consumes: existing `[data-scene-switcher]`, tab/panel ARIA relationships, `data-poster`, and no-JavaScript fallback images.
- Produces: `data-autoplay-video` for standalone hero media, `data-src` for deferred scene video bytes, and controller behavior that starts only visible motion-allowed media.

- [ ] **Step 1: Extend the JavaScript harness with failing autoplay contracts**

Change `HTMLElement.play()` to return a controllable promise, add `currentTime`,
add a shared `matchMedia`/`MediaQueryList` stub that can deliver a mid-visit
`change`, and make the intersection observer callback drive both entering and
leaving the viewport:

```javascript
class HTMLElement {
  constructor(tag, attributes = {}) {
    // Keep the existing fields.
    this.currentTime = 7;
    this.playError = null;
  }

  play() {
    this.played += 1;
    return this.playError === null
      ? Promise.resolve()
      : Promise.reject(this.playError);
  }
}

// One MediaQueryList per run, shared by every matchMedia() call: a stub that
// returned a fresh object per call could never deliver a `change` to the
// controller, hiding the very bug the fake exists to catch.
class MediaQueryListFake {
  constructor(matches, { changeEvents = true } = {}) {
    this.media = MOTION_QUERY;
    this.matches = matches;
    this.listeners = [];
    if (changeEvents) {
      this.addEventListener = (type, handler) => {
        if (type === "change") this.listeners.push(handler);
      };
    }
  }

  set(matches) {
    this.matches = matches;
    for (const handler of this.listeners) {
      handler({ type: "change", media: MOTION_QUERY, matches });
    }
  }
}

function run(
  document,
  {
    intersectionObserver = true,
    reducedMotion = false,
    matchMedia = true,
    motionChangeEvents = true,
  } = {},
) {
  const errors = [];
  const observers = [];
  const queries = [];
  const media = new MediaQueryListFake(reducedMotion, { changeEvents: motionChangeEvents });
  const sandbox = {
    document,
    HTMLElement,
    console: { error: (...args) => errors.push(args.map(String).join(" ")) },
  };
  if (matchMedia) {
    sandbox.matchMedia = (query) => {
      queries.push(query);
      return media;
    };
  }
  // Keep the existing IntersectionObserver fake and vm execution.
  return { errors, observers, media, queries };
}
```

Add scenarios asserting:

```javascript
observers[0].callback([{ isIntersecting: true }]);
assert.equal(first.videos[0].played, 1);
assert.equal(first.videos[0].currentTime, 0);
assert.equal(first.videos[1].played, 0);

first.tabs[1].dispatch("click", {});
assert.ok(first.videos[0].paused > 0);
assert.equal(first.videos[1].played, 1);
assert.equal(first.videos[1].getAttribute("src"), "agent.mp4");

observers[0].callback([{ isIntersecting: false }]);
assert.ok(first.videos.every((video) => video.paused > 0));
```

Add separate reduced-motion and rejected-promise scenarios. The
reduced-motion case must remain at `played === 0`; a `media.set(true)` flip
mid-visit must pause every managed video and `media.set(false)` must resume
none of them; a run without `IntersectionObserver` must leave both the
selected scene and the hero paused (unknown visibility is not visibility);
a modified `Alt`/`Ctrl`/`Cmd`/`Shift` chord must not be prevented or switch
scenes; and the rejected-promise case must retain `controls`, must not
remove `poster`, and must not roll back the switcher.

- [ ] **Step 2: Run the harness to verify RED**

Run:

```bash
node tests/js/scene_switcher_harness.mjs
```

Expected: FAIL because the controller never calls `play()`, never resets
`currentTime`, does not consult `matchMedia`, and does not promote `data-src`.

- [ ] **Step 3: Add failing Python markup and source-defer contracts**

In `tests/test_docs_landing_design.py`, replace the old assertion that forbids
all `.play()` calls with contracts that require:

```python
assert 'data-autoplay-video' in hero_video
assert 'muted' in hero_video
assert 'loop' in hero_video
assert 'playsinline' in hero_video
assert 'controls' in hero_video

for video in deferred_scene_videos:
    assert 'data-src="' in video
    assert not re.search(r'(?<!data-)src="', video)
    assert 'preload="none"' in video
```

Keep the current fallback-image, poster-promotion, focus-ring, ARIA, and
offscreen-pause assertions.

- [ ] **Step 4: Run the focused Python tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/test_docs_landing_design.py \
  -k 'video or scene or poster or javascript' -q
```

Expected: FAIL on the new autoplay and deferred-source assertions.

- [ ] **Step 5: Implement playback without introducing a dependency**

Update the landing markup:

```html
<video
  src="assets/demo.mp4"
  poster="assets/scenes/cockpit-poster.png"
  data-autoplay-video
  controls muted loop playsinline preload="metadata"
  aria-label="korvid browsing, filtering, describing, and following logs for a failing workload"
>...</video>
```

Keep the selected direct scene's `src`; move only the Agent and MCP sources to
`data-src`.

In `visual-storytelling.js`, add small helpers:

```javascript
const motionAllowed = () =>
  typeof matchMedia !== "function" ||
  !matchMedia("(prefers-reduced-motion: reduce)").matches;

const promoteVideo = (video) => {
  const source = video.dataset.src;
  if (source) {
    video.setAttribute("src", source);
    video.removeAttribute("data-src");
    video.load?.();
  }
};

const startFromBeginning = (video) => {
  if (!motionAllowed()) return;
  promoteVideo(video);
  video.currentTime = 0;
  const playback = video.play();
  if (playback && typeof playback.catch === "function") {
    playback.catch(() => {});
  }
};
```

Track `switcherVisible` inside `enhance()`. `select()` pauses every unselected
video, promotes the selected poster/source, and starts it only when
`switcherVisible` is true. The observer sets visibility from each entry:
entering starts the selected scene from the beginning; leaving pauses every
scene. Add a separate observer for each `[data-autoplay-video]` hero that uses
the same enter/restart and leave/pause rules.

Do not catch controller initialization errors broadly. Preserve the existing
per-switcher rollback and console error.

- [ ] **Step 6: Run GREEN tests**

Run:

```bash
node tests/js/scene_switcher_harness.mjs
uv run --frozen pytest -p no:tach \
  tests/test_docs_landing_design.py \
  -k 'video or scene or poster or javascript' -q
```

Expected: every Node scenario prints `ok`; selected pytest cases pass.

- [ ] **Step 7: Commit**

```bash
git add docs/index.md docs/assets/javascripts/visual-storytelling.js \
  tests/js/scene_switcher_harness.mjs tests/test_docs_landing_design.py
git commit -m "feat: autoplay visible landing videos"
```

---

### Task 2: Grounded deterministic Agent capture

**Files:**
- Create: `docs/demo/agent_story.py`
- Modify: `docs/demo/demo.py`
- Modify: `docs/demo/agent.tape`
- Modify: `tests/test_docs_visual_assets.py`
- Modify: `docs/superpowers/plans/2026-08-22-visual-storytelling.md`
- Regenerate: `docs/assets/scenes/agent-demo.mp4`
- Regenerate: `docs/assets/scenes/agent-poster.png`

**Interfaces:**
- Consumes: `ReadOps`, `ToolExecutor`, `AgentRuntime`, `LLMProvider`, the existing synthetic pod manifest/events/log stream, and `KorvidApp.agent_runtime`.
- Produces: `build_demo_agent_runtime(reads: ReadOps, aliases: Mapping[str, ResourceMeta]) -> AgentRuntime` whose turn mints real `E1`/`E2` evidence and reports no unsupported citation.

- [ ] **Step 1: Replace old unsupported-citation tests with failing grounded-turn tests**

In `tests/test_docs_visual_assets.py`, remove tests that require
`ScriptedAgentRuntime`, an empty ledger, no outbound payload, and
`uncited=("E1",)`. Add:

```python
def test_demo_agent_turn_uses_real_tools_and_mints_citations() -> None:
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
    complete = next(event for event in events if isinstance(event, TurnComplete))
    answer = "".join(event.text for event in events if isinstance(event, TextDelta))

    assert started == ["diagnose_pod", "get_logs"]
    assert complete.cited == ("E1", "E2")
    assert complete.uncited == ()
    assert runtime.evidence.resolve("E1") is not None
    assert runtime.evidence.resolve("E2") is not None
    assert "[E1]" in answer and "[E2]" in answer
```

Also assert the provider's second and third calls see `role="tool"` messages so
the capture cannot pass by injecting panel events.

- [ ] **Step 2: Run the grounded-turn test to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/test_docs_visual_assets.py::test_demo_agent_turn_uses_real_tools_and_mints_citations -q
```

Expected: FAIL because `build_demo_agent_runtime` does not exist and the current
runtime mints no evidence.

- [ ] **Step 3: Add the deterministic provider**

Create `docs/demo/agent_story.py`:

```python
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from korvid.agent.provider import LLMProvider
from korvid.agent.runtime import AgentRuntime
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.reads import ReadOps
from korvid.tools.executor import ToolExecutor


class DemoAgentProvider(LLMProvider):
    def __init__(self) -> None:
        self._iteration = 0

    @property
    def name(self) -> str:
        return "deterministic-demo"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        stream: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        del messages, tools, stream
        self._iteration += 1
        await asyncio.sleep(0.8)
        if self._iteration == 1:
            yield {
                "type": "tool_call",
                "id": "demo-diagnose",
                "name": "diagnose_pod",
                "arguments": (
                    '{"pod":"payment-worker-6c9f7d-b3xnq",'
                    '"namespace":"shop"}'
                ),
            }
        elif self._iteration == 2:
            yield {
                "type": "tool_call",
                "id": "demo-logs",
                "name": "get_logs",
                "arguments": (
                    '{"pod":"payment-worker-6c9f7d-b3xnq",'
                    '"namespace":"shop","container":"app","tail_lines":12}'
                ),
            }
        else:
            for chunk in (
                "The payment worker is repeatedly restarting after gateway failures. [E1] ",
                "Its recent logs show repeated gateway 503 responses; inspect the owner ",
                "and upstream availability before changing the workload. [E2]",
            ):
                await asyncio.sleep(0.45)
                yield {"type": "text_delta", "text": chunk}
        yield {"type": "done"}


def build_demo_agent_runtime(
    reads: ReadOps,
    aliases: Mapping[str, ResourceMeta],
) -> AgentRuntime:
    return AgentRuntime(
        DemoAgentProvider(),
        ToolExecutor(reads, aliases),
        cluster_context="current",
    )
```

- [ ] **Step 4: Adapt the synthetic fixture to the `ReadOps` boundary**

In `docs/demo/demo.py`, add `DemoReadOps(ReadOps)` implementing every abstract
method:

```python
class DemoReadOps(ReadOps):
    async def list_objects(
        self, meta: ResourceMeta, namespace: str | None
    ) -> list[GenericSummary]:
        rows = list(PODS) if meta.plural == "pods" else list(EXTRA.get(meta.plural, []))
        return [row for row in rows if namespace is None or row.namespace == namespace]

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        return await get_manifest(meta.plural, namespace, name)

    async def list_helm_releases(
        self, namespace: str | None
    ) -> list[HelmReleaseSummary]:
        releases = [
            HelmReleaseSummary(
                name="shop",
                namespace="shop",
                kind="HelmRelease",
                created="",
                revision=4,
                status="deployed",
                chart="shop-0.8.0",
                app_version="2.4.1",
            )
        ]
        return [
            release
            for release in releases
            if namespace is None or release.namespace == namespace
        ]

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        del kind
        return await DemoEvents().fetch(namespace, name, uid=uid)

    def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[LogLine]:
        del tail_lines
        return stream_logs(
            namespace,
            pod,
            container,
            previous=previous,
            follow=follow,
        )
```

Expose a zero-argument `build_demo_agent_runtime()` wrapper from `demo.py` for
tests:

```python
from agent_story import build_demo_agent_runtime as _build_grounded_agent_runtime


def build_demo_agent_runtime() -> AgentRuntime:
    return _build_grounded_agent_runtime(DemoReadOps(), ALIASES)
```

Inject its result into `DemoKorvidApp` for the `agent` scene. Remove
`ScriptedAgentRuntime`.

- [ ] **Step 5: Run the grounded runtime tests to verify GREEN**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/test_docs_visual_assets.py \
  -k 'agent and (runtime or evidence or citation or tape)' -q
```

Expected: PASS; the event order is diagnosis, logs, cited answer, completion.

- [ ] **Step 6: Lengthen the tape and regenerate the Agent media**

Keep the real input path, then hold the grounded answer:

```text
Type "Why is the payment worker failing?"
Enter
Sleep 12s
```

Synchronize the executable tape snippet in
`docs/superpowers/plans/2026-08-22-visual-storytelling.md` because the existing
contract compares it byte-for-byte.

Run:

```bash
vhs docs/demo/agent.tape
ffmpeg -y -ss 00:00:11 -i docs/assets/scenes/agent-demo.mp4 \
  -frames:v 1 docs/assets/scenes/agent-poster.png
ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1 docs/assets/scenes/agent-demo.mp4
```

Expected: MP4 duration between 12 and 15 seconds; the poster shows both tool
rows and a readable answer with `[E1]` and `[E2]`, with no yellow unsupported
citation warning.

- [ ] **Step 7: Run focused Agent asset contracts**

Run:

```bash
uv run --frozen pytest -p no:tach tests/test_docs_visual_assets.py \
  -k 'agent or storytelling_pngs' -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add docs/demo/agent_story.py docs/demo/demo.py docs/demo/agent.tape \
  docs/assets/scenes/agent-demo.mp4 docs/assets/scenes/agent-poster.png \
  docs/superpowers/plans/2026-08-22-visual-storytelling.md \
  tests/test_docs_visual_assets.py
git commit -m "docs: record grounded agent story"
```

---

### Task 3: Reproducible full MCP follow capture

**Files:**
- Create: `docs/demo/mcp_client.py`
- Create: `docs/demo/mcp-follow.tape`
- Create: `docs/demo/record-mcp-follow.sh`
- Modify: `docs/demo/demo.py`
- Modify: `tests/test_docs_visual_assets.py`
- Regenerate: `docs/assets/scenes/mcp-follow-demo.mp4`
- Regenerate: `docs/assets/scenes/mcp-poster.png`

**Interfaces:**
- Consumes: `KorvidMCPServer`, `MCPController`, `ToolExecutor`, `mcp_tool_schemas`, `AppUIBridge`, the late-bound `_UIBridgeProxy`/`_MCPAppHooks` wiring pattern, and MCP SDK `ClientSession`.
- Produces: a `--scene mcp` harness serving `http://127.0.0.1:7878/mcp`, plus a clean client that calls `list_resources`, `diagnose_pod`, `get_logs`, and `helm_list_releases` in order.

- [ ] **Step 1: Add failing MCP client and tape contracts**

In `tests/test_docs_visual_assets.py`, require:

```python
client = (DEMO_DIR / "mcp_client.py").read_text(encoding="utf-8")
positions = [
    client.index(f'call_tool("{name}"')
    for name in (
        "list_resources",
        "diagnose_pod",
        "get_logs",
        "helm_list_releases",
    )
]
assert positions == sorted(positions)
assert "from mcp import ClientSession" in client
assert "streamable_http_client" in client

tape = (DEMO_DIR / "mcp-follow.tape").read_text(encoding="utf-8")
assert "tmux" in tape
assert "docs/demo/demo.py --scene mcp" in tape
assert "docs/demo/mcp_client.py" in tape
assert "Sleep 65s" in tape  # hidden allowance, outlasting the 60s bound
```

Add duration and geometry assertions for the resulting MP4:

```python
duration = _media_duration(SCENES / "mcp-follow-demo.mp4")
assert 12 <= duration <= 15
assert _mp4_geometry(SCENES / "mcp-follow-demo.mp4")[0] == (1280, 710)
```

- [ ] **Step 2: Run the MCP contracts to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach tests/test_docs_visual_assets.py \
  -k 'mcp and (client or tape or duration or geometry)' -q
```

Expected: FAIL because the client and tape do not exist and the current clip is
only four seconds.

- [ ] **Step 3: Add a clean real MCP client**

Create `docs/demo/mcp_client.py` around the SDK pattern already proven in
`tests/mcp/test_server.py`:

```python
from __future__ import annotations

import asyncio
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL = "http://127.0.0.1:7878/mcp"


def _text(result: Any) -> str:
    return "\n".join(
        str(getattr(item, "text", ""))
        for item in result.content
        if getattr(item, "text", "")
    )


async def main() -> None:
    print("External MCP client")
    print("> Find the unhealthy shop pod, inspect its logs, then show Helm releases.")
    async with (
        streamable_http_client(URL) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        calls = (
            ("list_resources", {"kind": "pods", "namespace": "shop"}, 1.4),
            (
                "diagnose_pod",
                {"pod": "payment-worker-6c9f7d-b3xnq", "namespace": "shop"},
                1.6,
            ),
            (
                "get_logs",
                {
                    "pod": "payment-worker-6c9f7d-b3xnq",
                    "namespace": "shop",
                    "container": "app",
                    "tail_lines": 12,
                },
                2.4,
            ),
            ("helm_list_releases", {"namespace": "shop"}, 2.0),
        )
        for name, arguments, hold in calls:
            print(f"\n• {name}")
            result = await session.call_tool(name, arguments)
            lines = _text(result).splitlines()
            for line in lines[-5:]:
                print(f"  {line}")
            await asyncio.sleep(hold)
        print("\nRead-only investigation complete. Korvid followed every view.")
        await asyncio.sleep(2.0)


if __name__ == "__main__":
    asyncio.run(main())
```

Do not print the endpoint file, process ID, working directory, model, tokens, or
full unbounded tool results.

Wrap that entry point so the recording learns whether it succeeded. VHS records
a fixed window on its own clock and never observes this pane, so an unwrapped
exception prints a traceback into the captured frames, closes the pane — which
reflows the TUI to full width inside the last frames — and still yields an
apparently finished asset. Publish `.korvid-mcp-demo-client-ok` only after all
four calls and the closing card have been printed and before the closing hold;
catch any exception, publish `.korvid-mcp-demo-client-failed` instead, print no
traceback and no error or result text, and hold the pane open for a bounded
period longer than the tape's 15-second visible window. Both files are
repository-local recording side effects: gitignored, and removed on both sides
of a run like the two handshake files.

- [ ] **Step 4: Compose a real local MCP server with the demo app**

Extend `_parse_scene()` with `"mcp"`. For that scene, build:

```python
reads = DemoReadOps()
ui_proxy = _UIBridgeProxy()
mcp_hooks = _MCPAppHooks()
controller = MCPController(
    lambda: KorvidMCPServer(
        ToolExecutor(reads, ALIASES, ui=ui_proxy),
        mcp_tool_schemas(),
        port=7878,
        ui=ui_proxy,
        follow_enabled=mcp_hooks.follow_enabled,
        note_activity=mcp_hooks.note_activity,
    )
)
```

Set `KorvidConfig(namespace="shop", mcp_enabled=True, mcp_follow=True)`, inject
`mcp=controller`, then bind:

```python
ui_proxy.target = AppUIBridge(app)
mcp_hooks.app = app
```

Run the MCP scene through an async wrapper:

```python
async def run_mcp_demo(app: DemoKorvidApp, controller: MCPControllerBase) -> None:
    await controller.start()
    try:
        await app.run_async()
    finally:
        await controller.stop()
```

The base, Agent, and relationship scenes keep the existing synchronous
`app.run()` path.

- [ ] **Step 5: Add the deterministic tmux/VHS recording**

Create `docs/demo/mcp-follow.tape`:

```text
# Landing clip: an external MCP host drives korvid, and korvid follows.
#
# Two panes, one machine. The left pane runs the `mcp` scene of the demo
# harness — korvid's real TUI over the synthetic cluster, serving the real
# KorvidMCPServer over Streamable HTTP on 127.0.0.1:7878. The right pane
# runs docs/demo/mcp_client.py, a real MCP SDK ClientSession calling four
# read-only tools. Nothing is staged: every view the left pane opens is
# korvid's follow bridge mirroring the answer the right pane just got.
#
#   docs/demo/record-mcp-follow.sh
#
# Never `vhs` on this file directly. VHS renders the timeline below and exits
# 0 whatever the shell it typed into did, so nothing this tape can do decides
# whether the result is published. It renders to a candidate instead, and the
# wrapper above promotes that candidate onto the published clip only after
# the client pane certified its own run.
#
# Reproducible from this repository alone: loopback only, no credential,
# no MCP endpoint file, no cluster. The tmux status line is turned off
# before anything is captured, so no hostname, user or date reaches a
# frame; the shell that composes the panes is never captured either.

Output docs/assets/scenes/.mcp-follow-demo.candidate.mp4

Set Shell bash
Set FontSize 14
Set Width 1280
Set Height 710
Set Padding 0

# --- composition, never captured -------------------------------------
Hide

Type "rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready .korvid-mcp-demo-client-ok .korvid-mcp-demo-client-failed; tmux kill-session -t korvid-mcp-demo 2>/dev/null; true"
Enter
Sleep 1s

# 139x42 is this terminal's own grid at the geometry set above, so the
# attach below resizes nothing. `-f /dev/null` starts the server without
# anyone's tmux.conf, and the status line — the one surface that would
# print a hostname, a user and today's date into a frame — is turned off
# before the split, so both panes are laid out over the full 42 rows.
#
# Both panes are launched with `--extra mcp`. The MCP stack is an optional
# extra, not a dependency group, so a clean checkout running `uv run --frozen`
# alone gets an environment where neither the scene's lazy
# `korvid.mcp.server` import nor the client's MCP SDK import resolves. The
# flag changes nothing that reaches a frame: the same code runs and prints
# the same output.
Type "tmux -f /dev/null new-session -d -s korvid-mcp-demo -x 139 -y 42 'uv run --frozen --extra mcp python docs/demo/demo.py --scene mcp'"
Enter
Sleep 1s

Type "tmux set -t korvid-mcp-demo status off"
Enter
Sleep 500ms

# The client waits on a repo-local gate file rather than a fixed sleep:
# korvid's first start is slow and variable, and a client that raced it
# would open the story on a connection error. The gate below is dropped only
# after the scene publishes .korvid-mcp-demo-ready.
Type "tmux split-window -h -t korvid-mcp-demo -p 45 'while [ ! -f .korvid-mcp-demo-go ]; do sleep 0.1; done; uv run --frozen --extra mcp python docs/demo/mcp_client.py'"
Enter
Sleep 1s

Type "tmux select-pane -t korvid-mcp-demo:0.0"
Enter

# korvid's own startup: mount, first watch, and the MCP server's bind. The
# hidden allowance runs the scene's own readiness wait rather than an idle
# sleep; the reason the allowance is hidden at all is written down once, in
# docs/demo/visual-storytelling.md. `--scene mcp` publishes
# .korvid-mcp-demo-ready from its Textual mount, and only after the MCP
# server reported itself bound. The loop is bounded at a wall-clock 60s
# deadline via bash's own `SECONDS` builtin — reset with `SECONDS=0`, then
# looped while `$SECONDS` stays under 60 — so a server that never binds fails
# the recording instead of hanging it. Counting `sleep 0.1` iterations up to a
# fixed total instead assumes each one costs exactly 0.1s, but fork+exec of
# `sleep` itself is not free: measured wall time for that approach ran past
# this tape's 65s hidden allowance. `SECONDS` is wall-clock and immune to
# that overhead, so the deadline stays ~60s regardless of it.
#
# The Sleep below is VHS's own clock, and VHS never learns that the loop it
# typed has returned. So the allowance must outlast the loop's whole 60s
# wall-clock deadline rather than a typical start: at 65s the composition
# stays hidden even in the worst case, keeping >= 5s of margin. Readiness
# arriving early costs nothing — the client pane is still held by the
# separate .korvid-mcp-demo-go gate, dropped only below, so no part of the
# story can run inside the hidden block.
Type "SECONDS=0; while [ ! -f .korvid-mcp-demo-ready ] && [ $SECONDS -lt 60 ]; do sleep 0.1; done"
Enter
Sleep 65s

# Release the client from a background subshell, so the gate is dropped from
# outside tmux and no keystroke of the trigger can reach the attached TUI.
# Fail-closed, in an explicit branch: the release *and* the attach happen
# only where readiness is known to exist, and a missing signal prints the
# reason into the hidden block, tears the session down and exits without
# attaching. `[ -f ready ] && ( ... ) & clear; tmux attach-session` does not
# express that — bash's `&` terminates the whole and_or list, so the attach
# is a separate, unconditional command.
Type "if [ -f .korvid-mcp-demo-ready ]; then ( sleep 0.7; touch .korvid-mcp-demo-go ) & clear; tmux attach-session -t korvid-mcp-demo; else echo 'mcp-follow.tape: .korvid-mcp-demo-ready never appeared; failing without attach'; tmux kill-session -t korvid-mcp-demo 2>/dev/null; rm -f .korvid-mcp-demo-go .korvid-mcp-demo-ready .korvid-mcp-demo-client-ok .korvid-mcp-demo-client-failed; exit 1; fi"
Enter
Sleep 400ms

# --- the story, captured ---------------------------------------------
Show

# list_resources → pods table; diagnose_pod → describe; get_logs → log
# pane, held while the lines stay readable; helm_list_releases → Helm.
Sleep 15s

Hide

# --- teardown, never captured ----------------------------------------
Ctrl+B
Type "d"
Sleep 500ms

# VHS records the 15s above on its own clock and stops; it never observes
# the client pane, so a client that raised on its second call still yields
# an apparently finished asset. The client publishes its verdict into two
# repo-local files instead — .korvid-mcp-demo-client-ok only after all four
# calls and the closing card were printed, .korvid-mcp-demo-client-failed
# on any exception (no traceback, no error text, and the pane held open
# past this capture so it cannot close and reflow the TUI mid-frame).
#
# Both files are deliberately left in place here. Reading them from this
# tape would decide nothing: VHS has already rendered its Output and will
# exit 0 regardless, so the verdict belongs to docs/demo/record-mcp-follow.sh,
# which grades the markers after VHS returns, promotes the candidate only on
# a completed run, and removes every scratch file either way. The session is
# still torn down from here, after Hide and after the detach: before Hide it
# would type a shell command into the captured frames, and before the detach
# it would type it into the attached TUI.
Type "tmux kill-session -t korvid-mcp-demo 2>/dev/null; true"
Enter
Sleep 1s
```

The left pane remains the real TUI and the right pane contains only the clean
SDK client. The gate file prevents the client from completing during the
hidden cold-start allowance, and it is dropped only once the scene publishes
`.korvid-mcp-demo-ready` — from its Textual mount, over a server it has
already bound — so the visible timeline can never open on a connection error.
The tape renders to `docs/assets/scenes/.mcp-follow-demo.candidate.mp4` and
leaves the client's status markers in place: VHS exits 0 whatever the
recording did, so publication is decided outside it, by the wrapper in Step 6.
Do not fall back to the checked-in third-party-client GIF.

- [ ] **Step 6: Record through the promotion wrapper and inspect the media**

Create `docs/demo/record-mcp-follow.sh` (executable, `set -euo pipefail`): it
runs VHS on the tape, checks that the tape declares exactly one `Output` and
that it is the candidate, and promotes the candidate onto
`docs/assets/scenes/mcp-follow-demo.mp4` with a single `mv` only when VHS
returned 0, `.korvid-mcp-demo-client-failed` is absent,
`.korvid-mcp-demo-client-ok` is present and the candidate exists. Every other
path prints one line on stderr, removes the candidate and all four handshake
files, kills the `korvid-mcp-demo` tmux session by name from an `EXIT` trap,
exits non-zero and leaves any previously approved clip byte-identical. Its
paths default to those repository-relative values; the
`KORVID_MCP_VHS_BIN`/`KORVID_MCP_TAPE`/`KORVID_MCP_CANDIDATE`/
`KORVID_MCP_FINAL`/`KORVID_MCP_CLIENT_OK`/`KORVID_MCP_CLIENT_FAILED`/
`KORVID_MCP_READY`/`KORVID_MCP_GO` overrides exist so the contracts can drive
the boundary against a fake VHS in a temporary directory.

Run:

```bash
docs/demo/record-mcp-follow.sh
ffmpeg -y -ss 00:00:08 -i docs/assets/scenes/mcp-follow-demo.mp4 \
  -frames:v 1 docs/assets/scenes/mcp-poster.png
ffprobe -v error \
  -show_entries stream=width,height,sample_aspect_ratio:format=duration \
  -of default=noprint_wrappers=1 \
  docs/assets/scenes/mcp-follow-demo.mp4
```

Expected: duration 12–15 seconds, 1280×710, sample aspect ratio `1:1`. Visual
inspection must show the log pane for at least two seconds before Helm releases
and the final summary.

- [ ] **Step 7: Run focused MCP asset contracts**

Run:

```bash
uv run --frozen pytest -p no:tach tests/test_docs_visual_assets.py \
  -k 'mcp or storytelling_pngs' -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add docs/demo/mcp_client.py docs/demo/mcp-follow.tape docs/demo/demo.py \
  docs/demo/record-mcp-follow.sh \
  docs/assets/scenes/mcp-follow-demo.mp4 docs/assets/scenes/mcp-poster.png \
  tests/test_docs_visual_assets.py
git commit -m "docs: record complete mcp follow story"
```

---

### Task 4: Truthful copy, provenance, and local playback review

**Files:**
- Modify: `docs/index.md`
- Modify: `docs/agent.md`
- Modify: `docs/mcp.md`
- Modify: `docs/demo/visual-storytelling.md`
- Modify: `tests/test_docs_landing_design.py`
- Modify: `tests/test_docs_visual_assets.py`

**Interfaces:**
- Consumes: the autoplay controller and final Agent/MCP assets from Tasks 1–3.
- Produces: visitor copy and provenance that distinguish deterministic synthetic evidence, real MCP transport/follow behavior, and live-provider non-claims.

- [ ] **Step 1: Write failing disclosure and provenance tests**

Require the landing Agent scene to contain:

```python
for phrase in (
    "deterministic synthetic-cluster walkthrough",
    "real read tools",
    "not a live model",
):
    assert phrase in agent_scene.lower()
assert "unsupported citation" not in agent_scene.lower()
```

Require the MCP scene/provenance to contain:

```python
assert "real read-only mcp requests" in mcp_copy.lower()
assert "streamable http" in provenance.lower()
assert "logs remain visible" in provenance.lower()
assert "third-party" not in published_mcp_description.lower()
```

Keep the production limitations: MCP writes are opt-in proposals; Prometheus
and Loki activity notes are not follow navigation; Agent/MCP snapshots may
differ from the watch-backed table.

- [ ] **Step 2: Run disclosure tests to verify RED**

Run:

```bash
uv run --frozen pytest -p no:tach \
  tests/test_docs_landing_design.py tests/test_docs_visual_assets.py \
  -k 'agent or mcp or provenance or disclosure' -q
```

Expected: FAIL because the published copy still describes a panel-only scripted
Agent clip and the old sanitized third-party MCP derivation.

- [ ] **Step 3: Update visitor copy without inflating the page**

In `docs/index.md`, keep the existing three compact fact rows. Change the Agent
facts to:

```html
<div><strong>Input</strong> Prompt submitted through the real AgentPanel</div>
<div><strong>Evidence</strong> Real read tools over a deterministic synthetic cluster</div>
<div><strong>Result</strong> Grounded answer rendered by the real Agent loop</div>
<p>A deterministic synthetic-cluster walkthrough, not a live-model quality claim.</p>
```

Change the MCP facts to state that the clip uses real read-only MCP requests,
real follow navigation, and a clean local SDK client. Do not add another prose
section or duplicate the full sequence below the video.

Update the video `aria-label` and fallback `alt` text to describe the new
visible sequence.

- [ ] **Step 4: Replace the old capture disclosures**

In `docs/demo/visual-storytelling.md`, replace the Agent section's empty-ledger
and yellow-warning explanation with:

- exact provider/tool/evidence path;
- exact synthetic fixture and tool sequence;
- no live-provider or model-quality claim;
- exact VHS and poster commands.

Replace the MCP section's third-party GIF derivation with:

- tmux pane proportions;
- official MCP SDK and Streamable HTTP endpoint;
- exact tool sequence and hold durations;
- read-only/follow boundary;
- no external client metadata;
- exact VHS and poster commands.

Update the compact capture notes in `docs/agent.md` and `docs/mcp.md`; do not
expand either guide into a recording manual.

- [ ] **Step 5: Run focused documentation tests**

Run:

```bash
node tests/js/scene_switcher_harness.mjs
uv run --frozen pytest -p no:tach \
  tests/test_docs_landing_design.py tests/test_docs_visual_assets.py \
  tests/test_docs_links.py -q
```

Expected: PASS.

- [ ] **Step 6: Verify in the existing local MkDocs server**

The server at `http://127.0.0.1:8980/korvid/` watches the worktree. In a browser:

1. reload the home page at desktop width;
2. verify the hero starts without a click;
3. scroll to the scene switcher and verify only `You drive` starts;
4. select Agent and MCP and verify each restarts from frame zero;
5. scroll the switcher fully offscreen and verify playback pauses;
6. emulate `prefers-reduced-motion: reduce` and verify no programmatic start;
7. repeat at 390-pixel width and verify controls and captions do not overflow;
8. confirm the Agent citations and MCP logs/final summary are readable at the
   rendered widths.

Expected: the page behaves like a GIF-led product story while remaining
pausable, readable, and motion-aware.

- [ ] **Step 7: Confirm repository boundaries**

Run:

```bash
git diff --exit-code -- uv.lock
git status --short
```

Expected: no `uv.lock` diff; only the intended Task 4 documentation/test files
are modified.

- [ ] **Step 8: Commit**

```bash
git add docs/index.md docs/agent.md docs/mcp.md \
  docs/demo/visual-storytelling.md \
  tests/test_docs_landing_design.py tests/test_docs_visual_assets.py
git commit -m "docs: explain the complete landing demos"
```

## Post-merge review report

Five findings addressed after `79583ef` merged `origin/main` into
`docs/visual-storytelling`: two raised by Copilot on the merge itself, one —
finding 3 — found while verifying the second one's fix, and two more raised
against the promotion boundary that fix introduced. All were fixed test-first
on top of that merge; no media was regenerated, because no successful visible
frame changed and the published clip stayed byte-identical.

### 1. The observability diagram claimed a citation both consumers do not get

*Inline comment `3859923950`, `docs/observability.md`.*

The shared node the metrics and the logs path both flow into read
`Bound + mask + cite`. Citations are minted in one place only:
`AgentRuntime` records a read into its `EvidenceLedger` and hands the model an
`[E1]`-style reference (`src/korvid/agent/runtime.py`). An MCP `tools/call`
returns the executor's content to the host directly and keeps no ledger
(`src/korvid/mcp/server.py`), and this page documents both consumers.

The node now reads `Bound + mask + provenance` — the property both actually
receive — and the distinction is stated once in the prose under the diagram:
the agent can cite the answer `[E1]`, while an MCP host receives the same
bounded, masked text directly and has only the provenance carried inside it.
The pre-existing agent-scoped citation sentence under "Bounds and masking" is
unchanged. `tests/test_docs_readability.py::
test_observability_connector_diagram_claims_provenance_not_citations` parses
the mermaid node label, rejects any `cite`/`citation` claim on it, requires
`provenance` alongside the surviving `bound`/`mask` claims, and pins the
`endpoint`/`window`/`query` triple plus both halves of the prose distinction.

### 2. A failed client pane could still produce a finished-looking asset

*Inline comment `3859923996`, `docs/demo/mcp_client.py`.*

VHS records a fixed 15-second window on its own clock and never observes the
client pane. An exception there printed a traceback into the captured frames
and closed the pane — reflowing the TUI to full width inside the last frames —
while the tape went on to publish an apparently complete recording.

`docs/demo/mcp_client.py` now carries the verdict out of the pane in two
repository-local status files. `main` publishes `.korvid-mcp-demo-client-ok`
only after all four calls and the closing card have been printed, before the
existing closing hold, so the marker certifies a story that finished rather
than a process that survived. The new `run` entry point catches any
`Exception`, publishes `.korvid-mcp-demo-client-failed`, prints one fixed line
— no traceback, no error text, no tool result — holds the pane for a bounded
`FAILURE_HOLD = 30.0` s (longer than the visible window, so the composition
survives to the teardown) and exits `SystemExit(1)`, the one exception the
interpreter reports without a traceback. `BaseException` is deliberately not
caught. Normal visible output and timing are untouched.

`docs/demo/mcp-follow.tape` removes both status files before it launches the
panes and now leaves them in place afterwards: the acceptance rule those
markers feed moved out of the tape entirely in finding 3 below, because a
tape cannot enforce it.

Contracts added in `tests/test_docs_visual_assets.py`:

- `test_mcp_client_publishes_success_only_after_the_whole_story_is_printed` —
  patches the client's sleep and records `(seconds, ok, failed, output)` at
  every beat, so the success file is proven absent on all four answer beats
  and present on the closing hold, after the closing card was printed.
- `test_mcp_client_entry_point_holds_a_failed_pane_and_publishes_no_success` —
  fails the third call with a sensitive sentinel and proves `SystemExit`
  rather than a propagated exception, the failure file written, no success
  file, no sentinel/traceback/`RuntimeError` in either stream, no success
  card, and a final hold equal to `FAILURE_HOLD` and longer than the visible
  window parsed out of the shipped tape.
- `test_mcp_client_status_files_are_repo_local_and_never_committable` —
  relative paths, gitignored, removed on both sides of a run.
- `test_mcp_follow_tape_accepts_only_a_completed_client_run` — hands the
  shipped teardown command to **real bash** with a stubbed `tmux` across all
  four status combinations, asserting the exit status, the rejection message,
  the teardown and the cleanup of all four files in every branch.
- `test_mcp_follow_tape_checks_the_client_status_after_it_stops_recording` —
  the check runs after the last `Hide` and after the detach, nothing is
  captured afterwards, and both status files are cleared before the panes
  start.
- `test_mcp_capture_provenance_publishes_the_client_status_handshake` — keeps
  `docs/demo/visual-storytelling.md` in step with the shipped hold and rule.

`docs/demo/visual-storytelling.md` and this plan's Step 3 prose and Step 5 tape
snippet are resynced; the existing snippet-equality contract covers the tape
verbatim.

### 3. The tape's `exit 1` rejected nothing VHS had already published

*Found while verifying finding 2 on top of `ad6c142`.*

Finding 2 moved the client's verdict into two repository-local markers and had
the tape grade them after `Hide`. The grading branch was real, but the
consequence was not: VHS renders the timeline its tape describes and exits 0
whatever the shell it typed into did. `Output` named
`docs/assets/scenes/mcp-follow-demo.mp4` directly, so by the time the check
ran, the reviewed clip had already been overwritten with the truncated take,
and the `exit 1` only made the failure look handled. `vhs
docs/demo/mcp-follow.tape` — the published recipe — had no way to leave the
approved asset alone.

The boundary is now external and outside VHS's reach:

- `docs/demo/mcp-follow.tape` renders to
  `docs/assets/scenes/.mcp-follow-demo.candidate.mp4` (git-ignored), no longer
  names the published clip anywhere, and no longer claims its own exit status
  decides publication. Its teardown detaches and kills the session, and leaves
  both status markers for the wrapper. The readiness abort still exits, since
  that happens before anything is captured, but it is no longer the authority.
- `docs/demo/record-mcp-follow.sh` (new, executable, `set -euo pipefail`) is
  the recipe. It rejects a tape that does not declare exactly one `Output`
  equal to the candidate — a second `Output` would put the clip back under
  VHS's pen — clears stale scratch, runs VHS, and promotes the candidate onto
  the published clip with a single `mv` only when VHS returned 0, the failure
  marker is absent, the success marker is present and the candidate exists.
  Every other path prints one line on stderr, removes the candidate and all
  four handshake files from an `EXIT` trap, kills only the `korvid-mcp-demo`
  session by name, exits non-zero and leaves a previously approved clip
  byte-identical. Its defaults are the repository-relative paths the
  provenance page publishes; the `KORVID_MCP_*` overrides exist so the
  contracts can drive the boundary against a fake VHS in a temporary
  directory.

Contracts added in `tests/test_docs_visual_assets.py`, all driving the shipped
script through **real bash** with a fake VHS executable:

- `test_mcp_recorder_promotes_only_a_completed_run` — the candidate becomes
  the published clip, the candidate is moved rather than copied, every scratch
  marker is gone, and only the named session is killed.
- `test_mcp_recorder_publishes_nothing_on_a_failed_recording` — four cases
  (failure marker, missing success marker, VHS exiting non-zero, no candidate
  despite success): non-zero status, the approved clip byte-identical, the
  candidate and scratch removed, and the reason on stderr.
- `test_mcp_recorder_creates_no_clip_where_none_was_approved` — a rejected
  first run leaves no published file at all.
- `test_mcp_recorder_refuses_a_tape_that_would_write_the_published_clip` — the
  tape is read before VHS is invoked, so a hostile second `Output` never runs.
- `test_mcp_recorder_owns_the_canonical_path_and_the_tape_never_writes_it`,
  `test_mcp_recorder_is_a_strict_fail_closed_shell_script`,
  `test_mcp_recorder_defaults_are_repository_relative_and_quoted` — one
  `Output` and it is the candidate, the canonical name only in the wrapper's
  promotion target, strict mode, an `EXIT` trap, literal removals with no
  glob, and quoted expansions.
- `test_mcp_follow_tape_leaves_the_verdict_to_the_wrapper` and
  `test_mcp_candidate_recording_is_scratch_and_never_committable` — the tape
  claims no verdict and touches no marker after the capture; the candidate is
  git-ignored and untracked.
- `test_mcp_recording_recipe_is_the_wrapper_and_never_a_bare_vhs_run` — no
  page, plan or tape header may offer a bare `vhs` run on the tape as the
  recipe, and each must publish the wrapper, the candidate and the promotion
  rule.

`docs/demo/visual-storytelling.md`, the 2026-08-22 plan's regeneration
command, this plan's Step 5 snippet, Step 6, Step 8 and `.gitignore` are
resynced. No media was regenerated:
`docs/assets/scenes/mcp-follow-demo.mp4` is byte-identical to the clip
reviewed at `ad6c142`.

### 4. The `Output` preflight read one spelling of a directive VHS reads five ways

*Review of `561c752`, `docs/demo/record-mcp-follow.sh`.*

The promotion boundary refused a hostile tape with two line-anchored greps:
`grep -c '^Output '` for the count and `grep -qxF "Output <candidate>"` for
the path. VHS does not read a tape that way. Its lexer skips the whitespace in
front of a directive and accepts a tab between the directive and its argument,
so `  Output <clip>`, `<TAB>Output <clip>` and `Output<TAB><clip>` are all
directives it obeys — and none of them starts with `Output ` at column zero. A
tape whose first line was the candidate therefore satisfied both greps while a
second, indented `Output docs/assets/scenes/mcp-follow-demo.mp4` below it put
the published clip straight back under VHS's pen: exactly the bypass the
preflight exists to stop, reachable by pressing space.

The tape is now parsed the way VHS splits it. `awk` ignores leading blanks and
splits on runs of them, so `$1 == "Output"` finds every directive whatever its
indentation or separator; the wrapper requires exactly one such directive,
exactly one argument on it (the candidate is a repository-relative path with
no whitespace, so a second field is a directive nobody reviewed, not a longer
path), and that argument to equal the candidate. Each outcome keeps its own
reason on stderr. The real tape still passes, and the check is no stricter
than VHS: a tape VHS renders is a tape the wrapper runs.

Thirteen bypasses now run the shipped script under real bash against a fake
VHS that would overwrite the published clip the moment it were invoked —
a second `Output` plain, space-indented, tab-indented, tab-separated and
both at once; the candidate declared twice; a path followed by a second
field or a stray word; an `Output` with no path; the published clip alone,
indented and tab-separated; and no `Output` at all. Each asserts the same
three things: non-zero exit, an invocation log that was never created, and
the pre-existing approved clip byte-identical. Six accepted whitespace forms
of a single candidate `Output` assert the other half — indented with spaces
or a tab, separated by a tab or several spaces, with trailing whitespace —
each promoted to the published clip.

### 5. `docs/demo/mcp_client.py` still credited the tape with the verdict

*Review of `561c752`, `docs/demo/mcp_client.py`.*

Finding 3 moved publication out of the tape but left the client's prose
behind. Its module docstring still said the markers were what
`docs/demo/mcp-follow.tape` "reads once it has stopped recording" and that
"the tape publishes the capture only when success is present and failure is
absent"; `FAILED_FILE` said "the tape rejects on this file"; `_publish` and
`run` described a handshake the tape read. Every one of those sends a reader
to a file with no authority over the asset — the same misunderstanding that
made a tape-side `exit 1` look like a rejection.

The client now states the boundary it actually publishes into: the tape only
records and leaves both markers in place, and
`docs/demo/record-mcp-follow.sh` grades them after VHS returns, promoting the
candidate onto the published clip only when failure is absent and success is
present and rejecting it otherwise.
`test_mcp_client_status_files_are_repo_local_and_never_committable` — the
joint that already kept the marker names synchronized across the client, the
tape, the wrapper and `.gitignore` — now reads the client source too: it
requires the wrapper to be named there and rejects any sentence that makes
the tape the reader, publisher, rejecter or grader of these files. Against
the client as it stood at `561c752` it fails six times over.

Verified with `tests/test_docs_visual_assets.py` (117 tests) and
`tests/test_docs_readability.py`, `ruff check`, `ruff format --check`,
`mkdocs build --strict`, and `bash -n` on the wrapper. `uv.lock` is untouched
and no media was regenerated: `docs/assets/scenes/mcp-follow-demo.mp4` is
byte-identical.
