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
  --namespace shop --create-namespace --wait=false
kubectl -n shop get pods --watch
```

Stop the watch with Ctrl-C after the `payment-worker` pod shows
`CrashLoopBackOff`.

## Connect VS Code

Start korvid:

```sh
korvid --mcp --namespace shop
```

In korvid, run `:mcp follow on`. Configure `.vscode/mcp.json`:

```json
{"servers": {"korvid": {"type": "http", "url": "http://127.0.0.1:7878/mcp"}}}
```

Allow the korvid read-only tools for this recording session. Move the terminal
into the editor area, place Copilot Chat in the secondary side bar, and size
the regions approximately 75% TUI / 25% Chat. Keep `MCP ·follow` visible.

Enter this prompt before capture:

> In the shop namespace, find the unhealthy pod, inspect the cause and open
> its logs, then show me the Helm releases.

Start the visible capture with Enter. The target sequence is:

1. `list_resources` — pod list;
2. `diagnose_pod` — pod describe;
3. `get_logs` — live logs;
4. `helm_list_releases` — Helm release browser.

Repeat the take if the model chooses another order. Do not fake or reorder
tool cards.

## Export

Trim only idle regions from the screen recording and save the result as
`/tmp/korvid-mcp-follow.mov`. Export the GIF:

```sh
ffmpeg -y -i /tmp/korvid-mcp-follow.mov \
  -filter_complex \
  "fps=12,scale=1280:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
  -loop 0 docs/assets/mcp-follow-demo.gif
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
pod list, describe, logs, and Helm views are each readable.

## Clean up

```sh
helm uninstall shop-demo --namespace shop
kubectl delete namespace shop
```
