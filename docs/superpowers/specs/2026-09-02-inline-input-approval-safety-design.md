# Inline Input Approval Safety Design

- Date: 2026-09-02
- Issue: #333
- Milestone: `v0.4.0`
- Status: Approved for implementation

## Goal

Keep agent write approvals pending while an inline input surface owns keyboard
focus, so a `y` intended as text can never approve a cluster mutation.

## Root cause

`AgentUiController.can_surface_approval()` currently requires only an expanded
agent panel and a one-screen stack. Command, filter, namespace-picker, and agent
input surfaces live on that same screen, so a confirmation modal can replace
their focus while the user is typing.

## Design

Add `inline_input_active() -> bool` to `UiSurface`. `AppUiSurface` implements it
from live Textual focus:

- any focused `Input` owns text entry, including command bar, filter bar,
  agent input, and log search;
- the inline `NamespacePicker` owns selection keys while it is focused;
- modal pickers remain covered by the existing `screen_depth() == 1` rule.

`AgentUiController.can_surface_approval()` returns true only when:

1. the agent panel is expanded;
2. exactly one screen is stacked;
3. no inline input surface owns focus.

The existing wait-loop deadline and `0.05` second poll remain unchanged. A
blocked approval stays pending, emits a blocker-specific reminder (`Ctrl-A`
when the panel is closed, `Tab` when inline input owns focus), and surfaces
after focus leaves the inline editor. If the blocker changes while the request
is still pending, the controller emits the new distinct reminder immediately;
unchanged blockers keep the existing 30-second reminder cadence. No focus is
stolen and no new timer heuristic is introduced.

Every `UiSurface` fake implements the new query explicitly. Controller unit
tests default it to false and verify the new condition directly.

## Testing

Add Textual Pilot regressions for:

- command bar;
- filter bar;
- inline namespace picker;
- agent chat input.

For each surface, start an agent write request, prove `ConfirmScreen` does not
open while the surface owns focus, send `y`, and prove the input/selection
surface receives or retains the key rather than approving. Then dismiss or
move focus away and prove the same pending request surfaces for explicit user
review.

Use `tests/ui/waits.py::until()` for state transitions. Preserve the existing
approval timeout, reminder cadence, explicit-keystroke requirement, focus
ownership, and write/audit pipeline.

## Non-goals

- Changing user-initiated proposal review
- Adding key debounce or grace periods
- Treating ordinary table focus as an input blocker
- Changing confirmation keys, timeout, write execution, or audit behavior
