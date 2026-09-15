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
availability, unavailable rows, deterministic ranking, and dispatch.

This adds a small screen, but it is the only option that can explain
unavailable actions, exclude framework commands, and enforce Korvid's approval
boundary
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

Only the *open* key is remappable. The modal binds its own close keys
statically (`escape,ctrl+p`), so both survive a remap of
`open_action_palette`: `Escape` is korvid's universal modal close, and the
default `Ctrl-P` — inert as an opener once a remap frees it — still dismisses
the palette a remapped key opened. `docs/keybindings.md` says so in the remap
section, and a contract test derives both keys from the screen's own
`BINDINGS` so the page can neither promise a key the modal stopped binding nor
stay silent about one it gained.

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

The trigger a row shows is that effective key spelled by the one shared
`key_label` table the help overlay already uses, so a Textual key name that no
keyboard says out loud — `question_mark`, `colon`, `slash`, `tilde` — reaches
both surfaces as the character the user presses (`?`, `:`, `/`, `~`), whether
it is a binding's default or a key remapped onto it.

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

A probe answers in its handler's order, not in a tidier one. The drain key is
the clearest case: `drain_node` asks about an in-flight drain *before* it
resolves any target, because pressing it again is how a drain is cancelled. So
its probe reports the drain too — invocable only on the node actually being
drained (that press is the cancel), and otherwise the handler's own "drain of
nodes/X in progress — press the drain key on it to cancel". Cordon and uncordon
keep asking about the selected node alone: a drain elsewhere does not hold this
node's schedulable state. `Ctrl-S` is the smaller version of the same rule: its
binding is gated on the log pane's visibility and stays that way, but the row
reports an empty (or not yet built) buffer, because that is all the keypress
could say.

A protected cluster context does not make a write unavailable; it remains
invocable and reaches the stronger existing confirmation path. "Protected"
availability reasons refer only to UI surfaces or transitions where launching
another modal would be unsafe.

Commands have the same result type. Optional extras and executable-dependent
commands are marked unavailable with their install/capability reason rather
than silently disappearing. A command answers for what *its own* handler reads, which is not
always what the nearest bound key reads: `:pf` opens the list of forwards this
session already started, so its one question is whether this build carries a
forward registry — not the `kubectl` binary or the selected row that the
`shift+f` dialog also needs. Borrowing the dialog's probe would grey out a
command that works, so the owner exposes a second, narrower one
(`list_unavailable_reason`) and both it and `open_list`'s own notification read
one shared sentence, so the row and the command cannot drift. A command whose
own state decides the answer reports that state:
`:proposals` asks `ProposalController` the three questions `open_review` asks —
is the feature enabled (`mcp.write_proposals`), is anything pending, is a
review already open — in that order and with those exact sentences and
severities. Only the *reading* differs: the probe asks
`ProposalStore.has_pending()`, which is TTL-aware but changes nothing, while
the keypress reads through `pending()` and so still settles (and audits) any
proposal whose TTL ran out. Deriving a row must never be what expires a
proposal — that is the "must not mutate state" rule above, applied to an owner
whose state has a clock in it.

Unavailable entries remain searchable by default. They sort after equally
relevant available entries and render `Unavailable: <reason>`. They cannot be
selected or invoked.

A palette reason is **concise by contract**: it is one `Option`'s second line
at 36 columns, and a row whose reason is clipped there cannot be recovered by
any keystroke — revealing a row aligns its top, and the list scrolls between
rows rather than inside one. So an owner whose *command* answers with
remediation (an install
or reinstall command, a config snippet) splits the two: the probe returns the
short capability fact (`:mcp` — "the [mcp] extra is not installed"; `:tp` —
"no CLI in this session"), and the typed command's notification leads with that
same sentence and then adds the full remediation, which a toast has room for.
The row and the keypress therefore still agree on why, and only the length
differs.

The same split applies wherever a refusal would otherwise interpolate *cluster
data*, whose length korvid does not control: a node name (the drain key,
refused while another node is being drained; the cordon and uncordon keys,
refused while the selected node is) or a pod name (the log key, refused
because the pane is already showing its eight panels). The row states
the bounded fact — "Another node drain is in progress", "This node is being
drained", "Panel cap is 8 containers" — and the keypress keeps the name and
the instruction that names it. Two refusals that split this way must stay
distinguishable from each other once the names are gone, because they ask for
different things: waiting on the row under the cursor is not the same as
going to a node that is not on screen. The viewport floor is not raised to
accommodate an arbitrary identifier: one 253-character name would make any
floor wrong again.

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
reason. An unavailable row is visible, greyed and navigable — it simply cannot
be run. Category separators are
inserted only between non-empty groups.

