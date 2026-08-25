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

`docs/assets/mcp-follow-demo.gif` was recorded against the disposable local
cluster documented by its repository design and test contract. Its right-hand
pane is a third-party MCP client, and that window carries session details
which have nothing to do with korvid: a startup banner and tool inventory
above the exchange, and the client's working **directory** and branch, its
**token** spend and its **model** name below it. No official-site page embeds
or uses the unredacted GIF as visitor-facing evidence. Because the GIF remains
a checked-in source asset under `docs/assets`, MkDocs still serves it at
`assets/mcp-follow-demo.gif`. Sanitizing or re-recording that pre-existing
README/source asset is a separate follow-up. The landing page uses only the
sanitized derived MP4/poster.

The published clip is a deterministic reframe of the same reviewed recording:
its frames 36–83 (the four seconds in which korvid follows the client from
the pod table to the log stream to the Helm releases), with the client pane's
two off-topic bands cleared to that pane's own `#111111` background. Nothing
inside the retained band — the operator's prompt, `list_resources`,
`get_logs`, `helm_list_releases`, the summary — and nothing in korvid's own
pane is altered or moved, so the follow evidence stays exactly as recorded.

The poster is frame 9 of that clip: the external client's full prompt and its
first two tool calls beside korvid's own `agent logs → shop/payment-worker-…`
follow toast and its `⇄MCP on :7878 ·follow` status line.

The chain also ends its geometry pass with `setsar=1`. The GIF is 1280×711
with a 63:64 sample aspect ratio; rounding the height down to 710 for
`yuv420p` otherwise preserves the *display* aspect by rewriting the sample
aspect ratio to 2485:2528, which stores 1280×710 but lays out 1258×710 — so
the landing page's `1280 / 710` reservation would pillarbox the clip inside
its own box. Forcing square pixels changes no sample, only the metadata a
browser lays out from, and it is what makes the reserved box true:

```sh
ffmpeg -y -i docs/assets/mcp-follow-demo.gif -an -movflags +faststart \
  -pix_fmt yuv420p -crf 20 \
  -vf 'scale=trunc(iw/2)*2:trunc(ih/2)*2,trim=start_frame=36:end_frame=84,setpts=PTS-STARTPTS,setsar=1,drawbox=x=1000:y=22:w=280:h=320:color=0x111111:t=fill,drawbox=x=1000:y=578:w=280:h=132:color=0x111111:t=fill' \
  docs/assets/scenes/mcp-follow-demo.mp4
ffmpeg -y -i docs/assets/scenes/mcp-follow-demo.mp4 -vf "select='eq(n\,9)'" \
  -frames:v 1 docs/assets/scenes/mcp-poster.png
```

The shipped clip answers `ffprobe -show_entries
stream=width,height,sample_aspect_ratio,display_aspect_ratio` with
`1280`, `710`, `1:1` and `128:71`.

`tests/test_docs_visual_assets.py` decodes the poster and fails if either
cleared band regains legible content — or if the retained band loses it. It
also reads the MP4's own `tkhd`/`avc1`/`pasp` boxes and fails if the stored,
displayed and declared geometry ever disagree again.
