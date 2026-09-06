# AI agent

Requires the `[agent]` extra. A base install remains a complete Kubernetes TUI
that starts, watches, and drives the cluster as before.
Press `Ctrl-A` to open the agent panel. It answers questions about the current cluster, inspects it
through bounded read-only tools, and can drive the TUI, but never writes without
your keystroke.

<section class="docs-storyboard" aria-labelledby="agent-storyboard-title">
  <figure>
    <img src="../assets/scenes/agent-poster.png" width="1280" height="720" loading="lazy" alt="Korvid's real AgentPanel ending a deterministic synthetic-cluster walkthrough: the submitted prompt, real diagnose_pod and get_logs tool events, a grounded answer citing E1 and E2, and the describe pane agent follow mirrored beside it">
    <figcaption id="agent-storyboard-title">Capture: a deterministic synthetic-cluster walkthrough. Capture note — the real runtime, executor and evidence ledger run against a synthetic fixture behind a deterministic offline provider, so the turn is real but the recording is not a live-model quality claim.</figcaption>
  </figure>
  <div>
    <p><strong>What a real turn does</strong></p>
    <ol>
      <li><strong>Context</strong><span>Current view, namespace, selection, and filter.</span></li>
      <li><strong>Read</strong><span>Bounded tools gather manifests, events, logs, or diagnoses.</span></li>
      <li><strong>Cite</strong><span>Evidence references remain selectable and validated.</span></li>
      <li><strong>Drive</strong><span>Navigation can change; writes still stop at confirmation.</span></li>
    </ol>
  </div>
</section>

## Installing the agent

The agent is an **optional extra**. A base install has no provider transport or
keychain integration, so `Ctrl-A` is unavailable and `:ai` is not registered.

```sh
uv tool install "korvid[agent]"      # or: pipx install "korvid[agent]"
uv tool install "korvid[all]"        # agent, MCP, observability
```

Nothing about it loads until the agent is composed. If `agent.active` names a
configured profile but the extra is missing, startup fails with the exact
install command rather than silently disabling it.

A local Ollama endpoint keeps every request on the machine, and no API key
exists to leak. For an endpoint behind a private CA set `network.ca_bundle`;
verification can never be disabled. The [air-gapped guide](airgap.md) has the
rest of the no-egress story.

## From prompt to cited answer

A turn sees your screen context — view, namespace, selected resource, active
filter — and answers by calling bounded read-only tools: manifest, log, event
and listing reads, plus compound diagnostics that return projected evidence
instead of a raw dump. `diagnose_pod` collects container states, the owner
chain, warning events and targeted log excerpts in one deterministic call;
`diagnose_service` and `diagnose_pvc` do the same for EndpointSlice readiness
and unresolved claims. Results are capped at 8,000 characters, manifests are
shrunk structurally so they stay parseable, and `Secret` data is masked before
the model sees it.

Each successful read mints a numbered evidence reference (`[E1]`, `[E2]`, …)
that the answer cites, validated against what was actually fetched rather than
merely asserted. Navigable citations open their source view. Compound
diagnostics remain validated evidence but have no single destination — no
screen holds the whole report. Screen actions and writes mint none.

It can also drive the TUI — navigate, filter, drill down, open the log or
describe pane. korvid detects the cluster's cloud provider (AKS, EKS, GKE) from
node metadata, best-effort; an RBAC-limited, bare-metal, or local cluster
detects as "unknown" and nothing changes.

## From proposal to write

