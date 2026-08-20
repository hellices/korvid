# README demo recording

`demo.py` runs the real korvid TUI against canned in-memory data (no cluster
needed): a small "shop" namespace with a crashlooping payment worker, fake
describe manifests, warning events, and a synthetic log stream.

`demo.tape` is a [VHS](https://github.com/charmbracelet/vhs) script that
drives the harness and records `docs/assets/demo.gif`, the animation embedded
at the top of the repository README.

## Regenerating the GIF

```sh
brew install vhs          # pulls ttyd and ffmpeg
vhs docs/demo/demo.tape   # run from the repository root
```

## Trying it interactively

```sh
uv run python docs/demo/demo.py
```

The harness wires `KorvidApp` with fake watch sources the same way the UI
tests do, so it stays honest about what the real TUI looks like. If the app's
constructor or key flows change, re-run the tape and commit the new GIF.

The separate [MCP follow recording](mcp-follow.md) uses a disposable local
cluster and VS Code Copilot Chat to capture real external tool calls alongside
the TUI. It has its own fixture and does not change the canned VHS demo.
