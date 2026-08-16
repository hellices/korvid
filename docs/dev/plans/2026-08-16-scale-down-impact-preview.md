# Workload Scale-Down Impact Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a bounded graph-derived impact advisory to workload scale-down confirmations without changing scale-up, no-op, or unknown-current-count behavior.

**Architecture:** Extend the pure `ImpactAction` relation mapping with a closed `SCALE_DOWN` action, then reuse the existing renderer and `_impact_preview` loader. The scale UI captures the originating pane and exact target before `ReplicasPrompt`, requests an impact snapshot only for a known decrease, and revalidates origin/context/UID before mounting `ConfirmScreen`.

**Tech Stack:** Python 3.11+, asyncio, Textual, immutable relationship graph values, pytest/pytest-asyncio, Ruff, mypy strict, tach.

## Global Constraints

- `ImpactAction.SCALE_DOWN` activates only when `current is not None and replicas < current`.
- Scale-up, no-op, and unknown-current-count confirmations perform no relationship snapshot LISTs.
- The closed scale-down relation set is exactly `OWNED_BY`, `MANAGED_BY`, `SELECTS`, and `ROUTES_TO`.
- `PROTECTED_BY`, `USES_VOLUME`, `USES_CONFIG`, `SCHEDULED_ON`, and `BOUND_TO` never produce a scale-down impact claim.
- Routing resources are only known dependents that **may be affected**; never claim zero endpoints, exclusive ownership, outage, or guaranteed failure.
- Every scale-down advisory states that controller scale-down is not an Eviction API request and PodDisruptionBudgets do not gate it.
- Every scale-down advisory states that HorizontalPodAutoscaler targeting/reconciliation is not evaluated.
- An `apps/StatefulSet` scale-down advisory additionally states that PVC retention policy is not evaluated; a custom resource that merely spells its kind `StatefulSet` in another group does not, since `persistentVolumeClaimRetentionPolicy` is an `apps` API field.
- These three statements are machine-defined, not graph-derived: a scale-down whose snapshot timed out or failed still states them beneath the static `impact unavailable; approval remains available` line, rendered by the same action/group/kind-aware helper the available path uses. Delete and rollout restart keep the generic unavailable advisory verbatim.
- Capture pane identity, pane scope, target identity, UID, context epoch, kind alias, and current replicas before the first await and before `ReplicasPrompt`, and revalidate all of them - the captured replica count included - at every awaited gap: a count that moved under an unchanged identity reverses the scale-down classification and staled the `old -> new` line.
- After dry-run, ownership lookup, and impact loading, require the same pane identity/scope, context epoch, resource identity, and captured UID before `ConfirmScreen`.
- Cancellation or identity/origin drift creates no confirmation, write reservation, write operation, or audit entry.
- Preserve RBAC, server dry-run, fresh-keystroke approval, UID precondition, fail-closed audit, timeout, cancellation propagation, Unicode/Rich safety, deterministic ordering, and all existing bounds.
- Add no constructor parameter, Kubernetes client interface, per-node GET fan-out, dependency, or `uv.lock` change.
- Use `UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ...` for repository commands; never run `uv lock`.

---

## File Structure

- `src/korvid/core/impact.py`: owns `ImpactAction.SCALE_DOWN` and its exact relation set.
- `src/korvid/ui/impact_preview.py`: owns the scale-down action label and machine-defined limitation lines.
- `src/korvid/ui/app.py`: owns activation, origin capture, awaited work, identity revalidation, and confirmation wiring.
- `tests/core/test_impact.py`: pins included and excluded scale-down relation semantics.
- `tests/ui/test_impact_preview.py`: pins exact scale-down wording, order, bounds, and the `apps/StatefulSet`-conditional note.
- `tests/ui/test_impact_flow.py`: owns the end-to-end scale harness, activation boundary, routing traversal, and pane/scope/UID races.
- `tests/ui/test_impact_security.py`: pins approval, audit, RBAC, timeout, and cancellation invariants for scale-down.
- `docs/tui.md`: explains when scale-down impact appears and what it does not evaluate.
- `docs/dev/plans/2026-08-16-scale-down-impact-preview.md`: remains the authoritative executable plan and is updated if implementation details legitimately diverge.

---

### Task 1: Close the scale-down action semantics

**Files:**
- Modify: `src/korvid/core/impact.py:59-99`
- Modify: `tests/core/test_impact.py:188-260`

**Interfaces:**
- Consumes: `RelationKind`, `ImpactAction`, `ACTION_RELATIONS`, and `summarize_impact`.
- Produces: `ImpactAction.SCALE_DOWN` with the exact value `"scale_down"` and an immutable four-relation mapping used by the renderer and app.

- [ ] **Step 1: Write the failing enum and mapping test**

Replace the closed-set test and add explicit scale-down relation tests:

