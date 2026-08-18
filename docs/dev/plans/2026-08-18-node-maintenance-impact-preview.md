# Node Maintenance Impact Previews Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add closed, bounded impact previews to cordon, uncordon, and drain confirmations without changing the authoritative PDB-aware drain execution plan.

**Architecture:** Extend `ImpactAction` with three Node operations. Cordon and uncordon use empty graph policies and a pure constant-only local renderer; drain traverses only `scheduled_on`, composes that advisory with the existing `DrainPlan`, and leaves `DrainController` execution unchanged. Every awaited phase and confirmation callback revalidates pane, scope, context, selection, and UID.

**Tech Stack:** Python 3.11+, Textual, pytest/pytest-asyncio, ruff, mypy strict, tach.

## Global Constraints

- Work only in `/Users/hwang-inhwan/workspace/kube/.worktrees/feat-293-node-maintenance-impact` on branch `feat/293-node-maintenance-impact`.
- Never modify product behavior outside Node cordon, uncordon, and drain impact previews.
- `DrainPlan` remains the sole source of eviction targets, mirror/DaemonSet skips, `emptyDir` warnings, and current PDB blockers.
- Cordon and uncordon never call the relationship loader.
- Drain follows only `RelationKind.SCHEDULED_ON`; every other relation, including `PROTECTED_BY`, is excluded.
- Graph lines are advisory and can never add/remove an eviction target, approve, reserve, or execute a write.
- All local advisory text is machine-defined, contains no cluster-derived text, and is bounded for the confirmation modal.
- Permission, server dry-run, typed-name approval, UID preconditions, cancellation, and fail-closed audit behavior remain intact.
- Use TDD for every behavior change: RED, minimal GREEN, then refactor.
- Use `uv run --no-sync` for targeted commands. If the worktree has no local environment, use the existing dependency environment with `PYTHONPATH="$PWD/src:$PWD"` and `UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv`.
- Never run `uv lock`; `uv.lock` must remain byte-identical.

---

## File Structure

- Modify `src/korvid/core/impact.py`
  - Own the three closed action policies and exhaustive unresolved-reference mappings.
- Create `src/korvid/ui/node_impact_preview.py`
  - Render and compose constant-only Node maintenance advisory lines.
- Modify `src/korvid/ui/impact_preview.py`
  - Add labels for the new actions so the generic graph renderer stays exhaustive.
- Modify `src/korvid/ui/app.py`
  - Wire local notes into cordon/uncordon and graph + plan notes into drain.
  - Add exact identity revalidation after each await and at approval.
- Modify `tests/core/test_impact.py`
  - Pin every included/excluded relation and unresolved-reference policy.
- Create `tests/ui/test_node_impact_preview.py`
  - Pin local wording, composition order, and line bounds.
- Modify `tests/ui/test_node_ops.py`
  - Cover no-load cordon/uncordon, drain graph composition, failure behavior, and races.
- Modify `docs/tui.md`
  - Document confirmation sections and operational limits.
- Modify `docs/resource-relationships.md`
  - Extend the closed action/relation matrix.

---

### Task 1: Define closed Node action semantics

**Files:**
- Modify: `src/korvid/core/impact.py:64-179`
- Modify: `tests/core/test_impact.py:190-272`

**Interfaces:**
- Produces: `ImpactAction.CORDON_NODE`, `ImpactAction.UNCORDON_NODE`, and `ImpactAction.DRAIN_NODE`.
- Produces: exhaustive entries in `ACTION_RELATIONS` and `ACTION_UNRESOLVED_RELATIONS`.
- Consumes: existing `RelationKind`, `summarize_impact`, `_graph`, `_edge`, and `_res` test helpers.

- [ ] **Step 1: Add failing exhaustive action-policy tests**

Add these assertions to `test_only_supported_writes_carry_action_semantics`:

