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
demo_context_file="${XDG_STATE_HOME:-$HOME/.local/state}/korvid/mcp-demo-context"
demo_state_dir="$(dirname "$demo_context_file")"
demo_kubeconfig="$demo_state_dir/mcp-demo-kubeconfig"
demo_cluster_uid_file="$demo_state_dir/mcp-demo-cluster-uid"
demo_namespace_uid_file="$demo_state_dir/mcp-demo-namespace-uid"
clear_demo_identity() {
  rm -f "$demo_context_file" "$demo_kubeconfig" "$demo_cluster_uid_file" \
    "$demo_namespace_uid_file"
}
umask 077
if ! mkdir -p "$demo_state_dir"; then
  echo "Failed to create the demo state directory" >&2
  exit 1
fi
if ! printf '%s\n' "$context" > "$demo_context_file"; then
  echo "Failed to write the demo context marker" >&2
  clear_demo_identity
  exit 1
fi
if ! kubectl --context "$context" config view --minify --flatten --raw \
  > "$demo_kubeconfig"; then
  echo "Failed to snapshot the demo kubeconfig" >&2
  clear_demo_identity
  exit 1
fi
if ! cluster_uid="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$context" get namespace kube-system \
  -o jsonpath='{.metadata.uid}')"; then
  echo "Failed to read the demo cluster identity" >&2
  clear_demo_identity
  exit 1
fi
if ! printf '%s\n' "$cluster_uid" > "$demo_cluster_uid_file"; then
  echo "Failed to write the demo cluster identity" >&2
  clear_demo_identity
  exit 1
fi
if kubectl --kubeconfig "$demo_kubeconfig" --context "$context" \
  get namespace shop >/dev/null 2>&1; then
  echo "Refusing to reuse existing namespace: shop" >&2
  clear_demo_identity
  exit 1
fi
if ! DEMO_KUBECONFIG="$demo_kubeconfig" DEMO_CONTEXT="$context" \
  DEMO_NAMESPACE_UID_FILE="$demo_namespace_uid_file" uv run --no-sync python - <<'PY'
import asyncio
import os

from kubernetes_asyncio import client, config


async def create_namespace() -> None:
    configuration = client.Configuration()
    await config.load_kube_config(
        config_file=os.environ["DEMO_KUBECONFIG"],
        context=os.environ["DEMO_CONTEXT"],
        client_configuration=configuration,
    )
    api_client = client.ApiClient(configuration)
    try:
        namespace = await client.CoreV1Api(api_client).create_namespace(
            body=client.V1Namespace(
                metadata=client.V1ObjectMeta(
                    name="shop",
                    labels={"korvid.dev/demo": "mcp-follow"},
                )
            )
        )
        namespace_uid = namespace.metadata.uid
        if not namespace_uid:
            raise RuntimeError("created namespace has no UID")
        try:
            fd = os.open(
                os.environ["DEMO_NAMESPACE_UID_FILE"],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(f"{namespace_uid}\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            await client.CoreV1Api(api_client).delete_namespace(
                "shop",
                body=client.V1DeleteOptions(
                    preconditions=client.V1Preconditions(uid=namespace_uid)
                ),
            )
            raise
    finally:
        await api_client.close()


asyncio.run(create_namespace())
PY
then
  echo "Failed to atomically create and record the demo namespace" >&2
  echo "Demo identity state retained for investigation" >&2
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
demo_kubeconfig="$demo_state_dir/mcp-demo-kubeconfig"
demo_cluster_uid_file="$demo_state_dir/mcp-demo-cluster-uid"
demo_namespace_uid_file="$demo_state_dir/mcp-demo-namespace-uid"
if [ ! -r "$demo_context_file" ] || [ ! -r "$demo_kubeconfig" ] ||
  [ ! -r "$demo_cluster_uid_file" ] || [ ! -r "$demo_namespace_uid_file" ]; then
  echo "Refusing MCP startup without the recorded demo identity" >&2
  exit 1
fi
prepared_context="$(cat "$demo_context_file")"
prepared_cluster_uid="$(cat "$demo_cluster_uid_file")"
prepared_namespace_uid="$(cat "$demo_namespace_uid_file")"
case "$prepared_context" in
  kind-*|k3d-*|minikube|docker-desktop) ;;
  *) echo "Refusing MCP startup on non-local context: $prepared_context" >&2; exit 1 ;;
esac
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
if ! namespace_uid="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$prepared_context" get namespace shop \
  -o jsonpath='{.metadata.uid}')"; then
  echo "Refusing MCP startup because namespace identity is unavailable" >&2
  exit 1
fi
if ! test "$namespace_uid" = "$prepared_namespace_uid"; then
  echo "Refusing MCP startup after namespace identity changed" >&2
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
if tmux has-session -t korvid-mcp-demo 2>/dev/null; then
  echo "Refusing to reuse existing tmux session: korvid-mcp-demo" >&2
  exit 1
fi
if ! tmux new-session -d -s korvid-mcp-demo -x 160 -y 45 -c "$PWD" \
  "HOME=\"$demo_home\" XDG_CONFIG_HOME=\"$demo_home/.config\" XDG_STATE_HOME=\"$demo_home/.local/state\" XDG_CACHE_HOME=\"$demo_home/.cache\" KUBECONFIG=\"$demo_kubeconfig\" korvid --mcp --namespace shop"; then
  echo "Failed to create the recording tmux session" >&2
  exit 1
fi
if ! tmux set-option -t korvid-mcp-demo status off; then
  tmux kill-session -t korvid-mcp-demo
  echo "Failed to configure the recording tmux session" >&2
  exit 1
fi
```

Wait until the left pane status shows `MCP on` and `·follow`, then register only
the three read tools used by the recording:

```sh
recording_server="korvid-mcp-demo"
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

