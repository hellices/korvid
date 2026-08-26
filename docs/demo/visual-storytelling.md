# Official-site visual evidence

Every official-site scene uses synthetic resources from a disposable or
in-memory demo. No capture may contain a real cluster, credential, customer,
or production identifier.

## Running the tapes reproducibly

Each tape launches the harness with `uv run --frozen` — `uv run` on its own
re-resolves and may rewrite `uv.lock`, which must never happen as a side
effect of recording a screenshot.

Each tape then sleeps inside a `Hide` block before `Show`. That sleep is the
cold-start allowance: `uv run` can spend tens of seconds resolving and
installing the project before the TUI paints its first frame, and every
keystroke the tape sends in the meantime lands in the shell instead of the
app. `demo.tape`, `agent.tape` and `relationships.tape` therefore all allow
the same **20 seconds**, and none of them repeats the reason — they point
here. Because the sleep is inside `Hide`, it costs the recording nothing: the
output timeline still starts at `Show`, so the `ffmpeg -ss` offsets below are
unaffected by the allowance. Warm the environment first
(`uv run --frozen python -c "import korvid"`) if a tape still opens on a shell
prompt.

## Base cockpit and feature frames

`docs/demo/demo.tape` records the in-memory `shop` fixture to
`docs/assets/demo.gif`; `docs/demo/README.md` converts the same recording to
`docs/assets/demo.mp4`.

```sh
vhs docs/demo/demo.tape
ffmpeg -y -i docs/assets/demo.gif -an -movflags +faststart \
  -pix_fmt yuv420p -vf 'scale=trunc(iw/2)*2:trunc(ih/2)*2' \
  docs/assets/demo.mp4
ffmpeg -y -ss 00:00:05 -i docs/assets/demo.mp4 -frames:v 1 \
  docs/assets/scenes/cockpit-poster.png
ffmpeg -y -ss 00:00:16 -i docs/assets/demo.mp4 -frames:v 1 \
  docs/assets/scenes/diagnosis.png
ffmpeg -y -ss 00:00:23 -i docs/assets/demo.mp4 -frames:v 1 \
  docs/assets/scenes/merged-logs.png
```

`cockpit-poster.png` is cut at **00:00:05**, inside the tape's 1.5s pause
after the last `Down` and before the `0` all-namespaces toggle: a settled
frame with the full `shop` pod table, the crash-looping `payment-worker` row
selected, its `BackOff` ops hint, and the `ctx:/ns:` status row. Do not cut
it around 00:00:10 — the tape is mid-filter there, showing a single row and
a live `/` prompt. The demo has no metrics source, so every CPU/MEM column
renders `–`; nothing on the site may present this frame as evidence of
utilization.

## Embedded agent

`docs/demo/agent.tape` records the real AgentPanel driving korvid's real agent
loop. The documentation-only `agent` scene in `docs/demo/demo.py` auto-opens
and focuses the panel's real `#agent-input` widget after mount, then VHS types
the prompt itself and presses Enter, submitting it through the genuine
`Input`/`on_input_submitted` path to a real `AgentRuntime`.

What the capture **proves**: the shipped pipeline runs end to end. The panel
accepts and submits the typed prompt; the real `AgentRuntime` calls
`diagnose_pod` and then `get_logs` through the real `ToolExecutor`; each result
comes back into the conversation as a `role="tool"` message; the real
`EvidenceLedger` mints `[E1]` and `[E2]` for those two reads; and the runtime
validates the answer's markers against the ledger, so `TurnComplete.cited` is
`("E1", "E2")` and `uncited` is empty — which is why the panel renders no
unsupported citation warning under this answer.

What it **does not prove**: anything about a live model, a live cluster, or
answer quality. The only scripted part is the model's side of the
conversation: `DemoAgentProvider` in `docs/demo/agent_story.py` is a real
`LLMProvider` implementation that is deterministic and offline — it opens no
socket, reads no credential, and always chooses the same two tool calls and
streams the same answer text, with short pauses purely for pacing. Everything
those tools read is the synthetic fixture in `docs/demo/demo.py`
(`DemoReadOps`), served through the same `ReadOps` boundary a cluster is served
through: the CrashLoopBackOff pod `shop/payment-worker-6c9f7d-b3xnq`, its
synthetic Warning events, and its generated log lines. No credential, no
network, no cluster, and no external provider takes part in the recording.
Every surface that embeds this media — the landing Agent scene, the landing
evidence tile, and the `docs/agent.md` storyboard — must therefore call it a
deterministic synthetic-cluster walkthrough, and must never present it as a
live-provider or model-quality claim. The production behaviour those pages
link to is documented in `docs/agent.md`.

