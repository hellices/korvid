# Pod Resize Impact Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add closed, empty graph semantics and deterministic Pod-local runtime advisories to both TUI and agent in-place Pod resize approvals.

**Architecture:** `korvid.core.impact` owns the exhaustive graph action policy, while a new pure `korvid.core.resize_impact` module classifies only the captured Pod manifest and requested resource changes. A focused UI renderer turns that immutable context into machine-defined bounded lines. `KorvidApp` composes those local lines with the existing graph summary and preserves separate scope rules for pane-origin TUI writes and explicitly targeted agent writes.

**Tech Stack:** Python 3.11+, `dataclasses`, `Decimal`-based Kubernetes quantity parsing already exposed by `korvid.k8s.models.parse_quantity`, Textual `ConfirmScreen`, pytest/pytest-asyncio, Ruff, mypy strict, tach.

## Global Constraints

- Work only in `.worktrees/feat-293-pod-resize-impact` on branch `feat/293-pod-resize-impact`.
- The graph relation set for `ImpactAction.POD_RESIZE` is exactly empty and its unresolved-reference policy reuses the same frozenset object.
- Advisory text is machine-defined: never interpolate container names, quantity strings, annotations, exception messages, or API response bodies.
- Preserve the approval gate, `patch pods/resize` RBAC pre-check, server dry-run, UID precondition, cancellation propagation, write reservation, and fail-closed intent audit.
- TUI snapshots use the captured pane scope and abort on pane/scope/context/selection/UID drift; agent snapshots use the explicit target namespace and never the active pane.
- No new dependency, no `uv lock`, and no change to `uv.lock`.
- On this corporate-mirror worktree, do not run `uv sync`: use the `PYTHONPATH=src uv run --no-sync` command prefix shown below so uv 0.10.9 cannot rewrite lock URLs.
- Targeted UI tests run with `-p no:tach`; import-boundary changes require `PYTHONPATH=src uv run --no-sync tach check`.

---

## File Structure

- Modify `src/korvid/core/impact.py`: add the exhaustive empty `POD_RESIZE` graph policy.
- Create `src/korvid/core/resize_impact.py`: pure manifest/change classifier and immutable result type.
- Modify `src/korvid/ui/impact_preview.py`: add the `pod resize` action label; keep graph rendering exhaustive.
- Create `src/korvid/ui/resize_impact_preview.py`: bounded Pod-local renderer and graph/local composition helper.
- Modify `src/korvid/ui/app.py`: add scope-based graph loading, TUI resize race gates, agent resize scope, and impact-line plumbing.
- Modify `tests/core/test_impact.py`: pin every relation and unresolved-reference exclusion.
- Create `tests/core/test_resize_impact.py`: classifier unit tests.
- Modify `tests/ui/test_impact_preview.py`: pin action-label exhaustiveness for the new enum member.
- Create `tests/ui/test_resize_impact_preview.py`: Pod-local renderer unit tests.
- Modify `tests/ui/test_resize_flow.py`: TUI and agent end-to-end, scope, failure, and approval tests.
- Modify `docs/tui.md`: document the resize approval sections and operational limits.
- Modify `docs/resource-relationships.md`: document the intentionally empty relation policy.
- Update GitHub issues `#300` and parent `#293` only after implementation and validation; do not close either issue before a human merges the eventual PR.

---

### Task 1: Close the Graph Action Policy

**Files:**
- Modify: `src/korvid/core/impact.py:64-169`
- Modify: `tests/core/test_impact.py:190-367`
- Modify: `src/korvid/ui/impact_preview.py:84-88`
- Modify: `tests/ui/test_impact_preview.py`

**Interfaces:**
- Consumes: existing `RelationKind`, `ACTION_RELATIONS`, `ACTION_UNRESOLVED_RELATIONS`, and `summarize_impact`.
- Produces: `ImpactAction.POD_RESIZE` with an empty shared policy used by later app integration.

- [ ] **Step 1: Write failing enum and closed-set tests**

Add the new expected value and parameterized exclusions to
`tests/core/test_impact.py`:

```python
def test_only_supported_writes_carry_action_semantics() -> None:
    assert [action.value for action in ImpactAction] == [
        "delete",
        "rollout_restart",
        "scale_down",
        "pod_resize",
    ]
    assert set(ACTION_RELATIONS) == set(ImpactAction)
    assert ACTION_RELATIONS[ImpactAction.POD_RESIZE] == frozenset()


@pytest.mark.parametrize("relation", list(RelationKind))
def test_pod_resize_ignores_every_relationship(relation: RelationKind) -> None:
    pod = _res("Pod", "web-1", uid="pod-1")
    related = _res("Service", "web", uid="related-1")
    summary = summarize_impact(
        _graph(_edge(related, pod, relation, field="spec.example")),
        ImpactAction.POD_RESIZE,
        pod,
    )
    assert summary.direct == ()
    assert summary.transitive == ()


@pytest.mark.parametrize("relation", list(RelationKind))
def test_pod_resize_ignores_every_unresolved_relationship(relation: RelationKind) -> None:
    pod = _res("Pod", "web-1", uid="pod-1")
    missing = _res("ConfigMap", "gone")
    summary = summarize_impact(
        _graph(
            _edge(
                pod,
                missing,
                relation,
                resolution=EdgeResolution.MISSING,
                field="spec.example",
            )
        ),
        ImpactAction.POD_RESIZE,
        pod,
    )
    assert summary.unresolved == ()
```

