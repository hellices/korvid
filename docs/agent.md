# AI agent

Requires the `[agent]` extra (see the README's
[installation section](../README.md#installation)).
Press `Ctrl-A` to open the agent panel — a chat sidebar that answers questions
about the cluster you are looking at.  The agent sees your current screen
context (view, namespace, selected resource, active filter) and inspects the
cluster through read-only tools: fetching manifests, logs, events, and resource
listings, plus a compound `diagnose_pod` tool that gathers a broken pod's
container states, owner chain, warning events, and targeted log excerpts in a
single deterministic call — projected evidence instead of raw YAML dumps, which
is where small local models otherwise fail.  It can also drive the TUI itself — navigate views, apply filters,
drill down, and open the log pane or describe screen — so "show me the crashing
pod's logs" lands you in the actual log viewer instead of a text dump.
Tool results are capped at 8,000 characters and `Secret` data is masked before
it ever reaches the model.  The header shows the model name and cumulative
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
committed (estimated for the interrupted stream).  Typing a new prompt while
the agent runs is **interrupt-and-submit** — the correction is echoed
immediately, the old turn is cancelled, and a fresh turn starts with the new
prompt (only the latest submission is kept).  The conversation history is
repaired so the model never sees a half-finished tool exchange.  Interrupts
respect the write gate: a pending approval dialog is dismissed without
executing, while a write the user already approved always runs to completion
and is audited.

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
3k characters per tool result (compacted keeping head and tail so a
report's trailing evidence sections survive; when parallel calls were
discarded, a short fixed-size notice rides on top of the capped result),
and retains ~24k characters of history as a hard bound (sized to a
realistic local
serving context, not the model's advertised window) — a turn whose
retained text and tool-call arguments would push a follow-up request past
that bound ends early instead of sending it. The system
prompt is swapped for a short one with a single worked example. `full`
reproduces the
default wiring exactly, so frontier models are unaffected.

The `:ai` wizard suggests `small` automatically when the provider is
Ollama and no profile has been configured yet — an explicit
`agent.profile` (either value) is always preserved. The agent panel
header shows `[small]` so you always know which
mode is live. Compare the profiles on your own endpoint with the eval
harness: `python -m korvid.evals --profile small` (see below).

## Follow mode

Small models rarely volunteer the screen tools (`open_describe`,
`open_logs`) — they call the data-returning reads and answer in text
while the TUI sits idle. Agent follow mode mirrors each successful
cluster read from a chat turn on screen, using the same mapping as MCP
follow mode: `list_resources` navigates the view, `get_resource` /
`get_events` / `diagnose_pod` open the describe view, and `get_logs`
opens the live log pane.

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
usage and the totals are heuristic estimates covering message content, tool
schemas and tool-call payloads), and wall-time variance across
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