```python
assert [action.value for action in ImpactAction] == [
    "delete",
    "rollout_restart",
    "scale_down",
    "pod_resize",
    "cordon_node",
    "uncordon_node",
    "drain_node",
]
assert ACTION_RELATIONS[ImpactAction.CORDON_NODE] == frozenset()
assert ACTION_RELATIONS[ImpactAction.UNCORDON_NODE] == frozenset()
assert ACTION_RELATIONS[ImpactAction.DRAIN_NODE] == frozenset(
    {RelationKind.SCHEDULED_ON}
)
```

Add the empty-policy tests:

```python
@pytest.mark.parametrize(
    "action",
    [ImpactAction.CORDON_NODE, ImpactAction.UNCORDON_NODE],
)
@pytest.mark.parametrize("relation", list(RelationKind))
def test_node_scheduling_toggle_ignores_every_relationship(
    action: ImpactAction, relation: RelationKind
) -> None:
    node = _res("Node", "worker-1", namespace="", uid="node-1")
    related = _res("Pod", "web-1", uid="pod-1")
    summary = summarize_impact(
        _graph(_edge(related, node, relation, field="spec.example")),
        action,
        node,
    )
    assert summary.direct == ()
    assert summary.transitive == ()
    assert summary.unresolved == ()
```

Add drain include/exclude tests:

```python
def test_drain_follows_scheduled_pods() -> None:
    node = _res("Node", "worker-1", namespace="", uid="node-1")
    pod = _res("Pod", "web-1", uid="pod-1")
    summary = summarize_impact(
        _graph(_edge(pod, node, RelationKind.SCHEDULED_ON, field="spec.nodeName")),
        ImpactAction.DRAIN_NODE,
        node,
    )
    assert [item.resource for item in summary.direct] == [pod]
    assert summary.transitive == ()


@pytest.mark.parametrize(
    "relation",
    [relation for relation in RelationKind if relation is not RelationKind.SCHEDULED_ON],
)
def test_drain_ignores_every_relation_outside_scheduled_on(
    relation: RelationKind,
) -> None:
    node = _res("Node", "worker-1", namespace="", uid="node-1")
    related = _res("Pod", "web-1", uid="pod-1")
    summary = summarize_impact(
        _graph(_edge(related, node, relation, field="spec.example")),
        ImpactAction.DRAIN_NODE,
        node,
    )
    assert summary.direct == ()
    assert summary.transitive == ()
```

Extend `test_every_action_chooses_its_unresolved_reference_policy`:

```python
for action in (
    ImpactAction.CORDON_NODE,
    ImpactAction.UNCORDON_NODE,
    ImpactAction.DRAIN_NODE,
):
    assert ACTION_UNRESOLVED_RELATIONS[action] is ACTION_RELATIONS[action]
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --no-sync pytest -p no:tach -q \
  tests/core/test_impact.py::test_only_supported_writes_carry_action_semantics \
  tests/core/test_impact.py::test_node_scheduling_toggle_ignores_every_relationship \
  tests/core/test_impact.py::test_drain_follows_scheduled_pods \
  tests/core/test_impact.py::test_drain_ignores_every_relation_outside_scheduled_on \
  tests/core/test_impact.py::test_every_action_chooses_its_unresolved_reference_policy
```

Expected: collection or assertion failures because the three enum members and mappings do not exist.

- [ ] **Step 3: Implement the minimal closed policies**

In `src/korvid/core/impact.py`, add:

```python
class ImpactAction(StrEnum):
    DELETE = "delete"
    ROLLOUT_RESTART = "rollout_restart"
    SCALE_DOWN = "scale_down"
    POD_RESIZE = "pod_resize"
    CORDON_NODE = "cordon_node"
    UNCORDON_NODE = "uncordon_node"
    DRAIN_NODE = "drain_node"


_NODE_SCHEDULING_TOGGLE_RELATIONS: frozenset[RelationKind] = frozenset()
_DRAIN_NODE_RELATIONS: frozenset[RelationKind] = frozenset(
    {RelationKind.SCHEDULED_ON}
)
```

Extend the mappings:

