# Delete/Restart Selector Impact Paths Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct issue #297 by making delete and rollout-restart tests and documentation exercise the workload selector and owner-reference paths that production already builds.

**Architecture:** Keep `korvid.core.impact` and the relationship graph unchanged. First pin the realistic Deployment/ReplicaSet/Pod topology at the core and UI boundaries, then make the UI fixture helpers emit their production `spec.selector` facts unconditionally and synchronize the user and engineering documentation.

**Tech Stack:** Python 3.11+, pytest, pytest-asyncio, Textual test pilots, Ruff, mypy, tach, deptry, GitHub CLI.

## Global Constraints

- Preserve distinct `managed_by` selector evidence and `owned_by` owner-reference evidence.
- Preserve deterministic breadth-first traversal: the first shortest path lists a resource; later paths become revisits.
- Do not modify application source unless the RED evidence contradicts the root-cause reproduction in the approved design.
- Do not change action relation sets, approval, RBAC, UID, dry-run, audit, timeout, cancellation, or rendering bounds.
- Do not add dependencies or modify `uv.lock`.
- Use the repository's existing Python 3.12 environment from this worktree as `../../.venv/bin/<tool>` and set `PYTHONPATH="$PWD/src"` for Python/test commands; the corporate mirror cannot currently resolve locked `hatchling==1.32.0` into a new worktree-local environment.
- Every pytest invocation for a single file uses `-p no:tach`.
- Every commit includes `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.

## File Map

- `tests/core/test_impact.py` — characterize realistic selector/owner path ordering and revisit classification for delete and rollout restart.
- `tests/ui/test_impact_flow.py` — make workload fixtures production-shaped and pin the rendered end-to-end result.
- `tests/ui/test_impact_security.py` — update the shared-fixture direct-dependent count without weakening the agent-disabled security assertion.
- `docs/tui.md` — update the user-facing example and reading guidance.
- `docs/resource-relationships.md` — state that the selector/owner path rule applies to delete and restart as well as scale-down.
- `docs/dev/plans/2026-08-15-graph-derived-blast-radius.md` — synchronize the authoritative #283 fixture and assertion snippets.

---

### Task 1: Pin and Correct the Production-Shaped Topology

**Files:**
- Modify: `tests/core/test_impact.py:369`
- Modify: `tests/ui/test_impact_flow.py:90-162`
- Modify: `tests/ui/test_impact_flow.py:645-684`
- Modify: `tests/ui/test_impact_flow.py:910-917`
- Modify: `tests/ui/test_impact_security.py:376-386`

**Interfaces:**
- Consumes: `summarize_impact(graph, action, target, *, scope=None, limits=ImpactLimits()) -> ImpactSummary`.
- Consumes: `_workload_selector(*, app: str = "web") -> SelectorFact`.
- Produces: `_deployment(name: str, uid: str, *, desired: int | None = 3) -> GenericSummary`, always with one workload selector.
- Produces: `_replicaset(*, desired: int | None = 3) -> GenericSummary`, always with one workload selector.
- Preserves: `ImpactEnv`, `_scale_down_rows`, and every imported fake used by `tests/ui/test_impact_security.py`.

- [ ] **Step 1: Add the realistic core topology characterization**

Add this test after
`test_direct_and_transitive_dependents_stay_separate_with_their_paths`:

```python
@pytest.mark.parametrize("action", [ImpactAction.DELETE, ImpactAction.ROLLOUT_RESTART])
def test_workload_selector_and_owner_paths_remain_distinct(
    action: ImpactAction,
) -> None:
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    deployment_selector = _edge(
        pod,
        deployment,
        RelationKind.MANAGED_BY,
        field="spec.selector",
        evidence_resource=deployment,
    )
    replicaset_selector = _edge(
        pod,
        replicaset,
        RelationKind.MANAGED_BY,
        field="spec.selector",
        evidence_resource=replicaset,
    )
    pod_owner = _edge(pod, replicaset, RelationKind.OWNED_BY)
    replicaset_owner = _edge(replicaset, deployment, RelationKind.OWNED_BY)
    graph = _graph(
        deployment_selector,
        replicaset_selector,
        pod_owner,
        replicaset_owner,
    )

    summary = summarize_impact(graph, action, deployment)

    assert [item.resource for item in summary.direct] == [pod, replicaset]
    assert summary.transitive == ()
    assert summary.cycles == ()
    assert summary.revisits == (replicaset_selector, pod_owner)