The capture's selected row is whatever the demo table happens to have
highlighted; the answer is grounded in the two tool reads above, not in that
selection, and no surface may present the row as its evidence.

The panel header in these frames reads `⚡ korvid-demo · ~↑… ↓… tok`, and both
halves are korvid's own rendering rather than anything an external provider
said. `korvid-demo` is the synthetic label `docs/demo/demo.py` passes as
`agent_model_name` for this scene — no provider is named, because none takes
part. The counters are korvid's own token estimate derived from character
length, which is exactly what the leading `~` marks: `DemoAgentProvider`
reports no usage, so the runtime fills the totals in itself. Neither figure is
billing or token-spend metadata, and no surface may read them as cost or as
evidence about a live provider. That is why the landing-video design's privacy
criterion bans real provider identity and spend metadata rather than banning
korvid's own chrome, which cannot be covered without misrepresenting the
product.

The describe pane filling the left of the frame is `agent.follow`: the
shipped `AgentUiController` mirrors each successful read through the same
`UIBridge` mapping MCP follow uses, so the pane is a reflection of
`diagnose_pod`, not a UI-drive tool call and not a write. With the panel
expanded, `agent_open_describe` shares that pane instead of pushing a modal
`DescribeScreen`, so the guard that gates a second mirror — which only
trips for a pushed modal — never blocks `get_logs`: both the `diagnose_pod`
describe and the `get_logs` mirror succeed, and both fire their own success
toast (`describe →` and `logs →`). The log pane really opens; it is simply
not visible in this frame, because the docked describe pane (60% width)
and the docked `AgentPanel` (40% width) already fill the screen between
them, leaving the undocked log pane no room to render alongside.

```sh
vhs docs/demo/agent.tape
ffmpeg -y -ss 00:00:11 -i docs/assets/scenes/agent-demo.mp4 \
  -frames:v 1 docs/assets/scenes/agent-poster.png
```

## Relationship graph

`docs/demo/relationships.tape` drives the real relationship screen over
metadata-only synthetic facts from `docs/demo/demo.py`.

`RelationshipSnapshotLoader` LISTs a fixed catalog of core/apps/batch/
discovery/networking/policy kinds plus any discovered Gateway API resource,
and reports every catalog kind the discovery aliases do not offer as
`unavailable` before it issues a single LIST. `RELATIONSHIP_ALIASES` in
`docs/demo/demo.py` therefore publishes all of them — most answer with an
empty list, which is a complete answer for a synthetic cluster that has none
of those objects — so the capture shows `Coverage: complete` instead of a
banner listing fourteen absent kinds. The frame must contain at least one
resolved dependency (`uses_config` → the ConfigMap) and one resolved
dependent (`selects` ← the Service, derived by the product's own
`extract_relationship_facts` from a real Service manifest shape);
`tests/test_docs_visual_assets.py` runs the real loader over the fixture and
fails if either disappears.

```sh
vhs docs/demo/relationships.tape
ffmpeg -y -ss 00:00:05 -i docs/assets/scenes/relationship-demo.mp4 \
  -frames:v 1 docs/assets/scenes/relationship-graph.png
```

## MCP follow