```python
def test_only_supported_writes_carry_action_semantics() -> None:
    assert [action.value for action in ImpactAction] == [
        "delete",
        "rollout_restart",
        "scale_down",
    ]
    assert set(ACTION_RELATIONS) == set(ImpactAction)
    assert ACTION_RELATIONS[ImpactAction.SCALE_DOWN] == frozenset(
        {
            RelationKind.OWNED_BY,
            RelationKind.MANAGED_BY,
            RelationKind.SELECTS,
            RelationKind.ROUTES_TO,
        }
    )
    assert RelationKind.SELECTS not in ACTION_RELATIONS[ImpactAction.DELETE]
    assert RelationKind.SELECTS not in ACTION_RELATIONS[ImpactAction.ROLLOUT_RESTART]
    assert {
        cast(RelationshipEdge, edge).relation for param in _DELETE_CASES for edge in param.values
    } == ACTION_RELATIONS[ImpactAction.DELETE]


@pytest.mark.parametrize(
    "relation",
    [
        RelationKind.OWNED_BY,
        RelationKind.MANAGED_BY,
        RelationKind.SELECTS,
        RelationKind.ROUTES_TO,
    ],
)
def test_scale_down_follows_every_relation_in_its_closed_set(relation: RelationKind) -> None:
    workload = _res("Deployment", "web", group="apps", uid="deploy-1")
    dependent = _res("Pod", "web-abc-1", uid="pod-1")
    summary = summarize_impact(
        _graph(_edge(dependent, workload, relation, field="spec.selector")),
        ImpactAction.SCALE_DOWN,
        workload,
    )
    assert [item.resource for item in summary.direct] == [dependent]


@pytest.mark.parametrize(
    "relation",
    [
        RelationKind.USES_VOLUME,
        RelationKind.USES_CONFIG,
        RelationKind.PROTECTED_BY,
        RelationKind.SCHEDULED_ON,
        RelationKind.BOUND_TO,
    ],
)
def test_scale_down_ignores_every_relation_outside_its_closed_set(
    relation: RelationKind,
) -> None:
    workload = _res("Deployment", "web", group="apps", uid="deploy-1")
    other = _res("PodDisruptionBudget", "web", group="policy", uid="other-1")
    summary = summarize_impact(
        _graph(_edge(other, workload, relation, field="spec.selector")),
        ImpactAction.SCALE_DOWN,
        workload,
    )
    assert summary.direct == ()
    assert summary.transitive == ()
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync pytest -p no:tach -q \
  tests/core/test_impact.py::test_only_supported_writes_carry_action_semantics \
  tests/core/test_impact.py::test_scale_down_follows_every_relation_in_its_closed_set \
  tests/core/test_impact.py::test_scale_down_ignores_every_relation_outside_its_closed_set
```

Expected: collection or assertion failure because `ImpactAction.SCALE_DOWN` does not exist.

- [ ] **Step 3: Implement the closed action mapping**

Add the enum member and mapping:

```python
class ImpactAction(StrEnum):
    DELETE = "delete"
    ROLLOUT_RESTART = "rollout_restart"
    SCALE_DOWN = "scale_down"


_SCALE_DOWN_RELATIONS: frozenset[RelationKind] = frozenset(
    {
        RelationKind.OWNED_BY,
        RelationKind.MANAGED_BY,
        RelationKind.SELECTS,
        RelationKind.ROUTES_TO,
    }
)


ACTION_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind]] = {
    ImpactAction.DELETE: _DELETE_RELATIONS,
    ImpactAction.ROLLOUT_RESTART: _ROLLOUT_RESTART_RELATIONS,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
}
```

Document beside `_SCALE_DOWN_RELATIONS` that selectors and routes are followed
only to list conservative known dependents; this mapping does not assert
endpoint loss or failure. Document why PDB, volume, config, node, and binding
relations are excluded.