```python
ACTION_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind]] = {
    ImpactAction.DELETE: _DELETE_RELATIONS,
    ImpactAction.ROLLOUT_RESTART: _ROLLOUT_RESTART_RELATIONS,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
    ImpactAction.POD_RESIZE: _POD_RESIZE_RELATIONS,
    ImpactAction.CORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.UNCORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.DRAIN_NODE: _DRAIN_NODE_RELATIONS,
}

ACTION_UNRESOLVED_RELATIONS: Mapping[
    ImpactAction, frozenset[RelationKind] | None
] = {
    ImpactAction.DELETE: None,
    ImpactAction.ROLLOUT_RESTART: None,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
    ImpactAction.POD_RESIZE: _POD_RESIZE_RELATIONS,
    ImpactAction.CORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.UNCORDON_NODE: _NODE_SCHEDULING_TOGGLE_RELATIONS,
    ImpactAction.DRAIN_NODE: _DRAIN_NODE_RELATIONS,
}
```

Document beside `_DRAIN_NODE_RELATIONS` that `PROTECTED_BY` is deliberately absent because the current dependent walk cannot reach a PDB from a Node through that edge direction and `DrainPlan` owns blocker state.

- [ ] **Step 4: Run the complete core impact module**

Run:

```bash
uv run --no-sync pytest -p no:tach -q tests/core/test_impact.py
uv run --no-sync mypy src/korvid/core/impact.py tests/core/test_impact.py
uv run --no-sync ruff check src/korvid/core/impact.py tests/core/test_impact.py
uv run --no-sync ruff format --check src/korvid/core/impact.py tests/core/test_impact.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/impact.py tests/core/test_impact.py
git commit -m "feat: define node maintenance impact semantics"
```

---

### Task 2: Add bounded Node maintenance rendering

**Files:**
- Create: `src/korvid/ui/node_impact_preview.py`
- Create: `tests/ui/test_node_impact_preview.py`
- Modify: `src/korvid/ui/impact_preview.py:84-89`
- Modify: `tests/ui/test_impact_preview.py`

**Interfaces:**
- Consumes: `ImpactAction`.
- Produces: `render_node_maintenance_lines(action: ImpactAction) -> tuple[str, ...]`.
- Produces: `compose_node_maintenance_lines(graph_lines: tuple[str, ...] | None, action: ImpactAction) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing renderer tests**

Create `tests/ui/test_node_impact_preview.py`:

```python
import pytest

from korvid.core.impact import ImpactAction
from korvid.ui.node_impact_preview import (
    compose_node_maintenance_lines,
    render_node_maintenance_lines,
)


@pytest.mark.parametrize(
    ("action", "required"),
    [
        (
            ImpactAction.CORDON_NODE,
            (
                "current Pods are not evicted or moved",
                "new scheduling to the Node is blocked",
                "future placement and workload availability are not predicted",
            ),
        ),
        (
            ImpactAction.UNCORDON_NODE,
            (
                "current Pods are not moved",
                "future scheduling to the Node is permitted",
                "scheduler choice and capacity are not predicted",
            ),
        ),
        (
            ImpactAction.DRAIN_NODE,
            (
                "the drain impact plan defines exact eviction targets and skip reasons",
                "the Node remains cordoned if drain execution fails or is cancelled",
                "replacement placement, readiness, and application availability are not predicted",
            ),
        ),
    ],
)
def test_node_maintenance_lines_are_action_specific(
    action: ImpactAction, required: tuple[str, ...]
) -> None:
    lines = render_node_maintenance_lines(action)
    assert lines[0] == "Node maintenance impact (advisory):"
    assert all(f"  {text}" in lines for text in required)
    assert all(len(line) <= 240 for line in lines)


def test_graph_lines_precede_local_node_maintenance_lines() -> None:
    lines = compose_node_maintenance_lines(
        ("graph-derived impact (advisory):",),
        ImpactAction.DRAIN_NODE,
    )
    assert lines[0] == "graph-derived impact (advisory):"
    assert lines[1] == "Node maintenance impact (advisory):"