`docs/assets/scenes/mcp-follow-demo.mp4` is recorded from this repository
alone. `docs/demo/mcp-follow.tape` composes two panes with tmux over a fixed
`139`×`42` grid — the terminal's own grid at the tape's geometry, so attaching
resizes nothing — and splits it with `-p 45`, giving the client 45% of the
width and korvid the remaining 55%. Every tmux command in the capture carries
`-S .korvid-mcp-demo.tmux.sock`, a private server bound to a socket inside the
checkout. tmux's default socket belongs to whoever runs the recorder, and it
already carries that developer's own sessions; a fixed session name there is a
name the recording merely hopes is free, and the teardowns below would kill a
collision rather than step around it. On its own socket the name is the
recording's to claim, and nothing on the shared server is addressable from the
capture at all. The socket reaches no frame — the composition runs hidden, and
the visible window shows only the attached panes — so it changes nothing about
the recorded story. Both panes are launched with
`uv run --frozen --extra mcp`: korvid's MCP stack is an optional extra rather
than a dependency group, so `uv run --frozen` on its own leaves a clean
checkout without the scene's lazily imported `korvid.mcp.server` or the
client's MCP SDK. The flag reaches no frame — the same code runs and prints
the same output — so enabling it does not invalidate the recorded clip. On the
left is the `mcp` scene of `docs/demo/demo.py` (`--scene mcp`), korvid's real
TUI over the synthetic fixture, serving the real `KorvidMCPServer` over
Streamable HTTP on loopback port 7878. On the right is
`docs/demo/mcp_client.py`, a real MCP SDK
`ClientSession` that speaks Streamable HTTP to `http://127.0.0.1:7878/mcp`
and calls four **read-only** tools. Every view the left pane opens is
korvid's own follow bridge mirroring the answer the right pane just
received; no keystroke is sent to the TUI and no frame is staged, redrawn or
cleared.

Nothing outside the checkout takes part, and no external client metadata can
reach a frame: the right pane is this repository's own client, so it prints
no assistant name, no model, no token count, no working directory and no
endpoint file. The client speaks only to `127.0.0.1:7878`, no credential is
used, the demo server writes no MCP endpoint file, and the tape turns the
tmux status line off before the first captured frame — that line is the only
surface that would print a hostname, a user or today's date into a landing
asset. The shell that composes the panes is never captured, and the six
repository-local files a recording makes — the handshake pair
(`.korvid-mcp-demo-ready` and `.korvid-mcp-demo-go`), the client's status
pair (`.korvid-mcp-demo-client-ok` and `.korvid-mcp-demo-client-failed`),
the private tmux socket (`.korvid-mcp-demo.tmux.sock`)
and the candidate render described below — are all
created and removed inside the checkout.

The client is never released on a timer. `--scene mcp` publishes
`.korvid-mcp-demo-ready` from its real Textual mount, and only after its
`KorvidMCPServer` reported itself bound — the first moment an external call
can be both answered and mirrored on screen. The tape types a wait for that
file into the hidden shell and bounds it at a wall-clock 60 s deadline, so a
server that never binds fails the recording instead of hanging it. The bound
is wall-clock, not an iteration count: it resets bash's own `SECONDS`
builtin (`SECONDS=0`) and loops while `$SECONDS` stays under 60, rather than
counting `sleep 0.1` iterations up to some fixed total. Counting iterations
instead assumes each one costs exactly its `sleep` argument, but forking and
executing `sleep` itself is not free, and a large enough iteration count
measured real time well past this tape's 65 s hidden allowance. `SECONDS` is
immune to that overhead, so the deadline stays ~60 s regardless of how much
the loop's own bookkeeping costs.

VHS advances on its own clock: `Type` hands the loop to the shell and
returns, and the `Sleep` that follows never observes the loop returning. The
hidden allowance is therefore sized to cover the whole bound — 65 s against a
60 s wait — rather than to match a typical start, so VHS cannot type the
release or reach `Show` while the shell is still waiting. Readiness arriving
early costs nothing, because the client pane is still held by the separate
gate file and no part of the story can run inside the hidden block.

Only where readiness is known to exist does the tape drop
`.korvid-mcp-demo-go`, the gate the client pane waits on, from a background
subshell outside tmux — no keystroke of the trigger reaches the attached
TUI — and attach the session. Both live in the same branch, so neither can
happen without the signal; if the signal never arrives the tape prints the
reason into the hidden block and exits without attaching, leaving an obvious
failed recording rather than a plausible-looking wrong one. A fixed sleep
would release the client whether or not port 7878 was listening, so a slow
cold checkout would open the story on the client's connection error rather
than on the follow story.

