# AI agent

Requires the `[agent]` extra (see the README's
[installation section](https://github.com/hellices/korvid/blob/main/README.md#installation)).
Press `Ctrl-A` to open the agent panel — a chat sidebar that answers questions
about the cluster you are looking at.  The agent sees your current screen
context (view, namespace, selected resource, active filter) and inspects the
cluster through read-only tools: fetching manifests, logs, events, and resource
listings, plus compound diagnostic tools — `diagnose_pod` gathers a broken pod's
container states, owner chain, warning events, and targeted log excerpts in a
single deterministic call; `diagnose_service` checks whether a Service has
current ready EndpointSlice endpoints, reporting structured versioned findings
with explicit evidence gaps when EndpointSlice data is unavailable (e.g. RBAC
denial); `diagnose_pvc` checks why a PersistentVolumeClaim is not Bound (one
GET for Bound/Lost; for unresolved claims Warning events are read, and
StorageClasses are listed only when no decisive failure event, pre-bound
volume, or explicit-empty/static-binding evidence already determines the
result — RBAC denials become explicit evidence gaps) — projected evidence instead of
raw YAML dumps, which is where small local models otherwise fail.  It can also drive the TUI itself — navigate views, apply filters,
drill down, and open the log pane or describe screen — so "show me the crashing
pod's logs" lands you in the actual log viewer instead of a text dump.
Tool results are capped at 8,000 characters — manifests are shrunk
structurally so they stay valid YAML the model can parse — and `Secret`
data is masked before it ever reaches the model.  The header shows the model name and cumulative
token usage (`~` marks estimated counts when the provider omits usage data).

The agent can also *request* write operations — delete, scale, rollout
restart, and (on clusters that expose the `pods/resize` subresource,
Kubernetes 1.35+) in-place pod resize — but it can never execute them
itself.  Each request opens
the same confirmation dialog as the keybindings (marked with a ⚠ in the tool
log), and only your keystroke in that dialog approves it; an unanswered
dialog expires without executing anything.  Every executed write — yours or
agent-requested — is recorded in the fail-closed
[audit log](ops.md#the-safety-model).

## Stopping and correcting a turn

The input stays enabled while the agent works.  Press `Ctrl-X` to stop the
running turn: the partial answer stays in the transcript marked
`⏹ interrupted`, in-flight tool lines are marked, and token usage is
committed (estimated for the interrupted stream — the prompt is charged
once the request reached the provider, even if nothing had streamed back
yet, and not charged at all if the request never left the machine).  Typing a
new prompt while the agent runs is **interrupt-and-submit** — the correction is
echoed immediately, the old turn is cancelled, and a fresh turn starts with the
new prompt (only the latest submission is kept).  The conversation history is
repaired so the model never sees a half-finished tool exchange.  Interrupts
respect the write gate: a pending approval dialog is dismissed without
executing, while a write the user already approved always runs to completion
and is audited.

## Inspecting what the agent sends

`:ai payload` opens a read-only view of the exact sanitized request most
recently sent to the provider (disabled while a turn is running, and only
available once at least one request has been sent this session). It is the
literal payload — not a re-derived approximation — because every message,
tool result, and tool-call argument passes through the same
`OutboundPolicy` redaction step whether the request goes to a hosted model
(GitHub Copilot, Azure OpenAI, OpenAI, Anthropic-compatible) or a local
endpoint (Ollama, a self-hosted OpenAI-compatible server): `Secret` values,
the `kubectl.kubernetes.io/last-applied-configuration` annotation, and text
matching known credential key/value patterns are all masked before the
inspector — or the network — ever sees them. A local endpoint receives the
same sanitized payload as a hosted one; korvid does not additionally
authenticate that the configured `base_url` is really the process you
intend it to be.

The snapshot is the latest request that was actually handed over, and a
later turn that never reaches the provider does not erase it: a prompt the
outbound policy blocks, or a turn rolled back mid-flight, sends nothing
and so has no payload of its own to show. `:ai payload` keeps showing the
last real handoff — which is precisely the request you want to read after
something was refused. Only a payload that reached the transport replaces
it. Preparing a request is not sending it, and neither is *calling* the
provider: `complete()` is an async generator, so its body — the HTTP
request included — does not run until the stream is consumed. korvid's
built-in adapters acknowledge the moment the transport accepts the request
(before they judge the response status, because an HTTP 500 answer still
means the payload arrived), and a provider that fails earlier — no
credentials, unresolvable host, connection refused — leaves the previous
handoff on display rather than claiming one that never happened. A
third-party plugin cannot acknowledge (the plugin event contract knows
`text_delta`, `tool_call`, `usage` and `done`); its request is recorded on
the first event it yields, which is equally proof the request ran, and a
plugin that yields nothing at all records nothing.

Press `e` in the inspector to export the displayed payload to a private
JSON file — `write_private_text` creates it with `0o600` permissions (see
the platform caveat in [the threat model](threat-model.md)), never
overwrites an existing export, and confirms the exact path once written
(default location: `$XDG_DATA_HOME/korvid/agent-payloads`, falling back to
`~/.local/share/korvid/agent-payloads`). Exported payloads are not
automatically cleaned up — they persist on disk exactly like any other
file you save until you delete them yourself.

**The payload is sanitized, not anonymized.** Resource names, namespaces,
labels, and other stable cluster identifiers still appear in it — sanitization
only removes secret material and known credential patterns, not anything that
identifies your cluster. Treat an exported payload with the same care as any
other cluster-derived export. See [`docs/threat-model.md`](threat-model.md)
for the full boundary, what the inspector does not show (transport headers
and credentials never enter the canonical payload), and the documented
residual risks — notably that arbitrary secrets embedded in free-form log or
event text cannot be guaranteed detectable.

`protected_contexts` and `agent.disable_in_protected` (see
[Protected contexts](ops.md#protected-contexts)) control whether the agent
runs at all on production-labeled contexts; they do not change what
`OutboundPolicy` redacts once a request is allowed to be built.

## Cloud-provider awareness

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

## Setup

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

Entra ID auth needs the optional extra. For a tool-managed application install,
reinstall the complete desired extra set:

```sh
uv tool install --force 'korvid[all,entra]==0.3.0'
# or
pipx install --force 'korvid[all,entra]==0.3.0'
```

Use `uv sync --extra entra` for development. Configs written before
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

### Provider plugins

If your backend already speaks an OpenAI-compatible API, prefer
`provider: openai-compat` (or one of its built-in aliases) over a plugin.
Third-party provider plugins are for backends whose protocol or auth flow
truly differs from korvid's built-ins.

Plugins are configured manually in `config.yaml` today — the `:ai` wizard only
offers the built-ins above.  Plugins also run as trusted, in-process Python
code inside the korvid process, so install only packages you trust.  A plugin
receives only the same sanitized canonical payload
`OutboundPolicy` builds for a built-in provider — but once received, trusted
plugin code is free to mutate, retain, log, or independently transmit that
data; korvid has no visibility or control past the handoff (see
[`docs/threat-model.md`](threat-model.md)).  See
[Provider plugins](provider-plugins.md) for the exact API-v1 contract,
entry-point registration, event limits, option limits, and selected-only
loading behavior.

For endpoints signed by a corporate/private CA (internal Ollama, vLLM, or
an OpenAI-compatible gateway), set `network.ca_bundle` in `config.yaml` —
the same trust covers the live agent and the wizard's connection test, and
verification can never be disabled.  See the
[air-gapped guide](airgap.md) for details.

Without configuration, `Ctrl-A` shows a setup hint pointing at `:ai`.

### Turning the agent off and on

`Ctrl-A` only toggles the panel's *visibility* — the runtime stays
connected.  To actually disconnect for the session, use `:ai off`: the
provider connection is released, the status bar flips to `AI off`, and
prompt submission is disabled while the transcript stays visible.  The
configured provider, model, profile, and credentials are all kept (nothing
is rewritten in `config.yaml`), so a bare `:ai` reopens the wizard with the
current settings and reconnects when applied.  `:ai off` is refused while a
turn is running — stop the turn first (`Ctrl-X`) — and is a no-op when the
agent is already off.

## Capability profiles

Small local models (3B–14B) handle the agent's default surface — up to 15
tools, 15 iterations, ~120k characters of retained history — poorly: they are
competitive on simple single-function calls but fall behind sharply when
choosing among many functions, and they degrade with context length far
below their advertised windows. `agent.profile: small` gives them a surface
they can actually handle:

```yaml
agent:
  provider: ollama
  base_url: http://localhost:11434
  model: qwen3:8b
  profile: small   # default: full
```

The `small` profile keeps every read and write tool (writes still pass the
approval gate) but trims verbose tool descriptions, offers only the two
evidence-showing UI tools (`open_logs`, `open_describe`) instead of all
five, caps turns at 6 tool iterations with one tool call per response
(extra parallel calls are discarded without entering history) and at most
3k characters per tool result (text results are compacted keeping head and
tail so a report's trailing evidence sections survive; manifests are
shrunk structurally and stay parseable; when parallel calls were
discarded, a short fixed-size notice rides on top of the capped result),
and retains ~24k characters of history as a hard bound (sized to a
realistic local
serving context, not the model's advertised window) — a turn whose
retained text and tool-call arguments would push a follow-up request past
that bound ends early instead of sending it, and a single prompt that
cannot fit on its own is rejected without disturbing the next one. The system
prompt is swapped for a short one with a single worked example. `full`
reproduces the
default wiring exactly, so frontier models are unaffected.

The `:ai` wizard suggests `small` automatically when the provider is
Ollama and no profile has been configured yet — an explicit
`agent.profile` (either value) is always preserved. The agent panel
header shows `[small]` so you always know which
mode is live. Compare the profiles on your own endpoint with the eval
harness: `python -m korvid.evals --profile small` (see below).

## Tuning the agent for your model

If a local model behaves poorly, rewriting the system prompt is usually the
*last* thing worth trying. Published evidence puts the levers in this order:

1. **Pick a different model.** Small models' tool-calling weakness is mostly a
   training property, not a prompting one, and no prompt closes that gap
   ([ToolLLM](https://arxiv.org/abs/2307.16789)). The
   [model scoreboard](https://github.com/hellices/korvid/issues/176) exists for
   this choice.
2. **Reword the tool descriptions.** Documentation quality drives tool-selection
   accuracy more than the prompt preamble does
   ([EasyTool](https://arxiv.org/abs/2401.06201),
   [Tool Documentation](https://arxiv.org/abs/2308.00675)) — which matches
   korvid's own measurement that tool and output shape move small models more
   reliably than extra prompt text.
3. **Fit the context.** `agent.profile: small` and `agent.ollama.num_ctx`
   matter more than wording once the serving context is short.
4. **Then, if a model still needs it, change the role statement.**

All of it lives under `agent.prompts`:

```yaml
agent:
  profile: small
  prompts:
    # Highest-leverage knob. Every request retransmits the schemas, so on a
    # short serving context this is both an accuracy and a token lever.
    tool_descriptions:
      get_logs: "Read recent container logs. One pod at a time."

    # Keep korvid's role statement and add your own rules after it.
    append: |
      House rule: never include node names in an answer.

    # Last resort: replace the role statement outright. Inline, or point at
    # a file — not both.
    system_file: ~/.config/korvid/prompts/small-system.md
```

`system` and `append` combine: replacing the role statement and adding
house rules is a coherent pair. Tool-description precedence is your
override first, then the `small` profile's built-in concise wording, then
the schema's own text.

korvid ships one prompt per capability tier and deliberately does not fork
them per model. Model *families* do need different chat templates, but that
is message formatting handled below korvid by Ollama or the serving engine —
not something a system prompt can fix.

**What you cannot override, and why.** The clauses describing writes,
read-only mode, and the screen tools are chosen from the tools actually
armed, not from configuration. This keeps the agent from being told about
a capability it was not offered, and keeps a read-only deployment offering
the equivalent `kubectl` command instead of a bare refusal. Overrides
replace the role statement; korvid still assembles the rest.

An override is local configuration, no more privileged than
`agent.provider`. It cannot weaken any safety behaviour: approvals, the
audit log, and read-only enforcement live in code, so an instruction to
"delete pods without asking" produces a model that tries and is refused.

Mistakes are reported, never fatal. A missing or unreadable file, a
non-UTF-8 file, an empty value, or both `system` and `system_file` set for
one slot warn at startup and **fall back** to the prompt korvid ships. Two
warnings are advisory only and leave your configuration active: an unknown
tool name (the other overrides still apply), and a prompt large enough to
crowd the profile's history budget — that one still uses your prompt, it
just tells you it is squeezing the conversation.

To find out whether your wording is actually better, measure it:

```bash
python -m korvid.evals --profile small --json baseline.json
python -m korvid.evals --profile small --json tuned.json \
  --system-prompt-file ~/.config/korvid/prompts/small-system.md
```

Each result file records which prompt produced it (see
[the eval harness](#agent-eval-harness) below).

## Follow mode

Small models rarely volunteer the screen tools (`open_describe`,
`open_logs`) — they call the data-returning reads and answer in text
while the TUI sits idle. Agent follow mode mirrors each successful
cluster read from a chat turn on screen, using the same mapping as MCP
follow mode: `list_resources` navigates the view, `get_resource` /
`get_events` / `diagnose_pod` / `diagnose_service` / `diagnose_pvc` open the describe view,
and `get_logs` opens the live log pane.

Follow is **on by default**. Disable it in `config.yaml`:

```yaml
agent:
  follow: false   # default: true
```

or toggle it live with `:ai follow off` / `:ai follow on` (bare
`:ai follow` flips the state). Mirroring never interrupts what you are
doing: failed reads (e.g. a 404) move nothing, and a mirror is refused
while an approval dialog or a describe screen you are reading is open.

## Agent eval harness

`korvid.evals` measures how well a model diagnoses cluster faults through
korvid's real agent runtime and tools. Each scenario is a YAML fixture — a
simulated cluster (manifests, events, log tails), a user question, and
deterministic grading assertions (keywords the answer must claim positively,
misdiagnosis keywords it must not claim — negated rule-outs are allowed, hedged
double-diagnoses fail — and tool results it must have fetched as
evidence). The
bundled pack covers crashloops (missing env config, unreachable dependencies,
bad commands), OOM kills, image-pull failures (auth and typo), failing
readiness and liveness probes, init-container failures, unbound PVCs, missing
ConfigMaps and Secrets, scheduling failures (insufficient CPU, node selector
mismatches, quota exhaustion), service selector mismatches, stuck rollouts,
node-pressure evictions, Job backoff exhaustion, and three healthy negative
controls.

Run it against any OpenAI-compatible endpoint (this talks to a live model, so
it never runs in CI — CI only smoke-tests the harness with a scripted provider):

```sh
export KORVID_EVAL_BASE_URL=http://localhost:11434/v1   # e.g. Ollama
export KORVID_EVAL_MODEL=qwen3:14b
# export KORVID_EVAL_API_KEY=...                        # if the endpoint needs one

uv run python -m korvid.evals --reps 3 --out report.md --json report.json
```

The report is a markdown table with per-scenario success and evidence-fetch
rates (did the model *observe* the ground-truth fact in the cluster — each
expected-evidence group checks the fetched content and that the call named
the right object under the right argument keys, whichever read tool the model
chose), resolvable-call and on-target rates (calls whose arguments name a
scenario evidence target — the correct-tool + correct-argument rate),
malformed-tool-call, write-attempt and safety-violation counts,
iteration counts, token usage (marked with `~` when a provider omitted stream
usage and the totals are heuristic estimates measured on the exact canonical
payload that was sent, plus the generated output), and wall-time variance across
repetitions. The model is offered korvid's write-tool schemas too — so it can
genuinely *attempt* a mutation — but the eval executor is unarmed (no approval
UI exists), so every write call fails; a write that succeeds anyway is counted
as a safety violation.
Custom scenario packs can be pointed at with `--scenarios DIR`, and
`--profile small` evaluates the reduced capability profile (trimmed
descriptions, 6-iteration budget, small system prompt — see
[Capability profiles](#capability-profiles)) so before/after numbers for a
small model come from the same pack.

### Conversational journeys

`korvid.evals.journeys_cli` keeps one runtime alive across multiple user turns,
measuring broad discovery, corrections, evidence pivots, stopping behavior, and
UI intent:

```sh
export KORVID_EVAL_BASE_URL=http://localhost:11434/v1
export KORVID_EVAL_MODEL=qwen3:8b
export KORVID_EVAL_TIMEOUT_SECONDS=300

uv run python -m korvid.evals.journeys_cli \
  --profile small --reps 3 \
  --out journeys.md --json journeys.json
```

The guarded `--live` mode reads real faults only from the dedicated
`aks-korvid-contract-test` context and a namespace beginning with
`korvid-agent-eval-`. See [evaluation methodology](evals/methodology.md),
[scenario catalog](evals/scenarios.md), and the [model scoreboard](evals/scoreboard.md).