def test_non_node_action_is_rejected() -> None:
    with pytest.raises(ValueError, match="node maintenance"):
        render_node_maintenance_lines(ImpactAction.DELETE)
```

Also extend the action-label coverage in `tests/ui/test_impact_preview.py` so all new enum members are renderable by the generic graph renderer.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_node_impact_preview.py \
  tests/ui/test_impact_preview.py
```

Expected: import failure for `korvid.ui.node_impact_preview` and action-label failures.

- [ ] **Step 3: Implement the pure renderer**

Create `src/korvid/ui/node_impact_preview.py`:

```python
from __future__ import annotations

from collections.abc import Mapping

from korvid.core.impact import ImpactAction

_TITLE = "Node maintenance impact (advisory):"
_MAX_LINE = 240

_ACTION_LINES: Mapping[ImpactAction, tuple[str, ...]] = {
    ImpactAction.CORDON_NODE: (
        "  current Pods are not evicted or moved",
        "  new scheduling to the Node is blocked",
        "  future placement and workload availability are not predicted",
    ),
    ImpactAction.UNCORDON_NODE: (
        "  current Pods are not moved",
        "  future scheduling to the Node is permitted",
        "  scheduler choice and capacity are not predicted",
    ),
    ImpactAction.DRAIN_NODE: (
        "  the drain impact plan defines exact eviction targets and skip reasons",
        "  the Node remains cordoned if drain execution fails or is cancelled",
        "  replacement placement, readiness, and application availability are not predicted",
    ),
}


def render_node_maintenance_lines(action: ImpactAction) -> tuple[str, ...]:
    try:
        lines = (_TITLE, *_ACTION_LINES[action])
    except KeyError as exc:
        raise ValueError(f"{action.value} is not a node maintenance action") from exc
    if not all(len(line) <= _MAX_LINE for line in lines):
        raise AssertionError("rendered node maintenance line exceeded 240 characters")
    return lines


def compose_node_maintenance_lines(
    graph_lines: tuple[str, ...] | None,
    action: ImpactAction,
) -> tuple[str, ...]:
    local_lines = render_node_maintenance_lines(action)
    return (*graph_lines, *local_lines) if graph_lines is not None else local_lines
```

Extend `_ACTION_LABEL` in `src/korvid/ui/impact_preview.py`:

```python
_ACTION_LABEL = {
    ImpactAction.DELETE: "delete",
    ImpactAction.ROLLOUT_RESTART: "rollout restart",
    ImpactAction.SCALE_DOWN: "scale down",
    ImpactAction.POD_RESIZE: "pod resize",
    ImpactAction.CORDON_NODE: "cordon",
    ImpactAction.UNCORDON_NODE: "uncordon",
    ImpactAction.DRAIN_NODE: "drain",
}
```

- [ ] **Step 4: Verify renderer tests and boundaries**

Run:

```bash
uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_node_impact_preview.py \
  tests/ui/test_impact_preview.py
uv run --no-sync mypy \
  src/korvid/ui/node_impact_preview.py \
  src/korvid/ui/impact_preview.py
uv run --no-sync ruff check \
  src/korvid/ui/node_impact_preview.py \
  tests/ui/test_node_impact_preview.py
uv run --no-sync ruff format --check \
  src/korvid/ui/node_impact_preview.py \
  tests/ui/test_node_impact_preview.py
```

Expected: all commands pass.

- [ ] **Step 5: Commit**

```bash
git add \
  src/korvid/ui/node_impact_preview.py \
  src/korvid/ui/impact_preview.py \
  tests/ui/test_node_impact_preview.py \
  tests/ui/test_impact_preview.py
git commit -m "feat: render node maintenance impact notes"
```

---

### Task 3: Integrate cordon and uncordon without graph reads

**Files:**
- Modify: `src/korvid/ui/app.py:6349-6387`
- Modify: `tests/ui/test_node_ops.py:94-220`

**Interfaces:**
- Consumes: `render_node_maintenance_lines`.
- Consumes: `_write_origin()` and `_write_identity_intact(...)`.
- Produces: cordon/uncordon confirmations with `impact_lines` and an approval guard.
- Preserves: `WriteOps.preview_cordon` and `WriteOps.cordon_node` signatures.

