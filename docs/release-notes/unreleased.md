# Unreleased (main)

This page tracks changes merged to `main` that are not part of the
[latest published release](https://github.com/hellices/korvid/releases/latest).
The [current release milestone](https://github.com/hellices/korvid/milestone/6)
tracks the current release scope.

## Search actions by intent

Press `Ctrl-P` to search korvid actions and built-in commands — by intent
(`scale`, `logs`), by an action's canonical id (`delete_resource`), or by a
command's `:` spelling (`:pulse`). Each result reads *category · what it does ·
how to run it*, showing the effective remapped key or the canonical command,
and explains when an action does not apply to the current view or would
currently do nothing (nothing selected, no hint on the row, no search running).
Selecting a write action still opens the same approval flow and requires a
fresh confirmation keystroke. `Esc` closes the palette, and the key itself
remaps under `open_action_palette`. On a small terminal the palette takes the
height it needs instead of clipping a row's key or reason, and resizing the
terminal under the open palette keeps the row you were on in view and re-draws
it whole at the new size.

## Keys typed right after the command or filter bar closes

Pressing `:` or `/`, submitting or cancelling it and immediately pressing the
next key no longer loses that key. A dismissed bar now releases the keyboard as
it hides, instead of waiting for the next repaint to do it, so a quick `:helm`
then `i`, `:nodes` then `?`, or `:nodes` then `D` reliably opens the Helm chart
search, the help overlay and the drain approval.

## Ambient Pulse / Problems

The base TUI now keeps a one-line attention summary visible while you browse.
Open `:pulse` or `:problems` for current Pod/Deployment findings, recent Warning
Events (including unfamiliar controller reasons), and explicit observation gaps.
Enter follows a verified resource identity through the existing navigation path.

Reads, retention, and refresh frequency are bounded; Pulse shares the existing
Warning stream rather than adding watches. Updates do not move selections or
interfere with approval dialogs. “No matched problems” is not a cluster-health
guarantee. See [Pulse / Problems](../pulse.md) for coverage, limits, and keyboard use.

## Current configuration and extension contracts

This pre-1.0 cleanup removes obsolete input formats rather than maintaining
automatic conversion:

- Configure connections with `agent.profiles` and select one with `agent.active`.
  Use `active: null` to disable the agent and put model tuning in a profile's
  `options`. Flat provider settings are no longer read or migrated.
- Unsupported root and `agent` settings raise ordinary configuration errors.
  There are no per-key upgrade handlers.
- Provider extensions use `SpecialFlow` declarations and credential extensions
  use `ProviderDefaultCredential`. The disconnected provider-factory registry
  and its compatibility API are removed.
- Eval runs put any required provider prefix directly in
  `KORVID_EVAL_MODEL` and use `KORVID_EVAL_API_KEY_ENV` to name a credential
  variable. LiteLLM-resolvable bare model tags remain valid; the separate
  provider-prefix and inline eval-key variables are no longer supported.

These changes do not remove Kubernetes event-field support, OS support, or the
write-approval, masking, and audit controls. See the current
[agent configuration](../agent.md) and [extension contracts](../provider-plugins.md).

## Turn latency diagnostics

Every agent turn is now timed against a single injected monotonic clock, and
the timing is provider-neutral (OpenAI, a local Ollama model, or the offline
test provider are measured the same way). While a turn runs, the status line
distinguishes *waiting for model*, *running `<tool>`*, and *composing answer*
instead of a generic spinner. When a turn ends, one dim summary line reports
the provider-round count and model/tool wall time, plus token counts and the
prompt/generate/other-wait split when native provider metrics include them. A failed or
interrupted turn is prefixed with its outcome so it is never mistaken for a
success.

The same snapshot is emitted once per turn to the `korvid.agent.diagnostics`
logger at `INFO`, keyed by a locally generated, non-sensitive correlation ID.
Only an allowlist of numeric timings, token counts, round numbers, registry
tool names, per-tool success flags, and the outcome reaches the record — never
a prompt, reasoning, a tool argument or result, a Kubernetes object, a
credential, or a raw provider payload. Approvals, the audit log, masking,
cancellation, and token accounting are unchanged. See
[the agent guide](../agent.md#turn-latency-diagnostics) for how to read and
enable it, including how native Ollama timings are retained.

## Agent implementation cleanup

- Low and high behavior now lives in separate tier files, with their prompts,
  tool-surface selection and budgets visible together. Default routing remains
  unchanged; approval, audit, masking and execution remain shared. Low-tier
  tool-description version 3 now distinguishes `open_describe` display requests
  from `get_resource` reads, matching the existing prompt rule and changing the
  corresponding eval prompt digest.
- **Breaking:** The unwired `ProviderPlugin` construction API and its old registry have been
  removed without compatibility shims. Use the
  [SpecialFlow provider contract](../provider-plugins.md) and import agent
  contracts from their defining modules rather than package-level re-exports.
- **Breaking:** Evaluation artifacts no longer duplicate overlay provenance in
  `meta.policy.overlays`. Read `meta.prompts.overlays` instead; it contains the
  effective evaluation overlay IDs, or an empty array for a default run.
- Unused checkpoint return objects, redundant state and empty production prompt
  overlay registries have been removed. In-memory rollback and evaluation prompt
  experimentation remain supported.

See the [release history](https://github.com/hellices/korvid/releases) for
published versions and their migration notes. Candidate release notes can
appear in the navigation before publication.