Extend `test_every_action_chooses_its_unresolved_reference_policy`:

```python
assert (
    ACTION_UNRESOLVED_RELATIONS[ImpactAction.POD_RESIZE]
    is ACTION_RELATIONS[ImpactAction.POD_RESIZE]
)
```

- [ ] **Step 2: Run the RED core tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/core/test_impact.py::test_only_supported_writes_carry_action_semantics \
  tests/core/test_impact.py::test_pod_resize_ignores_every_relationship \
  tests/core/test_impact.py::test_pod_resize_ignores_every_unresolved_relationship \
  tests/core/test_impact.py::test_every_action_chooses_its_unresolved_reference_policy -q
```

Expected: FAIL because `ImpactAction.POD_RESIZE` does not exist.

- [ ] **Step 3: Implement the empty graph policy**

Add to `src/korvid/core/impact.py`:

```python
class ImpactAction(StrEnum):
    DELETE = "delete"
    ROLLOUT_RESTART = "rollout_restart"
    SCALE_DOWN = "scale_down"
    POD_RESIZE = "pod_resize"


_POD_RESIZE_RELATIONS: frozenset[RelationKind] = frozenset()

ACTION_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind]] = {
    ImpactAction.DELETE: _DELETE_RELATIONS,
    ImpactAction.ROLLOUT_RESTART: _ROLLOUT_RESTART_RELATIONS,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
    ImpactAction.POD_RESIZE: _POD_RESIZE_RELATIONS,
}

ACTION_UNRESOLVED_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind] | None] = {
    ImpactAction.DELETE: None,
    ImpactAction.ROLLOUT_RESTART: None,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
    ImpactAction.POD_RESIZE: _POD_RESIZE_RELATIONS,
}
```

Document above `_POD_RESIZE_RELATIONS` why all nine relation kinds are
excluded: the operation keeps the Pod object, membership, references, and
placement intact.

Add the exhaustive label in `src/korvid/ui/impact_preview.py`:

```python
_ACTION_LABEL = {
    ImpactAction.DELETE: "delete",
    ImpactAction.ROLLOUT_RESTART: "rollout restart",
    ImpactAction.SCALE_DOWN: "scale down",
    ImpactAction.POD_RESIZE: "pod resize",
}
```

Update renderer exhaustiveness expectations in
`tests/ui/test_impact_preview.py` without adding resize-specific local copy
there; that belongs to Task 3.

- [ ] **Step 4: Run GREEN core and graph-renderer tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/core/test_impact.py tests/ui/test_impact_preview.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Lint and commit the closed graph contract**

Run:

```bash
PYTHONPATH=src uv run --no-sync ruff check --fix \
  src/korvid/core/impact.py src/korvid/ui/impact_preview.py \
  tests/core/test_impact.py tests/ui/test_impact_preview.py
PYTHONPATH=src uv run --no-sync ruff format \
  src/korvid/core/impact.py src/korvid/ui/impact_preview.py \
  tests/core/test_impact.py tests/ui/test_impact_preview.py
git diff --check
git add src/korvid/core/impact.py src/korvid/ui/impact_preview.py \
  tests/core/test_impact.py tests/ui/test_impact_preview.py
git commit -m "feat: define empty Pod resize graph semantics (#300)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: pre-commit hooks pass and one focused commit is created.

---

### Task 2: Classify Pod-Local Resize Effects

**Files:**
- Create: `src/korvid/core/resize_impact.py`
- Create: `tests/core/test_resize_impact.py`

**Interfaces:**
- Consumes: `korvid.k8s.models.parse_quantity`.
- Produces:
  - `ResizeResourceChanges = Mapping[str, Mapping[str, Mapping[str, str]]]`
  - immutable `ResizeImpactContext`
  - `classify_pod_resize(manifest: Mapping[str, object], changes: ResizeResourceChanges) -> ResizeImpactContext`

- [ ] **Step 1: Write classifier tests before the module exists**

Create `tests/core/test_resize_impact.py` with production-shaped manifests and
the following assertions:

```python
from korvid.core.resize_impact import ResizeImpactContext, classify_pod_resize


def _manifest(*, policy: object = None, memory_limit: str = "512Mi") -> dict[str, object]:
    container: dict[str, object] = {
        "name": "app",
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": memory_limit},
        },
    }
    if policy is not None:
        container["resizePolicy"] = policy
    return {"spec": {"containers": [container]}}


def test_cpu_change_uses_default_not_required_policy() -> None:
    context = classify_pod_resize(
        _manifest(),
        {"app": {"requests": {"cpu": "200m"}}},
    )
    assert context == ResizeImpactContext(
        cpu_changed=True,
        memory_request_changed=False,
        memory_limit_changed=False,
        restart_required=False,
        restart_policy_unknown=False,
        all_changed_resources_not_required=True,
        memory_limit_decreased=False,
        memory_limit_decrease_not_required=False,
        memory_limit_assessment_unknown=False,
    )


def test_memory_limit_decrease_with_not_required_is_identified_numerically() -> None:
    context = classify_pod_resize(
        _manifest(memory_limit="1Gi"),
        {"app": {"limits": {"memory": "900Mi"}}},
    )
    assert context.memory_limit_changed is True
    assert context.memory_limit_decreased is True
    assert context.memory_limit_decrease_not_required is True
    assert context.memory_limit_assessment_unknown is False


def test_equivalent_memory_quantities_are_not_a_decrease() -> None:
    context = classify_pod_resize(
        _manifest(memory_limit="1Gi"),
        {"app": {"limits": {"memory": "1024Mi"}}},
    )
    assert context.memory_limit_decreased is False
    assert context.memory_limit_decrease_not_required is False
    assert context.memory_limit_assessment_unknown is False


def test_restart_container_policy_is_scoped_to_the_changed_resource() -> None:
    context = classify_pod_resize(
        _manifest(
            policy=[
                {"resourceName": "cpu", "restartPolicy": "NotRequired"},
                {"resourceName": "memory", "restartPolicy": "RestartContainer"},
            ]
        ),
        {"app": {"limits": {"memory": "768Mi"}}},
    )
    assert context.restart_required is True
    assert context.all_changed_resources_not_required is False


def test_missing_container_is_unknown_not_optimistic() -> None:
    context = classify_pod_resize(
        _manifest(),
        {"missing": {"limits": {"memory": "256Mi"}}},
    )
    assert context.restart_policy_unknown is True
    assert context.memory_limit_assessment_unknown is True
```