The agent can *request* write operations — delete, scale, rollout restart, and
(where `pods/resize` exists, Kubernetes 1.35+) in-place pod resize — but never
executes one. Every request opens the same confirmation dialog a direct
keybinding does (marked ⚠ in the tool log): the proposal stays inert until a
fresh user keystroke in that dialog approves it, and an unanswered dialog
expires without executing anything. Every executed write — yours or the
agent's — goes through the same fail-closed
[audit log](ops.md#one-write-path-three-drivers): if the audit entry cannot be
written, the write is blocked before it happens.

## Direct control and the conversation

The agent and the keyboard drive **one** workspace: what you have selected when
you submit is what the turn is asked about, and the agent moves the panes your
keys move. Pressing a normal key mid-turn keeps working, and the *next* turn
starts where **you** left it. `Ctrl-A` toggles the panel's *visibility* only.
`:ctx` hands the cluster back and clears the evidence ledger, so a citation
minted against the old cluster cannot resolve to a same-named object in the
new one.

`Ctrl-X` stops a running turn, leaving the partial answer marked
`⏹ interrupted`; typing a new prompt mid-turn is **interrupt-and-submit**. Both
respect the write gate: a pending approval dialog is dismissed unexecuted,
while an approved write finishes and is audited.

**Follow mode** mirrors each successful read: a listing navigates the view,
`diagnose_*` or a resource read opens the describe pane, a log read opens the
live log pane. On by default — `agent.follow: false`, or `:ai follow off` /
`:ai follow on`. A mirror is refused while an approval dialog or a describe
screen you are reading is open.

## Inspecting what the agent sends

`:ai payload` opens a read-only view of the exact sanitized request most
recently sent to the provider — the literal payload, not a re-derived
approximation. `Secret` values, the
`kubectl.kubernetes.io/last-applied-configuration` annotation, and
credential-shaped text are masked before the inspector, or the network, sees
them.

**The payload is sanitized, not anonymized** — resource names, namespaces and
labels still appear in it. Press `e` to export it to a private, `0o600` file.
[The threat model](threat-model.md) has the boundary and residual risks.

## Connect a provider

`:ai` (alias `:agent`) is the quickest path. On a first run it opens the setup
wizard — model, authentication, a live test call — saved to
`~/.config/korvid/config.yaml`; once profiles exist it opens the profile
manager, where `Enter` activates the highlighted profile, `a`/`e`/`d` add, edit
and delete it, and `t` sets the tier. `:model` prints the active model and
`:model <name>` switches it; `:ai off` releases the connection without
discarding the config.

A connection is a **profile** under `agent.profiles.<name>`, and `agent.active`
names the one in use — editing that key switches profiles outside the TUI.
Every model reference is `<prefix>/<tag>`: the prefix picks the transport
route, the tag names the model. Colon-form references (`ollama:qwen3:8b`) are
**not** accepted, because a tag can itself contain a colon.

```yaml
agent:
  active: local

  profiles:

    local:                                    # keyless Ollama on this machine
      model: ollama/qwen3:8b
      endpoint: http://localhost:11434
      auth:
        method: none
      options:
        num_ctx: 32768

    gpt:                                      # key stays in the environment
      model: openai/gpt-4o
      auth:
        method: environment
        key: OPENAI_API_KEY

    azure-work:                               # Entra ID; needs [entra]
      model: azure/gpt-4.1
      endpoint: https://my-hub.openai.azure.com/openai/deployments/gpt-4.1
      auth:
        method: provider-default
```

`auth.method` is one of five values. The secret **never belongs in the config
file**: `auth.key` is a *name* — of an environment variable, or a keychain
entry — and the other three methods ignore it.

| Method | Meaning |
|---|---|
| `environment` | Key in the environment variable named by `auth.key`; refused when it is unset. |
| `keyring` | Key in the OS keychain entry named by `auth.key`; falls back to `profile.model` if absent. |
| `provider-default` | A declared credential chain for the prefix — Entra ID, plus anything on the `korvid.credential` entry point; `azure` needs `[entra]`. |
| `device-login` | Interactive device-code sign-in, used by the `github-copilot` flow. |
| `none` | No credential; requires `endpoint`, since a keyless request without one goes to whatever host the SDK defaults to. |

| Prefix | Notes |
|---|---|
| `openai`, `anthropic` | `openai` also covers any OpenAI-compatible server; set `endpoint` for a self-hosted one. |
| `azure` | `endpoint` is the deployment URL. |
| Ollama (`ollama`) | `options.native_thinking: true` selects native `/api/chat`; the same `options` mapping tunes `num_ctx`, `temperature`, `seed`, `think`, `keep_alive`, `num_predict`. |
| Any LiteLLM-supported prefix | 2,000+ models ship in the bundled catalog; use any prefix directly. |

`options` carries **model parameters** — `temperature`, `max_tokens`, `seed`,
`timeout`, `api_version`, `extra_headers` and whatever else the provider
accepts. It does not carry the request itself. korvid owns `model`,
`messages`, `stream`, `stream_options`, `tools`, `tool_choice`, `base_url`,
`api_base`, `api_key`, `custom_llm_provider` and any credential-shaped key,
and drops those from a profile rather than letting one re-route the request,
swap the credential or mute the agent's tools. An option the provider does not
list as supported is dropped too — unless the capability lookup itself failed,
in which case it is forwarded so the vendor's error names it.

A config still using the retired flat scalars is migrated on load into one
profile named `default`, which `agent.active` then selects; the scalars are
dropped on the next save. The `agent.ollama.*` block is read **only** by that
migration — once `agent.profiles` exists it is ignored and removed on save, so
those knobs belong in a profile's `options`. See the
[migration notes](release-notes/unreleased.md).

!!! warning "GitHub Copilot"

    Copilot support uses an **unofficial internal API** that may change or
    break without notice, and requires an active GitHub Copilot
    subscription.

Entra ID ships in its own extra; a tool-managed install reinstalls the complete
set:

```sh
uv tool install --force 'korvid[all,entra]==0.3.0'
# or
pipx install --force 'korvid[all,entra]==0.3.0'
```

In a source checkout, `uv sync --extra entra` does the same job.

A non-standard backend registers a flow on the `korvid.provider` entry point;
[Provider plugins](provider-plugins.md) has the API 2 contract.

## Model search

The wizard's model step is a fuzzy search over a two-layer catalog.

**Bundled (primary).** LiteLLM ships a `model_prices_and_context_window.json`
table inside its wheel, read at startup — no network call, no internet
required — so 2,000+ models are discoverable offline.

**models.dev (optional enrichment).** korvid may fetch one JSON document from
`https://models.dev/api.json` for context lengths, quantization and
environment-variable hints. It is **never fetched at startup**, on mount, on a
keystroke, or during routing: only <kbd>Ctrl</kbd>+<kbd>R</kbd> on the search
screen contacts it, forcing an `ETag`-conditional revalidation and reporting
the outcome inline (`Model metadata updated.`, `Model metadata already up to
date.`, `Model metadata unavailable — keeping what korvid already had.`).
Otherwise korvid serves its `0600` cache unchanged between operator-requested refreshes.

Enrichment is on by default; `agent.model_search.models_dev: false` disables it
permanently, and only `true` and `false` parse — anything else warns at startup
and is treated as `false`. The
[air-gapped guide](airgap.md#offline-model-catalog) has the cache paths and
what the refresh key answers once it is off; the
[threat model](threat-model.md#modelsdev) has the request bounds. Routing never
reads models.dev.

## Model tiers and routing

korvid resolves one **model tier** per session, and the agent's budgets and
armed tool surface follow from it.

| | low | high |
| --- | --- | --- |
| iterations per turn | 6 | 15 |
| retained history | 24,000 chars (hard bound) | 120,000 chars |
| per tool result | 3,000 chars | the executor's 8,000-char cap |
| tool calls per response | 1 (extras discarded) | parallel, if confirmed |
| screen tools armed | `open_logs`, `open_describe` | all five |

The low tier exists because small local models (3B–14B) handle a frontier tool
surface poorly; it ships shorter tool wording and a tighter operating pack, and
its history budget is a *hard* bound — a turn that would push a request past it
ends early instead of sending it. Every write tool the environment arms passes
the approval gate at both tiers.

Routing precedence: `agent.model_tier`, then what the provider reports, then
korvid's shipped catalog, then `low` as the safe fallback. A model that cannot
call tools is refused at startup. The panel header shows the route as
`tier (source)` — `low (catalog)`, `high (user)`.

`agent.model_tier` and `agent.rules` sit beside `agent.active`, not inside a
profile. Rules are composed *after* korvid's immutable safety contract: a rule
can add caution but never widen what the agent may do, because approvals,
read-only enforcement and the audit log live in code, not wording.

The eval harness is **development-only** and excluded from wheels and sdists;
run it from a source checkout after `uv sync --frozen --dev --all-extras`. The
[evaluation methodology](evals/methodology.md), the
[scenario catalog](evals/scenarios.md) and the
[model scoreboard](evals/scoreboard.md) cover how a model or a prompt change
is measured.

## What the recording demonstrates

The capture above is a **deterministic synthetic-cluster walkthrough**, and the
turn in it is real: the real `AgentPanel` submits the prompt, the shipped
`DefaultAgentSession` and `NativeAgentEngine` dispatch `diagnose_pod` then
`get_logs` through the real `ToolExecutor`, and the real `EvidenceLedger` mints
`[E1]` and `[E2]` and validates the answer's markers against them.

Only the model's side is fixed: the provider is deterministic and offline, and
every byte the tools read comes from a synthetic fixture. The clip says nothing
about a live model, a live cluster, or answer quality — the read, write,
masking and audit guarantees above hold on every real turn regardless.
