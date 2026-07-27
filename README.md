# korvid

> A tool-using bird for your cluster.

AI-native Kubernetes TUI — a keyboard-first cockpit with an embedded LLM agent that drives the UI, diagnoses issues, and executes approved commands.

*Corvids are the only birds known to use tools. So does this one.*

## Status

Work in progress — core TUI, log viewer, live metrics, MCP server, and agent
runtime are functional. Read-heavy by design: cluster writes (delete / scale /
rollout restart / edit / node cordon & drain) exist but every one is
approval-gated and audited — delete, scale, rollout restart, and cordon
additionally show a server dry-run preview in the dialog, and drain shows a
PDB-aware impact plan; `--readonly` disables them all.

## Keybindings

| Key | Context | Action |
|-----|---------|--------|
| `:` | global | Open command bar — accepts `pods`, `deploy all`, `helm`, `ns <name>`, `ai`, `model`, `q` |
| `?` | global | Help overlay — keybindings grouped by context plus `:` commands (Esc/q/`?` closes) |
| `/` | table | Open filter — name, `~fuzzy`, `/regex/`, `!exclude`, `-l k=v`, `-s` hide Completed (Enter keeps, Esc clears) |
| `/` | log pane | Open inline log search |
| `Enter` | table | Drill down: pods → containers; deploy → replicasets (history) → pods; helm release → revisions |
| `Esc` | table | Pop one drill-down level |
| `Shift-N/A/C/M` | table | Sort by name / age / CPU / MEM (repeat flips ▲/▼; sorts on data, not rendered strings) |
| `0` | global | Toggle all-namespaces view |
| `d` | table | Describe selected resource (manifest + events) |
| `s` | pods table | Shell into selected pod (`kubectl exec`; offers `kubectl debug` fallback for distroless images) |
| `Shift-F` | pods / services table | Port-forward the selected target (local port prompt; prefilled from declared ports) |
| `l` | pods table | Open / close log pane for selected pod |
| `L` | pods table | Merge logs of all currently filtered pods (up to 8) |
| `f` | log pane | Toggle JSON-formatted / raw display |
| `w` | log pane | Toggle line wrap |
| `t` | log pane | Toggle kubelet-timestamp prefix |
| `Ctrl-S` | log pane | Save the current buffer to `$XDG_DATA_HOME/korvid/logs/` (default `~/.local/share/korvid/logs/`) |
| `p` | log pane | Reload pane with previous (terminated) container logs |
| `n` / `N` | log pane | Jump to next / previous search hit |
| `Ctrl-D` | table | Delete selected resource (confirm dialog; cluster-scoped kinds require typing the name) |
| `r` | table | Rolling restart of selected deployment / statefulset / daemonset (confirm dialog) |
| `S` | table | Scale selected deployment / replicaset / statefulset (replica prompt + confirm dialog) |
| `R` | pods table | In-place resize of pod CPU/memory requests/limits (Kubernetes 1.35+; prompt + confirm dialog) |
| `I` | operators tables | Install the selected catalog operator (wizard + confirm dialog) or approve a pending InstallPlan |
| `c` / `u` | nodes table | Cordon / uncordon the selected node (confirm dialog with server dry-run preview) |
| `Shift-D` | nodes table | Drain the selected node — PDB-aware impact preview (evictions, PDB-blocked pods, skipped DaemonSet/mirror pods, emptyDir warnings), typed-name confirm, live progress; press again to cancel mid-drain (node stays cordoned) |
| `e` | table | Edit selected resource manifest in `$VISUAL`/`$EDITOR` (kubectl edit style; confirm dialog before the PUT) |
| `i` | pods table | Open hint details overlay for a troubled pod (full container trouble + recent Warning events) |
| `Ctrl-T` | pods table | Transfer a file to/from the selected container (exec tar stream; upload needs approval) |
| `Ctrl-A` | global | Toggle AI agent panel |
| `q` | global | Quit |
| `Esc` | log pane | Close pane (or dismiss search / filter bar) |

### Remapping keys