Post-review divergence (PR #296 review): the closed set also filters the
`unresolved` warning for a scale-down. `summarize_impact` previously reported
every dangling edge whose subject was in the affected set, whatever its
relation, which let an excluded relation re-enter a scale-down advisory as a
warning. The policy now lives in a second closed mapping keyed by *every*
action:

```python
ACTION_UNRESOLVED_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind] | None] = {
    ImpactAction.DELETE: None,
    ImpactAction.ROLLOUT_RESTART: None,
    ImpactAction.SCALE_DOWN: _SCALE_DOWN_RELATIONS,
}
```

`None` means "warn about a dangling reference of any relation", a frozenset
means "warn only about these". `_unresolved_edges` indexes the mapping
directly — no permissive default and no membership fallback — so an action
added to `ACTION_RELATIONS` and forgotten here raises `KeyError` instead of
silently inheriting the relation-blind policy;
`test_every_action_chooses_its_unresolved_reference_policy` fails first.
Delete and rollout restart keep the relation-blind warning, since a restarted
Pod that mounts a deleted ConfigMap will not come back. The excluded-case
parameters are pinned equal to `set(RelationKind) -
ACTION_RELATIONS[ImpactAction.SCALE_DOWN]`, so a future `RelationKind` cannot
escape the policy either.

- [ ] **Step 4: Verify GREEN and regression coverage**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync pytest -p no:tach -q \
  tests/core/test_impact.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ruff check \
  src/korvid/core/impact.py tests/core/test_impact.py
```

Expected: all `test_impact.py` tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/impact.py tests/core/test_impact.py
git commit -m "feat(core): define scale-down impact semantics" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Render scale-down limitations

**Files:**
- Modify: `src/korvid/ui/impact_preview.py:46-156`
- Modify: `tests/ui/test_impact_preview.py`

**Interfaces:**
- Consumes: `ImpactSummary.action`, `ImpactSummary.target.group`,
  `ImpactSummary.target.kind`, and `ImpactAction.SCALE_DOWN`.
- Produces: `_action_note_lines(action: ImpactAction, group: str, kind: str) -> list[str]`,
  shared by the available and unavailable renderers so both use identical
  machine-defined limitations. The PVC line is selected by the *pair*
  `("apps", "StatefulSet")`: `persistentVolumeClaimRetentionPolicy` is an
  `apps` API field, so a CRD that spells its own kind `StatefulSet` in
  another group must not be told about a policy it does not have.

- [ ] **Step 1: Write exact failing renderer tests**

Add constants to the import list and tests:

```python
def test_scale_down_names_the_action_and_static_limitations() -> None:
    lines = render_impact_lines(
        _summary(action=ImpactAction.SCALE_DOWN, target=_DEPLOY)
    )
    assert lines[:6] == (
        IMPACT_TITLE,
        "  scale down apps/Deployment/prod/web",
        ADVISORY_LINE,
        _SCALE_DOWN_PDB_LINE,
        _SCALE_DOWN_HPA_LINE,
        "  known direct dependents (may be affected): none in this snapshot",
    )
    assert _SCALE_DOWN_STS_PVC_LINE not in lines


def test_statefulset_scale_down_names_the_unchecked_pvc_policy() -> None:
    statefulset = GraphResource(
        group="apps",
        kind="StatefulSet",
        namespace="prod",
        name="db",
        uid="sts-1",
    )
    lines = render_impact_lines(
        _summary(action=ImpactAction.SCALE_DOWN, target=statefulset)
    )
    assert lines.index(_SCALE_DOWN_STS_PVC_LINE) == lines.index(_SCALE_DOWN_HPA_LINE) + 1


@pytest.mark.parametrize("action", [ImpactAction.DELETE, ImpactAction.ROLLOUT_RESTART])
def test_non_scale_actions_never_render_scale_down_limitations(action: ImpactAction) -> None:
    lines = render_impact_lines(_summary(action=action))
    assert _SCALE_DOWN_PDB_LINE not in lines
    assert _SCALE_DOWN_HPA_LINE not in lines
    assert _SCALE_DOWN_STS_PVC_LINE not in lines
```

- [ ] **Step 2: Run the tests to verify RED**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_impact_preview.py -k "scale_down or non_scale_actions"
```

Expected: import/attribute failure because the scale-down constants and action
label do not exist.

- [ ] **Step 3: Implement bounded machine-defined notes**

Add:

```python
_SCALE_DOWN_PDB_LINE = (
    "  controller scale-down is not an Eviction API request;"
    " PodDisruptionBudgets do not gate it"
)
_SCALE_DOWN_HPA_LINE = (
    "  HorizontalPodAutoscaler targeting and reconciliation are not evaluated"
)
_SCALE_DOWN_STS_PVC_GROUP = "apps"
_SCALE_DOWN_STS_PVC_KIND = "StatefulSet"
_SCALE_DOWN_STS_PVC_LINE = "  StatefulSet PVC retention policy is not evaluated"

_ACTION_LABEL = {
    ImpactAction.DELETE: "delete",
    ImpactAction.ROLLOUT_RESTART: "rollout restart",
    ImpactAction.SCALE_DOWN: "scale down",
}


def _action_note_lines(action: ImpactAction, group: str, kind: str) -> list[str]:
    if action is not ImpactAction.SCALE_DOWN:
        return []
    lines = [_SCALE_DOWN_PDB_LINE, _SCALE_DOWN_HPA_LINE]
    if (group, kind) == (_SCALE_DOWN_STS_PVC_GROUP, _SCALE_DOWN_STS_PVC_KIND):
        lines.append(_SCALE_DOWN_STS_PVC_LINE)
    return lines
```

Wire it after the advisory:

```python
lines.append(ADVISORY_LINE)
lines.extend(_action_note_lines(summary.action, summary.target.group, summary.target.kind))
lines.extend(_section(_DIRECT_TITLE, summary.direct, capped=capped))
```

All three lines are machine-defined ASCII and still pass through the final
`_bounded` call.

- [ ] **Step 4: Verify GREEN and renderer invariants**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_impact_preview.py tests/ui/test_confirm_screen.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ruff check \
  src/korvid/ui/impact_preview.py tests/ui/test_impact_preview.py
```

Expected: all renderer and confirmation tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/impact_preview.py tests/ui/test_impact_preview.py
git commit -m "feat(ui): explain scale-down preview limits" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Wire scale-down into the scale flow

**Files:**
- Modify: `src/korvid/ui/app.py:5680-5761`
- Modify: `tests/ui/test_impact_flow.py:81-386`
- Modify: `tests/ui/test_impact_flow.py:650-730`

**Interfaces:**
- Consumes: `_WriteOrigin`, `_impact_preview`, `_write_identity_intact`, `ImpactAction.SCALE_DOWN`, `ReplicasPrompt`, and the existing `_push_write_confirmation`.
- Produces: `_is_scale_down(current: int | None, replicas: int) -> bool` and an `_confirm_scale(..., origin: _WriteOrigin)` flow that passes optional `impact_lines`.

- [ ] **Step 1: Extend the flow harness before writing production code**

Make `_deployment` accept a desired count:

```python
def _deployment(name: str, uid: str, *, desired: int | None = 3) -> GenericSummary:
    return GenericSummary(
        name=name,
        namespace="prod",
        kind="Deployment",
        created="",
        desired=desired,
        uid=uid,
    )
```

Add a scale preview fake so dry-run hooks and order assertions are real:

```python
async def preview_scale(
    self,
    meta: ResourceMeta,
    namespace: str | None,
    name: str,
    replicas: int,
    *,
    uid: str | None = None,
) -> list[str] | None:
    self._order.append("preview")
    self._preview_hook()
    return [f"~ spec.replicas: {replicas}"]
```

Add an Ingress helper whose declared `ROUTES_TO` reference targets the Service:

```python
def _ingress() -> GenericSummary:
    return GenericSummary(
        name="web",
        namespace="prod",
        kind="Ingress",
        created="",
        uid="ing-1",
        relationships=RelationshipFacts(
            api_group="networking.k8s.io",
            references=(
                ReferenceFact(
                    relation=RelationKind.ROUTES_TO,
                    target=TargetReference(
                        group="",
                        kind="Service",
                        namespace="prod",
                        name="web",
                        uid="svc-1",
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="spec.rules[0].http.paths[0].backend.service",
                ),
            ),
        ),
    )
```

- [ ] **Step 2: Write failing activation and traversal tests**

Replace the old unsupported-scale test with:

```python
async def test_scale_down_dialog_shows_controller_and_routing_dependents(
    tmp_path: Path,
) -> None:
    rows = {
        "pods": [_pod()],
        "deployments": [_deployment("web", "deploy-1")],
        "replicasets": [_replicaset()],
        "services": [_service()],
        "ingresses": [_ingress()],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(
            pilot,
            lambda: isinstance(env.app.screen, ReplicasPrompt),
            label="replicas prompt",
        )
        await pilot.press("1")
        await pilot.press("enter")
        await until(
            pilot,
            lambda: isinstance(env.app.screen, ConfirmScreen),
            label="scale-down confirm",
        )
        text = impact_text(env.app)
        assert "scale down apps/Deployment/prod/web" in text
        assert "apps/ReplicaSet/prod/web-abc" in text
        assert "Pod/prod/web-abc-1" in text
        assert "Service/prod/web" in text
        assert "networking.k8s.io/Ingress/prod/web" in text
        assert "may be affected" in text
        assert "will fail" not in text
        assert env.ops.calls == []


@pytest.mark.parametrize(
    ("desired", "requested"),
    [(3, 5), (3, 3), (None, 1)],
)
async def test_non_decreasing_or_unknown_scale_never_loads_relationships(
    tmp_path: Path,
    desired: int | None,
    requested: int,
) -> None:
    rows = {"deployments": [_deployment("web", "deploy-1", desired=desired)]}
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(env.app.screen, ReplicasPrompt))
        for char in str(requested):
            await pilot.press(char)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(env.app.screen, ConfirmScreen))
        assert not env.app.screen.query(".confirm-impact")
        assert env.lister.calls == []
```

Run RED:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_impact_flow.py -k "scale_down_dialog or non_decreasing_or_unknown"
```

Expected: scale-down has no impact section; the negative cases remain green.

> **Correction (recorded in the final whole-branch review).** The snippet
> above asserts `"networking.k8s.io/Ingress/prod/web" in text` for a
> Deployment scale-down, and that assertion is **correct**. Task 3
> implemented the test with a Deployment fixture that carried no
> relationships at all, and a Deployment with no `spec.selector` reaches its
> Pods only through the ReplicaSet — `Deployment -> ReplicaSet (owned_by) ->
> Pod (owned_by) -> Service (selects) -> Ingress (routes_to)`, four hops,
> one past `ImpactLimits.max_depth = 3` — so the test was changed to assert
> `"networking.k8s.io/Ingress/prod/web" not in text` and `"traversal capped"
> in text`, and the docs were written to match. That is not what production
> produces: `korvid.k8s.relationship_facts._workload_selector` gives every
> Deployment/ReplicaSet/StatefulSet a `SelectorFact(relation=MANAGED_BY,
> target Pod, match_is_subject=True)`, whose edge runs *Pod -> workload*, so
> the reverse walk reaches the Pod in one hop and the chain is `Deployment ->
> Pod (managed_by) -> Service (selects) -> Ingress (routes_to)` — three hops,
> inside the cap. The fixture, not the cap, was the deviation.
>
> The fix (final-review round): `tests/ui/test_impact_flow.py::_deployment`
> takes an opt-in `selects_pods` flag that attaches exactly that fact, and
> `_scale_down_rows()` uses it; the Deployment test now asserts the Ingress
> **is** named, that no `traversal capped` line appears, that the ReplicaSet
> is a direct dependent beside the Pod, and that the further routes to that
> Pod are counted under `additional known paths`. The second test that
> scales the ReplicaSet is kept (renamed
> `test_scale_down_of_a_replicaset_follows_the_same_routing_chain`) because
> it still pins a real, distinct thing: the same routing chain reached from
> a different scalable kind. The four-hop/`traversal capped` claims this
> note previously authorized have been removed from `docs/tui.md`,
> `docs/resource-relationships.md` and the design doc. Neither the cap nor
> any production behavior changed in either round.
>
> **Follow-up correction (final re-review round).** The paragraph above, and
> the ReplicaSet test as first written, still said that a ReplicaSet's Pods
> hang off `metadata.ownerReferences` rather than off a selector the target
> declares itself. That is wrong for the same reason the Deployment fixture
> was: `_workload_selector` gives a **ReplicaSet** the same
> `SelectorFact(relation=MANAGED_BY, target Pod, match_is_subject=True)` it
> gives a Deployment or a StatefulSet, so a real ReplicaSet summary carries
> `spec.selector` too. `_replicaset()` therefore grew the same opt-in
> `selects_pods` flag (used only by `_scale_down_rows()`, so the #294
> delete/rollout-restart fixtures are untouched), and the ReplicaSet test now
> asserts the first path the walk actually reports — `Pod/prod/web-abc-1 via
> managed_by (declared) at apps/ReplicaSet/prod/web-abc: spec.selector`, with
> `Service` and `Ingress` hanging off that same first hop. Hop count (three)
> and the Ingress being named with no `traversal capped` line are unchanged.
> The Pods' owner references are now a *second* known route to an
> already-listed dependent, so the Deployment case folds two of them into
> `additional known paths` (`2 or more`) and the ReplicaSet case one
> (`1 or more`); every count stays a lower bound because that snapshot's
> Gateway API coverage is `unavailable`, not because anything was capped.
> Again: fixture shape only — no cap, relation set, or production semantic
> changed.

- [ ] **Step 3: Write failing origin and scope race tests**

Follow the existing delete/restart split-pane helpers. Add one test that moves
focus to a second pane selecting the same UID during `preview_scale`, and one
that changes the originating pane scope during the impact LIST:

```python
async def test_scale_down_focus_move_to_same_object_aborts_before_confirmation(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane
        env.lister.on_first_call = app._focus_other_pane
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt))
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal(app, pilot, "scale-down pane refusal")
        assert app._pane is not origin
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert _namespaced_list_scopes(env) == {"prod"}


async def test_scale_down_scope_change_on_origin_aborts_before_confirmation(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app
    async with app.run_test() as pilot:
        await _prod_origin_beside_an_all_namespaces_pane(app, pilot)
        origin = app._pane

        def widen_the_origin_pane() -> None:
            origin.scope = ALL_NAMESPACES

        env.lister.on_first_call = widen_the_origin_pane
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt))
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal(app, pilot, "scale-down scope refusal")
        assert app._pane is origin
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert _namespaced_list_scopes(env) == {"prod"}
```

Add a UID-loss case beside the existing delete/restart cases:

```python
async def test_scale_down_uid_loss_during_impact_load_aborts_before_confirmation(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def drop_the_uid() -> None:
        app.store.apply_event(
            app.current_kind,
            app.current_scope,
            "MODIFIED",
            _deployment("web", ""),
        )

    env.lister.on_first_call = drop_the_uid
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(app.screen, ReplicasPrompt))
        await pilot.press("1")
        await pilot.press("enter")
        await _refusal(app, pilot, "scale-down uid refusal")
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
```

- [ ] **Step 4: Implement activation, capture, and final identity gate**

Add:

```python
@staticmethod
def _is_scale_down(current: int | None, replicas: int) -> bool:
    return current is not None and replicas < current
```

Capture values before the permission await:

```python
meta, ns, name, uid = target
if (meta.group, meta.plural) not in self._SCALABLE:
    ...
origin = self._write_origin()
epoch = self._ctx_epoch
kind_alias = self._canonical_kind(self.current_kind)
current = self._current_replicas(ns, name)
if not await self._precheck_keybinding_write("scale", meta, ns, name):
    return
if not self._write_identity_intact(
    "scale",
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

Pass `origin` through the callback:

```python
self.run_worker(
    self._confirm_scale(
        meta,
        ns,
        name,
        uid,
        current,
        replicas,
        epoch,
        kind_alias,
        origin,
    )
)
```

Extend the method signature:

```python
async def _confirm_scale(
    self,
    meta: ResourceMeta,
    ns: str | None,
    name: str,
    uid: str | None,
    current: int | None,
    replicas: int,
    epoch: int,
    kind_alias: str,
    origin: _WriteOrigin,
) -> None:
```

Load impact only for a known decrease and perform one exact final gate:

```python
preview = await self._dry_run_preview(
    ops.preview_scale(meta, ns, name, replicas, uid=uid)
)
note = await self._managed_note(kind_alias, ns, name)
is_scale_down = self._is_scale_down(current, replicas)
impact = (
    await self._impact_preview(
        ImpactAction.SCALE_DOWN,
        meta,
        ns,
        name,
        uid,
        origin=origin,
    )
    if is_scale_down
    else None
)
phase = "the impact summary" if is_scale_down else "the dry-run preview"
if not self._write_identity_intact(
    "scale",
    meta,
    ns,
    name,
    uid,
    phase=phase,
    epoch=epoch,
    origin=origin,
):
    return
```

Pass `impact_lines=impact` to `_push_write_confirmation`. Do not add a new
loader, bridge, or composition-root parameter.

> **Verified implementation deviation (recorded in the final re-review
> round, extended by the PR #296 review): the shipped flow performs five
> gates, not two, and each compares the captured replica count as well as
> the identity.** The snippet above places a single gate in
> `_confirm_scale`, *after* the impact load, whose `phase` label switches
> between `"the impact summary"` and `"the dry-run preview"`.
> `src/korvid/ui/app.py` instead calls `_scale_context_intact` five times.
> That helper composes `_write_identity_intact` (context epoch, focused
> pane identity and scope, selected resource identity, exact UID) with
> equality of the latest `_current_replicas(ns, name)` and the `current`
> the flow captured:
>
> 1. `action_scale_resource`, after `_precheck_keybinding_write` and
>    **before** `ReplicasPrompt` is pushed (`phase="the permission check"`);
> 2. `_confirm_scale`, at the top, **before** the dry-run round trip
>    (`phase="the replica count prompt"`) — the prompt is the flow's own
>    awaited gap and the one a user can hold open indefinitely, so drift
>    there costs no API call at all;
> 3. `_confirm_scale`, after the dry-run preview and the managed-resource
>    note and **before** any impact LIST (`phase="the dry-run preview"`) —
>    unconditional, so a doomed scale never spends the snapshot fan-out nor
>    scopes it to a pane the user has left;
> 4. `_confirm_scale`, after the impact summary and **before**
>    `ConfirmScreen` is mounted (`phase="the impact summary"`), guarded by
>    `is_scale_down` because the snapshot load is the only awaited gap it
>    covers;
> 5. on approval, as the `approval_guard` `_confirm_scale` hands
>    `_push_write_confirmation` (`phase="the confirmation dialog"`) — the
>    dialog is the flow's longest awaited gap, since it stays open until the
>    user answers, and gate 5 runs after it is dismissed and **before** the
>    `_run_write` worker exists, so before the write reservation, the intent
>    audit record and the operation factory. It fires only on a fresh
>    keystroke approval, so it can only refuse an approval the user gave and
>    never becomes a second path to the write, and it is deferred by one
>    event-loop iteration because Textual invokes a screen's result callback
>    before it pops the dismissed screen (run inline, the selection check
>    would see the confirmation itself still on the screen stack).
>    `approval_guard` defaults to `None`, so every other write flow keeps
>    the callback it had.
>
> The count is gated because a scale is the one write whose *meaning* is
> not fixed by its identity: the same requested number is a decrease or an
> increase depending on where the object stands. `current` decides whether
> the blast radius is loaded at all and is the number the approval line
> reads `replicas <old> -> <new>` from, so a controller or an autoscaler
> moving `spec.replicas` under an unchanged incarnation would otherwise
> reverse the classification and stale the dialog text with nothing
> noticing. `None` is a captured value like any other — a row that gains a
> readable count mid-flow drifted just as much — so the comparison is plain
> equality and cancels with its own banner (`the desired replica count
> changed during <phase>`).
>
> A scale that is not a known decrease therefore has gates 1–3 and 5, and
> no impact LIST at all; only gate 4 is conditional on the classification.
> This is strictly stronger than the snippet — the pre-prompt gate, the
> post-prompt gate, the pre-LIST gate, the post-approval gate and the count
> comparison are all new awaited-gap coverage — and it changes no cap,
> relation set, activation boundary, or write path.
> `tests/ui/test_impact_flow.py` and `tests/ui/test_impact_security.py` pin
> each gate (identity *and* count drift during the permission round trip,
> during the count prompt, during the dry-run preview, during the impact
> load, and while the confirmation dialog is open),
> `test_scale_replica_drift_during_the_prompt_never_previews_or_lists` and
> `test_scale_replica_drift_during_the_dry_run_never_loads_relationships`
> are parametrized over a requested decrease *and* a requested increase so
> that moving either unconditional gate under `is_scale_down` fails, and
> `test_non_decreasing_or_unknown_scale_never_loads_relationships` pins that
> the non-decrease shapes issue no LIST.

- [ ] **Step 5: Verify flow GREEN**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_impact_flow.py tests/ui/test_split_pane.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ruff check \
  src/korvid/ui/app.py tests/ui/test_impact_flow.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync mypy src/korvid/ui/app.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync tach check
```

Expected: all selected tests pass; Ruff, mypy, and tach are clean.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/ui/app.py tests/ui/test_impact_flow.py
git commit -m "feat(ui): preview workload scale-down impact" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Pin scale-down security and cancellation

**Files:**
- Modify: `tests/ui/test_impact_security.py`

**Interfaces:**
- Consumes: `ImpactEnv`, `RecordingLister`, `RecordingOps`, `ConfirmScreen`, `ReplicasPrompt`, and the final scale flow from Task 3.
- Produces: regression coverage proving the scale-down integration cannot weaken approval, RBAC, audit, UID, or cancellation behavior.

- [ ] **Step 1: Add a reusable real-flow helper**

In `test_impact_security.py`, add:

```python
from unittest import mock

from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt


async def _open_scale_down(pilot: Any, replicas: int = 1) -> None:
    await to_view(pilot, "deploy", expect="web")
    await pilot.press("S")
    await until(
        pilot,
        lambda: isinstance(pilot.app.screen, ReplicasPrompt),
        label="replicas prompt",
    )
    for char in str(replicas):
        await pilot.press(char)
    await pilot.press("enter")
```

Use the real keybinding and modal callback; do not call `_confirm_scale`
directly.

- [ ] **Step 2: Write failing decline and audit tests**

```python
async def test_declined_scale_down_with_impact_runs_no_operation(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await _open_scale_down(pilot)
        await until(pilot, lambda: isinstance(env.app.screen, ConfirmScreen))
        assert env.app.screen.query(".confirm-impact")
        await pilot.press("n")
        await until(pilot, lambda: len(env.app.screen_stack) == 1)
        assert env.ops.calls == []


async def test_scale_down_audit_failure_blocks_operation_factory(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()
    env = ImpactEnv(audit_path)
    blocked = "scale deployments/web blocked: audit log unavailable"
    async with env.app.run_test() as pilot:
        await _open_scale_down(pilot)
        await until(pilot, lambda: isinstance(env.app.screen, ConfirmScreen))
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(str(note.message) == blocked for note in env.app._notifications),
            label="scale-down audit refusal",
        )
        await until(
            pilot,
            lambda: env.app._active_cluster_writes == 0,
            label="scale-down write worker finished",
        )
        assert env.ops.calls == []
        assert list(audit_path.iterdir()) == []
```

Run RED and confirm failures occur because scale-down has no impact section or
the test harness needs the Task 3 wiring, not because the helper fails to open
the prompt.

- [ ] **Step 3: Add cancellation tests**

Cancel the Textual worker created by the real prompt callback:

```python
async def test_cancelling_scale_down_during_impact_load_writes_nothing(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    env.lister.delay = 60.0
    reservations_during_load: list[int] = []
    env.lister.on_first_call = lambda: reservations_during_load.append(
        env.app._active_cluster_writes
    )
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(env.app.screen, ReplicasPrompt))
        original_run_worker = env.app.run_worker
        started: list[Any] = []

        def record_worker(awaitable: Any, *args: Any, **kwargs: Any) -> Any:
            worker = original_run_worker(awaitable, *args, **kwargs)
            started.append(worker)
            return worker

        with mock.patch.object(env.app, "run_worker", side_effect=record_worker):
            await pilot.press("1")
            await pilot.press("enter")
        await until(pilot, lambda: env.lister.calls != [], label="scale impact listing")
        assert len(started) == 1
        started[0].cancel()
        await until(pilot, lambda: started[0].is_cancelled, label="scale worker cancelled")
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert len(env.app.screen_stack) == 1
        assert reservations_during_load == [0]
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_cancelled_scale_snapshot_is_not_an_unavailable_confirmation(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    env.lister.errors["deployments"] = asyncio.CancelledError()
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(pilot, lambda: isinstance(env.app.screen, ReplicasPrompt))
        original_run_worker = env.app.run_worker
        started: list[Any] = []

        def record_worker(awaitable: Any, *args: Any, **kwargs: Any) -> Any:
            worker = original_run_worker(awaitable, *args, **kwargs)
            started.append(worker)
            return worker

        with mock.patch.object(env.app, "run_worker", side_effect=record_worker):
            await pilot.press("1")
            await pilot.press("enter")
        await until(
            pilot,
            lambda: len(started) == 1 and started[0].is_cancelled,
            label="scale worker observed loader cancellation",
        )
        assert env.lister.calls != []
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert len(env.app.screen_stack) == 1
        assert not env.app.screen.query(".confirm-impact")
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()
```

> **Verified implementation deviation (recorded in Task 5): worker-state
> mechanics.** In the installed Textual (8.2.8), `Worker.cancel()` sets
> `is_cancelled` synchronously and eagerly, before the cancellation has
> actually propagated through the running coroutine; a `CancelledError`
> raised *inside* the work only moves `Worker.state` to
> `WorkerState.CANCELLED` once `Worker._run` has actually caught it. Task 4
> implemented both tests against `Worker.state` rather than `is_cancelled`:
>
> - `test_cancelling_scale_down_during_impact_load_writes_nothing` waits on
>   `started[0].is_cancelled and started[0].is_finished` (not `is_cancelled`
>   alone, which `cancel()` flips before the flow has actually stopped) and
>   then asserts `started[0].state is WorkerState.CANCELLED` before
>   proceeding — strictly stronger than the snippet's bare `is_cancelled`
>   wait;
> - `test_cancelled_scale_snapshot_is_not_an_unavailable_confirmation` waits
>   on `started[0].state is WorkerState.CANCELLED` directly, because the
>   snippet's `is_cancelled` would have stayed permanently `False` there (the
>   cancellation originates inside the loader, not from an external
>   `.cancel()` call) and the test could never have gone green for the right
>   reason.
>
> No behavior, cap, or invariant changed; only the Textual-version-correct
> observable the assertions wait on and check. See
> `.superpowers/sdd/task-4-report.md` §"Deviations from the brief" item 1 for
> the full account.

