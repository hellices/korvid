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
├── HelmController      (ui/helm_controller.py)   install/upgrade/rollback/uninstall
├── TransferController  (ui/transfer.py)          post-approval file transfer
├── DebugController     (ui/debug.py)             gated kubectl debug runs
└── ...                                           further extractions pending
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
  `AppWriteGate`, adapting the app.
- `ViewState` and `UiSurface` — planned, same treatment for "what is the user
  looking at" and "the Textual capabilities a controller may use".

`AppWriteGate` is an adapter rather than the app inheriting `WriteGate`
because Textual's `App` metaclass conflicts with `ABCMeta` — the same reason
`AppUIBridge` exists.

## What stays on the app

These are deliberately *not* distributed into controllers, because each one
must have exactly one implementation:

| Concern | Why it stays |
|---|---|
| `_push_write_confirmation` | The approval gate. Agent writes and user writes enter here and nowhere else. Reached through `WriteGate.confirm`. |
| `_write_context_intact` | Revalidation after an awaited gap, including the `:ctx` epoch check. Reached through `WriteGate.context_intact`. |
| `_run_write` | Audit-before-mutation, fail-closed. Never called by a controller. |
| `run_worker` ownership | Cancellation and exclusivity belong to the app that owns the event loop. |

A controller composes an operation *factory* and hands it to the gate. The
ordering is the invariant, not the location of the helm/kubectl call: a
declined dialog constructs nothing, and the app awaits the factory from its
own worker only after the intent audit record has persisted.

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
2. OLM / operator workflows — the next most self-contained
3. Logs, shell, port-forward session operations
4. Agent, follow mode, MCP bridge
5. Navigation, scope, pane lifecycle

The remaining areas are more entangled with navigation state than helm was,
so each needs its own responsibility map before it moves.
