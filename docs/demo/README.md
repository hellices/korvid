# README and site demo recording

`demo.py` runs the real korvid TUI against canned in-memory data (no cluster
needed): a small "shop" namespace with a crashlooping payment worker, fake
describe manifests, warning events, and a synthetic log stream.

`demo.tape` is a [VHS](https://github.com/charmbracelet/vhs) script that
drives the harness and records `docs/assets/demo.gif`, the animation embedded
at the top of the repository README. The official site uses a controllable MP4
derived from the same recording.

## Regenerating both formats

```sh
brew install vhs          # pulls ttyd and ffmpeg
vhs docs/demo/demo.tape   # run from the repository root
ffmpeg -y -i docs/assets/demo.gif -an -movflags +faststart \
  -pix_fmt yuv420p -vf 'scale=trunc(iw/2)*2:trunc(ih/2)*2' \
  docs/assets/demo.mp4
```

## Trying it interactively

```sh
uv run python docs/demo/demo.py
```

The harness wires `KorvidApp` with fake watch sources the same way the UI
tests do, so it stays honest about what the real TUI looks like. If the app's
constructor or key flows change, regenerate and commit both formats.