The modal uses `width: 76; max-width: 94%` and an **exact height it computes
from the terminal**, never `height: auto`. That height is bounded, but the
bound is not a single percentage: the modal takes at most 80% of the terminal
height *while that still leaves eight rows of results*, and otherwise grows up
to — never past — the full terminal height. Eight rows is what one real catalog
row needs at 36 columns, where a long title plus a `:` spelling, or an owner's
refusal such as "Relationships unavailable in this session", wraps to six to
eight lines. The 80% cap is cosmetic; on a 16-row terminal it leaves two rows
of results, and a row whose trigger or reason is clipped cannot be recovered by
any keystroke, because the list scrolls by whole options and never within one.
A terminal short enough to need that extra space also gives up the modal's
vertical padding and the results list's own border (Textual's compact
`OptionList`), which widens every row as well. Tall terminals stay capped at
80% and at eighteen result rows, so the palette stays a palette.

The results list gets no height of its own: it is `height: 1fr` and takes
whatever the modal's exact height leaves after the query input, the key hint
and the chrome. The height therefore depends on the terminal alone — the same
at 80x24 whether the list holds the whole catalog or the single row a query
left — and overflow becomes scrolling inside the list, with the key hint still
composited on screen. This is what keeps a row whole. A list sized from its own
content has to be *measured* first, and Textual asks `OptionList` for that
content height at the width it has before the vertical scrollbar is decided,
then draws the row against the width after: at 38 columns a six-line row was
measured at 25 columns, given a five-line viewport, and its last word was never
composited. Flex sizing asks no such question, so there is no measurement to
disagree with the render — no re-render pass, no line-height cache to
invalidate, and nothing to re-measure. The height budget and the compact chrome
are re-applied when the terminal resizes under the open modal, and the
highlighted row is scrolled back into the viewport once that new layout exists
— a resize both re-budgets the viewport and re-wraps every row, so the scroll
offset the previous size produced points at a different row afterwards. That
scroll is ordered through the results list's own post-refresh callback, so it
sees settled geometry, runs no timer, and causes no further resize; one that
arrives after the palette was dismissed finds no rows and does nothing.

The results list also keeps `scrollbar-gutter: stable`. With the viewport no
longer derived from a measurement, its remaining job is narrower: the two
columns are reserved whether or not a scrollbar is showing, so the width a row
wraps against is the same number as rows come and go with the query, rather
than moving by two columns every time the list crosses the point where it needs
a scrollbar. `Home`/`End` re-assert the scroll too: `OptionList` only scrolls
from its `highlighted` watcher, so the key that lands on the row that is
already highlighted would otherwise do nothing.

Two-line options wrap rather than forcing horizontal scrolling.
Narrow-terminal tests cover widths below the normal modal width. Below roughly
seven rows of terminal the modal's own chrome no longer fits and the results
clamp to a single row; that is a documented limit, not a supported size.

Typing filters results. Up/Down, PageUp/PageDown, Home/End, and Enter operate
the list while the input keeps keyboard focus. `Escape` and `Ctrl-P` cancel.
A disabled no-results row gives explicit feedback.

Those navigation keys are `OptionList`'s own public navigation actions —
`action_cursor_up`/`_down`, `action_page_up`/`_down`, `action_first`/`_last` —
over rows that are **all navigable**. Only the no-results row is a disabled
Textual option; a row whose owner has refused it is an ordinary row that
carries its refusal itself, as a greyed `Unavailable: <reason>` second line,
and `_activate` is what refuses to run it.

That separation is the whole rule, because Textual answers two different
questions with one flag. `OptionList.render_line` picks the disabled component
style *before* it looks at `highlighted`, so a disabled option can never draw a
cursor; and every navigation action moves between enabled options only
(`find_next_enabled` and its siblings answer `None` when none is enabled). A
palette whose refused rows are exactly the ones a user opens it to *read*
cannot spend that flag on "cannot run". A base install without helm leaves
`helm` matching four rows, all refused by the one owner, whose reason wraps:
twelve lines against a nine-row viewport at 80x24, twenty against eight at
36x16. Marking them disabled left every arrow, page and edge key with nothing
to land on — the highlight stayed unset and the viewport at the top — and once
the palette moved the highlight itself, the highlighted row still drew no
cursor at all: the index changed and the frame did not. With no query the same
rows rank last, below the final runnable one, and `End` stopped short of them.

So Up/Down move one row whatever its availability, Home/End reach the real
first and last row, a click reaches a refused row too, and a page moves in
*lines*: Textual anchors a page on the highlighted row's own first line and
adds or subtracts the viewport height, so the cursor never jumps more than a
screenful. Rows differ in height, so the *view* still can — landing on a taller
row scrolls further than the anchor moved, and paging up snaps back to the
start of the row the anchor landed inside — and the screen therefore steps the
cursor back a row at a time, with the list's own cursor actions, until the view
has moved no further than one viewport. Every key ends in an explicit
`scroll_to_highlight()`, which is what makes `End` at the end of the list and
`Home` at the top re-assert a scroll that a resize had moved away.

The query `Input` is the modal's only focus: the results list is created with
`can_focus` off, so clicking a row that cannot run does not take the keyboard
away from a query the user is still editing.

