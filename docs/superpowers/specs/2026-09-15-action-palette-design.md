# Context-aware Action Palette Design

## Status

This design implements #388 for the `v0.5.0` milestone.

The maintainer selected Action Palette as one of the two product features to
finish before `v0.5.0`, alongside Pulse / Problems, and reconfirmed on
2026-09-15 that the release should close the milestone as written. This
document records the smallest TUI-native design that satisfies #388 without
adding a second action path.

Implementation is based on #397 because that change makes transient inputs
release focus synchronously. #397 still requires maintainer merge; this design
does not authorize merging #397 or any Action Palette pull request.

## Problem

Korvid has more actions than its compact top bar can teach. `?` is exhaustive
but requires scanning a static reference, while `:` is optimized for resource
navigation and typed commands. An operator who remembers an intent such as
"drain", "relationships", or "review proposals" cannot currently search for it,
see the effective remapped key, or understand why it is unavailable in the
current context.

Textual already installs a hidden priority `Ctrl-P` system command palette on
every `App`. Korvid has not documented or populated it with Korvid actions. It
also cannot represent disabled hits through its public provider API and mixes
framework-level commands into product actions. Leaving it enabled would create
two palettes and would allow a priority palette binding to open over approval
modals.

## Goals

- Let an operator search Korvid actions and built-in `:` commands by intent.
- Show the effective configured key for bound actions.
- Keep unavailable actions visible with a concise reason.
- Invoke the existing action or command route exactly once.
- Preserve all confirmation, context/UID revalidation, dry-run, cancellation,
  protected-context, and fail-closed audit behavior.
- Work well in narrow terminals and from the ordinary workspace, split panes,
  log/describe panes, and Agent panel.
- Prevent catalog, help, remapping, availability, and palette behavior from
  drifting apart.

## Non-goals

- Resource-name navigation; `:` remains its owner.
- Arbitrary command arguments, shell commands, or free-form `kubectl`.
- Modal-local wizard actions such as approving or advancing a dialog.
- User-defined palette plugins in `v0.5.0`.
- Agent-generated executable actions.
- Opening the palette over any modal, especially an approval dialog.

## Alternatives

### Chosen: Korvid-owned modal

Korvid disables Textual's built-in system palette and binds a dedicated modal to
`Ctrl-P`. The modal uses public Textual widgets and fuzzy matching but owns
availability, disabled rows, deterministic ranking, and dispatch.

This adds a small screen, but it is the only option that can explain disabled
actions, exclude framework commands, and enforce Korvid's approval boundary
without subclassing Textual private methods.

### Rejected: Textual command provider

A provider would reuse Textual's palette UI and ranking. Its public `Hit` API
does not expose disabled state, while `CommandPalette` constructs every result
as enabled. Encoding "unavailable" in help text would still let selection close
the palette and call a callback. Fixing that requires overriding private gather
and option-building methods, coupling Korvid to Textual internals.

### Rejected: merge actions into `:`

This would minimize surface count, but mixes resource aliases, command grammar,
and fuzzy action intent in one input. Exact resource navigation would compete
with action ranking, unavailable reasons would not fit the inline completion
model, and the distinction between navigating and executing would become less
clear.

## Entry point and protected states

Korvid sets `ENABLE_COMMAND_PALETTE = False` so Textual does not install its
hidden system palette. `APP_BINDINGS` gains a remappable, priority
`open_action_palette` action with default key `Ctrl-P`.

Priority is required so the palette opens while focus is inside the Agent input,
log search, or describe search. The existing keybinding planner therefore
forbids remapping it to `y`, `n`, `Enter`, or `Escape`, the fixed approval keys.

`check_action` and `action_open_action_palette` both enforce the same surface
guard:

- allowed only on the base workspace screen;
- allowed while a split pane, log pane, describe pane, or Agent panel owns
  focus;
- unavailable while any `ModalScreen` is active;
- unavailable while the `:` command bar or `/` filter bar is actively editing;
- unavailable during app shutdown or a protected transition.

