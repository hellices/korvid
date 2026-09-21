# Interactive keybinding editor

## Scope

Implement issue #404 as the first v0.6.0 feature. The requested handoff is an
implementation PR with completed review rounds and exact-head checks, never a
merge. Milestone alignment is recorded in #404, #405, #406, and release tracker
#421. This change does not implement the other three feature issues.

The editor changes existing remappable app actions. It must support staged
conflict chains, two-way swaps, longer rotations, deterministic suggestions,
undo, cancellation, individual reset, and full reset. Startup and editing must
judge a keymap with the same rules. Dispatch, Help, the top bar, and Action
Palette must agree after apply and restart.

## Selected approach

Use a separate `:keys` / `:keybindings` modal, also discoverable through the
existing Action Palette. Do not consume another default global shortcut. The
modal owns one editing session, including its final preview and confirmation;
conflict chains must not open nested dialogs.

Keeping an editable mode inside Help would mix its generated documentation
with mutable pending state. A raw YAML editor would reuse configuration but
would not provide guided conflict resolution. A separate bounded editor with
a shared pure planner meets the requirements without replacing either Help
or the Action Palette.

## Pure model and validation

Extend `core/keybindings.py` with optional static action-view scopes. The UI
supplies these from the same `_ACTION_VIEWS` metadata that `ActionPolicy` uses
for dispatch; do not infer exclusivity from the currently selected view,
permissions, transient availability, or presentation/help groups. Actions
without a restricted view scope overlap every view. In particular, log-pane
actions can coexist with a different focused resource view and are not
disjoint from that view's actions merely because Help groups them as Logs.

Canonicalize physical aliases before checking conflicts, including shifted
letters and printable punctuation/Textual key-name equivalents. A remap is
one key, not a comma-separated list or a new chord grammar. Fixed namespace
slots, pane/navigation handlers, fixed modal controls, and approval keys are
not made available by a temporary edit. Priority actions must not intercept
protected modal keys.

Add `core/keymap_edit.py` for an editing session over a copy of the effective
overrides. Each operation stores a history snapshot. Staging a valid physical
key may leave a visible conflict while its owner is being reassigned; that
temporary conflict must not discard another action or change live bindings.
Validation judges the complete proposal, so swaps and rotations terminate
normally rather than following an automatic reassignment loop.

Conflicts identify both actions and the overlapping scope. Suggestions are
deterministic, bounded, and checked with the same complete-map validator.
A swap is offered only when moving the other action to the key just freed by
the initiating edit produces a valid complete proposal. Undo restores both
the pending map and the next conflict decision. Reset removes overrides,
rather than persisting default values as new custom settings.

## UI and binding application

Derive action names, default keys, priority, and descriptions from the real
app bindings. Only bindings with a remappable ID appear as editable rows;
the fixed 1-9 namespace shortcuts remain reserved. Show action context,
default/effective/proposed keys, pending changes, and unresolved conflicts.

The keyboard can select an action, edit its key, request a suggestion or valid
swap, resolve the next conflicting owner, undo, reset, and review the complete
proposal. Review and Apply are separate actions. Typing a key or pressing
Enter to stage it never applies the map. Editing after review invalidates the
confirmation. Escape cancels the session without touching config or dispatch.

Use one class-selected modal with a bounded `VerticalScroll` body, preserving
the project's small-terminal and focus-restoration conventions. Key text and
errors are rendered as text, not interpreted markup.

A UI controller owns startup loading and confirmed application. The app shell
only delegates, and the composition root injects its surface and persistence
callback. This extracts the existing app keymap application logic instead of
growing the capped app module. The controller does not introduce a new action
catalog or service locator.

Apply a complete Textual keymap for every remappable binding ID, including
unmodified defaults and shifted-letter alternative IDs. Textual's overlay can
otherwise remove an untouched binding sharing a key in a mutually exclusive
view. The complete map also restores defaults on a full reset. Keep the
declared binding catalog unchanged; dispatch still uses `ActionPolicy` to
select the binding appropriate to the current view.

## Persistence and failure behavior

Add `core/keybinding_config.py` with a narrow keybinding writer. Read the
current configuration at save time, require a mapping document, and replace
only its `keybindings` section. Reuse the existing atomic same-directory
writer. Empty overrides remove the section. Preserve model profiles, UI
preferences, configured favorites, and any separately saved namespace state.
Malformed YAML and non-mapping documents fail rather than being replaced by
an empty configuration.

Prepare and validate the full live keymap before saving. Persistence must
succeed before committing the effective overrides/config and refreshing the
live bindings. No asynchronous gap exists in this commit. A failed save
keeps the editor open with actionable feedback and leaves both the live map
and last saved settings unchanged. A session without a persistence callback
cannot claim its edits were saved.

The first version follows the existing configuration path and dependency
injection. It does not rewrite `uv.lock`, add a dependency, create user-defined
keymap profiles, or remap widget/approval actions.

## #406 boundary

Keybinding reset never reallocates namespaces. Namespace reallocation will
never reset action bindings. Slots 1-9 remain fixed; `0` remains the ordinary
remappable all-namespaces action. This PR proves preservation of favorites
and unrelated saved state. The full journey combining the real #406 feature
with remapping/reset/restart is a #421 release-integration responsibility,
not a circular prerequisite for closing either feature issue.

## Verification

- Pure tests: aliases, invalid keys, overlapping/disjoint views, fixed
  handlers, priority approval protection, chains, swaps, rotations, bounded
  suggestions, undo/cancellation, and both reset modes.
- Persistence tests: unrelated keys/favorites/state preserved, empty reset,
  malformed documents, read/write failure, and atomic replacement failure.
- TUI tests: keyboard-only entry/edit/review/apply, no early live changes,
  actual contextual dispatch, help/top-bar/palette agreement, restore on
  reset, reload equivalence, and save-failure/cancellation behavior.
- Guard tests: the editor cannot replace an approval modal or turn its
  editing keys into a cluster-write confirmation.
- Run targeted checks during implementation; run `make check`, coverage,
  docs and existing pre-commit gates before the PR handoff. Review all PR
  findings, use RED/GREEN for credible fixes, and obey the repository's
  two-consecutive-low-confidence-only-round limit. Never merge the PR.
