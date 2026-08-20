# MCP follow README recording

This recording uses a disposable local cluster. Never record a production
context: MCP log results are not credential-pattern masked by korvid.

## Prepare the fixture

The guard accepts only common local-cluster context names:

```sh
context="$(kubectl config current-context)"
case "$context" in
  kind-*|k3d-*|minikube|docker-desktop) ;;
  *) echo "Refusing to seed non-local context: $context" >&2; exit 1 ;;
esac

helm upgrade --install shop-demo docs/demo/mcp-follow-fixture \
  --namespace shop --create-namespace
kubectl -n shop get pods --watch
```

Stop the watch with Ctrl-C after the `payment-worker` pod shows `Error` or
`CrashLoopBackOff`, then confirm Kubernetes recorded the restart backoff:

```sh
kubectl -n shop get events \
  --field-selector reason=BackOff,type=Warning
```

## Connect GitHub Copilot CLI

Start korvid:

```sh
korvid --mcp --namespace shop
```

In korvid, run `:mcp follow on`. Register only the three read tools used by the
recording:

```sh
copilot mcp add --transport http \
  --tools 'list_resources,get_logs,helm_list_releases' \
  korvid http://127.0.0.1:7878/mcp
```

Create a tmux session named `korvid-mcp-demo`, place korvid in the left pane and
an interactive Copilot CLI in the right pane, and size the regions
approximately 75% TUI / 25% Copilot. Start Copilot with only the registered
korvid server available:

```sh
copilot --disable-builtin-mcps --allow-all-tools \
  --available-tools=korvid
```

Keep `MCP ·follow` visible. Enter this prompt before capture:

> Use korvid MCP in order: list_resources shop pods → get_logs unhealthy one
> → helm_list_releases.

Start the visible capture with Enter. The target sequence is:

1. `list_resources` — pod list;
2. `get_logs` — live logs;
3. `helm_list_releases` — Helm release browser.

Repeat the take if the model chooses another order. Do not fake or reorder
tool cards.

## Export

The tape hides only the model's initial idle period. Record the prepared tmux
session:

```sh
vhs docs/demo/mcp-follow.tape
```

VHS writes a 1440-pixel, 25 fps source GIF. Optimize it to the README contract
through a temporary output file:

```sh
ffmpeg -y -i docs/assets/mcp-follow-demo.gif \
  -filter_complex \
  "fps=12,scale=1280:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
  -loop 0 docs/assets/mcp-follow-demo.tmp.gif &&
mv docs/assets/mcp-follow-demo.tmp.gif docs/assets/mcp-follow-demo.gif
```

Verify the result:

```sh
ffprobe -v error -show_entries format=duration \
  -of default=noprint_wrappers=1:nokey=1 docs/assets/mcp-follow-demo.gif
sips -g pixelWidth -g pixelHeight docs/assets/mcp-follow-demo.gif
stat -f '%z bytes' docs/assets/mcp-follow-demo.gif
```

The duration must be at most 15 seconds, width exactly 1280 pixels, and file
size at most 8,388,608 bytes. Inspect the GIF at README width and confirm the
pod list, logs, and Helm views are each readable.

## Clean up

```sh
helm uninstall shop-demo --namespace shop
kubectl delete namespace shop
copilot mcp remove korvid
```