The action guard remains even though `check_action` normally blocks dispatch.
That protects direct calls and future remapping changes. A test must prove
`Ctrl-P` cannot replace, focus, type into, or satisfy `ConfirmScreen`.

Cancelling the palette returns focus to the previously focused widget. If that
widget disappeared while the modal was open, the existing workspace focus
restoration chooses the active table.

## Canonical metadata

The palette does not introduce a hand-maintained action list.

### App actions

The existing `APP_BINDINGS` list remains the canonical app action catalog. The
palette derives stable IDs, titles, descriptions, default keys, priority, and
visibility directly from its real `Binding` objects. Configured keymap overrides
are applied when runtime entries are built, so no palette key table exists.

The existing help grouping map moves next to `APP_BINDINGS` as a reusable
presentation helper consumed by both Help and the palette. The top bar keeps its
different compact grouping because it already consumes live active bindings;
neither grouping defines execution.

Handler-only keys such as table drill-down and split-pane chords remain in
`APP_HANDLER_KEY_HELP`. They stay visible in `?` but are not palette results
because they do not map to a single app action. A contract test makes that
boundary explicit.

Parameterized favorite-namespace shortcuts remain bindings and help metadata,
but the generic palette derivation excludes parameterized action expressions in
`v0.5.0`; namespace selection already has the typed `:ns` route. Alternate
terminal spellings such as `shift+l` and `L` deduplicate by binding ID suffix,
using the same rule as the top bar.

Contract tests require every palette action to resolve to one real binding and
every app binding to have a known help group. No migration or replacement of
the working binding catalog is part of #388.

### Built-in commands

The existing typed `COMMANDS` tuple remains the canonical command grammar.
`CommandDescriptor` gains palette presentation metadata for a meaningful bare
invocation: title, description/search aliases, and an explicit omit reason when
another app action already represents the same operation.

The palette includes the bare forms of commands whose no-argument behavior is
useful, including `:pulse` / `:problems`, namespace/context pickers, Agent/model,
MCP, proposals, port-forwards, and Telepresence. Resource aliases are excluded.
`:q` is omitted in favor of the bound Quit action. Other deliberate omissions
must carry a reason that a contract test can inspect.

### Runtime entries

The app derives immutable runtime `PaletteEntry` values from the two canonical
catalogs. Each entry contains display/search data, an effective trigger, a typed
invocation identity, declaration order, and current availability. It contains
no closure that can bypass routing.

An invocation is one of:

- `AppActionInvocation(action, parameters)`;
- `CommandInvocation(canonical_text)`.

The command invocation is parsed with `parse_command` and posted through the
same message path as `CommandBar`. The action invocation uses Textual's normal
app action dispatcher. No palette-specific controller or write call exists.

## Availability contract

`check_action` currently returns only a boolean, but that boolean has a narrow
job: it prevents wrong-view bindings from dispatching and lets overloaded keys
fall through. It must not make keys inert in states where the existing handler
deliberately explains a refusal, such as no selection or read-only mode.

The app therefore introduces a frozen `ActionAvailability` value with two
decisions:

- `binding_enabled: bool` preserves the existing key-dispatch policy;
- `invocable: bool` says whether the palette may invoke the action now;
- stable reason code;
- concise operator-facing reason when `invocable` is false.

Reason codes cover:

- wrong view or resource type;
- no selected resource;
- read-only mode;
- missing capability, optional extra, or executable;
- required pane not open;
- unsupported synthetic resource;
- protected UI surface or context-switch/shutdown transition.

Ownership follows existing boundaries:

- the app shell owns composition and modal/protected-state checks;
- `WorkspaceController` owns focused pane, current resource, and selection;
- resource-write coordination owns read-only and write-target eligibility;
- Helm, Agent, and integration controllers own their capabilities.

Controllers expose synchronous availability queries over state they already
own. The app composes those results in `action_availability`. `check_action`
returns `binding_enabled`, while the palette renders and enforces `invocable`.
For example, a Helm action on the pods view is neither binding-enabled nor
invocable; a write action with no selected row remains binding-enabled so its
key can explain the refusal, but is not palette-invocable. Action handlers retain
their existing guards as defense in depth. Availability checks must not perform
network I/O or mutate state.

