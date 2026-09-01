# MCP Log and Event Producer Redaction Design

- Date: 2026-09-01
- Issue: #330
- Status: Approved for implementation

## Goal

Redact credentials from Kubernetes log lines and event messages at the tool
producer boundary, before size clamping, so MCP clients and embedded providers
receive the same safe text while `ToolOutcome` retains deterministic redaction
evidence for Korvid's internal agent and audit surfaces.

## Context

Issue #331 established `korvid.core.redaction` as the shared structural and
free-text primitive. Structured resource reads already redact before leaving
`ToolExecutor`, and the embedded agent has an additional outbound-policy pass.
However, `ToolExecutor._get_logs` and `_get_events` currently join
cluster-controlled text into a `ToolOutcome` without producer-side redaction.
The MCP server returns `ToolOutcome.text` directly, so it does not benefit from
the agent-only final pass.

This is the second item in the active local-first MCP safety sequence. It does
not add transport authentication, OAuth, or stdio behavior.

## Approaches considered

### 1. Reuse the producer projection helper (selected)

Extend the existing `_projected` helper to preserve optional
`ToolOutcome.container` and `ToolOutcome.incarnation` metadata. Route the full
rendered `get_logs` and `get_events` text through that helper.

This keeps free-text redaction, record creation, and fail-closed behavior at one
producer boundary. `execute_recorded` remains the single final size clamp.

### 2. Redact independently in both handlers

Each handler could allocate records, call `redact_text`, and build its own
`ToolOutcome`. This is mechanically small but duplicates a security sequence
already implemented by `_projected`, increasing the chance that paths, error
flags, or metadata drift.

### 3. Redact every untrusted-text result in `execute_recorded`

The dispatcher could consult registry result formats and redact all text before
clamping. This centralizes the operation but loses producer-specific path and
metadata context, reprocesses UI and observability outcomes that already own
their shaping, and broadens the behavior beyond issue #330.

## Design

### Producer projection

`_projected` remains responsible for:

1. accepting a full, already rendered text result;
2. calling `redact_text` before any size transformation;
3. returning the redacted text and all `RedactionRecord` entries in a
   `ToolOutcome`.

Add keyword-only `incarnation: str | None = None` and
`container: str | None = None` parameters and copy them into the outcome.
Existing observability callers keep their current behavior because both
parameters default to `None`.

### Pod logs

`_get_logs` continues to resolve the default container and collect the requested
bounded number of Kubernetes log lines. After joining every line with newlines,
it calls `_projected(text, "logs", container=resolved_container)`.

The full joined text is redacted before `execute_recorded` calls `cap_result`.
This order prevents a credential assignment split at the head/tail clamp from
losing the classifier that identifies its value.

The existing no-incarnation rule remains unchanged because manifest lookup and
log streaming are separate name-based reads.

### Kubernetes events

`_get_events` keeps the current live-object lookup and UID-scoped event query.
It renders all event lines, then calls
`_projected(text, "events", incarnation=uid)`.

The no-events result also uses the helper so every successful event producer
has one consistent return path and retains the UID.

### MCP behavior

The MCP server remains unchanged. It continues to dispatch through
`ToolExecutor.execute_recorded` and return `ToolOutcome.text`. Because redaction
now happens in the producer, MCP receives masked log/event text rather than
relying on an agent-only outbound pass.

The existing MCP test that characterizes raw credential leakage is replaced
with a masking assertion. Add a corresponding event test. Together they cover
representative password, token, and authorization assignments.

## Error handling

If `redact_text` raises `RedactionError`, `execute_recorded` converts it into
`ToolResultBlocked` with a constant shape-only message. No original log line or
event message is included in that error.

`ToolExecutor.execute` converts the refusal to a safe error string for
string-only consumers. The MCP server catches the recorded refusal and returns
the same safe error shape. This preserves the current fail-closed contract.

Cluster read failures continue to use the existing typed/error result behavior;
the change applies only after a successful text read.

## Testing

Use TDD and cover the following boundaries:

- Executor log output masks password, token, and authorization values.
- Executor event output masks credential values while preserving event type,
  reason, count, and UID.
- `ToolOutcome.redactions` contains deterministic `logs` or `events` paths.
- The resolved log container and event incarnation remain unchanged.
- A long log credential crossing the eventual clamp boundary is redacted before
  clamping.
- A `RedactionError` in either producer becomes `ToolResultBlocked` without raw
  data.
- MCP `get_logs` and `get_events` return masked text with representative
  password, token, and authorization values.
- Existing agent outbound re-redaction and observability projection tests
  remain unchanged and pass.

Run targeted executor and MCP tests while iterating. Before completion, run
Ruff, mypy, Tach when imports change, the full repository gate, and the PR
review loop defined in `AGENTS.md`.

## Non-goals

- New credential heuristics in `core.redaction`
- Redacting logs in the Kubernetes client or TUI log display
- MCP authentication, stdio transport, OAuth, or transport identity
- Changing log tail limits, event UID scoping, result caps, or unrelated error
  wording; redaction refusals intentionally use the constant safe message above
- Refactoring unrelated diagnosis or observability producers