- [ ] **Step 1: Add failing no-load and wording tests**

Extend `make_app` in `tests/ui/test_node_ops.py` with:

```python
relationship_calls: list[tuple[str, str | None]] | None = None,
```

and pass a recording callable to `KorvidApp`:

```python
async def list_relationship_objects(
    meta: ResourceMeta, namespace: str | None
) -> list[GenericSummary]:
    assert relationship_calls is not None
    relationship_calls.append((meta.plural, namespace))
    return []
```

Use it only when `relationship_calls is not None`.

Add:

```python
@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("c", "new scheduling to the Node is blocked"),
        ("u", "future scheduling to the Node is permitted"),
    ],
)
async def test_cordon_toggle_shows_local_impact_without_graph_load(
    tmp_path: Path, key: str, expected: str
) -> None:
    calls: list[tuple[str, str | None]] = []
    app = make_app(
        NodeRecorder(),
        tmp_path / "audit.jsonl",
        relationship_calls=calls,
    )
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press(key)
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen)
            and bool(app.screen.query(".confirm-impact")),
            label="node scheduling impact rendered",
        )
        text = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "Node maintenance impact (advisory):" in text
        assert expected in text
        assert "graph-derived impact" not in text
        assert calls == []
        await pilot.press("n")
```

Add a replacement-UID test that mutates the Node summary while a blocking
`preview_cordon` fake is awaited and asserts no confirmation, write, or audit.
Add a confirmation-time replacement test that opens the dialog, replaces the
Node UID, presses `y`, and asserts the same refusal.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_node_ops.py::test_cordon_toggle_shows_local_impact_without_graph_load
```

Expected: no `.confirm-impact` widget because `_cordon_action` does not pass impact lines.

Run the UID tests and confirm they fail because `_cordon_action` currently uses
context-only revalidation and no approval guard.

- [ ] **Step 3: Implement identity-safe cordon/uncordon integration**

Import:

```python
from korvid.ui.node_impact_preview import render_node_maintenance_lines
```

Change `_cordon_action` to capture origin and use exact identity:

```python
epoch = self._ctx_epoch
origin = self._write_origin()
if not await self._precheck_keybinding_write(action, meta, None, name):
    return
if not self._write_identity_intact(
    action,
    meta,
    None,
    name,
    uid,
    phase="the permission check",
    epoch=epoch,
    origin=origin,
):
    return
preview = await self._dry_run_preview(
    ops.preview_cordon(name, unschedulable, uid=uid)
)
if not self._write_identity_intact(
    action,
    meta,
    None,
    name,
    uid,
    phase="the dry-run preview",
    epoch=epoch,
    origin=origin,
):
    return
impact_action = (
    ImpactAction.CORDON_NODE
    if unschedulable
    else ImpactAction.UNCORDON_NODE
)
```

Pass these arguments to `_push_write_confirmation`:

```python
impact_lines=render_node_maintenance_lines(impact_action),
approval_guard=lambda: self._write_identity_intact(
    action,
    meta,
    None,
    name,
    uid,
    phase="the confirmation dialog",
    epoch=epoch,
    origin=origin,
),
```

Do not call `_impact_preview` or `_impact_preview_for_scope`.

- [ ] **Step 4: Verify the complete Node operation test module**

Run:

```bash
uv run --no-sync pytest -p no:tach -q tests/ui/test_node_ops.py
uv run --no-sync mypy src/korvid/ui/app.py tests/ui/test_node_ops.py
uv run --no-sync ruff check src/korvid/ui/app.py tests/ui/test_node_ops.py
uv run --no-sync ruff format --check src/korvid/ui/app.py tests/ui/test_node_ops.py
```

Expected: all tests pass and relationship calls remain empty for cordon/uncordon.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/app.py tests/ui/test_node_ops.py
git commit -m "feat: add cordon and uncordon impact notes"
```

