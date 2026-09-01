# Context-Switch Default Namespace Design

- Date: 2026-09-02
- Issue: #332
- Milestone: `v0.4.0`
- Status: Approved for implementation

## Goal

When switching from a context with a named namespace to a context whose
kubeconfig namespace is unset, adopt Kubernetes' concrete `default` namespace
instead of retaining the previous cluster's namespace.

## Root cause

`ContextSwitchResult.context_namespace` correctly uses `None` for an unset
kubeconfig namespace. `AppSessionConfiguration.adopt()` currently interprets
that valid result with a truthiness fallback:

```python
result.context_namespace or self._app.config.namespace
```

The fallback selects old mutable session state. Workspace reset then reads the
wrong config value, so watches, metrics, UI scope, and later namespace
fallbacks can target the same-named namespace on the new cluster.

## Approaches

### Normalize at session adoption (selected)

Set `namespace=result.context_namespace or "default"` when atomically adopting
the proven context switch. This mirrors startup's concrete namespace invariant
and keeps kubeconfig facts separate from UI session policy.

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
default namespace, and per-cluster capability adoption. It replaces the old
namespace with:

```python
namespace=result.context_namespace or "default"
```

No coordinator ordering changes are required. `_apply()` already calls
`session.adopt()` before `workspace.reset_view_after_switch()`, so the workspace
reads the new concrete namespace in the same event-loop slice. Resume logic
then restarts watches, metrics, and namespace completion against that scope.

The `ContextSwitchResult.context_namespace` type stays `str | None`; `None`
continues to mean the target kubeconfig did not configure a namespace.

## Error handling

Probe or retarget failure continues to leave the old session untouched or use
the existing restoration path. The default is materialized only after a
successful target switch reaches `_apply()`.

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
the concrete namespace invariant, and add a direct coordinator assertion for
the unset result.

Run the focused coordinator and app context-switch tests, Ruff, mypy, and Tach
if imports change. Finish with the full repository gate and PR review loop.

## Non-goals

- Changing kubeconfig namespace resolution
- Treating an empty namespace as all-namespaces access
- Changing context-switch ordering or failure recovery
- Refactoring workspace, metrics, Helm, or operator controllers
