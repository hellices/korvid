# What korvid is

> A tool-using bird for your cluster.

korvid is one program that grows with what you install. The base is a
keyboard-first Kubernetes cockpit that needs nothing but a kubeconfig. Add an
extra and it gains an embedded AI agent. Add another and it becomes a tool
*other* AI clients can drive.

One core, independent adapters, one safety model.

---

## The shape

```mermaid
flowchart LR
    HUMAN["👤 Human operator"]
    MODEL["🤖 Model / provider<br/>Copilot · Azure · Anthropic<br/>OpenAI · Ollama · compatible"]
    CLIENT["🖥 Editor / external assistant<br/>VS Code · Claude Code<br/>Cursor · Zed"]
    CLUSTER[("☸ Kubeconfig +<br/>cluster")]

    subgraph KORVID["KORVID — product boundary"]
        direction LR
        TUI["TUI adapter<br/><b>INCLUDED</b><br/>browse · filter · describe<br/>logs · port-forward · exec"]
        AGENT["Agent adapter<br/><b>OPTIONAL</b> korvid[agent]<br/>Ctrl-A chat panel<br/>context-aware reads"]
        CORE["Observe · diagnose · navigate<br/>approval-gated operations<br/>audit log · secret masking"]
        MCP["MCP adapter<br/><b>OPTIONAL</b> korvid[mcp]<br/>read + UI-drive over MCP<br/>write proposals opt-in"]
        K8S["Kubernetes adapter<br/><b>INCLUDED</b><br/>watch · read · apply"]

        TUI --> CORE
        AGENT --> CORE
        MCP --> CORE
        CORE --> K8S
    end

    HUMAN <--> TUI
    MODEL <--> AGENT
    CLIENT <--> MCP
    K8S <--> CLUSTER

    style KORVID fill:#1a202c,color:#e2e8f0,stroke:#4a5568,stroke-width:2px
    style CORE fill:#2d3748,color:#e2e8f0,stroke:#4a5568
    style TUI fill:#22543d,color:#fff,stroke:#276749
    style K8S fill:#22543d,color:#fff,stroke:#276749
    style AGENT fill:#553c9a,color:#fff,stroke:#6b46c1
    style MCP fill:#2c5282,color:#fff,stroke:#3182ce
    style HUMAN fill:#edf2f7,color:#1a202c,stroke:#a0aec0,stroke-dasharray: 4 4
    style MODEL fill:#edf2f7,color:#1a202c,stroke:#a0aec0,stroke-dasharray: 4 4
    style CLIENT fill:#edf2f7,color:#1a202c,stroke:#a0aec0,stroke-dasharray: 4 4
    style CLUSTER fill:#edf2f7,color:#1a202c,stroke:#a0aec0,stroke-dasharray: 4 4
```

The center is a product contract, not the `src/korvid/core/` package. Every
adapter shares cluster reads and UI control. Any write request that reaches
korvid — from the TUI, the embedded agent, or an MCP write proposal — passes
through the approval gate and is logged. MCP itself exposes read and UI-drive
tools; write proposals are opt-in and handled inside korvid, not by the MCP
client.

Neither optional adapter is required, and neither implies the other.

The two extras are **independent**, not a ladder. Each adds to the cockpit on
its own: `korvid[mcp]` gives an editor's assistant cluster sight without
installing an embedded agent at all, and `korvid[agent]` needs no MCP. Install
what you want:

```sh
uv tool install korvid                 # cockpit only
uv tool install 'korvid[agent]'        # + embedded AI agent
uv tool install 'korvid[mcp]'          # + MCP server for external agents
uv tool install 'korvid[observability]' # + Prometheus/Loki investigation
uv tool install 'korvid[all]'          # agent,mcp,observability

brew install hellices/korvid/korvid    # macOS/Linux, no Python needed (= agent)
```

Entra ID authentication for Azure OpenAI is a separate extra —
`korvid[agent,entra]` — because it pulls in the Azure identity stack that only
Entra users need. `[all]` does *not* include it.

(`pipx` works the same way. Pin a version for a reproducible install — see the
[README](../README.md) for the current one.)

A missing extra is not an error. korvid starts, and the feature it powers is
simply absent — unless you *asked* for it on the command line, in which case it
refuses to start and tells you what to install. Silently doing nothing when you
explicitly requested a feature is the one behaviour worth failing over.

---

## Base — the cockpit

