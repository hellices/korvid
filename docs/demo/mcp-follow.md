# MCP follow README recording

This recording uses a disposable local cluster. Never record a production
context: MCP log results are not credential-pattern masked by korvid.

## Prepare the fixture

The workflow owns a uniquely named k3d cluster. It refuses an existing cluster
instead of reusing any operator-managed context:

```sh
demo_state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/korvid"
umask 077
if ! mkdir -p "$demo_state_dir"; then
  echo "Failed to create the demo state directory" >&2
  exit 1
fi
run_id="$(date -u +%Y%m%d%H%M%S)-$$"
cluster_name="korvid-mcp-demo-$run_id"
cluster_name_file="$demo_state_dir/mcp-demo-cluster-name"
if k3d cluster list --no-headers | awk '{print $1}' | grep -Fxq "$cluster_name"; then
  echo "Refusing to reuse existing k3d cluster: $cluster_name" >&2
  exit 1
fi
if ! printf '%s\n' "$cluster_name" > "$cluster_name_file"; then
  echo "Failed to write the demo cluster marker" >&2
  exit 1
fi
if ! k3d cluster create "$cluster_name" --agents 1 --wait \
  --kubeconfig-switch-context=false; then
  echo "Failed to create the dedicated k3d cluster" >&2
  rm -f "$cluster_name_file"
  exit 1
fi
identity_ready=false
rollback_incomplete_identity() {
  if [ "$identity_ready" != true ]; then
    k3d cluster delete "$cluster_name"
  fi
}
trap rollback_incomplete_identity EXIT
context="k3d-$cluster_name"
demo_context_file="$demo_state_dir/mcp-demo-context"
demo_kubeconfig="$demo_state_dir/mcp-demo-kubeconfig"
demo_cluster_uid_file="$demo_state_dir/mcp-demo-cluster-uid"
if ! printf '%s\n' "$context" > "$demo_context_file"; then
  echo "Failed to write the demo context marker" >&2
  echo "Dedicated cluster retained for cleanup: $cluster_name" >&2
  exit 1
fi
if ! kubectl --context "$context" config view --minify --flatten --raw \
  > "$demo_kubeconfig"; then
  echo "Failed to snapshot the demo kubeconfig" >&2
  echo "Dedicated cluster retained for cleanup: $cluster_name" >&2
  exit 1
fi
if ! cluster_uid="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$context" get namespace kube-system \
  -o jsonpath='{.metadata.uid}')"; then
  echo "Failed to read the demo cluster identity" >&2
  echo "Dedicated cluster retained for cleanup: $cluster_name" >&2
  exit 1
fi
if ! printf '%s\n' "$cluster_uid" > "$demo_cluster_uid_file"; then
  echo "Failed to write the demo cluster identity" >&2
  echo "Dedicated cluster retained for cleanup: $cluster_name" >&2
  exit 1
fi
identity_ready=true
trap - EXIT
if ! kubectl --kubeconfig "$demo_kubeconfig" --context "$context" create -f - <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  name: shop
  labels:
    korvid.dev/demo: mcp-follow
YAML
then
  echo "Failed to create the demo namespace in the dedicated cluster" >&2
  exit 1
fi

if ! helm upgrade --install shop-demo docs/demo/mcp-follow-fixture \
  --namespace shop --kubeconfig "$demo_kubeconfig" --kube-context "$context"; then
  echo "Failed to install the recording release" >&2
  echo "Run the cleanup section before retrying; demo identity state retained" >&2
  exit 1
fi
kubectl --kubeconfig "$demo_kubeconfig" --context "$context" \
  -n shop get pods --watch
```

Stop the watch with Ctrl-C after the `payment-worker` pod shows `Error` or
`CrashLoopBackOff`, then confirm Kubernetes recorded the restart backoff:

```sh
demo_state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/korvid"
context="$(cat "$demo_state_dir/mcp-demo-context")"
kubectl --kubeconfig "$demo_state_dir/mcp-demo-kubeconfig" \
  --context "$context" -n shop get events \
  --field-selector reason=BackOff,type=Warning
```

## Connect GitHub Copilot CLI

Re-read the recorded context immediately before startup, require an exact
match, and pin korvid to it through an isolated config:

```sh
demo_context_file="${XDG_STATE_HOME:-$HOME/.local/state}/korvid/mcp-demo-context"
demo_state_dir="$(dirname "$demo_context_file")"
cluster_name_file="$demo_state_dir/mcp-demo-cluster-name"
demo_kubeconfig="$demo_state_dir/mcp-demo-kubeconfig"
demo_cluster_uid_file="$demo_state_dir/mcp-demo-cluster-uid"
if [ ! -r "$cluster_name_file" ] || [ ! -r "$demo_context_file" ] ||
  [ ! -r "$demo_kubeconfig" ] ||
  [ ! -r "$demo_cluster_uid_file" ]; then
  echo "Refusing MCP startup without the recorded demo identity" >&2
  exit 1
fi
cluster_name="$(cat "$cluster_name_file")"
prepared_context="$(cat "$demo_context_file")"
prepared_cluster_uid="$(cat "$demo_cluster_uid_file")"
if ! test "$prepared_context" = "k3d-$cluster_name"; then
  echo "Refusing MCP startup because cluster marker and context differ" >&2
  exit 1
fi
if ! cluster_uid="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$prepared_context" get namespace kube-system \
  -o jsonpath='{.metadata.uid}')"; then
  echo "Refusing MCP startup because cluster identity is unavailable" >&2
  exit 1
fi
if ! test "$cluster_uid" = "$prepared_cluster_uid"; then
  echo "Refusing MCP startup after cluster identity changed" >&2
  exit 1
fi
if ! owner="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$prepared_context" get namespace shop \
  -o jsonpath='{.metadata.labels.korvid\.dev/demo}')"; then
  echo "Refusing MCP startup because namespace ownership is unavailable" >&2
  exit 1
fi
if [ "$owner" != "mcp-follow" ]; then
  echo "Refusing MCP startup because namespace ownership changed" >&2
  exit 1
fi
demo_home="$(dirname "$demo_context_file")/mcp-demo-home"
if ! mkdir -p "$demo_home/.config/korvid"; then
  echo "Failed to create the isolated config directory" >&2
  exit 1
fi
if ! printf 'kube_context: %s\nmcp:\n  enabled: true\n  follow: true\n' \
  "$prepared_context" > "$demo_home/.config/korvid/config.yaml"; then
  echo "Failed to write the isolated korvid config" >&2
  exit 1
fi
session_name="$cluster_name"
if tmux has-session -t "$session_name" 2>/dev/null; then
  echo "Refusing to reuse existing tmux session: $session_name" >&2
  exit 1
fi
if ! tmux new-session -d -s "$session_name" -x 160 -y 45 -c "$PWD" \
  "HOME=\"$demo_home\" XDG_CONFIG_HOME=\"$demo_home/.config\" XDG_STATE_HOME=\"$demo_home/.local/state\" XDG_CACHE_HOME=\"$demo_home/.cache\" KUBECONFIG=\"$demo_kubeconfig\" korvid --mcp --namespace shop"; then
  echo "Failed to create the recording tmux session" >&2
  exit 1
fi
if ! tmux set-option -t "$session_name" status off; then
  tmux kill-session -t "$session_name"
  echo "Failed to configure the recording tmux session" >&2
  exit 1
fi
```

Wait until the left pane status shows `MCP on` and `·follow`, then register only
the three read tools used by the recording:

```sh
cluster_name="$(cat "$(dirname "$demo_context_file")/mcp-demo-cluster-name")"
run_id="${cluster_name#korvid-mcp-demo-}"
recording_server="korvid-mcp-demo-$run_id"
registration_file="$(dirname "$demo_context_file")/mcp-demo-registration"
if copilot mcp get "$recording_server" >/dev/null 2>&1; then
  echo "Refusing to replace existing Copilot MCP server: $recording_server" >&2
  exit 1
fi
if ! printf '%s\n' "$recording_server" > "$registration_file"; then
  echo "Failed to write the recording MCP marker" >&2
  exit 1
fi
if ! copilot mcp add --transport http \
  --tools 'list_resources,get_logs,helm_list_releases' \
  "$recording_server" http://127.0.0.1:7878/mcp; then
  echo "Failed to register the recording MCP server" >&2
  rm -f "$registration_file"
  exit 1
fi
```