- [ ] **Step 4: Add RBAC and UID refusal tests**

Make `ImpactEnv` accept a `permission: bool = True` constructor argument and
have its `check_permission` fake return that value. Add:

```python
async def test_scale_down_rbac_denial_never_loads_or_confirms(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl", permission=False)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(
            pilot,
            lambda: any("not permitted" in note.message for note in env.app._notifications),
            label="scale RBAC denial",
        )
        assert env.lister.calls == []
        assert env.ops.calls == []
        assert len(env.app.screen_stack) == 1
```

> **Verified implementation deviation (recorded in Task 5): RBAC denial
> text.** The snippet above expects a notification containing `"not
> permitted"`. The app's actual denial message, produced by
> `KorvidApp._permitted` / `_write_perm_target`, is
> `missing permission: {verb} {resource}/{subresource}`. Task 4 implemented
> `test_scale_down_rbac_denial_never_loads_or_confirms` asserting the exact
> real string `missing permission: patch deployments/scale`
> (`str(note.message) == denied`) instead of a substring match against text
> the app never emits, and added `assert not audit_path.exists()` alongside
> it. Asserting the plan's wording would have failed against correct,
> unchanged production behavior; nothing in `_permitted`/`_write_perm_target`
> changed. See `.superpowers/sdd/task-4-report.md` §"Deviations from the
> brief" item 3.