Start Copilot in a 35-column right pane. Here
`--available-tools=korvid-mcp-demo` is Copilot CLI's server-level MCP selector:

```sh
tmux split-window -h -l 35 -t korvid-mcp-demo:0 -c "$PWD" \
  "copilot --disable-builtin-mcps --allow-all-tools --available-tools=korvid-mcp-demo"
tmux select-pane -t korvid-mcp-demo:0.1
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
demo_context_file="${XDG_STATE_HOME:-$HOME/.local/state}/korvid/mcp-demo-context"
demo_state_dir="$(dirname "$demo_context_file")"
demo_kubeconfig="$demo_state_dir/mcp-demo-kubeconfig"
demo_cluster_uid_file="$demo_state_dir/mcp-demo-cluster-uid"
demo_namespace_uid_file="$demo_state_dir/mcp-demo-namespace-uid"
if [ ! -r "$demo_context_file" ] || [ ! -r "$demo_kubeconfig" ] ||
  [ ! -r "$demo_cluster_uid_file" ] || [ ! -r "$demo_namespace_uid_file" ]; then
  echo "Refusing cleanup without the recorded demo identity" >&2
  exit 1
fi
prepared_context="$(cat "$demo_context_file")"
prepared_cluster_uid="$(cat "$demo_cluster_uid_file")"
prepared_namespace_uid="$(cat "$demo_namespace_uid_file")"
case "$prepared_context" in
  kind-*|k3d-*|minikube|docker-desktop) ;;
  *) echo "Refusing to clean non-local context: $prepared_context" >&2; exit 1 ;;
esac
if ! cluster_uid="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$prepared_context" get namespace kube-system \
  -o jsonpath='{.metadata.uid}')"; then
  echo "Refusing cleanup because cluster identity is unavailable" >&2
  exit 1
fi
if ! test "$cluster_uid" = "$prepared_cluster_uid"; then
  echo "Refusing cleanup after cluster identity changed" >&2
  exit 1
fi
if ! namespace_name="$(kubectl --kubeconfig "$demo_kubeconfig" \
  --context "$prepared_context" get namespace shop --ignore-not-found \
  -o jsonpath='{.metadata.name}')"; then
  echo "Refusing cleanup because namespace state is unknown" >&2
  exit 1
fi
namespace_exists=false
if [ -n "$namespace_name" ]; then
  if ! namespace_uid="$(kubectl --kubeconfig "$demo_kubeconfig" \
    --context "$prepared_context" get namespace shop \
    -o jsonpath='{.metadata.uid}')"; then
    echo "Refusing cleanup because namespace identity is unknown" >&2
    exit 1
  fi
  if ! test "$namespace_uid" = "$prepared_namespace_uid"; then
    echo "Refusing cleanup after namespace identity changed" >&2
    exit 1
  fi
  if ! owner="$(kubectl --kubeconfig "$demo_kubeconfig" \
    --context "$prepared_context" get namespace shop \
    -o jsonpath='{.metadata.labels.korvid\.dev/demo}')"; then
    echo "Refusing cleanup because namespace ownership is unknown" >&2
    exit 1
  fi
  if [ "$owner" != "mcp-follow" ]; then
    echo "Refusing cleanup of namespace not owned by this demo" >&2
    exit 1
  fi
  namespace_exists=true
else
  echo "Recording namespace is already absent; continuing cleanup" >&2
fi

if tmux has-session -t korvid-mcp-demo 2>/dev/null; then
  tmux kill-session -t korvid-mcp-demo
fi
registration_file="$(dirname "$demo_context_file")/mcp-demo-registration"
if [ -r "$registration_file" ]; then
  recording_server="$(cat "$registration_file")"
  if [ "$recording_server" != "korvid-mcp-demo" ]; then
    echo "Refusing to remove unexpected MCP registration: $recording_server" >&2
    exit 1
  fi
  if copilot mcp get "$recording_server" >/dev/null 2>&1; then
    if ! copilot mcp remove "$recording_server"; then
      echo "Failed to remove the recording MCP server; cleanup state retained" >&2
      exit 1
    fi
  fi
  rm -f "$registration_file"
elif copilot mcp get korvid-mcp-demo >/dev/null 2>&1; then
  echo "Refusing to remove an untracked korvid-mcp-demo registration" >&2
  exit 1
else
  echo "Recording MCP server is already absent; continuing cleanup" >&2
fi
if [ "$namespace_exists" = true ]; then
  if ! DEMO_KUBECONFIG="$demo_kubeconfig" DEMO_CONTEXT="$prepared_context" \
    DEMO_NAMESPACE_UID="$prepared_namespace_uid" uv run --no-sync python - <<'PY'
import asyncio
import os

from kubernetes_asyncio import client, config


async def delete_namespace() -> None:
    configuration = client.Configuration()
    await config.load_kube_config(
        config_file=os.environ["DEMO_KUBECONFIG"],
        context=os.environ["DEMO_CONTEXT"],
        client_configuration=configuration,
    )
    api_client = client.ApiClient(configuration)
    try:
        await client.CoreV1Api(api_client).delete_namespace(
            "shop",
            body=client.V1DeleteOptions(
                preconditions=client.V1Preconditions(
                    uid=os.environ["DEMO_NAMESPACE_UID"]
                )
            ),
        )
    finally:
        await api_client.close()


asyncio.run(delete_namespace())
PY
  then
    echo "Failed to delete the recording namespace; cleanup state retained" >&2
    exit 1
  fi
fi
demo_home="$(dirname "$demo_context_file")/mcp-demo-home"
rm -rf -- "$demo_home"
rm -f "$demo_context_file" "$demo_kubeconfig" "$demo_cluster_uid_file" \
  "$demo_namespace_uid_file"
```
