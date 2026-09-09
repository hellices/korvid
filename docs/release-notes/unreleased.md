# Unreleased

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

See the [release history](https://github.com/hellices/korvid/releases) for
published versions and their migration notes. Candidate release notes can
appear in the navigation before publication.