The UID-loss integration case is owned by Task 3 beside the other
origin/identity flow tests. Do not duplicate it here and do not weaken
`_write_identity_intact`.

- [ ] **Step 5: Verify security GREEN**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync pytest -p no:tach -q \
  tests/ui/test_impact_security.py tests/ui/test_impact_flow.py \
  tests/ui/test_write_ops.py tests/ui/test_write_confirm_characterization.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ruff check \
  tests/ui/test_impact_security.py tests/ui/test_impact_flow.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ruff format --check \
  tests/ui/test_impact_security.py tests/ui/test_impact_flow.py
```

Expected: all selected tests pass with no warnings.

- [ ] **Step 6: Commit**

```bash
git add tests/ui/test_impact_security.py tests/ui/test_impact_flow.py
git commit -m "test(ui): secure scale-down impact previews" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Document, review, and validate the complete slice

**Files:**
- Modify: `docs/tui.md`
- Modify: `docs/dev/plans/2026-08-16-scale-down-impact-preview.md` only for verified implementation deviations
- Modify: `docs/resource-relationships.md` — a correctness fix identified in
  Task 5's own review, not an implementation deviation: its "Blast radius in
  write previews" section still said only `Ctrl-D`/`r` show the impact
  section and that scale has no tested semantics, which issue #295 made
  false. Brought in line with the same activation boundary, relation set,
  PDB/HPA/PVC limitations, conservative routing, and 3-hop cap behavior as
  `docs/tui.md` and this plan.