A failed client fails the recording. VHS records the visible 15 s on
its own clock and stops; it never observes the client pane, so a client that
raised on its second call would still produce an apparently finished asset —
with a traceback in the frames and the TUI reflowed to full width once the
pane closed. So `docs/demo/mcp_client.py` lets no exception reach the
terminal: it catches the failure, publishes `.korvid-mcp-demo-client-failed`,
prints one fixed line — no traceback, no error text, no tool result — and
holds the pane open for a bounded 30 s, past the visible window, so the
composition survives intact to the teardown. `.korvid-mcp-demo-client-ok` is
published only after all four calls and the closing card have been printed and
the MCP session and its HTTP transport have both closed, before the closing
hold, so it certifies a story that finished rather than a process that
survived. That publish belongs to the entry point, not to the story: written
inside the story it would have certified a run whose own teardown had not
happened yet, and anything that teardown raised — a reset peer, a
half-closed stream — then arrived with a success already on disk. Since the
failure marker is best-effort, a checkout that could not write it left exactly
one marker behind, the success, and this wrapper promotes on that. Neither
marker is ever inherited: the client clears
both at the start of a run, before it connects, so only what this run
published can grade it. A client killed before it publishes anything — a
`SIGKILL`, a tape that timed out — therefore leaves no marker at all, which
is a rejection; the tape's own `rm -f` and the wrapper's cleanup remain as
further layers rather than the only ones. The closing hold itself sits outside
that failure channel: it is local pacing for the frames, and a story already
certified as complete may not be re-graded by whatever happens while its pane
idles.

Reading those two files from the tape would settle nothing. VHS renders the
timeline it was given and exits 0 whatever the shell it typed into did, so a
tape's own exit status is not a publication gate: whatever `Output` names has
already been written by the time any check inside the tape can run. The tape
therefore renders to a candidate —
`docs/assets/scenes/.mcp-follow-demo.candidate.mp4`, git-ignored like every
other recording side effect — and never to the published clip. The verdict
belongs to `docs/demo/record-mcp-follow.sh`, the one command that regenerates
this capture.

Before it starts VHS, that wrapper settles which tape it is about to run, by
its bytes. `docs/demo/record-mcp-follow.sh` carries the reviewed tape's raw
SHA-256 — `60334eb07ab42901a4885584174b9f1bfe4089f1ebdb685f64c8e136cbe2a743`,
computed with `sha256sum` or, where that is absent, `shasum -a 256` — and
refuses any tape that does not hash to it. The pin covers the whole file, so
nothing here has to know what a VHS directive means: an edit is an unreviewed
tape whatever it spells and wherever it sits, and no unreviewed tape reaches
VHS. **Editing the tape is therefore two steps, in this order:
review the new bytes, then recompute the pin** — here, in
`docs/demo/record-mcp-follow.sh`, and in the 2026-08-26 plan, which publish
the same digest. Recomputing it to make a refusal go away is the one move this
boundary exists to prevent.

The pin is not a normal environment override. The wrapper accepts
`KORVID_MCP_TAPE_SHA256` only with `KORVID_MCP_TEST_MODE=1`, which exists only
for the Bash contract tests. In that mode the final path's physical parent must
sit outside this checkout's physical repository root, so a test digest cannot
publish repository files even through a symlinked path.

Two literal checks stand beside the pin, and neither parses a directive. The
published clip's own basename must be **absent** from the tape's bytes —
anywhere, under any spelling of the path, comments included — so a pin moved
onto bytes nobody read carefully still cannot hand VHS the reviewed clip's
name; VHS cannot write a file it is not given the name of. And the candidate's
basename must be **present**, because `KORVID_MCP_CANDIDATE` is set
independently of the tape and the wrapper may not grade a file this run never
wrote. The byte rule is why `docs/demo/mcp-follow.tape` never names the
published clip, not in a directive and not in a comment.

That wrapper runs VHS and then grades the run from outside it. It promotes
the candidate onto `docs/assets/scenes/mcp-follow-demo.mp4` with one `mv` —
a **rename**, which the filesystem makes atomic only while both paths sit in
the **same directory**, so a reader sees either the previous asset or the
whole new one and never a half-written file. That is why the tape renders the
candidate beside the published clip rather than into a scratch directory, and
why the wrapper resolves both parents physically and refuses the run before
VHS starts if they differ: any override of `KORVID_MCP_CANDIDATE` or
`KORVID_MCP_FINAL` has to preserve that invariant. Promotion happens only
when VHS returned 0, the failure file is **absent**, the success file is
**present**, and the candidate exists.
Failure outranks success, as defence in depth: the client publishes its
success only once everything but that local closing hold has succeeded, so the
two markers should never appear together — and a run that somehow produced
both is a failed one. On every other path the
wrapper prints one line on stderr, removes the candidate and all four
handshake files, tears down the recording's own tmux session by name **on its
private socket** and removes the socket, and
exits non-zero, leaving any previously approved clip byte-identical. A failed
take publishes nothing at all, instead of replacing a reviewed asset with a
truncated story. The refusal paths that never reach VHS — a tape whose bytes
do not match the reviewed pin, most of all — are held to the same rule: they
probe and clear the private socket and never speak to the default server, so
a wrapper that refuses to record cannot disturb a session it did not create.

