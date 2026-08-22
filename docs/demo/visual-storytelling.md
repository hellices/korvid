# Official-site visual evidence

Every official-site scene uses synthetic resources from a disposable or
in-memory demo. No capture may contain a real cluster, credential, customer,
or production identifier.

## Base cockpit and feature frames

`docs/demo/demo.tape` records the in-memory `shop` fixture to
`docs/assets/demo.gif`; `docs/demo/README.md` converts the same recording to
`docs/assets/demo.mp4`.

```sh
vhs docs/demo/demo.tape
ffmpeg -y -i docs/assets/demo.gif -an -movflags +faststart \
  -pix_fmt yuv420p -vf 'scale=trunc(iw/2)*2:trunc(ih/2)*2' \
  docs/assets/demo.mp4
ffmpeg -y -ss 00:00:10 -i docs/assets/demo.mp4 -frames:v 1 \
  docs/assets/scenes/cockpit-poster.png
ffmpeg -y -ss 00:00:16 -i docs/assets/demo.mp4 -frames:v 1 \
  docs/assets/scenes/diagnosis.png
ffmpeg -y -ss 00:00:23 -i docs/assets/demo.mp4 -frames:v 1 \
  docs/assets/scenes/merged-logs.png
```

## Embedded agent

`docs/demo/agent.tape` records the real AgentPanel: the documentation-only
`agent` scene in `docs/demo/demo.py` auto-opens and focuses the panel's real
`#agent-input` widget after mount, then VHS types the prompt itself and
presses Enter, submitting it through the genuine
`Input`/`on_input_submitted` path to the deterministic `ScriptedAgentRuntime`.

```sh
vhs docs/demo/agent.tape
ffmpeg -y -ss 00:00:05 -i docs/assets/scenes/agent-demo.mp4 \
  -frames:v 1 docs/assets/scenes/agent-poster.png
```

## Relationship graph

`docs/demo/relationships.tape` drives the real relationship screen over
metadata-only synthetic facts from `docs/demo/demo.py`.

```sh
vhs docs/demo/relationships.tape
ffmpeg -y -ss 00:00:05 -i docs/assets/scenes/relationship-demo.mp4 \
  -frames:v 1 docs/assets/scenes/relationship-graph.png
```

## MCP follow

`docs/assets/mcp-follow-demo.gif` was recorded against the disposable local
cluster documented by its repository design and test contract. The site uses
a controllable MP4 and a poster derived from the reviewed GIF:

```sh
ffmpeg -y -i docs/assets/mcp-follow-demo.gif -an -movflags +faststart \
  -pix_fmt yuv420p -vf 'scale=trunc(iw/2)*2:trunc(ih/2)*2' \
  docs/assets/scenes/mcp-follow-demo.mp4
ffmpeg -y -ss 00:00:06 -i docs/assets/scenes/mcp-follow-demo.mp4 \
  -frames:v 1 docs/assets/scenes/mcp-poster.png
```