- Verify unchanged: `pyproject.toml`
- Verify unchanged: `uv.lock`

**Interfaces:**
- Consumes: the final UI wording and activation behavior from Tasks 1-4.
- Produces: user documentation, a synchronized authoritative plan, and complete local verification evidence.

- [ ] **Step 1: Update user documentation**

In the write-preview section of `docs/tui.md`, state:

- impact advisories cover delete, rollout restart, and known workload
  scale-down;
- scale-up, no-op, and unknown-current-count confirmations intentionally omit
  the graph section and make no snapshot reads;
- scale-down follows owner/manager, Service selector, EndpointSlice, and
  Ingress/Gateway routing relationships conservatively;
- PDBs do not gate controller scale-down;
- HPA targeting/reconciliation is not evaluated;
- `apps/StatefulSet` PVC retention is not evaluated;
- the advisory never predicts zero endpoints or failure and never replaces
  dry-run or approval.

- [ ] **Step 2: Verify documentation and plan consistency**

Run:

```bash
rg -n "scale.down|PodDisruptionBudget|HorizontalPodAutoscaler|PVC retention|SELECTS|ROUTES_TO" \
  docs/tui.md docs/resource-relationships.md \
  docs/dev/specs/2026-08-16-scale-down-impact-preview-design.md \
  docs/dev/plans/2026-08-16-scale-down-impact-preview.md \
  src/korvid/core/impact.py src/korvid/ui/impact_preview.py
```