---

### Task 4: Compose drain graph context with DrainPlan

**Files:**
- Modify: `src/korvid/ui/app.py:6388-6490`
- Modify: `tests/ui/test_node_ops.py`

**Interfaces:**
- Consumes: `ImpactAction.DRAIN_NODE`.
- Consumes: `compose_node_maintenance_lines`.
- Consumes: existing `_impact_preview(...) -> tuple[str, ...] | None`.
- Preserves: `DrainPlan.preview_lines()` as confirmation `preview`.
- Produces: separate graph and local advisory lines as confirmation `impact_lines`.

- [ ] **Step 1: Add failing drain composition tests**

Extend the relationship fake so:

- listing `nodes` returns `worker-1` with UID `node-uid-1`;
- listing `pods` returns `web-1` scheduled on `worker-1`;
- calls record `(plural, namespace)`.

Add:

```python
async def test_drain_shows_plan_graph_and_local_sections(tmp_path: Path) -> None:
    plan = DrainPlan(
        targets=(_target("web-1"),),
        skipped_daemonset=(_target("agent"),),
        skipped_mirror=(),
    )
    calls: list[tuple[str, str | None]] = []
    app = make_app(
        NodeRecorder(plan),
        tmp_path / "audit.jsonl",
        relationship_calls=calls,
    )
    async with app.run_test() as pilot:
        await _to_nodes(pilot)
        await pilot.press("D")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmScreen)
            and bool(app.screen.query(".confirm-impact")),
            label="drain impact rendered",
        )
        preview = str(app.screen.query_one(".confirm-preview", Static).render())
        impact = str(app.screen.query_one(".confirm-impact", Static).render())
        assert "web-1" in preview
        assert "agent" in preview
        assert "graph-derived impact (advisory):" in impact
        assert "Pod/default/web-1" in impact
        assert "Node maintenance impact (advisory):" in impact
        assert "the drain impact plan defines exact eviction targets" in impact
        assert {namespace for _, namespace in calls} == {None}
```

Add a graph-failure test with a relationship callable that raises
`RuntimeError("secret response body")`. Assert:

- `drain impact plan:` content remains;
- `impact unavailable; approval remains available` appears;
- Node-maintenance notes remain;
- the exception text does not appear;
- declining creates no write or audit entry.

Add a plan-failure test with a `drain_plan` fake that raises and a recording
relationship callable. Assert no graph call, confirmation, cordon, eviction, or
audit record.

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_node_ops.py::test_drain_shows_plan_graph_and_local_sections
```

Expected: no graph/local impact widget.

Run the failure tests. Expected: graph failure test fails because no graph is
loaded; plan failure test should preserve the existing fail-closed behavior.

- [ ] **Step 3: Implement drain graph composition**

Import:

```python
from korvid.ui.node_impact_preview import (
    compose_node_maintenance_lines,
    render_node_maintenance_lines,
)
```

Capture origin before the first await:

```python
epoch = self._ctx_epoch
origin = self._write_origin()
```

After `plan = await ops.drain_plan(name)`, replace context-only validation with:

```python
if not self._write_identity_intact(
    "drain",
    meta,
    None,
    name,
    uid,
    phase="the drain plan",
    epoch=epoch,
    origin=origin,
):
    return
```

Load graph impact and revalidate:

```python
graph_lines = await self._impact_preview(
    ImpactAction.DRAIN_NODE,
    meta,
    None,
    name,
    uid,
    origin=origin,
)
impact_lines = compose_node_maintenance_lines(
    graph_lines,
    ImpactAction.DRAIN_NODE,
)
if not self._write_identity_intact(
    "drain",
    meta,
    None,
    name,
    uid,
    phase="the impact preview",
    epoch=epoch,
    origin=origin,
):
    return