Browsable is not invocable, and the two stay separate. `_activate` is the one
gate, and every selection path reaches it — `Enter` on the input, a click, and
`OptionList`'s own `action_select` — so it dismisses only for an entry whose
`availability.invocable` is true. The no-results row has no entry behind it and
no id, so it stays a genuinely disabled option: no cursor is ever drawn on it
and nothing runs. Everything here is public `OptionList` API — the navigation
actions, `highlighted`, `option_count`, `scroll_offset`,
`scrollable_content_region` and `scroll_to_highlight()`. No private line map is
read.

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
- a row that cannot run is a *navigable* row: `Option.disabled` is spent only
  on the no-results sentinel, so the refusal never costs the row its cursor or
  its reachability, and `_activate` is the single gate every selection path
  reaches;
- the shared key-label table spells every Textual key name the palette and the
  help overlay can show, including `tilde` as `~`, by default and through a
  remap;
- the command reason map carries one resolver per palette command and every
  key in it is a real canonical command text — `:pf`'s resolver is the forward
  owner's *list* probe, which reports a missing registry and nothing else;

### Textual tests

- `Ctrl-P` opens from base, split, log, describe, and Agent surfaces;
- cancellation restores focus;
- command/filter editing and all modal screens block opening;
- search, no-result, unavailable-result, category, and narrow-terminal
  rendering;
- at 80x24 and 36x16 the whole modal stays inside the screen, the key hint is
  composited, and the highlighted row is rendered in the results viewport —
  including after `End`, after a resize under the open modal, and for a whole
  real row (`:proposals`, and an owner's long unavailable reason) at 36x16;
- shrinking a 36x24 terminal to 36x16 with a real row filtered in still
  composites that row whole — heading and description for `:proposals`, heading
  and the owner's refusal for the refused `relationships` row, which stays
  inert with the query input focused — and a scroll that lands after the
  palette was dismissed does nothing;
- shrinking 80x24 to 36x16 with a *query's worth* of rows left in — the whole
  derived catalog, so the results keep a scrollbar — composites the highlighted
  `:proposals` row whole, and again after walking back onto it with `Home` and
  `Down`; the refused `relationships` row among several results keeps its
  heading, the owner's refusal and the rule closing its category, and stays
  inert;
- narrowing 80x24 to 38x24 over the whole derived catalog composites the row a
  query left whole — `:ctx`, `:tp`, and the refused `relationships` row with
  the owner's refusal — with the query input still focused;
- the real integration rows a base install greys out are composited whole at
  the narrow terminals the design supports: `:mcp` at 36x16 and `:tp` at 36x24,
  each with the reason its own `IntegrationController` answers with, the row
  inert and the query input focused;
- the modal's height is exactly the bounded rule's budget for the terminal (19
  rows at 80x24, 28 at 80x40, 14 at 36x16, with 9/18/8 rows of results) whether
  the list holds the whole catalog or one filtered row, it does not change when
  a query narrows the list, and a run of resizes ends on the final size's
  budget with that row still whole;
- every row a query leaves is reachable by keyboard even when none of them can
  run: with `helm` matching four rows all refused by their owner, Up/Down walk
  each index in turn, Home/End reach the real first and last row, and repeated
  PageDown/PageUp walk to both edges — at 80x24 and at 36x16, compositing the
  heading and the whole reason of every row they stop on, with Enter inert and
  the query input focused throughout;
- with no query, `End` reaches the trailing unavailable rows that rank below
  the last enabled one, renders the last of them whole, runs nothing, and
  `Home` brings the first row back;
- a query whose results interleave refused and runnable rows is traversed
  without skipping either: Down visits every index and Up visits every index in
  reverse, Enter on a refused row does nothing, and the next runnable row still
  dispatches exactly once;
- a page key never leaves the highlight off screen: paging to the end of the
  whole derived catalog and back composites the highlighted row after every
  press;
- the cursor is *drawn* on the row it reaches, read from the frame the
  compositor renders rather than from the widget's index: on an all-refused
  `helm` list at 80x24 and 36x16 every Down changes the cells drawn in the
  list's own highlight background, and a mixed list keeps the cursor on the
  step from a runnable row onto a refused one and back;
- a click on a row that cannot run moves the cursor onto it, composites it, and
  runs nothing — and `OptionList`'s own `action_select` on such a row dismisses
  nothing while the next runnable row still dispatches, so removing the
  `_activate` guard fails the suite;
- a page key moves the view no further than the results viewport's own height,
  in both directions, over the real catalog at 80x24 and 36x16 — where rows are
  two to five lines against an eight- or nine-line viewport — and still reaches
  the trailing refused rows;
- the single no-results row stays inert under every navigation key — it stays a
  disabled, id-less option, no cursor is ever drawn on it, nothing is
  dispatched, and the query input keeps focus;
- overloaded view-specific actions show the correct effective key and reason;
- the `:pf` row is greyed out in a session wired without a forward registry
  and offered once one is, with no `kubectl` and nothing selected; a selection
  made before the registry disappeared is refused after the modal closes with
  the owner's own wording and routes nothing;
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