App-level actions can be remapped via the `keybindings:` section of
`~/.config/korvid/config.yaml`, mapping an **action name** from the list
below to a new key (Textual key syntax — `x`, `f1`, `ctrl+q`, `shift+g`).
Keys handled outside bindings (`Enter` drill-down, `Esc` close/pop, and the
dialogs' own keys) are not remappable:

```yaml
keybindings:
  delete_resource: ctrl+x   # free Ctrl-D for the terminal
  sort_by_age: g
```

Action names: `quit`, `help`, `open_command`, `open_filter`,
`toggle_all_namespaces`, `describe`, `shell`, `logs`, `logs_multi`,
`log_format`, `log_wrap`, `log_timestamps`, `log_save`, `log_previous`,
`log_search_next`, `log_search_prev`, `sort_by_age`, `sort_by_cpu`,
`sort_by_mem`, `toggle_agent`, `delete_resource`, `rollout_restart`,
`resize_pod`, `scale_resource`, `edit_resource`, `hint_details`, `operator_install`,
`cordon_node`, `uncordon_node`, `drain_node`, `port_forward`, `transfer`.

Unknown actions, duplicate keys, and keys that shadow another action's
default produce a startup warning and are skipped — never a crash. The
approval dialogs' confirm keys are **not remappable** by design: writes are
only ever confirmed by the fixed keystrokes. The help overlay (`?`) always
shows the effective keys.

## Live metrics

The pods table shows live `CPU` / `MEM` usage and `%CPU/R` / `%MEM/R`
(usage as a percentage of the declared request) from the `metrics.k8s.io`
API, polled every 15 seconds while the pods view is on screen.  The number
is always relative to the request, but the colour keys off enforced
**limits**: running above the request is normal bursting, while approaching
an enforced limit means OOMKill (memory) or throttling (CPU) territory.
Every applicable ceiling is checked — each container against its own limit
(the kubelet enforces them independently) and, on K8s 1.34+, the pod
aggregate against the pod-level limit — and the most severe colour wins
(green &lt; 70 % &le; yellow &lt; 90 % &le; red).  Only when no limit bounds
the usage does the colour fall back to the request ratio, capped at yellow
(bursting without a ceiling is expected, never critical).
On clusters without metrics-server the columns show `-` and korvid keeps
polling, so a later install is picked up without a restart.

## Ops hints

When the cursor lands on a troubled pod row (CrashLoopBackOff,
ImagePullBackOff, failing readiness, …) a hint strip appears under the
table with up to two concise lines built from the pod's container statuses
and its freshest Warning event — verbatim API data, no synthesized
diagnoses.  Anything that does not fit folds behind `+N more (i: details)`;
press `i` to open a read-only overlay with every troubled container (full
message, exit code, restart count, last-seen age) and the pod's recent
Warning events.

## Helm release browser

`:helm` lists the Helm releases installed in the current namespace — no
helm binary required (press `0` to widen the view to all namespaces).
korvid reads the release Secrets Helm 3 stores in the cluster
(`type=helm.sh/release.v1`) and decodes them in place, showing name,
revision, status, chart, and app version, live-updated through the same
watch pipeline as any other view.  `Enter` drills into the release's
revision history (newest first, like `helm history`); `d` describes a
release or a single revision with the decoded metadata and the
user-supplied values — the rendered manifest blob is deliberately left
out.  This slice is read-only; install/upgrade/rollback lands separately.

## Operator catalog (OLM)

Where [OLM](https://olm.operatorframework.io/) is installed, `:operators`
opens the operator catalog cluster-wide (catalog entries live in catalog
namespaces, so the view defaults to all namespaces; `:operators <ns>`
scopes it): every PackageManifest the cluster's catalog
sources serve, with its catalog, default channel, and available channels.
`:subscriptions`, `:csv`, and `:installplans` show the installed side with
typed columns (subscription channel/state, CSV version/phase).  Without
OLM, `:operators` explains what is missing instead of erroring.

Press `I` on a catalog row to install: a wizard collects the target
namespace, channel (validated against the package's own channel list),
and approval mode — then the **full Subscription manifest** is shown in
the standard approval dialog before anything is created.  On an
InstallPlan row, `I` approves a pending manual plan (the dialog lists the
CSVs the approval unblocks).  Everything shown comes from the cluster's
catalog objects — korvid hardcodes no operator knowledge — and both
writes go through the same SSAR pre-check, approval gate, and fail-closed
audit log as every other mutation.  The AI agent gets a matching read-only
`list_operators` tool (catalog + installed subscriptions); installing
stays behind the UI approval gate.

## Log viewer

The log pane supports multi-container merge: press `L` to stream logs from every
visible pod simultaneously with `[pod/container]` prefixes.  Lines that look like
JSON are auto-detected and rendered with colour highlighting; press `f` to toggle
between the formatted and raw views.  Press `p` to reload the pane with logs from
the previous (terminated) container instance.

Live streams reconnect automatically on transient errors or unexpected EOF.  After
five consecutive reconnect attempts without a successful line the header shows an
error state and a notification is raised.  The in-memory ring buffer retains the
last 5000 lines; when it overflows a one-time banner is written to the pane so you
know older lines were dropped.

## File transfer

`Ctrl-T` on a pod opens a transfer dialog (multi-container pods show a
container picker first): pick a direction, the remote path in the container,
and a local path — leaving the local path empty on a download saves to
`~/Downloads/<name>`, or to `~/<name>` when no `Downloads` directory exists.

Transfers ride the exec API as a tar stream, so there is no dependency on a
`kubectl` binary — but the container must have `tar` (the server's error is
shown verbatim when it doesn't, e.g. distroless images). A progress modal
shows the byte count as the stream advances; `Esc` cancels the transfer, and
a cancelled or failed download never leaves a half-written local file.

Uploads write into the container filesystem, so they are blocked in
read-only mode and pass the same approval dialog as every other write.
Both directions are audit-logged fail-closed (pod, container, paths,
direction, byte count): if the audit entry cannot be written, the transfer
does not run. Recursive directory sync is out of scope, and the agent has no
transfer tool — transfers are always user-driven.

## Debug fallback

When `s` lands in a container without a shell (distroless), korvid offers to
attach an ephemeral container via `kubectl debug` instead.  The image picker
is runtime-aware: the target container's image, ports, and env vars are
matched against known runtime signals (all local heuristics — no network
calls), and a detected JVM / Python / Node.js / Go runtime leads with the
matching [KoolKits](https://github.com/lightrun-platform/koolkits) toolkit
image, followed by `nicolaka/netshoot` for network debugging and
`busybox:1.36` as the minimal fallback.  A custom image can always be typed
in.  The chosen image appears in the approval dialog and in the audit log
entry.

If the chosen image cannot be pulled (`ErrImagePull` / `ImagePullBackOff`
within the first 30 seconds), the hung attach is killed and a retry with the
fallback image is offered — the failed ephemeral container entry stays in the
pod spec (Kubernetes does not allow removing it); the retry attaches an
additional container.

For air-gapped clusters or private registries, configure the images in
`~/.config/korvid/config.yaml`; when `debug.images` is set only configured
images are offered:

```yaml
debug:
  default_image: registry.corp.local/tools/busybox:1.36
  images:
    jvm: registry.corp.local/tools/debug-jvm:latest
    python: registry.corp.local/tools/debug-python:latest
```

## Port-forwarding

`Shift-F` on a pod or service opens a port-forward dialog with the remote
port prefilled from the target's declared TCP ports — `kubectl port-forward`
is TCP-only, so UDP/SCTP declarations are never offered and a service that
declares no TCP ports is rejected up front.  For pods both fields stay
fully editable — pod port declarations are informational and any remote port
is forwardable.  For services kubectl only accepts remote ports declared in
`Service.spec.ports`, so the dialog constrains the remote port to the
declared TCP ports.
Forwards run as `kubectl port-forward`
subprocesses bound to `127.0.0.1`, pinned to the kubeconfig context korvid
connected with, and are tracked for the session: `:pf` lists them with live
status, `Ctrl-D` stops the highlighted forward, and `r` re-attaches a broken
one in place.

Liveness is first-class: when a target pod dies the forward flips to
`broken` — with a toast even while `:pf` is closed — instead of failing
silently the way a hand-run `kubectl port-forward` does.  Every forward is
torn down when korvid exits, and start/stop are audit-logged (no approval
dialog: a forward reads from the cluster, it never mutates it).

## AI agent

Press `Ctrl-A` to open the agent panel — a chat sidebar that answers questions
about the cluster you are looking at.  The agent sees your current screen
context (view, namespace, selected resource, active filter) and inspects the
cluster through read-only tools: fetching manifests, logs, events, and resource
listings.  It can also drive the TUI itself — navigate views, apply filters,
drill down, and open the log pane or describe screen — so "show me the crashing
pod's logs" lands you in the actual log viewer instead of a text dump.
Tool results are capped at 8,000 characters and `Secret` data is masked before
it ever reaches the model.  The header shows the model name and cumulative
token usage (`~` marks estimated counts when the provider omits usage data).

The agent can also *request* write operations — delete, scale, rollout
restart, and (on clusters that expose the `pods/resize` subresource,
Kubernetes 1.35+) in-place pod resize — but it can never execute them
itself.  Each request opens
the same confirmation dialog as the keybindings (marked with a ⚠ in the tool
log), and only your keystroke in that dialog approves it; an unanswered
dialog expires without executing anything.  Every executed write — yours or
agent-requested — is recorded in an audit log at
`$XDG_STATE_HOME/korvid/audit.jsonl` (defaulting to
`~/.local/state/korvid/audit.jsonl` when `XDG_STATE_HOME` is unset;
0600 permissions, size-rotated).  If the audit entry cannot be written, the
write is blocked.

### Cloud-provider awareness

At startup korvid detects the cluster's cloud provider from
`node.spec.providerID` prefixes and well-known managed-cluster node labels
(AKS, EKS, GKE) — no Kubernetes API lists valid cloud annotations, so korvid
ships **no annotation catalog**; the detected provider is injected into the
agent's system context instead.  Ask "expose this service publicly" on an AKS
cluster and the agent proposes Azure-appropriate load balancer annotations
without you naming the CSP, applied through the same approval-gated write
flow.  Describing a `Service` or `Ingress` on a detected provider shows a
one-line footer pointing at the agent (`provider: aks — ask the agent about
load balancer annotations (ctrl+a)`).  Detection is a bounded, cached,
best-effort probe: RBAC-limited users (no node list permission), bare-metal,
and local clusters simply detect as "unknown" and nothing changes.

### Dry-run previews

Before a delete, scale, resize, cordon/uncordon, or rollout restart dialog opens, korvid replays the
write server-side with `dryRun=All` and shows the reported outcome inside the
dialog: a compact diff for scale and restart (`~ spec.replicas: 3 -> 5`,
additions green, removals red) and an object summary plus cascading note for
delete.  Admission webhooks and validation run during the dry-run, so the
preview is a point-in-time server evaluation of the mutation the approved
write will replay (the preview request additionally pins the object's
`resourceVersion` so it evaluates exactly the revision on screen; the
executed write sends no such pin).  The cluster can still change between
preview and execution (admission runs again then), and a uid precondition
rejects writes against a replaced object.  If the round trip fails or
takes longer than a few seconds, the dialog simply opens without a preview —
a preview never blocks the approval flow.

A node drain dialog shows a different kind of preview: the impact plan
computed before any eviction is issued — pods to be evicted, pods whose
eviction a PodDisruptionBudget currently refuses, skipped DaemonSet and
mirror (static) pods, and emptyDir data-loss warnings. Drain executes
through the Eviction API; PDB-refused evictions (HTTP 429) surface as live
warnings instead of hanging, and pressing the drain key again cancels the
remaining evictions while the node stays cordoned. After the evictions are
accepted, drain waits (bounded) for the pods to actually leave the node —
pods that linger past the deadline are reported as a partial outcome.

### Read-only mode

Start with `korvid --readonly` (or set `readonly: true` in
`~/.config/korvid/config.yaml`) to disable all cluster writes: the
keybindings above are rejected and the write tools are never offered to the
model.

### Setup

The quickest way to configure the agent is inside the TUI: type `:ai`
(alias `:agent`) to open the setup wizard.  It walks through provider,
authentication, connection details, runs a live test call, and saves the
result to `~/.config/korvid/config.yaml`.  Use `:model <name>` to switch
models later without re-running the wizard (`:model` alone shows the
current model).

Each provider supports the auth method that fits it — GitHub device login,
Microsoft Entra ID, an API key from the environment, or no auth at all:

```yaml
# GitHub Copilot (log in via :ai inside korvid — no PAT needed)
agent:
  provider: github-copilot
  model: gpt-4o
  auth: {method: device-login}

# Azure OpenAI / AI Foundry with Entra ID (az login or managed identity)
agent:
  provider: azure
  base_url: https://YOUR-RESOURCE.openai.azure.com/openai/v1
  model: gpt-4o
  auth: {method: entra}

# Any OpenAI-compatible endpoint with an API key from the environment
agent:
  provider: openai-compat
  base_url: https://api.openai.com/v1
  model: gpt-4o-mini
  api_key_env: OPENAI_API_KEY
  auth: {method: api_key}

# Local Ollama (native /api/chat — no auth)
agent:
  provider: ollama
  base_url: http://localhost:11434
  model: llama3
  auth: {method: none}
```

`provider: ollama` talks to Ollama's native `/api/chat` API instead of the
OpenAI-compatibility shim, which unlocks per-request tuning the shim drops
(a shim-era `base_url` ending in `/v1` keeps working — it is normalized
automatically).  All knobs are optional:

```yaml
agent:
  provider: ollama
  base_url: http://localhost:11434
  model: qwen3:8b
  ollama:
    num_ctx: 16384    # context window; Ollama's own default can be as low as 4k
    temperature: 0.0  # near-greedy decoding — more reliable tool dispatch
    seed: 42          # reproducible sampling (omitted when unset)
    think: false      # reasoning tokens off; enable for R1-style models
    keep_alive: 10m   # keep the model warm between turns ("10m" or seconds)
```

To keep using the OpenAI-compatibility shim instead, set
`provider: openai-compat` with `base_url: http://localhost:11434/v1`.

> **Warning:** GitHub Copilot support uses an unofficial internal API that
> may change or break without notice.  It requires an active GitHub Copilot
> subscription.

Entra ID auth needs the optional extra: `pip install korvid[entra]`
(or `uv sync --extra entra` for development).  Configs written before
`agent.auth` existed keep working: `api_key_env` implies
`auth: {method: api_key}`.

More OpenAI-compatible endpoints:

```yaml
# GitHub Models (any GitHub account; uses a PAT with `models: read` scope)
agent:
  provider: github
  base_url: https://models.github.ai/inference
  model: openai/gpt-4o-mini
  api_key_env: GITHUB_TOKEN

# Anthropic Claude (OpenAI SDK compatibility endpoint)
agent:
  provider: anthropic
  base_url: https://api.anthropic.com/v1
  model: claude-sonnet-4-5
  api_key_env: ANTHROPIC_API_KEY

# vLLM / any self-hosted OpenAI-compatible server
agent:
  provider: vllm
  base_url: http://localhost:8000/v1
  model: meta-llama/Llama-3.1-8B-Instruct
```

`api_key_env` names the environment variable holding the key — the key itself
never lives in the config file.  OAuth tokens from device login are stored in
the OS keyring (falling back to a `0600` file at
`~/.config/korvid/credentials.json`).  Claude Code is a CLI product, not an
API — use the Anthropic API entry above for Claude models.

Without configuration, `Ctrl-A` shows a setup hint pointing at `:ai`.

### External MCP hosts

Start with `korvid --mcp` (or set `mcp: {enabled: true}` in
`~/.config/korvid/config.yaml`) to expose the read and UI-drive tools to
external agents — VS Code Copilot Chat, Claude Code, Cursor, Zed — over a
[Streamable HTTP MCP](https://modelcontextprotocol.io) server bound to
`127.0.0.1:7878` (`mcp: {port: N}` to change).  External hosts can list
resources, fetch manifests, logs, and events, and drive the TUI you are
looking at (navigate, filter, open logs/describe).  Write tools are **not**
exposed: cluster mutations stay behind the in-TUI confirmation dialog.

The live endpoint is also published to
`$XDG_STATE_HOME/korvid/mcp-endpoint.json` (defaulting to
`~/.local/state/korvid/mcp-endpoint.json` when `XDG_STATE_HOME` is unset)
while korvid runs.  The file is a registry keyed by process id, so multiple
korvid instances can be discovered side by side:

```json
{"servers": {"12345": {"url": "http://127.0.0.1:7878/mcp", "port": 7878, "pid": 12345}}}
```

**VS Code** (`.vscode/mcp.json`):

```json
{"servers": {"korvid": {"type": "http", "url": "http://127.0.0.1:7878/mcp"}}}
```

**Claude Code:**

```sh
claude mcp add --transport http korvid http://127.0.0.1:7878/mcp
```

**Cursor** (`.cursor/mcp.json`):

```json
{"mcpServers": {"korvid": {"type": "http", "url": "http://127.0.0.1:7878/mcp"}}}
```

**Zed** (`settings.json`):

```json
{"context_servers": {"korvid": {"url": "http://127.0.0.1:7878/mcp"}}}
```

## Installation

Not yet on PyPI. Install straight from the repository:

```sh
uv tool install git+https://github.com/hellices/korvid   # or: pipx install ...
korvid
```

Or run it ad hoc without installing:

```sh
uvx --from git+https://github.com/hellices/korvid korvid
```

### Development

```sh
git clone https://github.com/hellices/korvid && cd korvid
uv sync --dev          # create .venv with locked deps
uv run korvid          # run against your current kubeconfig context
make check             # lint + mypy --strict + tach + tests
```

