# Low-Model Fast Operations Design

## Goal

Reduce latency for low-resource local models by eliminating avoidable model
generation while preserving Korvid's approval, masking, evidence, target
revalidation, and fail-closed audit guarantees.

## Scope

This change adds three bounded optimizations:

1. successful direct `open_logs` and `open_describe` operations may terminate
   without a second provider round;
2. Ollama exposes `num_predict` so operators can cap generated tokens; and
3. the LOW prompt requires immediate tool dispatch and a short
   root-cause/evidence/next-operation response.

It does not add intent classification, bypass the model before the first tool
call, change write behavior, or introduce provider-specific behavior into the
agent engine.

## Terminal UI Operations

`open_logs` and `open_describe` receive an optional `continue_analysis` boolean
argument. It defaults to `false`.

- `false`: after a successful UI action, the tool harness returns a fixed,
  trusted completion message. The engine stores that message, emits it to the
  UI, completes accounting, and does not issue another provider request.
- `true`: the engine stores the tool result and continues normally so the
  model can fetch evidence and analyze it in later rounds.
- failed or rejected UI actions always continue normally so the model can
  recover or explain the failure.

The trusted completion message never interpolates model arguments or tool
result text. Cluster-controlled and model-controlled strings therefore cannot
escape the existing outbound and masking boundaries through the fast path.

## Ollama Output Budget

`agent.ollama.num_predict` is an optional positive integer. When configured,
the native Ollama provider sends it as `options.num_predict`. When omitted,
Korvid preserves Ollama's existing behavior.

This setting remains provider-specific because `num_predict` is an Ollama
serving control, while the engine continues to enforce its provider-neutral
character and history ceilings.

## LOW Prompt Contract

The LOW pack instructs the model to:

- avoid narrating plans or internal reasoning;
- call the single best tool immediately;
- set `continue_analysis=false` for direct show/open/display requests;
- set it to `true` only when the user explicitly requests analysis;
- return at most three short bullets: root cause, decisive evidence, and next
  operation; and
- avoid generic Kubernetes advice and repetition of tool output.

The immutable safety contract remains unchanged and precedes these
instructions.

## Error Handling

Invalid `num_predict` values are ignored with the existing configuration
warning mechanism and revert to `None`. A terminal operation is recognized
only after the bridge reports success. Provider, bridge, masking, and audit
errors retain their current paths.

## Testing

- Provider tests pin `options.num_predict` presence and omission.
- Configuration and wiring tests pin parsing and propagation.
- Tool-harness tests pin terminal metadata only for successful direct open
  operations.
- Engine tests prove the terminal path makes one provider call, stores a
  protocol-complete turn, emits a trusted acknowledgement, and preserves the
  normal second round for analysis and failures.
- Prompt tests pin the operation-first instructions.
- Targeted Ruff, mypy, pytest, and tach checks validate the touched surfaces.