A protected cluster context does not make a write unavailable; it remains
invocable and reaches the stronger existing confirmation path. "Protected"
availability reasons refer only to UI surfaces or transitions where launching
another modal would be unsafe.

Commands have the same result type. Optional extras and executable-dependent
commands are disabled with their install/capability reason rather than silently
disappearing.

Unavailable entries remain searchable by default. They sort after equally
relevant available entries and render `Unavailable: <reason>`. They cannot be
selected or invoked.

## Search and ranking

Search uses Textual's public fuzzy matcher, with deterministic boosts and tie
breakers:

1. exact title, action ID, command text, or alias;
2. token-prefix match;
3. fuzzy title/alias match;
4. fuzzy description match.

For a non-empty query, match quality precedes availability so an exact
unavailable action remains easy to find; availability breaks equal scores.
For an empty query, currently available actions come first, followed by
available commands and then unavailable entries. Existing top-bar priority and
declaration order provide stable ordering. Final ties use the stable entry ID.

Search terms include title, description, action ID, command aliases, and the
small alias set stored on the canonical descriptor. Results never include
resource names.

## Palette UI

`ActionPaletteScreen` is a `ModalScreen` containing:

- a focused search `Input`;
- an `OptionList` of derived results;
- a compact hint/status row.

Each result renders a category, title, and effective key or canonical command
on the first line. The second line renders its description or unavailable
reason. Unavailable options are visible but disabled. Category separators are
inserted only between non-empty groups.

