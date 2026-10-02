# Stable automatic namespace shortcuts

## Scope

Implement issue #406 for v0.6.0. Keys 1-9 keep their fixed bindings and
dispatch path. What changes is the map behind them. Configured
`favorite_namespaces` stay pinned first. Saved automatic slots for the
current cluster come next. Namespaces found by an authorized listing fill
whatever slots are still free. The handoff is an implementation PR with
completed review rounds and exact-head checks. It is never merged by the
agent. The cross-feature remap/reset/reallocation/restart journey belongs to
release tracker #421, as recorded in the #404 design.

## Pure model

`core/namespace_slots.py` holds the rules as plain data and functions. It has
no I/O, no Textual and no Kubernetes.

- A `SlotEntry` is a namespace, an origin (`pinned` or `auto`) and an
  `available` flag. A `SlotMap` is nine optional entries, indexed 1-9.
- `Inventory` is the result of a namespace listing. It is either `complete`
  with a set of names, or `unknown`. Only a successful, unpaged LIST is
  complete. Failure, RBAC denial, a cancelled listing or a missing listing
  function is unknown. Unknown never infers a deletion.
- `build(pinned, saved, inventory)` lays out the effective map:
  1. Pins take slots 1..n in configured order, exactly as `favorite_namespaces`
     does today, capped at nine by the existing parser. Pins always dispatch;
     their authorization behavior is unchanged.
  2. Saved automatic entries keep their slot numbers. An entry is dropped if
     its slot is now pinned, its namespace is now pinned, or it repeats an
     earlier namespace. Pins always win.
  3. With a complete inventory, an automatic entry whose namespace is absent
     becomes unavailable. It keeps its slot, so the number is not reused.
     An unavailable namespace that reappears becomes available again in the
     same slot. With an unknown inventory, saved availability is kept as is.
  4. With a complete inventory, unmapped names fill free slots in ascending
     slot order, using sorted name order. They are never re-sorted on a later
     refresh. Example: `1 dev / 2 prod / 3 staging` plus `alpha` becomes
     `... / 4 alpha`.
- `reallocate(pinned, inventory)` is the explicit rebuild. It needs a complete
  inventory. It keeps pins and packs every other listed name into the free
  slots in name order. Unavailable slots are reclaimed.
- `preview(current, proposed)` lists the slots that change, for the
  confirmation modal.

Nothing in the model ever selects a namespace, so a refresh cannot switch the
active namespace. Namespaces past the ninth slot stay reachable through the
picker and `:ns <name>`. No multi-digit shortcut is invented.

## Cluster identity and isolation

A map belongs to a `ClusterIdentity`: the resolved context name plus the API
server URL of the live connection. `k8s/cluster_identity.py` resolves it
without network access. The name is the explicit context, or else the
kubeconfig's current-context. The server comes from the connected client
configuration. If the context is reused for another cluster, the server
differs, so the old map is not inherited. If the identity cannot be resolved,
slots run in memory only, with pins plus discovery, and nothing is saved.

The slot controller holds a generation counter. Activation (startup and each
completed `:ctx` switch) and deactivation (context-switch teardown) advance
it. Every listing captures the generation before it awaits. A result that
returns under a different generation is discarded. During a switch the
effective map falls back to pins only, so a numeric key can never dispatch an
old cluster's automatic slot.

## Persistence

`core/namespace_slot_store.py` reads and writes a versioned JSON document at
`$XDG_STATE_HOME/korvid/namespace-slots.json` (default
`~/.local/state/korvid/`). It stores only automatic entries, keyed by identity.
Pins stay in user-authored configuration, which is never written by this
feature. Unknown top-level config keys are a startup error, which is why the
state lives outside config.

Saving takes the existing `interprocess_lock`, re-reads the latest document,
replaces only this identity's record and writes through the shared atomic
same-directory writer. Other clusters' records survive concurrent korvid
processes. A malformed or wrong-version document is not overwritten. Loading
it yields an empty saved map with one warning, and saving it fails. A failed
save keeps the last saved file intact and reports an actionable notice once.
Routine discovery keeps the new map in memory for this session. Reallocation
commits only after its save succeeds, mirroring the #404 apply contract.

## Discovery triggers

No new trigger and no new watch. The existing startup/post-`:ctx` completion
prefetch and the `:ns` picker listing feed their results to the slot
controller. A 403 keeps the existing single permission notice and never
probes individual namespaces. Configured favorites are never used as a
fallback inventory. When discovery fails, help shows the map as last known,
with a stale note.

## UI and dispatch

A `NamespaceSlotController` in `ui/` owns the effective map, its generation
and persistence. The workspace controller asks it for slot *n* in
`favorite_namespace`. An available entry navigates through the unchanged
`navigate_command` path, in the focused pane only. An unavailable slot posts
a notice naming the namespace and `:slots` instead of navigating. An empty
slot stays a no-op.

Help and the picker read the same map object that dispatch reads:

- Help keeps the generic 1-9 row and adds a "Namespace slots" group. Each row
  is a slot number and `namespace (pinned|auto[, unavailable])`, plus a stale
  note when discovery failed.
- The picker labels listed namespaces with their slot number. Unavailable
  slots appear as disabled rows. Selection navigates by the option's namespace
  id, never by parsing its label.

`:slots` (alias `:ns-slots`, also in the Action Palette) runs a fresh listing,
builds the reallocation proposal and opens a class-selected confirmation modal
with a bounded `VerticalScroll` preview. Enter confirms; Escape cancels and
keeps the current map. An unchanged proposal is reported without opening the
modal. A failed listing refuses with the same notice as the picker.

## #404 boundary

Keys 1-9 stay fixed, unremappable bindings, so the keybinding editor still
reserves them and never offers them as a target. `0` stays the remappable
all-namespaces action. Keybinding apply/reset writes only the `keybindings`
section of the config file. Reallocation writes only the slot state file. Each
reset domain therefore leaves the other untouched, and tests assert both
directions.

## Size budget

`__main__.py`, `ui/app.py`, `ui/workspace_controller.py` and `core/config.py`
are within a few lines of their reviewed caps. The new behavior lives in new
modules. The capped files gain only delegation lines, and they make room by
extracting existing code rather than raising caps.

## Verification

- Pure tests: pins first, pin/auto collisions, duplicates, additions without
  renumbering, confirmed removal, reappearance, unknown inventory, more than
  nine names, reallocation and preview.
- Store tests: round trip, per-identity isolation, other records preserved,
  malformed/wrong-version documents, write failure leaves the old file.
- Identity tests: explicit and current-context names, server change isolates.
- Controller and TUI tests: dispatch matches help and picker labels, unavailable
  notice, stale result after `:ctx` rejected, 403 does not probe, refresh never
  navigates, reallocation confirm/cancel/save failure, keybinding reset leaves
  slots and reallocation leaves keybindings.
- Targeted checks while iterating; `make check`, coverage and the existing
  pre-commit gates before handoff. Follow the review loop in AGENTS.md and
  never merge.