```

The edge order matches `build_relationship_graph`'s production ordering:
Pod-subject edges precede the ReplicaSet-subject edge, and `managed_by`
precedes `owned_by` for the same Pod and ReplicaSet.

- [ ] **Step 2: Change the UI expectations before changing the fixtures**

Replace the delete flow test with:

```python
async def test_delete_dialog_shows_realistic_workload_selector_paths(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "delete apps/Deployment/prod/web" in text
        assert "known direct dependents (may be affected): 2 or more" in text
        assert (
            "Pod/prod/web-abc-1 via managed_by (declared) at"
            " apps/Deployment/prod/web: spec.selector" in text
        )
        assert (
            "apps/ReplicaSet/prod/web-abc via owned_by (declared) at"
            " apps/ReplicaSet/prod/web-abc: metadata.ownerReferences[0]" in text
        )
        assert "known transitive dependents (may be affected): none in this snapshot" in text
        assert "additional known paths: 2 or more" in text
        assert "scope: prod" in text
        assert env.ops.calls == []
```

Replace the rollout-restart test with:

```python
async def test_rollout_restart_dialog_shows_realistic_workload_selector_paths(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="restart dialog"
        )
        text = impact_text(env.app)
        assert "rollout restart apps/Deployment/prod/web" in text
        assert "known direct dependents (may be affected): 2 or more" in text
        assert (
            "Pod/prod/web-abc-1 via managed_by (declared) at"
            " apps/Deployment/prod/web: spec.selector" in text
        )
        assert "apps/ReplicaSet/prod/web-abc via owned_by (declared)" in text
        assert "known transitive dependents (may be affected): none in this snapshot" in text
        assert "additional known paths: 2 or more" in text
        assert "ConfigMap/prod/app-config" not in text
        assert env.ops.calls == []
```

Also update `tests/ui/test_impact_security.py::test_impact_preview_works_with_the_agent_disabled`
to assert the new 2-direct count — the shared `_deployment` fixture now always carries the
workload selector, so this test too sees 2 direct dependents:

```python
        assert "known direct dependents (may be affected): 2 or more" in impact_text(env.app)
```

Replace the previous assertion `"known direct dependents (may be affected): 1"`.

- [ ] **Step 3: Run the RED evidence**

Run:

```bash
PYTHONPATH="$PWD/src" ../../.venv/bin/python -m pytest -p no:tach \
  tests/core/test_impact.py::test_workload_selector_and_owner_paths_remain_distinct \
  tests/ui/test_impact_flow.py::test_delete_dialog_shows_realistic_workload_selector_paths \
  tests/ui/test_impact_flow.py::test_rollout_restart_dialog_shows_realistic_workload_selector_paths \
  -q
```

Expected:

- the new core characterization passes, proving production traversal already
  preserves the topology;
- both UI tests fail because the fixture still reports one direct ReplicaSet,
  one transitive Pod, and one revisit.

- [ ] **Step 4: Make workload selectors unconditional in the UI fixtures**

Replace `_deployment` with:

```python
def _deployment(
    name: str, uid: str, *, desired: int | None = 3
) -> GenericSummary:
    """A production-shaped Deployment row with its workload selector."""
    return GenericSummary(
        name=name,
        namespace="prod",
        kind="Deployment",
        created="",
        desired=desired,
        uid=uid,
        relationships=RelationshipFacts(
            api_group="apps",
            selectors=(_workload_selector(app=name),),
        ),
    )
```

Replace `_replicaset` with:

```python
def _replicaset(*, desired: int | None = 3) -> GenericSummary:
    """A production-shaped ReplicaSet owned by `web` with its selector."""
    return GenericSummary(
        name="web-abc",
        namespace="prod",
        kind="ReplicaSet",
        created="",
        desired=desired,
        uid="rs-1",
        relationships=RelationshipFacts(
            api_group="apps",
            references=(_owner("Deployment", "web", "deploy-1", group="apps"),),
            selectors=(_workload_selector(),),
        ),
    )
```

In `_scale_down_rows`, the fixtures already use the shared helpers:

```python
"deployments": [_deployment("web", "deploy-1")],
"replicasets": [_replicaset()],
```

Verify the shared selector helper is present:

```bash
rg "_workload_selector" tests/ui/test_impact_flow.py
```

Expected: matches in the fixture helper definitions.

- [ ] **Step 5: Run the GREEN evidence and affected suites**

Run:

```bash
PYTHONPATH="$PWD/src" ../../.venv/bin/python -m pytest -p no:tach \
  tests/core/test_impact.py \
  tests/ui/test_impact_flow.py \
  tests/ui/test_impact_security.py \
  -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Format and lint the changed tests**

Run:

```bash
../../.venv/bin/ruff check --fix \
  tests/core/test_impact.py tests/ui/test_impact_flow.py
../../.venv/bin/ruff format \
  tests/core/test_impact.py tests/ui/test_impact_flow.py
../../.venv/bin/ruff check \
  tests/core/test_impact.py tests/ui/test_impact_flow.py
```

Expected: Ruff exits 0.

- [ ] **Step 7: Commit the tested fixture correction**

```bash
git add tests/core/test_impact.py tests/ui/test_impact_flow.py tests/ui/test_impact_security.py
git commit \
  -m "test: align impact fixtures with workload selectors (#297)" \
  -m "Exercise the selector and owner-reference paths that production already builds for delete and rollout restart." \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit succeeds and pre-commit hooks pass.

---

### Task 2: Synchronize User and Engineering Documentation

**Files:**
- Modify: `docs/tui.md:202-226`
- Modify: `docs/tui.md:405-415`
- Modify: `docs/resource-relationships.md:431-452`
- Modify: `docs/dev/plans/2026-08-15-graph-derived-blast-radius.md:3169-3195`
- Modify: `docs/dev/plans/2026-08-15-graph-derived-blast-radius.md:3479-3518`

**Interfaces:**
- Consumes: the exact UI output pinned in Task 1.
- Produces: user-facing and authoritative engineering text with the same direct, transitive, and revisit counts.
- Does not change Python interfaces.

- [ ] **Step 1: Update the `docs/tui.md` example**

Replace the example's dependent body with:

```text
      known direct dependents (may be affected): 2 or more
        - Pod/prod/web-abc-1 via managed_by (declared) at apps/Deployment/prod/web: spec.selector
        - apps/ReplicaSet/prod/web-abc via owned_by (declared) at apps/ReplicaSet/prod/web-abc: metadata.ownerReferences[0]
      known transitive dependents (may be affected): none in this snapshot
      additional known paths: 2 or more (already-listed dependents reached again)
```

Change the explanation immediately below it from "Every count reads `1 or
more`" to "Every non-zero count reads `2 or more`", and state that incomplete
Gateway API coverage makes both counts lower bounds.

Extend the workload-selector explanation to say that the shortest selector
path is used for delete and rollout restart as well as scale-down because all
three actions include `managed_by`; the two ReplicaSet-to-Pod facts remain
additional known paths.

- [ ] **Step 2: Clarify the shared rule in `docs/resource-relationships.md`**

After the paragraph defining breadth-first deterministic traversal, add:

```markdown
This shortest-path rule also applies to delete and rollout restart. Because
both actions include `managed_by`, a production-shaped Deployment reaches a
matching Pod directly through its own `spec.selector`; its ReplicaSet is a
second direct dependent, while the ReplicaSet selector and the Pod's
ownerReference are two additional known paths to the already-listed Pod.
Selector and owner evidence are kept distinct: a selector identifies a Pod a
controller can manage or acquire, while an ownerReference records current
ownership.
```

- [ ] **Step 3: Synchronize the authoritative #283 plan**

Update its fixture snippet to include the same `_workload_selector`,
production-shaped `_deployment`, and production-shaped `_replicaset` helpers
from Task 1. Update the delete and rollout-restart assertion snippets to the
exact tests from Task 1.

Also update the validation-test inventory at the end of the plan to use:

```text
test_delete_dialog_shows_realistic_workload_selector_paths
test_rollout_restart_dialog_shows_realistic_workload_selector_paths
```

instead of the superseded test names.

- [ ] **Step 4: Verify documentation consistency**

Run:

```bash
rg -n \
  "known direct dependents|known transitive dependents|additional known paths|realistic_workload_selector_paths" \
  docs/tui.md docs/resource-relationships.md \
  docs/dev/plans/2026-08-15-graph-derived-blast-radius.md
rg -n \
  "test_delete_dialog_shows_realistic_workload_selector_paths|test_rollout_restart_dialog_shows_realistic_workload_selector_paths|workload_selector" \
  docs tests
```

Expected:

- current examples show direct `2 or more`, transitive none, and additional
  paths `2 or more`;
- the superseded test names have no matches, and the current workload-selector
  test names do.

- [ ] **Step 5: Commit the documentation synchronization**

```bash
git add docs/tui.md docs/resource-relationships.md \
  docs/dev/plans/2026-08-15-graph-derived-blast-radius.md
git commit \
  -m "docs: align delete impact examples with selectors (#297)" \
  -m "Document the shortest managed_by path and preserve both ReplicaSet routes as additional known paths." \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: commit succeeds and documentation hooks pass.

---

### Task 3: Run the Full Gate and Open the Pull Request

**Files:**
- Verify: all tracked files
- Do not modify: `uv.lock`

**Interfaces:**
- Consumes: Tasks 1 and 2 commits.
- Produces: a reviewed PR linked to #297; no local merge.

- [ ] **Step 1: Run format and lint checks**

```bash
../../.venv/bin/ruff check --fix src tests
../../.venv/bin/ruff format src tests
../../.venv/bin/ruff check src tests
../../.venv/bin/ruff format --check src tests
```

Expected: both final check commands exit 0.

- [ ] **Step 2: Run type, architecture, and dependency checks**

```bash
PYTHONPATH="$PWD/src" ../../.venv/bin/mypy src
../../.venv/bin/tach check
../../.venv/bin/deptry .
```

Expected: all commands exit 0.

- [ ] **Step 3: Run the full test suite**

```bash
PYTHONPATH="$PWD/src" ../../.venv/bin/python -m pytest -x -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 4: Verify repository state and lock safety**

```bash
git status --short
git diff origin/main -- uv.lock
git diff --check origin/main...HEAD
```

Expected:

- working tree is clean;
- `uv.lock` has no diff;
- `git diff --check` exits 0.

- [ ] **Step 5: Push and create the PR**

```bash
git push -u origin fix/297-selector-impact-paths
gh pr create \
  --repo hellices/korvid \
  --base main \
  --head fix/297-selector-impact-paths \
  --title "test: align impact fixtures with workload selectors" \
  --body "$(printf '%s\n' \
    'Closes #297' \
    '' \
    '## Summary' \
    '- make delete/restart fixtures carry production workload selectors' \
    '- pin selector and owner-reference paths as distinct evidence' \
    '- synchronize impact-preview examples and the authoritative #283 plan' \
    '' \
    '## Verification' \
    '- Ruff check and format' \
    '- mypy' \
    '- tach' \
    '- deptry' \
    '- full pytest suite')"
```

The PR body must contain:

```markdown
Closes #297

## Summary
- make delete/restart fixtures carry production workload selectors
- pin selector and owner-reference paths as distinct evidence
- synchronize impact-preview examples and the authoritative #283 plan

## Verification
- Ruff check and format
- mypy
- tach
- deptry
- full pytest suite
```

- [ ] **Step 6: Complete the repository review loop**

Follow `AGENTS.md` exactly:

1. wait for all required checks and Copilot review;
2. read every comment, including suppressed findings;
3. address each credible finding with a failing test first;
4. run the full gate again;
5. commit and push without amending or force-pushing;
6. reply to and resolve each addressed thread;
7. re-request review until the documented stopping condition;
8. verify `gh pr view <number> --json statusCheckRollup` contains only
   successful required checks.

Stop with the PR ready for the maintainer. Do not merge it.