The modal uses `width: 76; max-width: 94%` and `height: auto`. Its height is
bounded, but the bound is not a single percentage: the modal takes at most 80%
of the terminal height *while that still leaves eight rows of results*, and
otherwise grows up to — never past — the full terminal height. Eight rows is
what one real catalog row needs at 36 columns, where a long title plus a `:`
spelling, or an owner's refusal such as "Relationships unavailable in this
session", wraps to six to eight lines. The 80% cap is cosmetic; on a 16-row
terminal it leaves two rows of results, and a row whose trigger or reason is
clipped cannot be recovered by any keystroke, because the list scrolls by whole
options and never within one. A terminal short enough to need that extra space
also gives up the modal's vertical padding and the results list's own border
(Textual's compact `OptionList`), which widens every row as well. Tall
terminals stay capped at 80% and at eighteen result rows, so the palette stays
a palette.

The results list is capped through its `max-height` rather than allowed to grow
the `height: auto` container, so overflow becomes scrolling inside the list and
the key hint stays composited on screen. Both the cap and the compact chrome
are re-applied when the terminal resizes under the open modal, and the rows are
then re-rendered once that new layout exists. Re-rendering, not just
re-scrolling, is what a resize needs: switching to compact chrome hands the
list back the columns of its own border, so every row re-wraps, and a list that
measures the new width for its `height: auto` while still holding the line
heights it cached at the old one ends up one line taller than the viewport it
was given — the last words of a row are then never composited, and no keystroke
recovers them. The rebuild re-measures every row against the width it actually
has, carries the highlighted row across, and ends by scrolling that row back
into the viewport; it is ordered through the results list's own
post-refresh callback, so it sees settled geometry, runs no timer, and causes
no further resize. A rebuild that arrives after the palette was dismissed finds
no rows and does nothing. `Home`/`End` re-assert that scroll too: `OptionList`
only scrolls from its `highlighted` watcher, so the key that lands on the row
that is already highlighted would otherwise do nothing.

Two-line options wrap rather than forcing horizontal scrolling.
Narrow-terminal tests cover widths below the normal modal width. Below roughly
seven rows of terminal the modal's own chrome no longer fits and the results
clamp to a single row; that is a documented limit, not a supported size.

Typing filters results. Up/Down, PageUp/PageDown, Home/End, and Enter operate
the list while the input keeps keyboard focus. `Escape` and `Ctrl-P` cancel.
A disabled no-results row gives explicit feedback.

## Invocation and stale state

The screen returns only the selected stable entry ID. It never calls an app
action itself.

After the modal is dismissed, the app rebuilds the entry from current state and
checks availability again. This catches context, selection, pane, capability,
and read-only changes that occurred while the palette was open. If the entry is
missing or unavailable, the app reports the current reason and performs no
action.

For an available entry, the app schedules exactly one existing action or parsed
command after modal teardown. Destructive actions therefore open their existing
confirmation screen only after the palette no longer exists. The Enter used to
select a result is consumed before the approval dialog is created and cannot
approve it. A fresh user keystroke remains mandatory.

## Error handling

- Invalid or duplicate catalog metadata fails contract tests and startup
  validation; it is not silently dropped.
- A stale unavailable selection produces a warning with the typed reason.
- An unexpected invocation identity is exhaustively rejected rather than
  defaulting to a callback.
- Search and availability are local and synchronous; exceptions are programmer
  errors and must not be converted into empty-result success.
- Existing action/command errors remain owned and reported by their current
  controller or router.

## Testing

### Pure tests

- descriptor generation produces the expected bindings and handler help;
- every palette action resolves to one real action and every command entry to
  one typed command descriptor;
- alternate shifted keys and parameterized favorites deduplicate correctly;
- configured remaps appear as effective palette keys;
- exact, prefix, fuzzy, availability, declaration-order, and stable-ID ranking;
- `binding_enabled` remains consistent with boolean `check_action` behavior,
  while `invocable` covers deeper refusal states without making explanatory
  keyboard paths inert;

### Textual tests

- `Ctrl-P` opens from base, split, log, describe, and Agent surfaces;
- cancellation restores focus;
- command/filter editing and all modal screens block opening;
- search, no-result, disabled-result, category, and narrow-terminal rendering;
- at 80x24 and 36x16 the whole modal stays inside the screen, the key hint is
  composited, and the highlighted row is rendered in the results viewport —
  including after `End`, after a resize under the open modal, and for a whole
  real row (`:proposals`, and an owner's long unavailable reason) at 36x16;
- shrinking a 36x24 terminal to 36x16 with a real row filtered in still
  composites that row whole — heading and description for `:proposals`, heading
  and the owner's refusal for the disabled `relationships` row, which stays
  inert with the query input focused — and a rebuild that lands after the
  palette was dismissed does nothing;
- overloaded view-specific actions show the correct effective key and reason;
- Pulse comes from `COMMANDS`, not a palette-only route;
- a context or selection change while open is rejected at invocation time;
- an available action and command reach their existing route exactly once.

### Safety tests

- destructive selection opens exactly one existing confirmation screen;
- palette Enter does not approve, write, or create an audit entry;
- approval screens ignore `Ctrl-P` and any allowed remap;
- priority remaps to `y`, `n`, `Enter`, or `Escape` are rejected;
- read-only and protected-UI-state reasons match actual dispatch refusal;
- no palette code imports or invokes write implementations.

### Platform and contracts

- native terminal coverage proves `Ctrl-P` arrives on supported Windows and
  POSIX terminal paths;
- random-order UI tests cover focus restoration;
- Ruff, strict mypy, tach, source-size, docs, and full CI remain required.

## Documentation

- `docs/keybindings.md`: add `Ctrl-P`, remapping, and unavailable reasons.
- `docs/tui.md`: explain `:`, `?`, and Action Palette as complementary tools.
- `docs/release-notes/unreleased.md`: concise discoverability note.
- Generated help and catalog contract tests remain the source of truth; no
  manual action table is added to public documentation.

## Delivery

1. Land #397 so transient command/filter inputs release focus synchronously.
2. Implement the canonical metadata and availability contract with TDD.
3. Add ranking and the modal with TDD.
4. Wire guarded invocation and approval-boundary tests.
5. Update documentation and run the full gate.
6. Open a pull request only with explicit maintainer instruction. The
   maintainer, not the agent, decides whether and when to merge.
