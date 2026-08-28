# What korvid is

> A tool-using bird for your cluster.

korvid is one program that grows with what you install: a keyboard-first
Kubernetes cockpit that needs nothing but a kubeconfig, plus one extra for an
embedded AI agent and another that makes it a tool *other* AI clients can
drive. One core, independent adapters, one safety model.

## The shape

Three independent drivers, one korvid boundary, one Kubernetes adapter. The
center is a behavior contract, not a claim that every read shares one snapshot.

```mermaid
flowchart LR
    HUMAN["👤 Human operator"]
    MODEL["🤖 Model / provider<br/>Copilot · Azure · Anthropic<br/>OpenAI · Ollama · compatible"]
    CLIENT["🖥 Editor / external assistant<br/>VS Code · Claude Code<br/>Cursor · Zed"]
    CLUSTER[("☸ Kubeconfig +<br/>cluster")]

    subgraph KORVID["KORVID — product boundary"]
        direction LR
        TUI["TUI adapter<br/><b>INCLUDED</b><br/>browse · filter · describe<br/>logs · port-forward · exec"]
        AGENT["Agent adapter<br/><b>OPTIONAL</b> korvid[agent]<br/>Ctrl-A chat panel<br/>context-aware reads<br/>provider payload masking"]
        CORE["Observe · diagnose · navigate<br/>approval-gated operations<br/>audit log"]
        MCP["MCP adapter<br/><b>OPTIONAL</b> korvid[mcp]<br/>read + UI-drive over MCP<br/>write proposals opt-in<br/>tool-specific disclosure"]
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

The center is a product contract, not the `src/korvid/core/` package: every
adapter shares cluster reads and UI control, and any write request that reaches
korvid — from the TUI, the embedded agent, or an MCP write proposal — passes
through the approval gate and is logged. Provider payload masking belongs to
the embedded Agent boundary; MCP result disclosure remains tool-specific.

The two adapter extras are **independent**, not a ladder, and neither implies
the other: `korvid[mcp]` gives an editor's assistant cluster sight with no
embedded agent, and `korvid[agent]` needs no MCP. Observability is an add-on to
either tool surface (`korvid[agent,observability]`, `korvid[mcp,observability]`),
never a standalone base-TUI surface:

```sh
uv tool install korvid                 # cockpit only
uv tool install 'korvid[agent]'        # + embedded AI agent
uv tool install 'korvid[mcp]'          # + MCP server for external agents
uv tool install 'korvid[all]'          # agent, mcp, observability

