# Context-Switch Default Namespace Design

- Date: 2026-09-02
- Issue: #332
- Milestone: `v0.4.0`
- Status: Approved for implementation

## Goal

When switching from a context with a named namespace to a context whose
kubeconfig namespace is unset, adopt Kubernetes' concrete `default` namespace
instead of retaining the previous cluster's namespace. If that target switch
fails mid-swap and korvid restores the old context, restore the exact
pre-switch concrete session namespace rather than defaulting.

## Root cause

`ContextSwitchResult.context_namespace` correctly uses `None` for an unset
kubeconfig namespace. The original bug came from reading old mutable session
state when adoption saw that `None`. Task 1 corrected successful adoption by
normalizing `None` to `"default"`, but that change exposed a second problem:
the same adoption path is also used when a target swap fails and the
coordinator restores the old context.

Recovery cannot infer the old session namespace from the restore result alone.
That result still reports the old context's kubeconfig fact, which may be
`None` even when the session had a concrete namespace before the switch (for
example from prior adoption or startup configuration). Without an explicit
recovery override, restore replays the success-path normalization and loses the
pre-switch namespace.

The old successful-adoption bug was:

```python
result.context_namespace or self._app.config.namespace
```

The fallback selects old mutable session state. Workspace reset then reads the
wrong config value, so watches, metrics, UI scope, and later namespace
fallbacks can target the same-named namespace on the new cluster.

## Approaches

### Normalize at session adoption (selected)

Normalize successful adoption to `result.context_namespace or "default"`, but
teach the coordinator to snapshot the pre-switch concrete session namespace and
pass it back only when recovery adopts the restored context. This keeps
`ContextSwitchResult` as a raw kubeconfig fact while preserving exact recovery
state.

### Normalize in the composition root

Returning `"default"` from the `switch_context` wiring would work, but changes
`ContextSwitchResult` from a kubeconfig fact into an effective UI value and
duplicates startup/session policy in the Kubernetes wiring.

### Preserve `None` and rely on consumers

Every consumer could use `config.namespace or "default"`. This leaves the
session config non-concrete and makes safety depend on every present and future
consumer remembering the fallback.

## Design

`AppSessionConfiguration.adopt()` remains the single atomic owner of context,
default namespace, and per-cluster capability adoption. On a successful target
switch it resolves the adopted namespace with:

```python
namespace=result.context_namespace or "default"
```

The coordinator now snapshots `SessionConfiguration.default_namespace()` before
the first swap attempt. If retargeting the requested context fails and the old
context is restored, `_apply()` passes that snapped concrete namespace back into
`session.adopt(..., namespace=old_namespace)`.

No coordinator ordering changes are required. `_apply()` still calls
`session.adopt()` before `workspace.reset_view_after_switch()`, so the workspace
reads the intended concrete namespace in the same event-loop slice. Resume logic
then restarts watches, metrics, and namespace completion against the applied
scope whether the target succeeded or the old context was restored.

The `ContextSwitchResult.context_namespace` type stays `str | None`; `None`
continues to mean the target kubeconfig did not configure a namespace.

## Error handling

Probe failure continues to leave the old session untouched. Mid-swap failure
still uses the existing restoration path, but recovery adoption now restores
the snapped pre-switch concrete namespace instead of re-deriving from the
restore result. The `default` fallback is materialized only for successful
target adoption.

This change does not weaken proposal expiry, write blocking, audit retargeting,
or context restoration behavior.

## Testing

Add an app-level context-switch regression that starts with a non-default
namespace and returns `context_namespace=None` for the target:

- `app.config.namespace` becomes `"default"`;
- `app.current_scope` becomes `"default"`;
- the new-cluster pod watch serves the `"default"` bucket;
- the metrics poller, when configured, receives `"default"`;
- all-namespaces toggle-off returns to `"default"`;
- the previous namespace is never requested after the switch.

Also update the coordinator fake's `SessionConfiguration` behavior to preserve
the explicit recovery override contract, and add a direct coordinator
regression that starts with `team-old`, forces the target swap to fail, returns
`context_namespace=None` during restore, and proves recovered session/view/watch
state stays on `team-old`.

Run the focused coordinator and app context-switch tests, Ruff, mypy, and Tach
if imports change. Finish with the full repository gate and PR review loop.

## Non-goals

- Changing kubeconfig namespace resolution
- Treating an empty namespace as all-namespaces access
- Changing context-switch ordering or whether failure recovery happens
- Refactoring workspace, metrics, Helm, or operator controllers
