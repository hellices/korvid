# Stable automatic namespace shortcuts

## Scope

Implement issue #406 for v0.6.0. Keys 1-9 keep their fixed bindings and
dispatch path. What changes is the map behind them. Configured
`favorite_namespaces` stay pinned first. Saved automatic slots for the
current cluster come next. A namespace the user switches to takes whatever
slot is still free, the first time they visit it. The handoff is an implementation PR with
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
     their authorization behavior is unchanged. A namespace listed twice is
     pinned once, at its first position, so the map never repeats a name and
     visits still take the lowest genuinely free slot.
  2. Saved automatic entries keep their slot numbers. An entry is dropped if
     its slot is now pinned, its namespace is now pinned, or it repeats an
     earlier namespace. Pins always win.
  3. With a complete inventory, an automatic entry whose namespace is absent
     becomes unavailable. It keeps its slot, so the number is not reused.
     An unavailable namespace that reappears becomes available again in the
     same slot. With an unknown inventory, saved availability is kept as is.
  4. A listing never adds an entry. Only a visit does.
- `place(slots, namespace)` records a visit. A namespace not yet in the map
  takes the lowest free slot; one already there, pinned, automatic or
  unavailable, keeps its slot. With no free slot nothing changes. Example:
  `1 dev / 2 prod / 3 staging` plus a visit to `alpha` becomes `... / 4 alpha`.
- `reallocate(pinned, saved, inventory)` is the explicit rebuild. It needs a
  complete inventory. It keeps pins, drops automatic entries the listing no
  longer contains and moves the rest up into the lowest free slots in their
  current slot order. Unavailable slots are reclaimed.
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
processes. A malformed or wrong-version document (the version must be the
integer 1, not `true` or `1.0`) is not overwritten. Loading
it yields an empty saved map with one warning, and saving it fails. A failed
save keeps the last saved file intact and reports an actionable notice once.
Routine discovery keeps the new map in memory for this session. Reallocation
commits only after its save succeeds, mirroring the #404 apply contract.

Because a save waits on that lock and fsyncs, it never runs on the event loop.
Visits and discovery queue the latest map per cluster, and one worker writes
the queue in a thread. Every write, including a confirmed reallocation, holds
one asyncio lock, so an older map never lands after a newer one. A map queued
before a `:ctx` switch is still written to the cluster it belongs to. A
confirmed reallocation also installs its fresh listing as the inventory later
visits are checked against. Visits made while its write waited are replayed
onto the new map when that listing still contains them. If `:ctx` switched
during that write, the old cluster's queued map is rebuilt the same way
before it is written, so it cannot overwrite the confirmed map. Quitting
during that write rebuilds the queue the same way before `shutdown` drains
it. If the write
fails, maps already queued stay queued and are still written. A queued save
that fails after `:ctx` switched away is reported with the old context name.
It does not stop the new cluster's saves.

Activation reads the file under the same lock. A write still in flight for
the cluster lands first, and a map still queued for it is adopted instead
of the older file. Restored entries and replayed visits are judged against
a listing that completed during the restore. A cancelled worker leaves its
thread write running.
Textual cancels app workers before `on_unmount`, so unmount calls the
controller's `shutdown`. It waits for that write, then writes the maps still
queued, so a visit just before quitting is kept.

## Visits

Slots follow the namespaces the user works in, not the cluster's name order.
An earlier draft filled free slots from the listing in name order. In a
cluster with more than nine namespaces that fills every key with
`cert-manager`, `default`, `kube-node-lease`, `kube-public`, `kube-system` and
similar names, and saving the numbers would make that permanent.

A visit is a user-initiated `NavigateCommand` that lands on a namespace:
`:ns <name>`, `:<view> <name>` or a picker selection. Agent navigation, the `0`
all-namespaces toggle and the slot keys themselves are not visits. After the
navigation, the app asks the slot controller to `visit` the namespace when
the pane that issued the command is now on it. Focus may have moved to another
split pane meanwhile. A visit made while activation resolves the cluster
identity takes a free slot of the restored map. A navigation that a `:ctx`
switch started or finished during is not recorded: the namespace belongs to
the cluster switched away from.

