# korvid

> A tool-using bird for your cluster.

AI-native Kubernetes TUI — a k9s-style keyboard-first cockpit with an embedded LLM agent that drives the UI, diagnoses issues, and executes approved commands.

*Corvids are the only birds known to use tools. So does this one.*

## Status

Work in progress — core TUI, log viewer, and agent runtime functional.
Read-heavy by design: cluster writes (delete / scale / rollout restart) exist
but every one is approval-gated and audited; `--readonly` disables them all.

## Keybindings

| Key | Context | Action |
|-----|---------|--------|
| `:` | global | Open command bar — accepts `pods`, `deploy all`, `ns <name>`, `q` |
| `/` | table | Open name filter (Enter keeps filter, Esc clears) |
| `/` | log pane | Open inline log search |
| `0` | global | Toggle all-namespaces view |
| `d` | table | Describe selected resource (manifest + events) |
| `s` | pods table | Open shell inside selected pod via kubectl exec |
| `l` | pods table | Open / close log pane for selected pod |
| `L` | pods table | Merge logs of all currently filtered pods (up to 8) |
| `f` | log pane | Toggle JSON-formatted / raw display |
| `p` | log pane | Reload pane with previous (terminated) container logs |
| `n` | log pane | Jump to next search hit |
| `N` | log pane | Jump to previous search hit |
| `Ctrl-D` | table | Delete selected resource (confirm dialog; cluster-scoped kinds require typing the name) |
| `r` | table | Rolling restart of selected deployment / statefulset / daemonset (confirm dialog) |
| `S` | table | Scale selected deployment / replicaset / statefulset (replica prompt + confirm dialog) |
| `Ctrl-A` | global | Toggle AI agent panel |
| `q` | global | Quit |
| `Esc` | log pane | Close pane (or dismiss search / filter bar) |

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

## AI agent

Press `Ctrl-A` to open the agent panel — a chat sidebar that answers questions
about the cluster you are looking at.  The agent sees your current screen
context (view, namespace, selected resource, active filter) and inspects the
cluster through read-only tools: fetching manifests, logs, events, and resource
listings.

The agent can also *request* three write operations — delete, scale, and
rollout restart — but it can never execute them itself.  Each request opens
the same confirmation dialog as the keybindings (marked with a ⚠ in the tool
log), and only your keystroke in that dialog approves it; an unanswered
dialog expires without executing anything.  Every executed write — yours or
agent-requested — is recorded in an audit log at
`$XDG_STATE_HOME/korvid/audit.jsonl` (defaulting to
`~/.local/state/korvid/audit.jsonl` when `XDG_STATE_HOME` is unset;
0600 permissions, size-rotated).  If the audit entry cannot be written, the
write is blocked.

### Read-only mode

Start with `korvid --readonly` (or set `readonly: true` in
`~/.config/korvid/config.yaml`) to disable all cluster writes: the
keybindings above are rejected and the write tools are never offered to the
model.

Tool results are capped at 8,000 characters and `Secret` data is masked before
it ever reaches the model.  The header shows the model name and cumulative
token usage (`~` marks estimated counts when the provider omits usage data).

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

# Local Ollama (no auth)
agent:
  provider: ollama
  base_url: http://localhost:11434/v1
  model: llama3
  auth: {method: none}
```

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

The live endpoint is also written to
`$XDG_STATE_HOME/korvid/mcp-endpoint.json` (defaulting to
`~/.local/state/korvid/mcp-endpoint.json` when `XDG_STATE_HOME` is unset)
while korvid runs.

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