```

Keep `preview=plan.preview_lines()` and add:

```python
impact_lines=impact_lines,
```

Do not modify `DrainPlan`, `DrainController`, or `_run_drain`.

- [ ] **Step 4: Verify drain integration and regressions**

Run:

```bash
uv run --no-sync pytest -p no:tach -q tests/ui/test_node_ops.py
uv run --no-sync pytest -p no:tach -q \
  tests/core/test_impact.py \
  tests/ui/test_impact_preview.py \
  tests/ui/test_node_impact_preview.py
```

Expected: all tests pass. Existing eviction, PDB, skip, cancellation, and audit tests remain unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/app.py tests/ui/test_node_ops.py
git commit -m "feat: add graph context to drain approvals"
```

---

### Task 5: Harden drain identity and cancellation races

**Files:**
- Modify: `src/korvid/ui/app.py:6388-6490`
- Modify: `tests/ui/test_node_ops.py`

**Interfaces:**
- Consumes: `_WriteOrigin`, `_write_identity_intact`, and the Task 4 impact loader.
- Produces: no-confirm/no-write guarantees for drift during plan, graph, and confirmation phases.

- [ ] **Step 1: Add blocking test fakes**

Add a relationship lister with:

```python
class BlockingNodeRelationshipLister:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(
        self, meta: ResourceMeta, namespace: str | None
    ) -> list[Summary]:
        self.calls.append((meta.plural, namespace))
        self.entered.set()
        await self.release.wait()
        if meta.plural == "nodes":
            return [
                GenericSummary(
                    name="worker-1",
                    namespace="",
                    kind="Node",
                    created="",
                    uid="node-uid-1",
                )
            ]
        if meta.plural == "pods":
            return [
                PodSummary(
                    name="web-1",
                    namespace="default",
                    phase="Running",
                    ready="1/1",
                    restarts=0,
                    node="worker-1",
                    uid="pod-uid-1",
                )
            ]
        return []
```

Add a blocking `NodeRecorder.drain_plan` variant with entered/release events.

- [ ] **Step 2: Add failing drift/cancellation tests**

Add separate tests for:

- context switch during `drain_plan`;
- focus moved to a second pane during graph load;
- originating pane scope changed during graph load;
- selected Node UID replaced during graph load;
- Node UID replaced while confirmation is open;
- graph worker cancellation.

Every test must assert:

```python
assert not isinstance(app.screen, ConfirmScreen)
assert app._active_cluster_writes == 0
assert not any(call[0] == "cordon" for call in recorder.calls)
assert not any(call[0] == "evict" for call in recorder.calls)
assert not audit_path.exists()
```

For confirmation-time UID drift, type the required Node name, submit approval,
and wait for the exact cancellation notification before asserting.

- [ ] **Step 3: Run tests and verify RED**

Run each new test directly:

```bash
uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_node_ops.py::test_drain_context_switch_during_plan_refuses_confirmation \
  tests/ui/test_node_ops.py::test_drain_focus_change_during_graph_load_refuses_confirmation \
  tests/ui/test_node_ops.py::test_drain_scope_change_during_graph_load_refuses_confirmation \
  tests/ui/test_node_ops.py::test_drain_uid_change_during_graph_load_refuses_confirmation \
  tests/ui/test_node_ops.py::test_drain_uid_change_in_confirmation_dispatches_nothing \
  tests/ui/test_node_ops.py::test_cancelled_drain_graph_load_dispatches_nothing
```

Expected: tests that exercise confirmation-time UID drift fail until the
callback has an identity guard; any already-correct earlier-phase tests may
pass immediately and should remain as acceptance coverage.

- [ ] **Step 4: Add the confirmation callback guard**

Change `_done` in `action_drain_node`:

```python
def _done(confirmed: bool | None) -> None:
    if not confirmed:
        return
    if not self._write_identity_intact(
        "drain",
        meta,
        None,
        name,
        uid,
        phase="the confirmation dialog",
        epoch=epoch,
        origin=origin,
    ):
        return
    self._drain_node = name
    self._drain_worker = self.run_worker(
        self._run_drain(ops, meta, name, uid, plan)
    )
```

Do not reserve a write before this guard succeeds.

- [ ] **Step 5: Verify security and cancellation coverage**

Run:

```bash
uv run --no-sync pytest -p no:tach -q tests/ui/test_node_ops.py
uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_ctx_switch.py \
  tests/ui/test_impact_security.py
uv run --no-sync mypy src/korvid/ui/app.py tests/ui/test_node_ops.py
uv run --no-sync ruff check src/korvid/ui/app.py tests/ui/test_node_ops.py
uv run --no-sync ruff format --check src/korvid/ui/app.py tests/ui/test_node_ops.py
```

Expected: all commands pass; no test relies on wall-clock sleeps.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/ui/app.py tests/ui/test_node_ops.py
git commit -m "test: harden drain impact race handling"
```

---

### Task 6: Document and verify the complete slice

**Files:**
- Modify: `docs/tui.md`
- Modify: `docs/resource-relationships.md`
- Verify: all files changed by Tasks 1-5

**Interfaces:**
- Consumes: final user-visible strings and closed action mapping.
- Produces: user and developer documentation matching production behavior.

- [ ] **Step 1: Update the TUI guide**

Document:

- cordon and uncordon show only `Node maintenance impact (advisory):`;
- neither operation loads the relationship graph;
- drain keeps `drain impact plan:` as authoritative and adds graph/local advisory sections;
- the drain graph can list mirror/DaemonSet Pods while the plan skips them;
- graph failure never removes the plan;
- plan failure never opens a graph-only approval;
- cancellation leaves the Node cordoned after execution begins.

Use the exact production headings and limitation lines from
`src/korvid/ui/node_impact_preview.py`.

- [ ] **Step 2: Update the relationship action matrix**

In `docs/resource-relationships.md`, add:

| Action | Followed relations |
|---|---|
| cordon Node | none |
| uncordon Node | none |
| drain Node | `scheduled_on` |

State explicitly that `protected_by` remains excluded because PDB blocker state
comes from `DrainPlan`, not the graph dependent walk.

- [ ] **Step 3: Run targeted formatting and architecture checks**

Run:

```bash
uv run --no-sync ruff check src/ tests/
uv run --no-sync ruff format --check src/ tests/
uv run --no-sync mypy
uv run --no-sync tach check
```

Expected: all commands pass.

- [ ] **Step 4: Run all directly affected tests together**

Run:

```bash
uv run --no-sync pytest -p no:tach -q \
  tests/core/test_impact.py \
  tests/ui/test_impact_preview.py \
  tests/ui/test_node_impact_preview.py \
  tests/ui/test_node_ops.py \
  tests/ui/test_ctx_switch.py \
  tests/ui/test_impact_security.py
```

Expected: all tests pass.

- [ ] **Step 5: Run the full repository gate with lock protection**

Run:

```bash
before=$(git hash-object uv.lock)
UV_NO_SYNC=1 make check
rc=$?
after=$(git hash-object uv.lock)
printf 'uv.lock-before=%s\nuv.lock-after=%s\nmake-check-exit=%s\n' \
  "$before" "$after" "$rc"
test "$before" = "$after"
exit "$rc"
```

Expected:

- ruff passes;
- mypy passes;
- pytest has zero failures;
- tach passes;
- before/after `uv.lock` hashes are identical.

If the known unsupported Python 3.14 nested-JSON test is nondeterministic, run
the same full proxy used for PR #301 with only
`tests/obs/test_fail_closed.py::TestRoundTenFindings::test_a_deeply_nested_body_is_a_backend_error`
deselected, and rely on required Python 3.11/3.12/3.13 CI for the authoritative
full-suite result.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/tui.md docs/resource-relationships.md
git commit -m "docs: explain node maintenance impact previews"
```

- [ ] **Step 7: Request whole-branch review**

Review `origin/main...HEAD` for:

- graph claims outside the closed sets;
- DrainPlan/graph authority inversion;
- missing pane/scope/context/UID revalidation;
- approval or audit bypass;
- graph error text leaking into the dialog;
- unnecessary graph loads for cordon/uncordon.

Address credible findings with RED/GREEN tests before creating a pull request.