- The all-namespaces scope is never assigned.
- With a complete inventory from the current generation, a name the listing
  lacks (a mistyped `:ns`) is not assigned. Without one the visit is trusted,
  and the next complete listing marks it unavailable if it does not exist.
- A visit while a `:ctx` switch is in progress lands in the cleared map and is
  replaced when the new cluster's saved map is restored, so it is never saved
  to either cluster.

## Discovery triggers

No new trigger and no new watch. The existing startup/post-`:ctx` completion
prefetch and the `:ns` picker listing feed their results to the slot
controller, which uses them only to judge availability and to reject
mistyped visits. A 403 keeps the existing single permission notice and never
probes individual namespaces. Configured favorites are never used as a
fallback inventory. When discovery fails, help shows the map as last known,
with a stale note. A failed listing also clears the inventory, so a visit
during the outage is trusted until the next complete listing.

## UI and dispatch

A `NamespaceSlotController` in `ui/` owns the effective map, its generation
and persistence. The workspace controller asks it for slot *n* in
`favorite_namespace`. An available entry navigates through the unchanged
`navigate_command` path, in the focused pane only. An unavailable slot posts
a notice naming the namespace and `:slots` instead of navigating. An empty
slot stays a no-op. While a `:ctx` switch runs, slot keys do nothing. A key
whose slot was read before the switch is checked again under the navigation
lock, so it never sends the new cluster to the old cluster's namespace.

Help and the picker read the same map object that dispatch reads:

- Help keeps the generic 1-9 row and adds a "Namespace slots" group. Each row
  is a slot number and `namespace (pinned|auto[, unavailable])`, plus a stale
  note when discovery failed. Help opened during a `:ctx` switch omits the
  group, because the map then belongs to neither cluster.
- The picker labels listed namespaces with their slot number. Slots whose
  namespace the listing lacks follow it; only unavailable ones are disabled,
  so an unlisted pin stays selectable. An empty listing still opens the
  picker when the map has slots. Selection navigates by the option's namespace
  id, never by parsing its label.

`:slots` (alias `:ns-slots`, also in the Action Palette) runs a fresh listing,
builds the reallocation proposal and opens a class-selected confirmation modal
with a bounded `VerticalScroll` preview. Enter confirms; Escape cancels and
keeps the current map. An unchanged proposal is reported without opening the
modal; its listing still counts, clearing a stale map and becoming the
inventory later visits are checked against. A failed listing refuses with the same notice as the picker. If a
dialog opened while the listing ran, the preview is not stacked over it. A
confirmation while the saved map is still loading is refused with a retry
notice, because the restore would replace the new map.

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

- Pure tests: pins first, pin/auto collisions, duplicates, visits in
  first-visit order, a listing that never assigns, confirmed removal,
  reappearance, unknown inventory, more than nine visits, reallocation and
  preview.
- Store tests: round trip, per-identity isolation, other records preserved,
  malformed/wrong-version documents (including a slot key beyond `int()`'s
  digit limit), write failure leaves the old file.
- Identity tests: explicit and current-context names, server change isolates.
- Controller and TUI tests: dispatch matches help and picker labels, visits
  through `NavigateCommand`, a mistyped visit is not assigned, a visit during
  `:ctx` is not saved, unavailable notice, stale result after `:ctx` rejected,
  403 does not probe, refresh never navigates, reallocation confirm/cancel/save failure, keybinding reset leaves
  slots and reallocation leaves keybindings.
- Targeted checks while iterating; `make check`, coverage and the existing
  pre-commit gates before handoff. Follow the review loop in AGENTS.md and
  never merge.
