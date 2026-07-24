# korvid

> A tool-using bird for your cluster.

AI-native Kubernetes TUI — a k9s-style keyboard-first cockpit with an embedded LLM agent that drives the UI, diagnoses issues, and executes approved commands.

*Corvids are the only birds known to use tools. So does this one.*

## Status

Work in progress — core TUI, log viewer, and read-only agent runtime functional.

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
listings.  It cannot mutate anything in this slice — no create, patch, delete,
or exec.

Tool results are capped at 8,000 characters and `Secret` data is masked before
it ever reaches the model.  The header shows the model name and cumulative
token usage (`~` marks estimated counts when the provider omits usage data).

Configure any OpenAI-compatible endpoint (Ollama, vLLM, Azure OpenAI, OpenAI)
in `~/.config/korvid/config.yaml`:

```yaml
agent:
  provider: openai-compat
  base_url: http://localhost:11434/v1
  model: llama3
  api_key_env: KORVID_API_KEY   # optional — name of the env var holding the key
```

Without configuration, `Ctrl-A` shows this setup hint instead of a prompt.