Also add cases for:

```python
def test_malformed_resize_policy_is_unknown() -> None:
    context = classify_pod_resize(
        _manifest(policy={"resourceName": "memory"}),
        {"app": {"requests": {"memory": "256Mi"}}},
    )
    assert context.restart_policy_unknown is True


def test_invalid_captured_memory_quantity_is_unknown() -> None:
    context = classify_pod_resize(
        _manifest(memory_limit="not-a-quantity"),
        {"app": {"limits": {"memory": "256Mi"}}},
    )
    assert context.memory_limit_assessment_unknown is True
```

- [ ] **Step 2: Run the RED classifier tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach tests/core/test_resize_impact.py -q
```

Expected: collection FAILS because `korvid.core.resize_impact` does not exist.

- [ ] **Step 3: Implement the pure classifier**

Create `src/korvid/core/resize_impact.py` with these public types and function:

```python
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from korvid.k8s.models import parse_quantity

ResizeResourceChanges = Mapping[str, Mapping[str, Mapping[str, str]]]


@dataclass(frozen=True, slots=True)
class ResizeImpactContext:
    cpu_changed: bool
    memory_request_changed: bool
    memory_limit_changed: bool
    restart_required: bool
    restart_policy_unknown: bool
    all_changed_resources_not_required: bool
    memory_limit_decreased: bool
    memory_limit_decrease_not_required: bool
    memory_limit_assessment_unknown: bool


def classify_pod_resize(
    manifest: Mapping[str, object], changes: ResizeResourceChanges
) -> ResizeImpactContext:
    containers = _containers_by_name(manifest)
    changed_resources = [
        (name, resource)
        for name, sections in changes.items()
        for values in sections.values()
        for resource in values
        if resource in {"cpu", "memory"}
    ]
    policies = [
        _restart_policy(containers.get(name), resource)
        for name, resource in changed_resources
    ]
    memory_decreased = False
    memory_decrease_not_required = False
    memory_unknown = False
    for name, sections in changes.items():
        desired = sections.get("limits", {}).get("memory")
        if desired is None:
            continue
        container = containers.get(name)
        current = _current_limit(container, "memory")
        policy = _restart_policy(container, "memory")
        if current is None:
            memory_unknown = True
            continue
        try:
            decreased = parse_quantity(desired) < parse_quantity(current)
        except ValueError:
            memory_unknown = True
            continue
        if decreased:
            memory_decreased = True
        if decreased and policy == "NotRequired":
            memory_decrease_not_required = True
        elif decreased and policy is None:
            memory_unknown = True

    policy_unknown = any(policy is None for policy in policies)
    restart_required = any(policy == "RestartContainer" for policy in policies)
    return ResizeImpactContext(
        cpu_changed=any(resource == "cpu" for _, resource in changed_resources),
        memory_request_changed=any(
            "memory" in sections.get("requests", {}) for sections in changes.values()
        ),
        memory_limit_changed=any(
            "memory" in sections.get("limits", {}) for sections in changes.values()
        ),
        restart_required=restart_required,
        restart_policy_unknown=policy_unknown,
        all_changed_resources_not_required=bool(policies)
        and not restart_required
        and not policy_unknown,
        memory_limit_decreased=memory_decreased,
        memory_limit_decrease_not_required=memory_decrease_not_required,
        memory_limit_assessment_unknown=memory_unknown,
    )
```

Implement the private helpers without broad catches:

```python
def _containers_by_name(manifest: Mapping[str, object]) -> dict[str, Mapping[str, Any]]:
    spec = manifest.get("spec")
    if not isinstance(spec, Mapping):
        return {}
    raw = spec.get("containers")
    if not isinstance(raw, list):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str):
            result[name] = item
    return result


def _restart_policy(container: Mapping[str, Any] | None, resource: str) -> str | None:
    if container is None:
        return None
    raw = container.get("resizePolicy")
    if raw is None:
        return "NotRequired"
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        if item.get("resourceName") != resource:
            continue
        policy = item.get("restartPolicy")
        return policy if policy in {"NotRequired", "RestartContainer"} else None
    return "NotRequired"


def _current_limit(container: Mapping[str, Any] | None, resource: str) -> str | None:
    if container is None:
        return None
    resources = container.get("resources")
    if not isinstance(resources, Mapping):
        return None
    limits = resources.get("limits")
    if not isinstance(limits, Mapping):
        return None
    value = limits.get(resource)
    return value if isinstance(value, str) else None
