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
if kubectl --context "$context" get namespace shop >/dev/null 2>&1; then
  echo "Refusing to reuse existing namespace: shop" >&2
  exit 1
fi
demo_context_file="${XDG_STATE_HOME:-$HOME/.local/state}/korvid/mcp-demo-context"
mkdir -p "$(dirname "$demo_context_file")"
printf '%s\n' "$context" > "$demo_context_file"
kubectl --context "$context" create namespace shop
kubectl --context "$context" label namespace shop korvid.dev/demo=mcp-follow

helm upgrade --install shop-demo docs/demo/mcp-follow-fixture \
  --namespace shop --kube-context "$context"
kubectl -n shop get pods --watch
```

Stop the watch with Ctrl-C after the `payment-worker` pod shows `Error` or
`CrashLoopBackOff`, then confirm Kubernetes recorded the restart backoff:

```sh
kubectl -n shop get events \
  --field-selector reason=BackOff,type=Warning
```

## Connect GitHub Copilot CLI

Re-read the recorded context immediately before startup, require an exact
match, and pin korvid to it through an isolated config:

```sh
demo_context_file="${XDG_STATE_HOME:-$HOME/.local/state}/korvid/mcp-demo-context"
if [ ! -r "$demo_context_file" ]; then
  echo "Refusing MCP startup without the recorded demo context" >&2
  exit 1
fi
prepared_context="$(cat "$demo_context_file")"
context="$(kubectl config current-context)"
if ! test "$context" = "$prepared_context"; then
  echo "Refusing MCP startup after context changed: $prepared_context -> $context" >&2
  exit 1
fi
case "$prepared_context" in
  kind-*|k3d-*|minikube|docker-desktop) ;;
  *) echo "Refusing MCP startup on non-local context: $prepared_context" >&2; exit 1 ;;
esac
demo_home="$(dirname "$demo_context_file")/mcp-demo-home"
mkdir -p "$demo_home/.config/korvid"
printf 'kube_context: %s\n' "$prepared_context" \
  > "$demo_home/.config/korvid/config.yaml"
demo_kubeconfig="${KUBECONFIG:-$HOME/.kube/config}"
HOME="$demo_home" KUBECONFIG="$demo_kubeconfig" korvid --mcp --namespace shop
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
approximately 75% TUI / 25% Copilot. Start Copilot with only the registered korvid server available. Here
`--available-tools=korvid` is Copilot CLI's server-level MCP selector:

```sh
copilot --disable-builtin-mcps --allow-all-tools \
  --available-tools=korvid
```

Keep `MCP ·follow` visible. Leave the Copilot pane focused at its empty prompt
before running VHS. Do not enter the scenario prompt yourself; the tape types
and submits it after capture starts:

> Use korvid MCP in order: list_resources shop pods → get_logs unhealthy one
> → helm_list_releases.

Run the tape. Its target sequence is:

1. `list_resources` — pod list;
2. `get_logs` — live logs;
3. `helm_list_releases` — Helm release browser.

Repeat the take if the model chooses another order. Do not fake or reorder
tool cards, and discard any take where all three cards and their visible TUI
transitions were not captured.

## Export

The tape captures the complete interaction, including model idle time, to a
source GIF:

```sh
vhs docs/demo/mcp-follow.tape
```

Inspect the source GIF and set three timestamps: `idle_start` immediately after
Enter, `idle_end` immediately before the first MCP tool card, and `demo_end`
after the Helm browser has been visible long enough to read. The filter removes
only that observed idle interval and the trailing tail; it retains every MCP
call and follow transition.

```sh
idle_start=0.5
idle_end=7.0
demo_end=17.0
ffmpeg -y -i docs/assets/mcp-follow-demo.raw.gif \
  -filter_complex \
  "[0:v]trim=start=0:end=${idle_start},setpts=PTS-STARTPTS[first];[0:v]trim=start=${idle_end}:end=${demo_end},setpts=PTS-STARTPTS[rest];[first][rest]concat=n=2:v=1:a=0,fps=12,scale=1280:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" \
  -loop 0 docs/assets/mcp-follow-demo.tmp.gif &&
mv docs/assets/mcp-follow-demo.tmp.gif docs/assets/mcp-follow-demo.gif &&
rm docs/assets/mcp-follow-demo.raw.gif
```

Verify the result:

```sh
ffprobe -v error -select_streams v:0 \
  -show_entries stream=width,height,r_frame_rate:format=duration \
  -of default=noprint_wrappers=1 docs/assets/mcp-follow-demo.gif
wc -c < docs/assets/mcp-follow-demo.gif
```

The duration must be at most 15 seconds, width exactly 1280 pixels, and file
size at most 8,388,608 bytes. Inspect the GIF at README width and confirm the
pod list, logs, and Helm views are each readable.

## Clean up

```sh
demo_context_file="${XDG_STATE_HOME:-$HOME/.local/state}/korvid/mcp-demo-context"
if [ ! -r "$demo_context_file" ]; then
  echo "Refusing cleanup without the recorded demo context" >&2
  exit 1
fi
prepared_context="$(cat "$demo_context_file")"
context="$(kubectl config current-context)"
if ! test "$context" = "$prepared_context"; then
  echo "Refusing cleanup after context changed: $prepared_context -> $context" >&2
  exit 1
fi
case "$prepared_context" in
  kind-*|k3d-*|minikube|docker-desktop) ;;
  *) echo "Refusing to clean non-local context: $prepared_context" >&2; exit 1 ;;
esac
if ! owner="$(kubectl --context "$prepared_context" get namespace shop \
  -o jsonpath='{.metadata.labels.korvid\.dev/demo}')"; then
  echo "Refusing cleanup because namespace ownership is unknown" >&2
  exit 1
fi
if [ "$owner" != "mcp-follow" ]; then
  echo "Refusing cleanup of namespace not owned by this demo" >&2
  exit 1
fi

helm uninstall shop-demo --namespace shop --kube-context "$prepared_context"
kubectl --context "$prepared_context" delete namespace shop --ignore-not-found
copilot mcp remove korvid
demo_home="$(dirname "$demo_context_file")/mcp-demo-home"
rm -rf -- "$demo_home"
rm -f "$demo_context_file"
```
