# UI controllers

`ui/app.py` is being decomposed into focused controllers (issue #187). This
records where the boundaries are, so the next extraction has something to
follow and a reviewer can tell at a glance what a controller is allowed to
touch.

## The shape

```
KorvidApp  (ui/app.py)
│
│  owns: Textual composition and message translation, screens and modals,
│         navigation and scope, the write-approval perimeter, run_worker
│         ownership, and the audited execution path
│
│  reached through: WriteGate, ViewState, UiSurface
│
├── HelmController      (ui/helm_controller.py)      install/upgrade/rollback/uninstall
├── OperatorController  (ui/operator_controller.py)  OLM subscribe/approve/uninstall
├── ForwardController   (ui/forward_controller.py)   port-forward sessions
├── ShellController     (ui/shell_controller.py)     pod exec, debug fallback, node shell
├── TransferController  (ui/transfer.py)             post-approval file transfer
├── DebugController     (ui/debug.py)                gated kubectl debug runs
└── ...                                              further extractions pending
```

Controllers do **not** import `app.py`. Dependencies arrive in the
constructor, and the load-bearing ones arrive as *named interfaces* rather
than anonymous callables.

### Why interfaces and not callables

The first extraction used one callable per dependency and `HelmController`
ended up with 21. Measuring what the remaining areas would need showed that
does not scale:

| Area | Methods | Distinct app attributes used |
|---|---:|---:|
| OLM | 15 | 26 |
| port-forward | 14 | 27 |
| logs | 24 | 36 |
| shell / debug | 19 | 41 |

Worse than the count, `Callable[..., bool]` erases the argument contract. The
approval and revalidation hooks carry the security-relevant keywords
(`action`, `meta`, `op_factory`, `epoch`), and under an ellipsis mypy stops
checking them — dropping `epoch=epoch` from a context check type-checked
clean. As `WriteGate` it fails with *Missing named argument "epoch"*.

So the boundaries that matter are named:

- **`WriteGate`** (`ui/write_gate.py`) — approval, revalidation, the
  fail-closed audit precondition, and the context epoch. One implementation,
  `AppWriteGate`, adapting the app. Approval has two typed entry points,
  `confirm` and `confirm_interactive`; they differ in who records the intent
  audit, which is spelled out below.
- **`ViewState`** (`ui/view_state.py`) — what the user is currently looking
  at: the focused kind and scope, alias resolution, the selected row, and a
  `resources(kind, scope)` query. Read-only structurally, not just by
  convention: there are no setters, `aliases()` returns a `Mapping` view,
  and neither the `ResourceStore` nor `KorvidConfig` is exposed — the
  first would let a controller `clear` the view, and the second is only
  shallowly frozen, so its `keybindings` and `agent_options` dicts are
  mutable. Configuration arrives as `readonly()` and
  `default_namespace()`, which is all any controller uses. Implemented by
  `AppViewState`.
- **`UiSurface`** (`ui/ui_surface.py`) — the Textual capabilities a
  controller may use: `notify`, `push_screen`, `run_worker`, `progress` and
  screen inspection, plus the terminal capabilities an interactive child
  process needs (`suspend`, `refresh`, `call_from_thread`).
  `push_screen` is generic over the screen's result type,
  so a callback written for a different screen is a type error; `notify`
  takes the `Literal` severity Textual accepts rather than a bare `str`;
  and screens are *asked about* rather than handed over — `screen_depth()`
  and `is_current_screen(screen)` — because the live stack would let a
  controller `pop` or reorder screens, and a live `Screen` carries
  `dismiss` and `app`, which is app access routed around the surface.
  Implemented by `AppUiSurface`.

`UiSurface.run_worker` gives supervision, not context safety: a `:ctx`
switch cancels only the `hint-events` group, so a worker started before the
switch keeps running against the cluster it captured. Controllers that
outlive an await revalidate through `WriteGate.context_intact` or the epoch
they captured.

`AppWriteGate` is an adapter rather than the app inheriting `WriteGate`
because Textual's `App` metaclass conflicts with `ABCMeta` — the same reason
`AppUIBridge` exists.

## What stays on the app

These are deliberately *not* distributed into controllers, because each one
must have exactly one implementation:

| Concern | Why it stays |
|---|---|
| `_push_write_confirmation` | The approval gate for a write that is an API call. Agent writes and user writes enter here and nowhere else. Reached through `WriteGate.confirm`. |
| `_push_interactive_confirmation` | The approval gate for a write whose approved form is an interactive subprocess. Reached through `WriteGate.confirm_interactive`. |
| `_write_context_intact` | Revalidation after an awaited gap, including the `:ctx` epoch check. Reached through `WriteGate.context_intact`. |
| `_run_write` | Audit-before-mutation, fail-closed. Never called by a controller. |
| `run_worker` ownership | Cancellation and exclusivity belong to the app that owns the event loop. |

A controller composes an operation *factory* and hands it to the gate. The
ordering is the invariant, not the location of the helm/kubectl call: a
declined dialog constructs nothing, and the app awaits the factory from its
own worker only after the intent audit record has persisted.

### Two approval contracts, and where the audit lives

`confirm` covers the ordinary case: the gate audits the intent, fail-closed,
and only then awaits the operation. Nothing about the write is unknown to it,
so it can record everything before anything happens.

`confirm_interactive` (#236) exists because the shell flows — the `kubectl
debug` fallback and `kubectl debug node/` — cannot honour that contract. Their
approved form is a subprocess that suspends the app, and the facts worth
auditing only exist once it has started: the pod kubectl actually created, its
uid, and the session's exit outcome. `_run_write` cannot know any of them.

So the split is explicit rather than accidental:

| | `confirm` | `confirm_interactive` |
|---|---|---|
| dialog | gate | gate |
| epoch recheck after the dialog | gate | gate |
| write reservation | gate (`_run_write`) | the flow's `@_tracks_cluster_write` coroutine |
| fail-closed intent audit | gate (`_run_write`) | **the flow** |
| outcome audit | — | the flow |

The invariant is unchanged — no approved write reaches a cluster before an
intent record has persisted — but for the interactive flows it is `debug.run`
and `_run_node_shell` that enforce it, each blocking the subprocess when the
append fails. `tests/ui/test_node_shell.py::test_node_shell_blocked_when_audit_append_fails`
pins that, and it is the reason this row reads "the flow" rather than "gate".

## Late binding

Dependency getters are read at call time, not captured at construction:

- a `:ctx` switch retargets clients (the helm wrapper pins `--kube-context`
  per instance), so a captured client would write to the previous cluster;
- the active scope, kind and namespace change constantly;
- tests patch app methods after constructing the app, and a bound method
  captured in `__init__` would freeze whatever existed at that moment.

`HelmController` takes `helm=lambda: self._helm` rather than `helm=self._helm`
for the first reason, and wraps the editor entry points in lambdas for the
third.

## Extraction order

Least coupled first, one responsibility per change, with characterization
tests added before the move where the behaviour is not already pinned.

1. ~~Helm workflows~~ — done (#187)
2. ~~OLM / operator workflows~~ — done (#187)
3. ~~The `WriteGate` / `ViewState` / `UiSurface` seams~~ — done (#187);
   this is what dropped `HelmController` from 21 dependencies to 6
4. ~~Port-forward~~ — done (#187)
5. ~~Shell / debug / node shell~~ — done (#187)

Stopping here is a proposal, not a completed criterion. #187 as written
asks for `app.py` at most 5,000 lines; it is 7,885. Classifying all of
`KorvidApp` shows that target cannot be met without also moving
navigation and pane composition, which the same issue says stay on the
app — extracting every remaining candidate lands at ~5,700.

The argument for stopping is that the property worth having is coupling,
not line count, and that property is already achieved. Measuring the
remaining areas against the seams:

- **logs** and **describe** reach into pane composition (`_pane`,
  `_describe_pane`, `_focused_table`, `query_one`). Extracting them behind
  the current seams would relocate that coupling rather than remove it;
  they need a pane seam first, or they should stay.
- **agent + MCP** needs 64 distinct app attributes. At that level an
  extraction is a rewrite, and the approval perimeter is exactly where a
  rewrite is least welcome.

Navigation, scope and pane lifecycle stay on the app: composing panes and
owning the focused view *is* the app's job, so moving it would relocate the
coupling rather than remove it.