Start Copilot in a 35-column right pane. The recorded unique server name is
Copilot CLI's server-level MCP selector:

```sh
recording_server="$(cat "$(dirname "$demo_context_file")/mcp-demo-registration")"
session_name="$(cat "$(dirname "$demo_context_file")/mcp-demo-cluster-name")"
tmux split-window -h -l 35 -t "$session_name:0" -c "$PWD" \
  "copilot --disable-builtin-mcps --allow-all-tools --available-tools=$recording_server"
tmux select-pane -t "$session_name:0.1"
```

Complete any Copilot trust prompt.
Leave the Copilot pane focused at its empty prompt before running VHS.
Do not enter the scenario prompt yourself; the tape types and submits it after
capture starts:

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
idle_start=1.0
idle_end=7.0
demo_end=17.0
kept_duration="$(awk -v a="$idle_start" -v b="$idle_end" -v c="$demo_end" \
  'BEGIN { print (a < b && b < c) ? a + (c - b) : "invalid" }')"
if [ "$kept_duration" = "invalid" ]; then
  echo "Invalid trim timestamps" >&2
  exit 1
fi
if ! awk -v duration="$kept_duration" 'BEGIN { exit !(duration <= 15) }'; then
  echo "Trimmed duration exceeds the 15s budget" >&2
  exit 1
fi
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
demo_state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/korvid"
cluster_name_file="$demo_state_dir/mcp-demo-cluster-name"
demo_context_file="$demo_state_dir/mcp-demo-context"
demo_kubeconfig="$demo_state_dir/mcp-demo-kubeconfig"
demo_cluster_uid_file="$demo_state_dir/mcp-demo-cluster-uid"
if [ ! -r "$cluster_name_file" ] || [ ! -r "$demo_context_file" ] ||
  [ ! -r "$demo_kubeconfig" ] || [ ! -r "$demo_cluster_uid_file" ]; then
  echo "Refusing cleanup without the recorded demo identity" >&2
  exit 1
fi
cluster_name="$(cat "$cluster_name_file")"
prepared_context="$(cat "$demo_context_file")"
prepared_cluster_uid="$(cat "$demo_cluster_uid_file")"
if ! test "$prepared_context" = "k3d-$cluster_name"; then
  echo "Refusing cleanup because cluster marker and context differ" >&2
  exit 1
fi

# Local teardown comes first so no MCP endpoint remains live if cluster
# validation later refuses a mutation.
if tmux has-session -t "$cluster_name" 2>/dev/null; then
  tmux kill-session -t "$cluster_name"
fi
registration_file="$demo_state_dir/mcp-demo-registration"
if [ -r "$registration_file" ]; then
  recording_server="$(cat "$registration_file")"
  case "$recording_server" in
    korvid-mcp-demo-*) ;;
    *)
    echo "Refusing to remove unexpected MCP registration: $recording_server" >&2
    exit 1
      ;;
  esac
  if copilot mcp get "$recording_server" >/dev/null 2>&1; then
    if ! copilot mcp remove "$recording_server"; then
      echo "Failed to remove the recording MCP server; cleanup state retained" >&2
      exit 1
    fi
  fi
  rm -f "$registration_file"
else
  echo "Recording MCP server is already absent; continuing cleanup" >&2
fi

if ! cluster_uid="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$prepared_context" get namespace kube-system \
  -o jsonpath='{.metadata.uid}')"; then
  echo "Refusing cluster deletion because identity is unavailable" >&2
  exit 1
fi
if ! test "$cluster_uid" = "$prepared_cluster_uid"; then
  echo "Refusing cleanup after cluster identity changed" >&2
  exit 1
fi
if ! k3d cluster delete "$cluster_name"; then
  echo "Failed to delete the dedicated recording cluster; state retained" >&2
  exit 1
fi
demo_home="$(dirname "$demo_context_file")/mcp-demo-home"
rm -rf -- "$demo_home"
rm -f "$cluster_name_file" "$demo_context_file" "$demo_kubeconfig" \
  "$demo_cluster_uid_file"
```
