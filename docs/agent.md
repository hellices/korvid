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

## Turn latency diagnostics

Every turn is timed against a single monotonic clock, so you can see *where* a
slow turn spent its time without guessing. The measurement is
provider-neutral — the same for OpenAI, a local Ollama model, or the offline
test provider — and it never reads a prompt, a tool argument, a tool result,
or any provider payload.

**Live phase in the status line.** While a turn runs, the status line names
the phase it is in rather than a generic spinner:

- *waiting for model* — preparing or running the first provider round.
- *running `<tool>`* — a tool call is executing. Its human-readable label can
  include arguments such as pod or namespace; diagnostic logs retain only
  the registry tool name.
- *composing answer* — a later provider round is streaming the answer after a
  tool result went back.

**Compact summary on the finished turn.** When a turn ends, one dim line is
added to the transcript, for example:

```
2 model rounds · model 3.4s · tools 1.1s · ↑1.8k ↓200 tok · prompt 2.0s · generate 1.0s · other wait 0.4s
```

It reports the number of provider rounds and the wall time spent in the model
and in tools. Token counts and the prompt evaluation, generation, and other-wait
split appear only when native provider metrics include those measurements.
Other wait includes transport and queueing. A **failed or interrupted** turn is never shown as a clean success:
its summary is prefixed with `failed ·` or `interrupted ·` so the timings are
never mistaken for a completed answer's.

**Structured logs.** The same snapshot is emitted once per turn to the
`korvid.agent.diagnostics` logger at `INFO`, keyed by a locally generated,
non-sensitive correlation ID. To record JSON lines without disturbing the
TUI, launch it with a file handler attached only to this logger:

```bash
python -c 'import logging, runpy; log = logging.getLogger("korvid.agent.diagnostics"); log.setLevel(logging.INFO); log.addHandler(logging.FileHandler("korvid-agent-timing.log", encoding="utf-8")); log.propagate = False; runpy.run_module("korvid", run_name="__main__")'
```

Each record includes the turn outcome and total duration, plus per-round
`request_started_at_seconds`, `request_acknowledged_at_seconds`,
`first_event_at_seconds`, and `first_content_at_seconds`. These are monotonic
offsets from that round's start. Dispatch is only an attempt; acknowledgement
records when transport acceptance was proven, not a socket-write timestamp.
The first event may be reasoning or usage; first content is a nonempty text
or tool-call event. Missing boundaries and optional metrics remain unknown,
not measured zeroes. `other wait` is shown only for rounds with provider totals
and includes local preparation/transport overhead as well as possible queueing.

Only an explicit allowlist reaches the log record — durations, token counts,
round numbers, registry tool names, the per-tool success flag, the correlation
ID, and the turn outcome. There is no generic object serialization, so a
prompt, a reasoning trace, a tool argument or result, a Kubernetes object, a
credential, or a raw provider payload can never ride along.

**Provider-reported timings (Ollama).** korvid always reports its own monotonic
round and tool timings. The panel header tracks ordinary token usage separately;
token counts in the diagnostic summary require optional native metrics.
Ollama additionally reports native total, load, prompt-evaluation, and generation
durations. There is no native queue-duration counter: `other wait` is inferred
from local round time minus provider total and can include transport or adapter
overhead. The LiteLLM adapter would
otherwise discard these durations, so korvid captures each request's terminal
HTTP frame immediately before LiteLLM transforms it, reusing LiteLLM's own JSON
decode for streaming and non-streaming responses rather than buffering a second
copy. Only the six allowlisted numeric counters are retained; the model, prompt,
response text, context, and every other raw field are discarded. If an
Ollama-compatible endpoint omits a counter, that metric is simply absent while
korvid's local monotonic timings remain available.

## What counts as a finished answer

A streamed answer is accepted only when the provider's own protocol says it
finished — `[DONE]`, `done: true`, or a `finish_reason` the provider sent —
and reading stops at that point. A stream that ends without it fails the
turn: the text that arrived stays on screen, but its tool calls and token
counts are discarded rather than treated as a complete response. What an
adapter buffers before the turn's budget can see it is bounded as well: a
call's arguments, how many calls one response may open, reasoning kept for
the next request, and the wizard's connection-test reply. The reply bound
is a refusal, not a trim: an answer past it fails the test rather than
being shown cut short.