```

Keep the implementation at complexity 10 or below by extracting helpers
rather than nesting more branches inside `classify_pod_resize`.

- [ ] **Step 4: Run GREEN classifier tests and type checks**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach tests/core/test_resize_impact.py -q
PYTHONPATH=src uv run --no-sync ruff check --fix \
  src/korvid/core/resize_impact.py tests/core/test_resize_impact.py
PYTHONPATH=src uv run --no-sync ruff format \
  src/korvid/core/resize_impact.py tests/core/test_resize_impact.py
PYTHONPATH=src uv run --no-sync mypy src/korvid/core/resize_impact.py
PYTHONPATH=src uv run --no-sync tach check
```

Expected: all commands PASS; tach accepts the `core -> k8s` import.

- [ ] **Step 5: Commit the classifier**

```bash
git add src/korvid/core/resize_impact.py tests/core/test_resize_impact.py
git commit -m "feat: classify Pod-local resize effects (#300)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: pre-commit hooks pass.

---

### Task 3: Render Bounded Pod-Local Advisories

**Files:**
- Create: `src/korvid/ui/resize_impact_preview.py`
- Create: `tests/ui/test_resize_impact_preview.py`

**Interfaces:**
- Consumes: `ResizeImpactContext` from Task 2 and optional graph lines from `render_impact_lines`.
- Produces:
  - `render_resize_impact_lines(context: ResizeImpactContext) -> tuple[str, ...]`
  - `compose_resize_impact_lines(graph_lines: tuple[str, ...] | None, context: ResizeImpactContext) -> tuple[str, ...]`

- [ ] **Step 1: Write exact renderer tests**

Create `tests/ui/test_resize_impact_preview.py`:

```python
from korvid.core.resize_impact import ResizeImpactContext
from korvid.ui.resize_impact_preview import (
    compose_resize_impact_lines,
    render_resize_impact_lines,
)


def _context(**changes: bool) -> ResizeImpactContext:
    values = {
        "cpu_changed": True,
        "memory_request_changed": False,
        "memory_limit_changed": False,
        "restart_required": False,
        "restart_policy_unknown": False,
        "all_changed_resources_not_required": True,
        "memory_limit_decreased": False,
        "memory_limit_decrease_not_required": False,
        "memory_limit_assessment_unknown": False,
    }
    values.update(changes)
    return ResizeImpactContext(**values)


def test_cpu_only_not_required_resize_is_specific() -> None:
    lines = render_resize_impact_lines(_context())
    assert lines == (
        "Pod-local resize impact (advisory):",
        "  Pod identity and relationship membership stay unchanged; graph relations are not traversed",
        "  changed resources do not require a container restart under resizePolicy",
        "  node feasibility, Deferred/Infeasible status, actuation, and completion are not predicted",
    )


def test_restart_and_memory_decrease_warnings_are_conditional() -> None:
    lines = render_resize_impact_lines(
        _context(
            memory_limit_changed=True,
            restart_required=True,
            all_changed_resources_not_required=False,
            memory_limit_decrease_not_required=True,
        )
    )
    assert "  one or more changed resources require a container restart under resizePolicy" in lines
    assert (
        "  a memory-limit decrease using NotRequired has only best-effort OOM avoidance"
        in lines
    )


def test_unknown_input_never_becomes_a_no_restart_claim() -> None:
    lines = render_resize_impact_lines(
        _context(
            restart_policy_unknown=True,
            all_changed_resources_not_required=False,
            memory_limit_assessment_unknown=True,
        )
    )
    assert "  restart requirements could not be determined for every changed resource" in lines
    assert "  memory-limit direction or policy could not be determined" in lines
    assert not any("do not require" in line for line in lines)


def test_graph_and_local_sections_keep_their_order() -> None:
    lines = compose_resize_impact_lines(("graph-derived impact (advisory):",), _context())
    assert lines[0] == "graph-derived impact (advisory):"
    assert lines[1] == "Pod-local resize impact (advisory):"


def test_every_line_is_machine_bounded() -> None:
    assert all(len(line) <= 240 for line in render_resize_impact_lines(_context()))
```

- [ ] **Step 2: Run the RED renderer tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/ui/test_resize_impact_preview.py -q
```

Expected: collection FAILS because the renderer module does not exist.

- [ ] **Step 3: Implement the renderer and composer**

Create `src/korvid/ui/resize_impact_preview.py`:

```python
from __future__ import annotations

from korvid.core.resize_impact import ResizeImpactContext

_MAX_LINE = 240
_TITLE = "Pod-local resize impact (advisory):"
_RELATION_BOUNDARY = (
    "  Pod identity and relationship membership stay unchanged; "
    "graph relations are not traversed"
)
_RUNTIME_LIMIT = (
    "  node feasibility, Deferred/Infeasible status, actuation, "
    "and completion are not predicted"
)


def render_resize_impact_lines(context: ResizeImpactContext) -> tuple[str, ...]:
    lines = [_TITLE, _RELATION_BOUNDARY]
    if context.restart_required:
        lines.append(
            "  one or more changed resources require a container restart under resizePolicy"
        )
    if context.restart_policy_unknown:
        lines.append(
            "  restart requirements could not be determined for every changed resource"
        )
    elif context.all_changed_resources_not_required:
        lines.append(
            "  changed resources do not require a container restart under resizePolicy"
        )
    if context.memory_limit_decrease_not_required:
        lines.append(
            "  a memory-limit decrease using NotRequired has only best-effort OOM avoidance"
        )
    if context.memory_limit_assessment_unknown:
        lines.append("  memory-limit direction or policy could not be determined")
    lines.append(_RUNTIME_LIMIT)
    result = tuple(lines)
    assert all(len(line) <= _MAX_LINE for line in result)
    return result


def compose_resize_impact_lines(
    graph_lines: tuple[str, ...] | None, context: ResizeImpactContext
) -> tuple[str, ...]:
    local_lines = render_resize_impact_lines(context)
    return (*graph_lines, *local_lines) if graph_lines is not None else local_lines
```

