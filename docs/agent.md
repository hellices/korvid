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

The agent is an **optional extra**. A base install has no provider HTTP client
or keychain integration, so `Ctrl-A` is unavailable and `:ai` is not
registered.

```sh
uv tool install "korvid[agent]"      # or: pipx install "korvid[agent]"
uv tool install "korvid[all]"        # agent + MCP + observability
```

Nothing about the extra is loaded until the agent is composed: an MCP-only or
agent-disabled start never imports the engine, request gateway, or provider
adapter. If `agent.enabled` is set but the extra is missing, startup fails
with the exact install command instead of silently disabling the agent.

A local Ollama endpoint keeps every request on the machine — nothing leaves
it, and no API key exists to leak. For an endpoint behind a private CA, set
`network.ca_bundle`; verification can never be disabled. The rest of the
no-egress story lives in the [air-gapped guide](airgap.md).

## From prompt to cited answer

A turn sees your screen context — view, namespace, selected resource, active
filter — and answers by calling bounded read-only tools: manifest, log, event
and listing reads, plus compound diagnostics that return projected evidence
instead of a raw dump. `diagnose_pod` collects container states, the owner
chain, warning events and targeted log excerpts in one deterministic call;
`diagnose_service` reports EndpointSlice readiness and names the gap when that
data is unavailable; `diagnose_pvc` escalates to events and StorageClasses
only while a claim is unresolved. Results are capped at 8,000 characters,
manifests are shrunk structurally so they stay parseable, and `Secret` data is
masked before it ever reaches the model.

Each successful read mints a numbered evidence reference (`[E1]`, `[E2]`, …)
that the answer cites, validated against what was actually fetched rather than
merely asserted. Navigable citations open their source view. Compound
diagnostics remain validated evidence but have no single destination because no
screen contains the whole report. Screen actions and writes never mint evidence
— only a read of cluster data does.

The agent can also drive the TUI — navigate, filter, drill down, open the log
or describe pane — so "show me the crashing pod's logs" lands you in the real
log viewer instead of a text dump.

At startup korvid also detects the cluster's cloud provider (AKS, EKS, GKE)
from node metadata on a best-effort basis; an RBAC-limited, bare-metal, or
local cluster detects as "unknown" and nothing changes.

## From proposal to write

