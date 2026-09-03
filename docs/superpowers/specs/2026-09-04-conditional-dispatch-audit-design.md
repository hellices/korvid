# Conditional Dispatch Audit and AgentPanel Design

## Goal

Classify long conditional chains by responsibility, preserve branches whose
explicit ordering is part of correctness or security, and define the first
follow-up refactor for `AgentPanel.apply_event()`.

## Audit Method

The audit covers every function under `src/korvid/` with an `if`/`elif` chain
of at least three branches. Branch count is only a discovery signal. Each
candidate is classified by what the conditions mean:

- **Refactor:** selection by a stable event type, widget ID, or resource kind.
- **Extract only:** branching is valid, but a pure helper can make the rule
  easier to test.
- **Keep explicit:** ordering, validation, protocol parsing, or safety policy is
  clearer and safer as visible control flow.

The audit does not target guard clauses, type narrowing, validation, approval,
audit, redaction, or fail-closed behavior merely because they contain `if`.

## Audit Results

| Function | Longest chain | Classification | Rationale |
|---|---:|---|---|
| `ui/widgets/agent_panel.py::AgentPanel.apply_event` | 6 | Refactor first | Closed `AgentEvent` type dispatch with large branch bodies. |
| `ui/widgets/agent_setup_screen.py::on_option_list_option_selected` | 4 | Refactor later | Stable widget-ID dispatch, but provider-stage transitions remain explicit. |
| `ui/widgets/agent_setup_screen.py::on_input_submitted` | 4 | Refactor later | Stable widget-ID dispatch, but validation remains in handlers. |
| `evals/runner.py::_TurnTally.note` | 4 | Investigate shared abstraction | Event tally logic overlaps the journey runner but has different counters. |
| `evals/journey_runner.py::_TurnTally.note` | 4 | Investigate shared abstraction | A common typed event-folding helper may remove duplication without merging policies. |
| `ui/agent_workspace_bridge.py::snapshot` | 4 | Extract only | Pane ownership and focus precedence are ordered UI rules, not dispatch. |
| `evals/operation_grader.py::_retry_after_terminal_approval` | 4 | Keep explicit | Journal ordering is a safety grading policy. |
| `__main__.py::_make_watch_source.source` | 4 | Keep explicit | Composition-root routing includes special async sources, generic aliases, logging, and an error path. |
| `k8s/models.py::_display_phase` | 3 | Keep explicit | Ordered kubectl-compatible phase precedence. |
| `agent/native_engine.py::_consume` | 3 | Keep explicit | Provider protocol parsing, accounting, and interruption checks are interleaved intentionally. |
| `ui/workspace_controller.py::handle_pane_chord` | 3 | Keep explicit | Keyboard state transition with early exits. |
| `evals/operation_grader.py::_unpaired_within_turn` | 3 | Keep explicit | Small state machine encoding event ordering. |
| `evals/operation_grader.py::_mutation_without_matching_event` | 3 | Keep explicit | Safety-policy event matching. |
| `evals/operation_grader.py::_write_before_fresh_read` | 3 | Keep explicit | Fail-closed write ordering rule. |
| `evals/operation_grader.py::_mutation_after_audit_failure` | 3 | Keep explicit | Audit fail-closed policy. |
| `evals/grader.py::_canonical_args` | 3 | Keep explicit | Input canonicalization and validation. |

The AST output reports `_make_watch_source` twice because the nested `source`
function is also counted as part of its parent. It represents one code site,
so the table contains 16 unique functions rather than 17 rows for that output.

## Follow-up Sequence

Each item is an independent PR:

1. Refactor `AgentPanel.apply_event`.
2. Refactor `AgentSetupScreen` widget-ID dispatch.
3. Investigate a shared event tally boundary for scenario and journey evals.
4. Optionally extract a pure pane-selection helper from
   `AgentWorkspaceBridge.snapshot` if its tests show a meaningful simplification.

No later item is bundled into the AgentPanel change.

## AgentPanel Architecture

### Chosen Approach

Use a `match` statement as a thin dispatcher and move each event body into a
method with the exact event type:

- `_apply_text_delta(event: TextDelta)`
- `_apply_tool_started(event: ToolCallStarted)`
- `_apply_tool_finished(event: ToolCallFinished)`
- `_apply_agent_error(event: AgentError)`
- `_apply_turn_complete(event: TurnComplete)`
- `_apply_turn_interrupted(event: TurnInterrupted)`

`apply_event()` remains the public entry point. It contains only class-pattern
selection and forwards the narrowed event to the matching handler.

### Why Not a Handler Registry

Unlike resource row renderers, AgentEvent handlers do not share one input
type. A `dict[type[AgentEvent], Callable[..., None]]` loses the relationship
between a key and its handler argument, requiring a cast, `Any`, or a runtime
type check. That weakens strict mypy coverage. `match` preserves narrowing and
keeps the closed union visible.

### Why Not `singledispatchmethod`

`singledispatchmethod` provides extensibility that this closed internal event
union does not need. It also spreads registration across decorators and makes
the supported event set harder to review than one small dispatcher.

## Shared Terminal Behavior

`TurnComplete` and `TurnInterrupted` both update the token header. Extract:

```python
def _finish_turn_header(
    self,
    input_tokens: int,
    output_tokens: int,
    estimated: bool,
) -> None:
    ...
```

This helper performs only the common `set_header()` calculation. It must not
absorb event-specific behavior:

- `TurnComplete` ends the stream, records citation problems, stops the timer,
  clears status, and enables input.
- `TurnInterrupted` stops the timer, conditionally marks interruption, resets
  the marker, clears status, enables input, and restores focus.
- `AgentError` remains separate and does not update token totals.

## Behavioral Contracts

- Event rendering order and all mounted text/classes remain unchanged.
- Tool-start bookkeeping stores both widget and raw arguments before mounting.
- Tool-finish handling returns status to `thinking`.
- Error and terminal events re-enable input exactly as before.
- Interrupted turns retain partial output and never add a duplicate marker.
- Interrupt handling restores input focus.
- Citation warnings are processed only for `TurnComplete`.
- Token totals and estimated status are updated identically for complete and
  interrupted turns.
- No agent write approval, audit, tool execution, or provider behavior changes.

## Error Handling

`AgentEvent` is a closed union. The dispatcher uses six explicit class patterns
and no broad exception handling. It does not catch handler failures or turn
them into success-shaped UI output.

The refactor preserves the current behavior for values outside the annotated
union: no handler runs. It does not add a runtime assertion for an object the
public type contract already rejects.

## Testing

Existing `tests/ui/test_agent_panel.py` integration tests remain the primary
behavior contract. Add focused regressions that:

1. Exercise all six event types through `apply_event`.
2. Prove each event reaches its dedicated handler without requiring `Any`,
   casts, or `type: ignore`.
3. Prove complete and interrupted events use the shared header calculation
   while preserving their distinct marker and focus behavior.
4. Preserve the current no-op behavior for an out-of-contract object only if
   it can be tested without weakening the public `AgentEvent` annotation.

Run the focused AgentPanel suite, Ruff, format check, strict mypy for the
changed source and tests, then the repository gate before publishing.

## Non-Goals

- Changing event dataclasses or the `AgentEvent` union.
- Adding plugin-extensible UI events.
- Refactoring provider stream parsing.
- Changing tool labels, markers, citation semantics, or token accounting.
- Refactoring AgentSetupScreen or eval tallies in the same PR.
- Reducing condition counts in security or validation code.
