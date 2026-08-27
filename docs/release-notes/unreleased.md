# Unreleased

These notes cover changes on `main` that have not been tagged yet. Released
versions are listed under [Release notes](v0.3.0.md).

## Breaking: the agent's configuration and plugin APIs

The embedded agent was rebuilt as one interaction harness — a single
`AgentSession` over one `AgentEngine`, with the provider boundary, tool
dispatch, prompt composition, durable history and evidence ledger each owned
by exactly one component. Two public surfaces changed with it.

### `agent.profile` and `agent.prompts` were removed

korvid **refuses to start** when either key is present and prints the
replacement. Silently ignoring a prompt override would leave a deployment
believing its wording was still in effect.

| removed | replacement |
| --- | --- |
| `agent.profile: small` | `agent.model_tier: low` — or omit the key |
| `agent.profile: full` | `agent.model_tier: high` — or omit the key |
| `agent.prompts.append` | `agent.rules` (a list of short house rules) |
| `agent.prompts.system` / `system_file` | *(none)* — grind the tier pack in the eval harness |
| `agent.prompts.tool_descriptions` | *(none)* — deployment overrides removed: the low tier ships its own versioned, bounded wording by exact tool name; the high tier and the MCP server still use the registry's wording |

```yaml
# before
agent:
  profile: small
  prompts:
    append: "House rule: never include node names in an answer."

# after
agent:
  model_tier: low
  rules:
    - "Never include node names in an answer."
```

Omitting `agent.model_tier` is now the recommended setting: routing resolves
the tier from what the provider reports, then korvid's shipped model catalog,
then a conservative `low` fallback. The agent panel header shows the resolved
route as `tier (source)` — for example `low (catalog)` — so both the tier and
the reason for it are visible without re-reading configuration.

Every other `agent:` key keeps its meaning: `provider`, `base_url`, `model`,
`auth`, `api_key_env`, `follow`, `disable_in_protected`, and the
`agent.ollama.*` tuning knobs.

### Migration warning: a large `agent.rules` block can now fail to start

The low tier's static prompt — the safety contract, the common role, the
tier pack, and the armed-capability clauses, fully armed (writes and both
screen tools) — is exactly **4,283** characters of the **6,000-character**
budget `PromptHarness` enforces for it (25% of the tier's 24,000-character
history budget), leaving **1,717 characters** of headroom for
`agent.rules` and any provider/exact-model overlay. This budget is new:
the retired `agent.prompts.append` mechanism enforced no such share of the
history budget, so a rule set (or `system`/`append` text migrated
verbatim into `agent.rules`) that the old profile-based agent accepted
without complaint can now be too large. Agent composition raises
`StaticPromptTooLargeError` instead of silently crowding out the conversation;
initial application startup catches that error, warns, and starts with the
agent disabled. Shorten the rule set (or move guidance the tool descriptions
and safety contract already cover) before reconnecting the agent.

### Provider plugin API 1 → API 2

`PROVIDER_PLUGIN_API_VERSION` is now `2`, and a plugin declaring a different
version is rejected at load. `LLMProvider.name` was replaced by two
properties:

| API 1 | API 2 |
| --- | --- |
| `name -> str` | `descriptor -> ModelDescriptor` (provider id **and** model tag) |
| *(nothing)* | `capabilities -> ModelCapabilities` |

`capabilities` is how a plugin participates in tier routing. Reporting
nothing is valid — return `ModelCapabilities.unknown()` and korvid falls back
to its catalog and then to `low`. `descriptor.provider` must equal the
plugin's registered entry-point name, and `supports_tools=False` is a hard
stop rather than a routing hint. See
[provider plugins](../provider-plugins.md) for the full surface and a
worked adapter.

## What tiers actually change

| | low | high |
| --- | --- | --- |
| iterations per turn | 6 | 15 |
| retained history | 24,000 chars (hard bound) | 120,000 chars |
| per tool result | 3,000 chars | 8,000 chars |
| tool calls per response | 1 | provider-confirmed parallel, else 1 |
| screen tools armed | `open_logs`, `open_describe` | all five |

## The safety perimeter is unchanged

Nothing in this rebuild moved a security boundary. Every write tool the
environment arms still opens the same approval dialog, at every tier, and
only a user keystroke executes it; a write still carries the preconditions
for the exact target UID established before approval; audit logging is still
fail-closed, so a write whose audit record cannot be written does not run;
read-only mode still means no write schema is offered at all; there is
still no shell or free-form `kubectl` tool at either tier, so the agent's
whole cluster surface remains the structured tool registry — the resolved
policy arms only the registry's own exact tool names, the registry
validates every dispatch target against its import-time metadata, and the
`ToolExecutor` rejects any name outside that registry as an unknown tool
and performs its own explicit, typed argument validation before a write
reaches the cluster (the tool's declared JSON schema is model-facing
wording, not the runtime check); and every tool result still passes the
masking pipeline before it reaches the model or the provider. House rules
are composed *after* the immutable safety contract and cannot widen it.

## Other agent-visible changes

- **Evidence citations.** Each successful cluster read mints a numbered
  reference the answer cites; opening a citation navigates to the exact
  object the read looked at. Screen actions and writes never mint evidence.
  Switching Kubernetes context clears the ledger.
- **Direct control.** The agent drives the same panes your keys drive.
  Pressing a normal key mid-turn keeps working, and the next turn starts
  from wherever you left the screen.
- **Optional install unchanged in shape, stricter in behaviour.** The agent
  still ships in the `[agent]` extra; a base or MCP-only start now provably
  loads no engine, gateway or provider module, and an enabled agent with the
  extra missing fails with the exact install command instead of degrading
  silently.
- **Eval artifacts.** Journey artifacts publish `successful_journeys` (whole
  conversations whose every turn succeeded) and the markdown tables label
  their verdict columns `all-turn journeys` and `correct diagnosis`, so the
  two different measurements are no longer both called "success". A run's
  per-turn `success` is derived from its outcome and can no longer
  contradict it.
- **Truncation marker.** A tool result too large for the tier's budget is
  still shortened from the middle, and the marker the model reads there now
  names the budget it hit: `… [middle truncated — tier result budget]`. It
  is a small change to what the model sees, so a campaign comparing scores
  across this release is comparing two slightly different prompts.