brew install hellices/korvid/korvid    # macOS/Linux, no Python needed (= agent)
```

Entra ID auth for Azure OpenAI is a separate `korvid[agent,entra]` extra,
excluded from `[all]`. (`pipx` works the same way.) A missing extra is not an
error: korvid starts and the feature it powers is simply absent — unless you
*asked* for it on the command line, in which case it refuses to start and tells
you what to install.

---

## The three surfaces

**The cockpit (included).** Navigate any resource kind with `:`, filter with
`/` (fuzzy, regex, or label selectors), drill into relationships with `Enter` —
deployment to replicaset to pods, a Helm release or operator to everything it
installed. Split panes, live sort, CPU/MEM against enforced limits, and a
troubled pod that explains itself before you open it. Plus log tailing, `exec`,
file transfer without the `kubectl` binary, and port-forwards that flip to
`broken` when their pod dies instead of failing silently. All it needs is a
kubeconfig — no AI, no account, no network beyond your cluster. Keys in
[`keybindings.md`](keybindings.md), the tour in [`tui.md`](tui.md).

**The agent (`korvid[agent]`).** `Ctrl-A` opens a chat panel that already knows
your view, namespace, selection, and filter. It reads through read-only tools —
manifests, logs, events, compound diagnostics — and **drives the UI itself**:
"show me the crashing pod's logs" navigates, filters, and opens the log pane
instead of printing a suggestion. Answers cite the reads behind them and a
navigable citation opens its actual view; korvid reports an invented reference
rather than accepting it quietly, but it cannot force a model to cite
everything, so an uncited sentence stays exactly that. It needs a provider
(Copilot, Azure OpenAI, Anthropic, OpenAI, Ollama, or any OpenAI-compatible
endpoint), and `agent.model_tier: low` targets 3B–14B models on your own
hardware. See [`agent.md`](agent.md).

**The MCP server (`korvid[mcp]`).** The same read and UI-driving tools over
MCP, so VS Code Copilot Chat, Claude Code, Cursor, or Zed gains cluster sight
and moves korvid's UI while you watch. It cannot change anything: cluster
writes are not on the MCP surface — not gated there, *absent*. Opting in
with `mcp.write_proposals: true` (off by default) adds one ability: leaving a
write **proposal** a human approves inside korvid. See [`mcp.md`](mcp.md).

---

## Four agent/MCP adapter compositions

Four core shapes come from the two independent adapters; observability is an
optional overlay on any shape with agent or MCP, and `korvid[all]` includes it.

<!-- LAYOUT NOTE: the subgraph order below is deliberately C4 → C2 then
     C3 → C1. Mermaid 11 renders nested subgraphs in reverse declaration
     order per row, so "tidying" this into logical order silently reverses
     the rendered grid. -->
```mermaid
flowchart TB
    subgraph ROW1["  "]
        direction LR
        subgraph C4["korvid[all]  —  agent + MCP + observability"]
            direction TB
            C4A["Agent<br/>OPTIONAL"]
            C4M["MCP<br/>OPTIONAL"]
            C4O["Prometheus/Loki<br/>OPTIONAL"]
            C4T["TUI<br/>INCLUDED"]
            C4R["Core"]
            C4K["K8s<br/>INCLUDED"]
            C4A --> C4R
            C4M --> C4R
            C4O --> C4R
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

Not a policy prompt, not an opt-in confirmation setting. The write path *goes
through* the dialog — there is no second route, for you, for the agent, or for
an MCP client — and a write whose audit record cannot be
written does not happen at all. Most dialogs preview the change (a server-side
dry-run diff, a rendered Helm manifest); some have none, and a preview that
times out never holds the dialog hostage: the preview is a courtesy, the dialog
and the audit entry are the guarantee. `--readonly` removes writes entirely; a
protected context makes every confirmation there require typing the
**context** name instead of a single `y`.

`Secret` values and the credential patterns korvid recognises are masked before
anything reaches the **embedded** agent's provider, and `:ai payload` shows
what was sent — a boundary you cannot inspect is a promise, not a control. Two
honest limits: a secret only your application knows is a secret, sitting in a
log line, may pass undetected; and an external MCP client applies its own data
policy, not korvid's. [`threat-model.md`](threat-model.md) is specific about
both, [`ops.md`](ops.md) about the write path itself.

---

## Why this shape

Three convictions, visible in the structure above:

**An operator's tool should work without AI.** The cockpit is complete on its
own, so it keeps working when a provider is down, a token expires, or policy
forbids sending cluster data anywhere.

**An AI that cannot act is a search engine; one that acts unsupervised is a
liability.** The middle ground does everything *except* the irreversible part,
and hands you that part with a preview attached.

**Where the model runs is your decision.** A frontier API, or a 3B model on a
node in your own cluster; the low tier and a published per-model scoreboard
make that choice informed rather than hopeful.

---

## Where to go next

| You want to | Read |
|---|---|
| Install and start using it | [README](https://github.com/hellices/korvid/blob/main/README.md) |
| Learn the keys | [`keybindings.md`](keybindings.md) |
| Set up the agent | [`agent.md`](agent.md) |
| Connect an editor over MCP | [`mcp.md`](mcp.md) |
| Understand the write safety model | [`ops.md`](ops.md) |
| Know exactly what reaches a model | [`threat-model.md`](threat-model.md) |
| Check the measured envelope | [`performance.md`](performance.md) |
| Run without internet access | [`airgap.md`](airgap.md) |
| See how the code is organised | [`dev/specs/2026-08-12-korvid-architecture.md`](dev/specs/2026-08-12-korvid-architecture.md) |