The captured timeline runs the story once, at reading speed. Each call is
announced, answered, and then held for a fixed beat while the mirrored view
is read:

| Call | Hold | Mirrored view |
| --- | --- | --- |
| `list_resources` | 2.2 s | the `shop` pod table |
| `diagnose_pod` | 3.2 s | the failing pod's describe pane |
| `get_logs` | 3.6 s | its log pane |
| `helm_list_releases` | 2.4 s | the Helm releases that own it |

The client pane never clears and its lines are clipped rather than wrapped,
so the logs remain visible under the Helm beat and the closing
`read-only investigation complete` card: the whole read-only investigation
stays legible in the final frame. The closing card's own `6.0`s hold outlasts
the capture on purpose — a client that exited first would close its pane and
reflow the TUI to full width inside the last captured frames.

One piece of choreography, disclosed plainly: `diagnose_pod` opens a modal
describe screen through korvid's own follow bridge, and korvid's shipped
user-priority guard would refuse to mirror any later call while that screen
stayed up — the user is reading it. So `DemoKorvidApp`, a documentation-only
harness used only for this recording, closes that modal after 2.2 seconds,
standing in for the Esc a watching operator would press. That dismissal lands
inside the client's 3.2 s `diagnose_pod` beat, so the screen is already gone
before `get_logs` is issued: nothing in the captured timeline is ever
refused, and all four mirrors — the pod table, the describe pane, the log
pane and the Helm releases — succeed. No keystroke is sent to the TUI, and
the shipped guard is not weakened, bypassed or reconfigured by this
documentation-only harness; it is simply never asked to refuse.

The poster is cut from the log beat, where the external client's first three
calls sit beside korvid's own log pane, its `agent logs →
shop/payment-worker-…` follow toast and its `⇄MCP on :7878 ·follow` status
line. It is cut from the promoted clip, after the wrapper has published it,
so a recording that was never promoted leaves the poster untouched too.

```sh
docs/demo/record-mcp-follow.sh
ffmpeg -y -ss 00:00:08.5 -i docs/assets/scenes/mcp-follow-demo.mp4 \
  -frames:v 1 -vf setsar=1 docs/assets/scenes/mcp-poster.png
```

The shipped clip answers `ffprobe -show_entries
stream=width,height,sample_aspect_ratio,display_aspect_ratio` with
`1280`, `710`, `1:1` and `128:71`, and runs between 12 and 15 seconds.

`docs/assets/mcp-follow-demo.gif` is the README's animated copy of that same
capture, derived from the MP4 above and from nothing else:

```sh
ffmpeg -y -i docs/assets/scenes/mcp-follow-demo.mp4 \
  -lavfi "fps=12.5,split[a][b];[a]palettegen=max_colors=256:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" \
  -loop 0 docs/assets/mcp-follow-demo.gif
```

Nothing in that chain rescales, crops, or composites: the GIF stores the
clip's own `1280`×`710` frames, quantised to a 256-colour palette and
resampled to 12.5 fps, and runs the full 13.76 s story at the same reading
pace. The README GIF therefore contains no external client session metadata.
It inherits this section's provenance exactly — this repository's own MCP SDK
client, the synthetic fixture, no assistant name, no model, no token count,
no working directory, no branch and no hostname. The GIF that shipped here
before was a different recording whose right-hand pane was someone else's
client session; it was replaced, not sanitized. No official-site page embeds
it. Because the GIF is a checked-in asset under `docs/assets`, MkDocs still
serves it at `assets/mcp-follow-demo.gif`. The landing page uses only the
locally recorded MP4/poster above.

`tests/test_docs_visual_assets.py` decodes the poster and fails if either pane
loses its legible evidence. It also reads the MP4's own `tkhd`/`avc1`/`pasp`
boxes and fails if the stored, displayed and declared geometry ever disagree
again. `tests/test_mcp_follow_demo_asset.py` decodes the GIF's own frame
delays and pins the reviewed bytes by SHA-256, so regenerating it is a
deliberate act that re-opens the frame review rather than a silent swap.