The agent can *request* write operations — delete, scale, rollout restart,
and (on clusters that expose `pods/resize`, Kubernetes 1.35+) in-place pod
resize — but it can never execute one itself. Every request opens the same
confirmation dialog as a direct keybinding (marked ⚠ in the tool log): the
proposal stays inert until a fresh user keystroke in that dialog approves
it, and an unanswered dialog expires without executing anything. Every
executed write — yours or the agent's — is recorded through the same
fail-closed [audit log](ops.md#one-write-path-three-drivers): if the audit
entry cannot be written, the write is blocked before it happens.

## Direct control and the conversation

The agent and the keyboard drive **one** workspace. Nothing about a chat turn
takes the TUI away from you:

- What you have selected when you submit is what the turn is asked about —
  view, namespace, filter, focused pane and selected resource travel with the
  question as structured context, not as a screenshot.
- When the agent navigates or opens a pane, it moves the same panes your keys
  move. There is no separate agent view to reconcile afterwards.
- Pressing a normal key mid-turn keeps working, and the *next* turn starts
  from where **you** left it — the handoff runs in both directions.
- Switching context with `:ctx` hands the cluster back immediately. The
  conversation survives, but the evidence ledger is cleared: a citation minted
  against the old cluster must not resolve to a same-named object in the new
  one.

## Inspecting what the agent sends

`:ai payload` opens a read-only view of the exact sanitized request most
recently sent to the provider — the literal payload, not a re-derived
approximation. Every message, tool result and tool-call argument passes
through the same `OutboundPolicy` redaction step regardless of provider
(hosted or local): `Secret` values, the
`kubectl.kubernetes.io/last-applied-configuration` annotation, and text
matching known credential patterns are masked before the inspector, or the
network, ever sees them.

**The payload is sanitized, not anonymized.** Resource names, namespaces and
labels still appear in it; sanitization removes secret material and known
credential patterns only. Press `e` to export the displayed payload to a
private, `0o600` file — never overwritten, never auto-deleted, so treat it
like any other cluster-derived artifact. [The threat model](threat-model.md)
carries the boundary and the residual risks.

## Connect a provider

The quickest path is inside the TUI: `:ai` (alias `:agent`) opens a wizard for
provider, authentication and a live test call, and saves it to
`~/.config/korvid/config.yaml`. `Ctrl-A` toggles the panel's *visibility*;
`:ai off` releases the provider connection without discarding the saved
configuration.

### Named profiles and model references

A connection is a **profile** under `agent.profiles.<name>`; `agent.active`
names the one in use. Every model reference is `<prefix>/<tag>` — the prefix
picks the transport route, the tag is the model identifier. Colon-form
references (`ollama:qwen3:8b`) are **not** accepted: a model tag can itself
contain a colon (e.g. `qwen3:8b`), so the colon cannot serve as a separator.

To switch profiles, run `:ai switch <name>` or edit `agent.active` in the
config file. `:model [name]` sets the model directly; to browse what is
available, run `:ai` — see [Model search](#model-search) below.

A full three-profile `~/.config/korvid/config.yaml`:

```yaml
agent:
  active: local

  profiles:

    # Local Ollama endpoint — no API key, nothing leaves the machine.
    local:
      model: ollama/qwen3:8b
      auth:
        method: none
      options:
        num_ctx: 32768

    # OpenAI with the key in the environment, never in this file.
    gpt:
      model: openai/gpt-4o
      auth:
        method: environment
        key: OPENAI_API_KEY

    # Azure OpenAI / AI Foundry with Entra ID credential (needs [entra]).
    azure-work:
      model: azure/gpt-4.1
      endpoint: https://my-hub.openai.azure.com/openai/deployments/gpt-4.1
      auth:
        method: provider-default
```

### Auth methods

`auth.method` is one of five values:

| Method | Meaning |
|---|---|
| `environment` | Read the API key from `auth.key` (the variable name, not the value). Fails at startup if the variable is unset. |
| `keyring` | Read the key from the OS keychain under `auth.key`. |
| `provider-default` | Resolve a declared credential chain for the prefix — used for Azure / Entra ID, and extensible by `korvid.credential` entry points. The `[entra]` extra is required for `azure` profiles. |
| `device-login` | Perform an interactive device-code login when the provider requires it. Used by the `github-copilot` flow. |
| `none` | No credential. For a private local endpoint that needs no key. |

`auth.key` names the environment variable. The secret itself **never belongs
in the config file**.

### Supported prefixes

| Prefix | Auth | Notes |
|---|---|---|
| `openai` | `environment` / `keyring` | Also covers any OpenAI-compatible endpoint; set `endpoint` for a self-hosted server. |
| `anthropic` | `environment` / `keyring` | |
| `azure` | `provider-default` (Entra ID via `az login` or managed identity) | Requires the `[entra]` extra. `endpoint` is the deployment URL. |
| `github-copilot` | `device-login` | Unofficial internal API; requires an active Copilot subscription. |
| Ollama (`ollama`) | `none` | `options.native_thinking: true` selects native `/api/chat`; `options` also tunes `num_ctx`, `temperature`, `seed`, `think`, `keep_alive`, `num_predict`. |
| Any LiteLLM-supported prefix | varies | LiteLLM ships a catalog of 2,000+ models; use any of its prefixes directly. |

An older single-scalar config (`agent.provider`, `agent.model`) is migrated
automatically on first load.

!!! warning "GitHub Copilot"

    Copilot support uses an **unofficial internal API** that may change or
    break without notice, and requires an active GitHub Copilot
    subscription.

Entra ID auth ships in its own extra. For a tool-managed install, reinstall
the complete desired extra set:

```sh
uv tool install --force 'korvid[all,entra]==0.3.0'
# or
pipx install --force 'korvid[all,entra]==0.3.0'
```

In a source checkout, `uv sync --extra entra` does the same job.

If your backend already speaks an OpenAI-compatible API, prefer a profile with
an `endpoint` over a plugin. A backend whose protocol or auth flow truly
differs registers as a [Provider plugin](provider-plugins.md): trusted,
in-process code that receives the same sanitized payload a built-in provider
gets, and is outside korvid's visibility past the handoff. That page carries
the API 2 contract and the operator checklist.

## Model search

The `:ai` wizard opens with a fuzzy-search screen over korvid's model
catalog. The catalog has two layers:

1. **Bundled (primary)**: LiteLLM ships a `model_prices_and_context_window.json`
   table inside its wheel. korvid reads that table at startup — no network call,
   no internet required. 2,000+ models from dozens of providers are discoverable
   entirely offline.

2. **models.dev (optional enrichment)**: korvid may fetch a single JSON document
   from `https://models.dev/api.json` to add context lengths, quantization info,
   and environment-variable hints. This fetch is **never made at startup** and
   **never made during a routing call** — it happens only when you press
   <kbd>Ctrl</kbd>+<kbd>R</kbd> on the search screen, which reports the
   outcome inline and re-renders the current query. The result is cached in
   the platform cache directory (`$XDG_CACHE_HOME/korvid/models-dev.json`,
   mode `0600`, TTL 24 h).

   To disable models.dev enrichment permanently, set:

   ```yaml
   agent:
     model_search:
       models_dev: false
   ```

   korvid then builds no models.dev client, so <kbd>Ctrl</kbd>+<kbd>R</kbd>
   reports it disabled. Only `true` and `false` parse; anything else warns
   and disables.

   A network observer can infer that an instance refreshed its model
   metadata — the only outbound agent connection carrying no cluster
   payload. See the [threat model](threat-model.md#modelsdev).

Routing never consults models.dev: `provider/model` is resolved by LiteLLM's
bundled tables (plus any registered special flows) with no external calls.

## Stop, correct, or follow

Press `Ctrl-X` to stop a running turn: the partial answer stays in the
transcript marked `⏹ interrupted`, and the panes keep the state they had.
Typing a new prompt while the agent works is **interrupt-and-submit** — the
old turn is cancelled and a fresh one starts immediately. Interrupts respect
the write gate: a pending approval dialog is dismissed without executing,
while a write you already approved runs to completion and is audited.

**Follow mode** mirrors each successful read onto the screen, for models that
answer in text without touching the screen tools: a listing navigates the
view, a resource, event or `diagnose_*` read opens the describe pane, and a
log read opens the live log pane. It is on by default — disable it with
`agent.follow: false`, or toggle it with `:ai follow off` / `:ai follow on`.
A mirror never interrupts you: a failed read moves nothing, and a mirror is
refused while an approval dialog or a describe screen you are reading is open.

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
surface poorly and degrade with context length well below their advertised
windows, so it also ships shorter tool wording and a tighter operating pack.
Its history budget is a *hard* bound: a turn that would push a request past it
ends early instead of sending it. Writes are unaffected — every write tool the
environment arms still passes the approval gate at both tiers, and a read-only
deployment is never offered one.

Routing has one precedence order: `agent.model_tier`, then what the provider
reports, then korvid's shipped model catalog, then `low` as the safe fallback.
A model that cannot call tools at all is refused at startup. The panel header
shows the route as `tier (source)` — `low (catalog)`, `high (user)` — beside
the model name and the session's token usage. For profile-based upgrades, see
the [migration notes](release-notes/unreleased.md).

```yaml
agent:
  active: local
  profiles:
    local:
      model: ollama/qwen3:8b
      endpoint: http://localhost:11434
      auth:
        method: none
      # Omit for automatic routing; set only to override what routing decided.
      # model_tier: low   # low | high
  rules:
    - "Never include node names in an answer."
```

`agent.rules` appends short house rules, composed *after* korvid's immutable
safety contract: a rule can add caution but never widen what the agent may do,
because approvals, read-only enforcement and the audit log live in code, not
in wording. To choose a model, or measure a prompt change, see the
[evaluation methodology](evals/methodology.md), the
[scenario catalog](evals/scenarios.md), and the
[model scoreboard](evals/scoreboard.md) — a development-only harness
excluded from wheels and sdists; run it from a source checkout with
`uv sync --frozen --dev --all-extras`.

## What the recording demonstrates

The capture above is a **deterministic synthetic-cluster walkthrough**, and
the turn in it is real: the prompt is submitted through the real
`AgentPanel`, the shipped `DefaultAgentSession` and `NativeAgentEngine` dispatch
`diagnose_pod` and then
`get_logs` through the real `ToolExecutor`, and the real `EvidenceLedger`
mints `[E1]` and `[E2]` for those two reads and validates the answer's
markers against them — which is why the frame carries no unsupported-citation
warning. The describe pane beside the panel is `agent.follow` mirroring the
first read; it is not a UI-drive tool call and not a write.

Only the model's side of the conversation is fixed. The recording's provider
is deterministic and offline — it opens no socket and reads no credential —
and every byte the tools read comes from a synthetic fixture. So the clip
says nothing about a live model, a live cluster, or answer quality; the
read, write, masking, and audit guarantees above hold on every real turn
regardless.