Expected: the action name, activation boundary, relation set, and limitation
wording agree. No unfinished marker or stale statement that only
delete/restart are supported remains in the touched feature docs/tests.

- [ ] **Step 3: Run targeted formatting and static checks**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ruff check \
  src/korvid/core/impact.py src/korvid/ui/impact_preview.py src/korvid/ui/app.py \
  tests/core/test_impact.py tests/ui/test_impact_preview.py \
  tests/ui/test_impact_flow.py tests/ui/test_impact_security.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync ruff format --check \
  src/korvid/core/impact.py src/korvid/ui/impact_preview.py src/korvid/ui/app.py \
  tests/core/test_impact.py tests/ui/test_impact_preview.py \
  tests/ui/test_impact_flow.py tests/ui/test_impact_security.py
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync mypy
UV_NO_SYNC=1 UV_FROZEN=1 uv run --no-sync tach check
```

Expected: every command exits zero.

- [ ] **Step 4: Run the full gate**

Run:

```bash
UV_NO_SYNC=1 UV_FROZEN=1 make check
```

Expected: Ruff, mypy, pytest, and tach all pass. Record the exact pass/skip
count in the SDD progress ledger.

- [ ] **Step 5: Verify repository hygiene**

Run:

```bash
git diff --exit-code origin/main...HEAD -- pyproject.toml uv.lock
git status --short
```

Expected: no dependency or lock-file diff and no unstaged changes.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/tui.md docs/resource-relationships.md \
  docs/dev/plans/2026-08-16-scale-down-impact-preview.md \
  docs/dev/specs/2026-08-16-scale-down-impact-preview-design.md
git commit -m "docs: explain workload scale-down impact previews" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 7: Request final review**

Generate a review package from `git merge-base origin/main HEAD` through
`HEAD`, dispatch the `requesting-code-review` reviewer on the most capable
available model, fix every Critical/Important finding with TDD, and re-review
until both spec compliance and code quality pass.