All strings are constants; do not add a parameter that accepts arbitrary
display text.

- [ ] **Step 4: Run GREEN renderer tests and layer checks**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/ui/test_resize_impact_preview.py -q
PYTHONPATH=src uv run --no-sync ruff check --fix \
  src/korvid/ui/resize_impact_preview.py tests/ui/test_resize_impact_preview.py
PYTHONPATH=src uv run --no-sync ruff format \
  src/korvid/ui/resize_impact_preview.py tests/ui/test_resize_impact_preview.py
PYTHONPATH=src uv run --no-sync mypy src/korvid/ui/resize_impact_preview.py
PYTHONPATH=src uv run --no-sync tach check
```

Expected: all commands PASS.

- [ ] **Step 5: Commit the renderer**

```bash
git add src/korvid/ui/resize_impact_preview.py tests/ui/test_resize_impact_preview.py
git commit -m "feat(ui): render Pod-local resize advisories (#300)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: pre-commit hooks pass.

---

### Task 4: Integrate the TUI Resize Flow

**Files:**
- Modify: `src/korvid/ui/app.py:5988-6117`
- Modify: `tests/ui/test_resize_flow.py`

**Interfaces:**
- Consumes: `classify_pod_resize`, `compose_resize_impact_lines`, `ImpactAction.POD_RESIZE`, `_WriteOrigin`, and existing `_push_write_confirmation`.
- Produces: TUI `R` confirmations with graph plus local impact and identity-safe approval.

- [ ] **Step 1: Extend the resize harness and write RED flow assertions**

In `tests/ui/test_resize_flow.py`, add a recording relationship lister seam to
`make_app`. A `None` value leaves the loader unwired:

```python
async def list_relationship_objects(
    meta: ResourceMeta, namespace: str | None
) -> list[GenericSummary]:
    relationship_calls.append((meta.plural, namespace))
    if meta.plural == "pods":
        return [
            GenericSummary(
                name="web-1",
                namespace="default",
                kind="Pod",
                created="",
                uid="pod-uid-1",
            )
        ]
    return []
```

Add this optional parameter and constructor argument:

```python
def make_app(
    recorder: ResizeRecorder,
    audit_path: Path,
    *,
    resize_supported: bool = True,
    readonly: bool = False,
    permitted: bool | None = None,
    check_calls: list[tuple[str, str, str, str | None, str, str]] | None = None,
    get_manifest: object = None,
    relationship_calls: list[tuple[str, str | None]] | None = None,
) -> KorvidApp:
    async def list_relationship_objects(
        meta: ResourceMeta, namespace: str | None
    ) -> list[GenericSummary]:
        assert relationship_calls is not None
        relationship_calls.append((meta.plural, namespace))
        if meta.plural == "pods":
            return [
                GenericSummary(
                    name="web-1",
                    namespace="default",
                    kind="Pod",
                    created="",
                    uid="pod-uid-1",
                )
            ]
        return []

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest or default_get_manifest,
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None if permitted is None else check_permission,
        pod_resize_supported=resize_supported,
        list_relationship_objects=(
            list_relationship_objects if relationship_calls is not None else None
        ),
    )
```

Add a helper and a real `R` flow test:

```python
async def _open_resize_confirmation(
    app: KorvidApp, pilot: object, *, value: str = "200m"
) -> None:
    await pilot.press("R")
    await until(pilot, lambda: isinstance(app.screen, ResizePrompt), label="resize prompt opened")
    field = app.screen.query_one("#resize-0-requests-cpu", Input)
    field.value = value
    field.focus()
    await pilot.press("enter")
    await until(
        pilot,
        lambda: isinstance(app.screen, ConfirmScreen),
        label="resize confirmation opened",
    )


async def test_resize_confirm_shows_empty_graph_and_pod_local_impact(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    recorder = ResizeRecorder()
    app = make_app(
        recorder,
        tmp_path / "audit.jsonl",
        relationship_calls=calls,
    )
    async with app.run_test() as pilot:
        await _open_resize_confirmation(app, pilot)
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "pod resize Pod/default/web-1" in text
        assert "known direct dependents (may be affected): none in this snapshot" in text
        assert "Pod-local resize impact (advisory):" in text
        assert "graph relations are not traversed" in text
        assert not any(
            word in text
            for word in ("Service/default", "Deployment/default", "PodDisruptionBudget/default")
        )
        assert calls
        assert recorder.calls == []
```

Add complete no-loader and dialog-drift tests:

```python
async def test_resize_keeps_local_notes_without_relationship_loader(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    app = make_app(recorder, tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _open_resize_confirmation(app, pilot)
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "Pod-local resize impact (advisory):" in text
        assert "graph-derived impact" not in text
        assert recorder.calls == []


async def test_resize_refuses_uid_drift_while_confirmation_is_open(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(recorder, audit_path, relationship_calls=[])
    async with app.run_test() as pilot:
        await _open_resize_confirmation(app, pilot)
        app.store.apply_event(
            "pods",
            "default",
            "MODIFIED",
            PodSummary(
                name="web-1",
                namespace="default",
                phase="Running",
                ready="1/1",
                restarts=0,
                node=None,
                uid="pod-uid-2",
            ),
        )
        await until(
            pilot,
            lambda: app.store.get("pods", "default")[0].uid == "pod-uid-2",
            label="replacement pod rendered",
        )
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(
                "selection changed during the confirmation dialog" in n.message
                for n in app._notifications
            ),
            label="stale resize approval refused",
        )
        assert recorder.calls == []
        assert not audit_path.exists()
```

Add a `BlockingRelationshipLister` callable in this file. Start a resize,
wait for its first LIST, switch context or cancel the app worker, and assert
no `ConfirmScreen`, reservation, audit entry, or operation. Use
`asyncio.Event` to detect entry and release the fake in `finally`; never use a
wall-clock assertion.

