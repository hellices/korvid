# Getting started

Five minutes from an empty terminal to a running cockpit.

## Requirements

- **Python 3.11 or newer** on Linux, macOS, or Windows. CI currently
  qualifies 3.11, 3.12, and 3.13.
- A working `kubeconfig` — korvid reads the same config, contexts, and
  credential plugins as `kubectl`.
- The [Helm and operator views](helm-operators.md) do not require the Helm CLI;
  they read release data from the cluster. Helm write actions require `helm`
  on `PATH`, while shell/debug fallbacks require `kubectl`. korvid looks for
  `kubectl` once per session, at startup: install it while korvid is running
  and restart to pick it up.

korvid is an application, not a library: install it into its own isolated
environment rather than whatever Python happens to be active. `uv` and
`pipx` both do this for you; a bare `pip install --user` does not.

## Current release

Homebrew installs the version currently published by the live tap. The
`uv tool` and `pipx` commands below install the latest published package
from PyPI. Check the
[latest release](https://github.com/hellices/korvid/releases/latest) for
its version, pinned installation commands, and migration notes. Most
users should stop at **Install**; use **Development build** only when you
need an unreleased change from `main`.

## Install

### Homebrew (macOS and Linux)

The live Homebrew tap needs no system Python:

```sh
brew install hellices/korvid/korvid
```

The tap follows its own reviewed update schedule;
`brew info hellices/korvid/korvid` shows the available version. The
formula builds against Homebrew's Python and deliberately excludes MCP.
Use `uv tool` or `pipx` below for the MCP server and stdio adapter.

### `uv tool` (recommended cross-platform)

```sh
uv tool install 'korvid[all]'
korvid
```

`uv` fetches a suitable Python interpreter automatically, which matters on
macOS and most enterprise Linux where the system Python predates 3.11.

### `pipx`

```sh
pipx install 'korvid[all]'
korvid
```

## Choose your extras

`korvid` alone installs only the TUI. Add extras for the agent, MCP server,
or both:

| Install | Adds |
| --- | --- |
| `korvid` | The keyboard-first TUI only — no agent, no MCP server |
| `korvid[agent]` | The embedded agent (`Ctrl-A`, `:ai`, `:model`) |
| `korvid[mcp]` | The MCP server (`korvid --mcp`) and local adapter (`korvid mcp stdio`) |
| `korvid[agent,observability]` | Embedded agent plus Prometheus/Loki investigation tools |
| `korvid[mcp,observability]` | MCP server plus Prometheus/Loki investigation tools |
| `korvid[all]` | Agent, MCP, and observability connectors — the recommended first install |
| `korvid[all,entra]` | Everything above, plus Entra ID auth for Azure OpenAI |

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

## Development build

The guides describe `main`, which can include changes not yet published.
To test the reviewed source instead of the latest PyPI release:

```sh
uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'
```

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

## Practice on a throwaway cluster

To try writes and the agent without risking a real cluster, create a
disposable one with [kind](https://kind.sigs.k8s.io/) and load three small
workloads: one healthy, two broken. You need Docker or Podman, `kind`, and
`kubectl`.

```sh
playground="$(mktemp -d)" &&
  cd "$playground" &&
  export KUBECONFIG="$PWD/korvid-playground.kubeconfig" &&
  kind create cluster --name korvid-playground &&
  curl -fsSLO https://raw.githubusercontent.com/hellices/korvid/main/deploy/playground/workloads.yaml
```

The block works in a new, empty directory, so the download cannot overwrite
a file of yours, and gives this terminal its own kubeconfig file there. kind
writes the new context into that file, so `kubectl` and korvid in this
terminal see only the playground cluster, and your usual kubeconfig is not
touched. Each step runs only if the one before it succeeded. Creating the
cluster takes a minute or two. If a cluster with this name is left from an
earlier try, the block stops there: delete it with
`kind delete cluster --name korvid-playground`, then run the block again.

Read `workloads.yaml` before you apply it. It creates only a `playground`
namespace, three Deployments, and one Service, and none of them needs host
access, privileges, or an API token. Then, in the same terminal, apply it
and start korvid:

```sh
cd "${playground:?}" && test "$KUBECONFIG" = "$PWD/korvid-playground.kubeconfig" &&
  kubectl --context kind-korvid-playground apply -f workloads.yaml &&
  korvid -n playground
```

The first line stops the block unless this terminal is still in the
playground directory with its own kubeconfig, so a setup that failed cannot
send korvid to your usual clusters, and korvid starts only after the apply
succeeds.

korvid's status row at the bottom shows the context, `kind-korvid-playground`.
These commands are for bash or zsh. In PowerShell, first create a new, empty
directory and enter it, then set the variable with
`$env:KUBECONFIG = "$PWD\korvid-playground.kubeconfig"` and download with
`curl.exe`. Before you apply the manifest or start korvid, check that
`$env:KUBECONFIG` still names the file in that directory. When you are done,
delete the cluster, leave the directory, and remove it.

| Try | How |
| --- | --- |
| Find what is wrong | Type `:pulse`: the [problems view](pulse.md) lists the crash loop and the failed image pull, with their evidence |
| Read why `checkout` crashes | Select its pod and press `l`; in the log pane, `p` shows the previous container's output |
| See why `payments` never starts | Select its pod and press `d`; the events name the image that cannot be pulled |
| Trace `web` | Select a `web` pod and press `g` for the ReplicaSet that owns it, the Node it runs on, and the Service that selects it |
| Approve a write | Type `:deploy`, select `web`, press `S`, and scale to 3. Nothing changes until you confirm, and `:outcomes` then shows what the Deployment controller did ([operations](ops.md)) |
| Ask the agent | With the `[agent]` extra and a model from `:ai`, press `Ctrl-A` and ask why `checkout` is failing ([agent](agent.md)) |

When you are done, delete the cluster and then the directory with its two
files:

```sh
kind delete cluster --name korvid-playground &&
  cd && rm -r "${playground:?}"
```

If deleting the cluster fails, the directory stays, so you can try again
with the same kubeconfig.

Then open a new terminal to return to your usual clusters: `KUBECONFIG` was
set only in this one.

## Where to next

- **Learn the cockpit:** [Browsing the cluster](tui.md) covers tables,
  filters, custom columns, the split workspace, and the log viewer in depth.
- **Add the agent:** [The embedded agent](agent.md) explains what it can see,
  which tools it can call, and how to point it at a provider.
- **Connect an external client:** [The MCP server](mcp.md) exposes the same
  read and UI-drive tools to VS Code, Claude Code, Cursor, or Zed.
- **Running without internet access?** [Air-gapped operation](airgap.md)
  covers offline bundles and internal trust configuration.
