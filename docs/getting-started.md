# Getting started

Five minutes from an empty terminal to a running cockpit.

## Requirements

- **Python 3.11, 3.12, or 3.13** on Linux, macOS, or Windows.
- A working `kubeconfig` — korvid reads the same config, contexts, and
  credential plugins as `kubectl`.
- Optionally, `helm` on `PATH` for the [Helm and operators](helm-operators.md)
  views, and `kubectl` itself for shell/debug fallbacks.

korvid is an application, not a library: install it into its own isolated
environment rather than whatever Python happens to be active. `uv` and
`pipx` both do this for you; a bare `pip install --user` does not.

## Current release

**`0.1.2`** is the current published release and the version installed by
the commands below. A `0.2.0` release is in progress — it adds the
relationship graph, session timeline, Prometheus/Loki tooling, MCP 2.0
support, and Homebrew distribution described elsewhere in these docs — but
until it is tagged and published, treat any `0.2.0` install command as
release-candidate material, not something to run yet. Watch the
[release notes](release-notes/v0.2.0.md) for the publish announcement.

## Install

### Homebrew (macOS and Linux)

Once the Homebrew tap is published for a release, this is the fastest path
to the TUI and embedded agent:

```sh
brew install hellices/korvid/korvid
```

The formula deliberately excludes the MCP HTTP server — install with `uv` or
`pipx` below if you need `korvid --mcp`.

### `uv tool` (recommended cross-platform)

```sh
uv tool install 'korvid[all]==0.1.2'
korvid
```

`uv` fetches a suitable Python interpreter automatically, which matters on
macOS and most enterprise Linux where the system Python predates 3.11.

### `pipx`

```sh
pipx install 'korvid[all]==0.1.2'
korvid
```

## Choose your extras

`korvid` alone installs only the TUI. Add extras for the agent, MCP server,
or both:

| Install | Adds |
| --- | --- |
| `korvid==0.1.2` | The keyboard-first TUI only — no agent, no MCP server |
| `korvid[agent]==0.1.2` | The embedded agent (`Ctrl-A`, `:ai`, `:model`) |
| `korvid[mcp]==0.1.2` | The MCP server (`korvid --mcp`) |
| `korvid[all]==0.1.2` | Both agent and MCP — the recommended first install |
| `korvid[all,entra]==0.1.2` | Everything above, plus Entra ID auth for Azure OpenAI |

Extras do not expand in place: if you installed `korvid` alone and later want
the agent, reinstall with the full extra set you want rather than layering
extras on top of an existing install.

## First run

```sh
korvid
```

korvid opens on the pods table for your current `kubeconfig` context and
namespace. No agent, no MCP server, and no network calls beyond the
Kubernetes API happen unless you ask for them.

- Press `:` and type a resource name (`pods`, `deploy all`, `helm`, …) to
  navigate.
- Press `?` any time for the full in-app help overlay.
- Press `q` or `Ctrl-C` to quit.

## Ten keys to get moving

| Key | Does |
| --- | --- |
| `:` | Open the command bar (`pods`, `ns <name>`, `ctx <name>`, `ai`, `q`, …) |
| `?` | Open the help overlay — every keybinding, in context |
| `/` | Filter the current table (fuzzy, `/regex/`, or `-l label=value`) |
| `Enter` | Drill into the selected resource (pods → containers, deploy → pods, …) |
| `Esc` | Pop back up one drill-down level |
| `d` | Describe the selected resource (manifest + events) |
| `l` | Open the log pane for the selected pod |
| `g` | Show the operational relationship graph for the selected resource |
| `Ctrl-A` | Open the embedded agent panel *(requires the `[agent]` extra)* |
| `Ctrl-D` | Delete the selected resource, with a confirm dialog |

The full, always-current key reference — grouped by context, including
node, Helm, and operator actions — lives in [keybindings.md](keybindings.md).

## Where to next

- **Learn the cockpit:** [Browsing the cluster](tui.md) covers tables,
  filters, custom columns, the split workspace, and the log viewer in depth.
- **Add the agent:** [The embedded agent](agent.md) explains what it can see,
  which tools it can call, and how to point it at a provider.
- **Connect an external client:** [The MCP server](mcp.md) exposes the same
  read and UI-drive tools to VS Code, Claude Code, Cursor, or Zed.
- **Running without internet access?** [Air-gapped operation](airgap.md)
  covers offline bundles and internal trust configuration.