- [ ] **Step 2: Run the RED TUI tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/ui/test_resize_flow.py -q
```

Expected: the new tests FAIL because resize does not request or render impact
and has no approval guard.

- [ ] **Step 3: Add imports and a scope-based impact loader**

In `src/korvid/ui/app.py`, import:

```python
from korvid.core.resize_impact import classify_pod_resize
from korvid.ui.resize_impact_preview import compose_resize_impact_lines
```

Extract the load body from `_impact_preview` into:

```python
async def _impact_preview_for_scope(
    self,
    action: ImpactAction,
    meta: ResourceMeta,
    ns: str | None,
    name: str,
    uid: str | None,
    *,
    scope: str | None,
) -> tuple[str, ...] | None:
    loader = self._relationship_loader
    if loader is None or uid is None:
        return None
    root = GraphResource(
        group=meta.group,
        kind=meta.kind,
        namespace=ns or "",
        name=name,
        uid=uid,
    )
    try:
        async with asyncio.timeout(_IMPACT_TIMEOUT):
            graph = await loader.load(root, scope, self.aliases)
            return render_impact_lines(
                summarize_impact(graph, action, root, scope=scope)
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("impact summary unavailable for %s: %s", action, type(exc).__name__)
        return render_unavailable_lines(action, meta.group, meta.kind)
```

Keep `_impact_preview(..., origin: _WriteOrigin)` as a wrapper:

```python
return await self._impact_preview_for_scope(
    action,
    meta,
    ns,
    name,
    uid,
    scope=origin.impact_scope(meta),
)
```

- [ ] **Step 4: Capture and gate the TUI resize origin**

In `action_resize_pod`, capture `origin = self._write_origin()` next to
`epoch`, before `_precheck_keybinding_write`. Immediately after the permission
pre-check, refuse a stale target before fetching the manifest:

```python
if not self._write_identity_intact(
    "resize",
    meta,
    ns,
    name,
    uid,
    phase="the permission check",
    epoch=epoch,
    origin=origin,
):
    return
```

Replace the post-manifest context-only check with an exact identity check:

```python
if not self._write_identity_intact(
    "resize",
    meta,
    ns,
    name,
    uid,
    phase="the manifest fetch",
    epoch=epoch,
    origin=origin,
):
    return
```

Pass `origin` into `_confirm_resize`. At the start of `_confirm_resize`, gate
the time spent in `ResizePrompt` before making the dry-run call. After dry-run
and ownership lookup, gate again before snapshot fan-out.

Classify and compose:

```python
context = classify_pod_resize(pod_manifest, resources)
graph_lines = await self._impact_preview(
    ImpactAction.POD_RESIZE,
    meta,
    ns,
    name,
    uid,
    origin=origin,
)
impact_lines = compose_resize_impact_lines(graph_lines, context)
```

After the graph await, re-run `_write_identity_intact`. Pass
`impact_lines=impact_lines` and:

```python
approval_guard=lambda: self._write_identity_intact(
    "resize",
    meta,
    ns,
    name,
    uid,
    phase="the confirmation dialog",
    epoch=epoch,
    origin=origin,
)
```

Do not construct `ops.resize_pod(...)` before `_push_write_confirmation`
accepts a real user approval.

- [ ] **Step 5: Run GREEN TUI flow and security tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/ui/test_resize_flow.py \
  tests/ui/test_impact_security.py \
  tests/ui/test_confirm_screen.py -q
PYTHONPATH=src uv run --no-sync ruff check --fix \
  src/korvid/ui/app.py tests/ui/test_resize_flow.py
PYTHONPATH=src uv run --no-sync ruff format \
  src/korvid/ui/app.py tests/ui/test_resize_flow.py
PYTHONPATH=src uv run --no-sync tach check
```

Expected: all selected tests and checks PASS.

- [ ] **Step 6: Commit the TUI integration**

```bash
git add src/korvid/ui/app.py tests/ui/test_resize_flow.py
git commit -m "feat(ui): add impact to Pod resize approvals (#300)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: pre-commit hooks pass.

---

### Task 5: Integrate Agent Resize with Explicit Namespace Scope

**Files:**
- Modify: `src/korvid/ui/app.py:7816-7883`
- Modify: `src/korvid/ui/app.py:8669-8705`
- Modify: `tests/ui/test_resize_flow.py`

**Interfaces:**
- Consumes: `_impact_preview_for_scope` from Task 4, the target manifest already fetched by `agent_request_write`, and `compose_resize_impact_lines`.
- Produces: resize-only agent impact lines passed through `_await_user_approval(..., impact_lines=...)`.

- [ ] **Step 1: Write RED agent namespace and approval tests**

Extend the agent resize tests in `tests/ui/test_resize_flow.py`:

```python
async def test_agent_resize_uses_explicit_namespace_for_impact(tmp_path: Path) -> None:
    calls: list[tuple[str, str | None]] = []
    recorder = ResizeRecorder()
    app = make_app(
        recorder,
        tmp_path / "audit.jsonl",
        relationship_calls=calls,
    )
    resources = {"app": {"requests": {"cpu": "200m"}}}
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.create_task(
            app.agent_request_write(
                "resize",
                "pods",
                "web-1",
                namespace="default",
                resources=resources,
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent resize confirmation opened",
        )
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "Pod-local resize impact (advisory):" in text
        assert any(namespace == "default" for _, namespace in calls)
        assert app.current_scope == "default"
        assert not recorder.calls
        await pilot.press("n")
        assert "denied" in await task
```

Add complete fail-open and non-resize regression tests:

```python
async def test_agent_resize_keeps_local_notes_when_manifest_lookup_fails_open(
    tmp_path: Path,
) -> None:
    async def failing_manifest(kind: str, ns: str | None, name: str) -> dict[str, Any]:
        raise RuntimeError("manifest backend unavailable")

    recorder = ResizeRecorder()
    app = make_app(
        recorder,
        tmp_path / "audit.jsonl",
        get_manifest=failing_manifest,
        relationship_calls=[],
    )
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.create_task(
            app.agent_request_write(
                "resize",
                "pods",
                "web-1",
                namespace="default",
                resources={"app": {"requests": {"cpu": "200m"}}},
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent resize confirmation opened",
        )
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "restart requirements could not be determined" in text
        assert recorder.calls == []
        await pilot.press("n")
        assert "denied" in await task


async def test_non_resize_agent_writes_do_not_gain_impact(tmp_path: Path) -> None:
    recorder = ResizeRecorder()
    app = make_app(
        recorder,
        tmp_path / "audit.jsonl",
        relationship_calls=[],
    )
    async with app.run_test() as pilot:
        _expand_panel(app)
        task = asyncio.create_task(
            app.agent_request_write(
                "delete",
                "deployments",
                "web",
                namespace="default",
            )
        )
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen),
            label="agent delete confirmation opened",
        )
        assert not app.screen.query(".confirm-impact")
        await pilot.press("n")
        assert "denied" in await task
```

Add an agent cancellation test to `tests/ui/test_resize_flow.py` using the
same `BlockingRelationshipLister`: cancel the `agent_request_write` task
while the loader is blocked, release the fake in `finally`, assert
`CancelledError`, and assert the dialog, recorder calls, and audit file are
all absent.

- [ ] **Step 2: Run the RED agent tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/ui/test_resize_flow.py -q
```

Expected: new tests FAIL because `_await_user_approval` has no impact-lines
parameter and agent resize never loads a graph.

- [ ] **Step 3: Add resize-only agent impact preparation**

Add a focused helper in `KorvidApp`:

```python
async def _agent_resize_impact(
    self,
    meta: ResourceMeta,
    ns: str | None,
    name: str,
    uid: str | None,
    snapshot: dict[str, Any] | None,
    resources: dict[str, dict[str, dict[str, str]]],
) -> tuple[str, ...]:
    context = classify_pod_resize(snapshot or {}, resources)
    graph_lines = await self._impact_preview_for_scope(
        ImpactAction.POD_RESIZE,
        meta,
        ns,
        name,
        uid,
        scope=ns if meta.namespaced else None,
    )
    return compose_resize_impact_lines(graph_lines, context)
```

In `agent_request_write`, compute impact only for resize:

```python
impact_lines = (
    await self._agent_resize_impact(
        meta,
        ns,
        name,
        uid,
        snapshot,
        resources,
    )
    if action == "resize" and resources
    else None
)
```

Pass it to `_await_user_approval`.

- [ ] **Step 4: Plumb impact lines through the agent approval helper**

Change `_await_user_approval`:

```python
async def _await_user_approval(
    self,
    title: str,
    operation: str,
    *,
    require_name: str | None = None,
    preview: list[str] | None = None,
    managed_note: str | None = None,
    impact_lines: tuple[str, ...] | None = None,
) -> Literal["approved", "declined", "expired"]:
```

Pass `impact_lines=impact_lines` to `_confirm_screen`. Do not change
surfaceability, expiry, cancellation cleanup, or user-keystroke handling.

- [ ] **Step 5: Run GREEN agent tests and regression tests**

Run:

```bash
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/ui/test_resize_flow.py \
  tests/ui/test_agent_write.py \
  tests/ui/test_agent_interrupt.py \
  tests/ui/test_proposals_ui.py -q
PYTHONPATH=src uv run --no-sync ruff check --fix \
  src/korvid/ui/app.py tests/ui/test_resize_flow.py
PYTHONPATH=src uv run --no-sync ruff format \
  src/korvid/ui/app.py tests/ui/test_resize_flow.py
PYTHONPATH=src uv run --no-sync mypy src/korvid/ui/app.py
```

Expected: all selected tests and checks PASS. Existing non-resize agent
confirmations remain without an impact section.

- [ ] **Step 6: Commit the agent integration**

```bash
git add src/korvid/ui/app.py tests/ui/test_resize_flow.py
git commit -m "feat(agent): show Pod resize impact before approval (#300)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: pre-commit hooks pass.

---

### Task 6: Synchronize Documentation and Run the Full Gate

**Files:**
- Modify: `docs/tui.md:202-239`
- Modify: `docs/resource-relationships.md:500-510`
- Verify: `docs/dev/specs/2026-08-17-pod-resize-impact-preview-design.md`
- Verify: `docs/dev/plans/2026-08-17-pod-resize-impact-preview.md`

**Interfaces:**
- Consumes: final production wording and behavior from Tasks 1-5.
- Produces: user-facing and architecture documentation matching the shipped dialog.

- [ ] **Step 1: Update user-facing TUI documentation**

Change the supported action list in `docs/tui.md` to include Pod resize and add
a production-shaped example:

```text
graph-derived impact (advisory):
  pod resize Pod/prod/web-abc-1
  advisory only: known relationships from one bounded snapshot - not a prediction of failure, no replacement for the server dry-run, and never a block on approval.
  known direct dependents (may be affected): none in this snapshot
  known transitive dependents (may be affected): none in this snapshot
  scope: prod
  graph coverage: complete
Pod-local resize impact (advisory):
  Pod identity and relationship membership stay unchanged; graph relations are not traversed
  changed resources do not require a container restart under resizePolicy
  node feasibility, Deferred/Infeasible status, actuation, and completion are not predicted
```

Explain that restart and memory-limit-decrease lines are conditional, graph
failure never removes safe local notes, TUI scope is captured from the pane,
and agent scope comes from the explicit namespace.

- [ ] **Step 2: Update relationship semantics documentation**

In `docs/resource-relationships.md`, add a `POD_RESIZE` row or paragraph that
states:

```text
Pod resize intentionally traverses no relation. The existing Pod object keeps
its UID/IP, owner, node placement, mounts/config references, PDB membership,
and routing membership. Runtime resize considerations are rendered from the
captured Pod manifest and requested resources, not inferred from graph edges.
```

Remove `resize` from the list of unsupported write types while leaving edit,
cordon/uncordon, drain, Helm, and operator flows unchanged.

- [ ] **Step 3: Run documentation and source consistency searches**

Run:

```bash
rg -n "resize.*no tested|Edit, resize|edit, resize|POD_RESIZE|Pod-local resize" \
  docs src tests
git diff --check
git status --short
```

Expected: no stale claim says resize lacks tested semantics; only intended
source, tests, and docs are modified; `uv.lock` is absent from status.

- [ ] **Step 4: Run targeted lint, type, architecture, and test gates**

Run:

```bash
PYTHONPATH=src uv run --no-sync ruff check --fix src/korvid/core/impact.py \
  src/korvid/core/resize_impact.py src/korvid/ui/impact_preview.py \
  src/korvid/ui/resize_impact_preview.py src/korvid/ui/app.py \
  tests/core/test_impact.py tests/core/test_resize_impact.py \
  tests/ui/test_impact_preview.py tests/ui/test_resize_impact_preview.py \
  tests/ui/test_resize_flow.py tests/ui/test_impact_security.py
PYTHONPATH=src uv run --no-sync ruff format src/korvid/core/impact.py \
  src/korvid/core/resize_impact.py src/korvid/ui/impact_preview.py \
  src/korvid/ui/resize_impact_preview.py src/korvid/ui/app.py \
  tests/core/test_impact.py tests/core/test_resize_impact.py \
  tests/ui/test_impact_preview.py tests/ui/test_resize_impact_preview.py \
  tests/ui/test_resize_flow.py tests/ui/test_impact_security.py
PYTHONPATH=src uv run --no-sync mypy src/
PYTHONPATH=src uv run --no-sync tach check
PYTHONPATH=src uv run --no-sync pytest -p no:tach \
  tests/core/test_impact.py tests/core/test_resize_impact.py \
  tests/ui/test_impact_preview.py tests/ui/test_resize_impact_preview.py \
  tests/ui/test_resize_flow.py tests/ui/test_impact_security.py \
  tests/ui/test_agent_write.py tests/ui/test_agent_interrupt.py \
  tests/ui/test_confirm_screen.py -q
```

Expected: all commands PASS.

- [ ] **Step 5: Run the repository full gate**

Run:

```bash
PYTHONPATH=src make check
```

If `make check` invokes `uv sync` in this environment, stop before it rewrites
`uv.lock` and instead run its existing constituent commands with
`PYTHONPATH=src uv run --no-sync`. Do not edit gate files or the lock to make
the command pass.

Expected: Ruff, format, mypy, tach, deptry, and the full pytest suite pass;
coverage remains at or above 80%; `git status --short uv.lock` is empty.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/tui.md docs/resource-relationships.md
git commit -m "docs: explain Pod resize impact boundaries (#300)" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: pre-commit hooks pass.

- [ ] **Step 7: Request code review before any PR action**

Invoke `superpowers:requesting-code-review` against the complete branch diff.
Address credible correctness, security, architecture, or required-check
findings with RED tests first. Do not create or update a pull request unless
the user explicitly asks, and never merge; the maintainer merges.

- [ ] **Step 8: Update issue tracking after verified implementation**

Add a progress comment to `#300` naming the validation commands and commits,
and update the `#293` delivery-slice checkbox only when the implementation is
merged. Do not close either issue from this branch merely because local tests
pass.
