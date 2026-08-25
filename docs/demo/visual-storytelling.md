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
alone. `docs/demo/mcp-follow.tape` composes two panes with tmux: on the left
the `mcp` scene of `docs/demo/demo.py` (`--scene mcp`), which is korvid's real
TUI over the
synthetic fixture, serving the real `KorvidMCPServer` over Streamable HTTP on
loopback port 7878; on the right `docs/demo/mcp_client.py`, a real MCP SDK
`ClientSession` that calls four **read-only** tools — `list_resources`,
`diagnose_pod`, `get_logs`, `helm_list_releases`. Every view the left pane
opens is korvid's own follow bridge mirroring the answer the right pane just
received; no keystroke is sent to the TUI and no frame is staged, redrawn or
cleared.

Nothing outside the checkout takes part. The client speaks only to
`127.0.0.1:7878`, no credential is used, the demo server writes no MCP
endpoint file, and the tape turns the tmux status line off before the first
captured frame — that line is the only surface that would print a hostname, a
user or today's date into a landing asset. The shell that composes the panes
is never captured, and the handshake file the tape uses to release the client
(`.korvid-mcp-demo-go`) is created and removed inside the checkout.

The captured timeline runs the story once, at reading speed: the pod table,
the failing pod's diagnosis in a describe pane, its log stream held long
enough to read before the story moves on, then the Helm releases that own it.

The poster is cut from the log beat, where the external client's first three
calls sit beside korvid's own log pane, its `agent logs →
shop/payment-worker-…` follow toast and its `⇄MCP on :7878 ·follow` status
line.

```sh
vhs docs/demo/mcp-follow.tape
ffmpeg -y -ss 00:00:08.5 -i docs/assets/scenes/mcp-follow-demo.mp4 \
  -frames:v 1 -vf setsar=1 docs/assets/scenes/mcp-poster.png
```

The shipped clip answers `ffprobe -show_entries
stream=width,height,sample_aspect_ratio,display_aspect_ratio` with
`1280`, `710`, `1:1` and `128:71`, and runs between 12 and 15 seconds.

`docs/assets/mcp-follow-demo.gif` is an older, unrelated capture whose
right-hand pane is a third-party MCP client, carrying that session's own
directory, branch, token spend and model name. No official-site page embeds
or uses the unredacted GIF as visitor-facing evidence. Because the GIF remains
a checked-in source asset under `docs/assets`, MkDocs still serves it at
`assets/mcp-follow-demo.gif`. Sanitizing or re-recording that pre-existing
README/source asset is a separate follow-up. The landing page uses only the
locally recorded MP4/poster above, which is derived from no part of it.

`tests/test_docs_visual_assets.py` decodes the poster and fails if either pane
loses its legible evidence. It also reads the MP4's own `tkhd`/`avc1`/`pasp`
boxes and fails if the stored, displayed and declared geometry ever disagree
again.