**What it is:** a terminal UI for operating a cluster, built for people who
would rather not remember `kubectl` flag order.

Navigate any resource kind with `:`, filter with `/` (fuzzy, regex, or label
selectors), drill into relationships with `Enter` — pods to containers,
deployment to replicaset to pods, a Helm release or operator to the tree of
everything it installed. Split into two panes. Sort on live data. Pods show
live CPU/MEM against their enforced limits, and a troubled pod explains itself
before you open anything.

Beyond browsing: live log tailing, port-forwards that notice when their target
pod dies — flipping to `broken` with a toast, and re-attachable in place with
one key, instead of failing silently the way a hand-run `kubectl port-forward`
does — file transfer in and out of containers without the `kubectl` binary, and
`exec` into a shell.

**What it needs:** a kubeconfig. No AI, no network beyond your cluster, no
account anywhere.

---

## Agent module — an agent inside the cockpit

**What it is:** `Ctrl-A` opens a chat panel that already knows what you are
looking at — view, namespace, selection, filter. You do not describe your
screen to it.

It reads the cluster through read-only tools (manifests, logs, events, compound
diagnostics) and **drives the UI itself**. "Show me the crashing pod's logs"
does not print a suggestion; it navigates, filters, and opens the log pane.

Its answers cite the reads behind them, and selecting a citation opens the
actual view. korvid validates the citations that appear — an invented reference
is reported rather than quietly accepted — but it cannot make a model cite
everything it says, so an uncited sentence is exactly that: uncited. The point
is that the checkable parts *are* checkable. An answer you cannot check at all
is a guess with better grammar.

**What it needs:** a provider. GitHub Copilot, Azure OpenAI, Anthropic, OpenAI,
a local Ollama, or any OpenAI-compatible endpoint. A `small` profile targets
3B–14B models that run on your own hardware — for air-gapped clusters, or for
people who would rather their production incidents not leave the building.

---

## MCP module — korvid as a tool for other agents

**What it is:** the same read and UI-driving tools, exposed over MCP so an
external agent can use them. VS Code Copilot Chat, Claude Code, Cursor, Zed.

Your editor's assistant gains cluster sight: it can list, describe, read logs,
run diagnostics, and move korvid's UI so you watch what it looks at.

**What it cannot do:** change anything. Cluster writes are not on the MCP
surface at all — not gated there, *absent*.

If you opt in with `mcp.write_proposals: true` (off by default), a client gains
one further ability: leaving a write **proposal** for a human to review and
approve inside korvid. That is the whole automation story over MCP,
deliberately — and it stays off until you turn it on.

---

## Four valid compositions

Four installs, four shapes — the two optional adapters occupy independent ports.

<!-- LAYOUT NOTE (Mermaid 11 nested-subgraph rendering):
     The subgraph declaration order below is C4 → C2 (ROW1) then C3 → C1 (ROW2).
     Mermaid 11 renders nested subgraphs in reverse declaration order within each
     row, so this intentional "reversed" source order produces the desired
     rendered grid: cockpit (C1) top-left, Agent (C2) top-right, MCP (C3)
     bottom-left, all (C4) bottom-right.
     Do NOT "tidy" the source order to alphabetical/logical sequence — doing so
     silently reverses the grid without any visible warning. -->