### Self-hosted endpoints and proxies

A gateway that streams tokens but forwards no stop signal is refused, and
`:ai`'s connection test is where you will see it first. For an
OpenAI-compatible endpoint the signal is the `finish_reason` the provider
itself sent — the body ending cleanly is not one, and neither is a bare
`data: [DONE]`, which some proxies add themselves. Ollama's native API
sends `"done": true`; the Copilot dialect sends the wire's `[DONE]`.

To see what your gateway really forwards, stream one request through it:

```sh
curl -N -H 'Content-Type: application/json' \
  -d '{"model":"MODEL","stream":true,"messages":[{"role":"user","content":"ok"}]}' \
  https://gateway.internal/v1/chat/completions | grep -c finish_reason
```

`0` means the field is being dropped or rewritten in transit: upgrade the
proxy, or turn off any middleware that rebuilds streamed chunks. Until
then, a non-streaming-only backend is unaffected — the rule applies to
streams, which is where "the connection died" and "the answer ended" are
otherwise indistinguishable.

## Connect a provider

`:ai` (alias `:agent`) is the quickest path. On a first run it opens the setup
wizard — model, authentication, a live test call — saved to
`~/.config/korvid/config.yaml`; once profiles exist it opens the profile
manager, where `Enter` activates the highlighted profile, `a`/`e`/`d` add, edit
and delete it, and `t` sets the tier. `:model` prints the active model and
`:model <name>` switches it; `:ai off` releases the connection without
discarding the config.

When the test call fails, the wizard stays open and shows korvid's own
sentence for what happened — a refused credential, a model the provider does
not know, an answer that never finished. A failure korvid has no written
words for is reported as "the connection test failed" and the underlying
reason is written to the log instead: those messages come from the library,
the keychain or the endpoint, and they routinely quote the key that was
refused. `Ctrl+R` retries with everything the wizard already collected.

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

`options` carries **model parameters** only. `model`, `messages`, `stream`,
`tools`, `tool_choice`, `base_url`, `api_key`, `custom_llm_provider`, any
credential-shaped key and LiteLLM's controls (`mock_*`, `fallbacks`, callbacks,
`litellm_*`) are korvid's, and are dropped from a profile: none can re-route a
request, mute the agent's tools or fabricate an answer.

Provider connections are configured only through `agent.profiles`; `agent.active`
selects one, and `active: null` disables the agent. Model tuning belongs in the
selected profile's `options`. Unsupported root or `agent` settings are
configuration errors rather than alternate input formats.

Configurations from versions that accepted flat `agent.provider`,
`agent.model`, `agent.base_url`, or `agent.ollama.*` settings must be moved
manually; they are no longer migrated on load. For example:

```yaml
# before
agent:
  provider: ollama
  model: qwen3:8b
  base_url: http://localhost:11434
  ollama:
    num_ctx: 8192

# after
agent:
  active: default
  profiles:
    default:
      model: ollama/qwen3:8b
      endpoint: http://localhost:11434
      auth: {method: none}
      options:
        num_ctx: 8192
```

See the [unreleased migration notes](release-notes/unreleased.md#current-configuration-and-extension-contracts)
for the complete set of removed compatibility inputs.

!!! warning "GitHub Copilot"

    Copilot support uses an **unofficial internal API** that may change or
    break without notice, and requires an active GitHub Copilot
    subscription.

Entra ID ships in its own extra; a tool-managed install reinstalls the complete
set:

```sh
uv tool install --force 'korvid[all,entra]'
# or
pipx install --force 'korvid[all,entra]'
```

In a source checkout, `uv sync --extra entra` does the same job.

A non-standard transport registers a `SpecialFlow` on the `korvid.provider`
entry point; custom authentication can use a `korvid.credential` chain instead.
[Provider plugins](provider-plugins.md) describes both extension points.

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