```mermaid
flowchart TB
    subgraph ROW1["  "]
        direction LR
        subgraph C4["korvid[all]  —  agent + MCP"]
            direction TB
            C4A["Agent<br/>OPTIONAL"]
            C4M["MCP<br/>OPTIONAL"]
            C4T["TUI<br/>INCLUDED"]
            C4R["Core"]
            C4K["K8s<br/>INCLUDED"]
            C4A --> C4R
            C4M --> C4R
            C4T --> C4R --> C4K
        end

        subgraph C2["korvid[agent]  —  + embedded AI"]
            direction TB
            C2A["Agent<br/>OPTIONAL"]
            C2T["TUI<br/>INCLUDED"]
            C2R["Core"]
            C2K["K8s<br/>INCLUDED"]
            C2A --> C2R
            C2T --> C2R --> C2K
        end
    end

    subgraph ROW2["  "]
        direction LR
        subgraph C3["korvid[mcp]  —  + MCP server"]
            direction TB
            C3M["MCP<br/>OPTIONAL"]
            C3T["TUI<br/>INCLUDED"]
            C3R["Core"]
            C3K["K8s<br/>INCLUDED"]
            C3M --> C3R
            C3T --> C3R --> C3K
        end

        subgraph C1["korvid  —  cockpit only"]
            direction TB
            C1T["TUI<br/>INCLUDED"]
            C1R["Core"]
            C1K["K8s<br/>INCLUDED"]
            C1T --> C1R --> C1K
        end
    end

    NOTE["korvid[agent,entra] is a separate<br/>auth extra — not included in [all]"]

    style ROW1 fill:none,stroke:none
    style ROW2 fill:none,stroke:none
    style NOTE fill:#fffbeb,color:#92400e,stroke:#d97706,stroke-dasharray: 4 4

    style C1T fill:#22543d,color:#fff,stroke:#276749
    style C1R fill:#2d3748,color:#e2e8f0,stroke:#4a5568
    style C1K fill:#22543d,color:#fff,stroke:#276749

    style C2T fill:#22543d,color:#fff,stroke:#276749
    style C2A fill:#553c9a,color:#fff,stroke:#6b46c1
    style C2R fill:#2d3748,color:#e2e8f0,stroke:#4a5568
    style C2K fill:#22543d,color:#fff,stroke:#276749

    style C3T fill:#22543d,color:#fff,stroke:#276749
    style C3M fill:#2c5282,color:#fff,stroke:#3182ce
    style C3R fill:#2d3748,color:#e2e8f0,stroke:#4a5568
    style C3K fill:#22543d,color:#fff,stroke:#276749

    style C4T fill:#22543d,color:#fff,stroke:#276749
    style C4A fill:#553c9a,color:#fff,stroke:#6b46c1
    style C4M fill:#2c5282,color:#fff,stroke:#3182ce
    style C4R fill:#2d3748,color:#e2e8f0,stroke:#4a5568
    style C4K fill:#22543d,color:#fff,stroke:#276749
```

---

## The one rule all four share

**Nothing changes your cluster without a human keystroke, and nothing changes
it unlogged.**

Most dialogs show a preview of what will change — a server-side dry-run diff
for Kubernetes API writes, a rendered manifest for Helm. Some operations have
none to show (a file upload into a pod), and a preview that times out does not
hold the dialog hostage. The preview is a courtesy; the dialog and the audit
entry are the guarantee.

Not "the agent is instructed not to". Not "you can turn on confirmations". The
write path *goes through* the dialog — there is no second route, for you, for
the agent, or for an MCP client, and a write whose audit record cannot be
written does not happen at all.

Add `--readonly` to remove writes entirely, or mark production contexts as
protected: every confirmation then requires typing the **context** name instead
of a single `y` — and the operations that already demand the resource name
(cluster-scoped deletes, node drains) keep that stronger gate.

`Secret` values and the credential patterns korvid recognises are masked before
anything reaches the **embedded** agent's provider, and `:ai payload` shows you
what was sent — a boundary you cannot inspect is a promise, not a control. Two
honest limits: a secret that only your application knows is a secret, sitting in
a log line, may pass undetected; and an external MCP client applies its own data
policy, not korvid's. [`threat-model.md`](threat-model.md) is specific about
both.

---

## Why this shape

Three convictions, each visible in the structure above:

**An operator's tool should work without AI.** The base cockpit is complete on its own.
The agent is an addition to a working cockpit, not the reason it exists — so
your cluster tooling does not stop working when a provider is down, a token
expires, or a policy forbids sending cluster data anywhere.

**An AI that cannot act is a search engine; one that acts unsupervised is a
liability.** The middle ground is an agent that does everything *except* the
irreversible part, and hands you that part with a preview attached.

**Where the model runs is your decision.** Frontier API, or a 3B model on a
node in your own cluster. The `small` profile and a published per-model
scoreboard exist so that choice is informed rather than hopeful.

---

## Where to go next

| You want to | Read |
|---|---|
| Install and start using it | [README](../README.md) |
| Learn the keys | [`keybindings.md`](keybindings.md) |
| Set up the agent | [`agent.md`](agent.md) |
| Connect an editor over MCP | [`mcp.md`](mcp.md) |
| Understand the write safety model | [`ops.md`](ops.md) |
| Know exactly what reaches a model | [`threat-model.md`](threat-model.md) |
| Run without internet access | [`airgap.md`](airgap.md) |
| See how the code is organised | [`dev/specs/2026-08-12-korvid-architecture.md`](dev/specs/2026-08-12-korvid-architecture.md) |
