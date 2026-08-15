# Graph-Derived Blast-Radius Summaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver issue #283: deterministic, advisory `ImpactSummary` text beside the server dry-run preview in the delete and rollout-restart approval dialogs, without weakening approval, UID, RBAC, dry-run, or fail-closed audit guarantees.

**Architecture:** Add a pure `korvid.core.impact` model that walks one immutable `RelationshipGraph` snapshot backwards (dependent → dependency edges, reversed) under closed per-action relation semantics, and a pure `korvid.ui.impact_preview` renderer that turns the resulting `ImpactSummary` into bounded, literal text lines. `ConfirmScreen` grows an `impact_lines` section rendered above the existing dry-run preview; `KorvidApp` loads the snapshot through the `RelationshipSnapshotLoader` it already owns for `g`, behind a 5-second deadline, only in `action_delete_resource` and `action_rollout_restart`.

**Tech Stack:** Python 3.11+, frozen `slots=True` dataclasses, `enum.StrEnum`, asyncio (`asyncio.timeout`), Rich `Text`, Textual `ModalScreen`/`Static`, pytest/Pilot, Ruff, mypy strict, tach, deptry.

## Global Constraints

- Follow `docs/dev/specs/2026-08-14-operational-relationships-roadmap-design.md` Slice 3 (lines 213-234), its "Error handling and safety" list (lines 236-248), its "Blast radius" testing list (lines 277-284), and GitHub issue #283.
- **Documented deviation from that design's testing line 282** ("incomplete-graph warning in every destructive preview"): this delivery integrates the summary into `delete` and `rollout_restart` only. Scale, edit, resize, cordon/uncordon, drain, Helm, and OLM writes have no tested per-relation semantics yet — what would "scale to 0" claim about a `routes_to` backend, or `helm uninstall` about a `bound_to` PersistentVolume? — and the design's own line 225 ("only relationship types with explicit, tested action semantics participate") outranks blanket coverage. Task 5 Step 6 opens the follow-up issue for the remaining write types **before** #283 is closed; if that issue cannot be created, #283 stays open. No task, doc, or self-review line may claim the summary reaches *every* destructive preview.
- `ImpactSummary` and its calculation stay in `src/korvid/core/`; Textual imports remain confined to `src/korvid/ui/`.
- Only relationship/action combinations with tested semantics may make an impact claim. The closed sets are: `delete` → `owned_by`, `managed_by`, `routes_to`, `uses_volume`, `uses_config`, `protected_by`, `scheduled_on`, `bound_to`; `rollout_restart` → `owned_by`, `managed_by` only.
- `selects` is excluded from every action, so deleting one selected Pod never claims every Service that selects it will fail.
- Every relation in a closed set is tested against the resource pair it actually occurs between — `scheduled_on` Pod→Node, `bound_to` PVC→PV, `protected_by` Pod→PDB, `routes_to` Ingress→Service and EndpointSlice→Pod, `uses_config` workload→ConfigMap, `uses_volume` workload→PVC, `owned_by` child→owner, `managed_by` Pod→workload. A synthetic pair no extractor could ever produce proves nothing about an action's semantics.
- Traverse resolved edges only (`EdgeResolution.RESOLVED`); an unresolved target is a warning, never a dependent.
- Direct (one hop) and transitive (two or more hops) dependents stay separate, deterministically ordered, and each carries the deterministic first path that reached it.
- Traversal caps are `ImpactLimits(max_depth=3, max_nodes=50)` and are always reported when hit; snapshot truncation is reported separately.
- The impact traversal is deliberately a *second* walk, not an extension of `RelationshipGraph.walk_dependents`: it filters edges by the closed action set before walking, keeps the full path behind every dependent, filters unresolved references by the affected set it produced, and carries its own much smaller caps. A parity test pins the one thing both walks must agree on — cycle versus revisit classification on equivalent unfiltered edges.
- Cycles are classified as genuine loops only (the dependent is an ancestor on the path that reached it); a converging or parallel repeat edge is a `revisit` — counted, rendered as `additional known paths: <n>`, never a second item and never a cycle.
- Relevant unresolved warnings are **every** unresolved edge whose subject is the action target or an included impacted resource, whatever its relation: a restarted workload whose Pod mounts a missing ConfigMap must warn even though `uses_config` is not a restart relation. Unresolved edges whose subject sits outside the affected set are never reported.
- A target that is not in the snapshot — an object deleted and recreated under the same name carries a new UID — is reported as `target_present=False`, makes the summary incomplete, and renders `target not found in this snapshot - dependents unknown`. "No dependents" is never claimed for an identity the snapshot never saw.
- The snapshot scope is chosen by the target, not by the pane: a namespaced target uses the pane's namespace (or every namespace when the pane is in all-namespaces scope), and a cluster-scoped target (Node, PersistentVolume) always uses every namespace, so a cross-namespace dependent cannot be silently omitted. The same scope value is passed to the loader and recorded on the summary, and the rendered text always states it (`scope: prod` / `scope: all namespaces`) — including when coverage is `complete`, which is only ever complete *within that scope*.
- Coverage stays explicit: the summary carries the graph's `CoverageRecord`s verbatim and reports `incomplete` for any non-`complete` record, a missing target, any traversal cap, or a truncated snapshot.
- Inferred edges are labelled in the rendered text and never block a write.
- Preview text is advisory: it never claims guaranteed failure, never replaces the server dry-run, and never blocks approval.
- The impact summary cannot approve, execute, reserve, or bypass a write; the impact load is not a cluster write and takes no write reservation.
- Existing fresh-keystroke, typed-name, context-epoch, UID precondition, RBAC, server dry-run, and fail-closed audit behavior stays unchanged. Audit failure still prevents the operation factory from running.
- No Secret value and no full manifest may reach a summary, a rendered line, or the dialog.
- Cluster-controlled text (resource identity, evidence field, coverage scope) is rendered literally: control characters flattened, every fragment length-bounded, every composed line capped at `_MAX_LINE = 240` characters so a pathological cluster cannot flood the 70-column modal, and never parsed as Rich markup.
- Only `action_delete_resource` and `action_rollout_restart` gain a summary. Edit, scale, resize, cordon/uncordon, drain, helm, OLM, transfer, agent-write, and external-proposal flows stay byte-for-byte unchanged — Task 4 proves it for the scale and cordon dialogs.
- The app reuses the existing `RelationshipSnapshotLoader` — no new LIST/GET interface, no per-node GET fan-out, no new constructor parameter, no composition-root change.
- The impact load has a hard 5-second deadline (`_IMPACT_TIMEOUT = 5.0`); timeout or unexpected failure renders a static "impact unavailable; approval remains available" advisory and logs the exception *type* only. `asyncio.CancelledError` propagates untouched.
- The feature must work with `KorvidConfig(agent_enabled=False)`; no LLM/provider dependency is allowed.
- No new dependency, no `pyproject.toml` dependency edit, and no `uv lock` invocation.
- Use TDD for every task: RED on the targeted tests first, then GREEN.
- Run `uv run tach check` whenever imports cross packages.
- Commit after each task once targeted pytest, Ruff, mypy, and (when imports cross packages) tach pass.
- Every commit includes `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.
- Do not open a pull request; the final gate is `make check` plus `uv run deptry src` at the end of Task 5.

---

## File map

### New files

- `src/korvid/core/impact.py` — `ImpactAction`, `ImpactLimits`, `ImpactItem`, `ImpactSummary`, the closed per-action relation sets, and the deterministic bounded reverse traversal `summarize_impact` (its own walk, deliberately not an extension of `RelationshipGraph.walk_dependents`).
- `src/korvid/ui/impact_preview.py` — pure, Textual-free renderer: `render_impact_lines(summary) -> tuple[str, ...]`, plus the static `IMPACT_UNAVAILABLE_LINES` advisory.
- `tests/core/test_impact.py` — action semantics on realistic resource pairs, traversal, cycles/revisits (including parity with `RelationshipGraph.walk_dependents`), caps, unresolved relevance, a target absent from the snapshot, the recorded scope, immutability, and the no-Textual import probe.
- `tests/ui/test_impact_preview.py` — exact line sequence, bounding (per fragment and per composed line), literal text, scope/target-missing/revisit/coverage/cap/unresolved sections, advisory wording.
- `tests/ui/test_impact_flow.py` — app integration for `Ctrl-D` and `r`: section rendering, cluster-scoped targets covering every namespace, a target replaced since the watch, an unresolved reference outside the action's relations, missing loader, incomplete graph, timeout, loader failure, unsupported flows (scale, cordon), and post-await revalidation. Owns the shared `ImpactEnv` harness.
- `tests/ui/test_impact_security.py` — the security invariants pinned against the integrated flow (imports the harness from `tests/ui/test_impact_flow.py`).

### Modified files

- `src/korvid/ui/widgets/confirm_screen.py` — `ConfirmScreen(..., impact_lines: tuple[str, ...] | None = None)`, the `.confirm-impact` section above the dry-run preview, and its CSS rule.
- `src/korvid/ui/app.py` — `_IMPACT_TIMEOUT`, `KorvidApp._impact_scope(...)`, `KorvidApp._impact_preview(...)`, `impact_lines` pass-through on `_confirm_screen` and `_push_write_confirmation`, and the two call sites in `action_delete_resource` / `action_rollout_restart`.
- `tests/ui/test_confirm_screen.py` — impact-section rendering, ordering, literal markup, and gate-unchanged tests.
- `docs/tui.md` — a "Write impact preview" section.
- `docs/resource-relationships.md` — a "Blast radius in write previews" section.

### Signature-change blast radius (verify, do not refactor)

Every new parameter is keyword-only **with a default**, so no existing caller, controller, or fake needs an edit. The exhaustive list of surfaces that see the changed signatures — check them, change none of them:

- `ConfirmScreen.__init__` (`src/korvid/ui/widgets/confirm_screen.py:104`) is constructed in exactly one production place, `KorvidApp._confirm_screen` (`src/korvid/ui/app.py:6372`).
- `KorvidApp._confirm_screen` callers, none of which pass `impact_lines`: `src/korvid/ui/app.py:4214` (transfer upload), `:4321` (debug image retry), `:5102` (`_push_write_confirmation`), `:5155` (`_push_interactive_confirmation`), `:5889` (drain), `:7908` (external proposal review), `:8260` (agent write gate), and the `confirm_screen=lambda *a, **k: self._confirm_screen(*a, **k)` wiring at `src/korvid/ui/app.py:892` used by `src/korvid/ui/operator_controller.py:242` and `:400` (typed `Callable[..., Any]`, so unaffected).
- `KorvidApp._push_write_confirmation` (`src/korvid/ui/app.py:5069`) callers: `:5209` (delete — **passes** `impact_lines`), `:5254` (rollout restart — **passes** `impact_lines`), `:5340` (scale), `:5537` (edit), `:5669` (resize), `:5824` (cordon/uncordon), and `AppWriteGate.confirm` (`:8553`).
- `WriteGate.confirm` (`src/korvid/ui/write_gate.py:29`) is **deliberately not changed**: helm, operator, forward, and shell controllers never build an impact section, so the checked ABC signature and `AppWriteGate` stay as they are.
- `KorvidApp.__init__` is **not** changed: `list_relationship_objects` (`src/korvid/ui/app.py:710`) is already injected by the composition root (`src/korvid/__main__.py:1336`) and already builds `self._relationship_loader` (`src/korvid/ui/app.py:776`). `tests/test_main_wiring.py::_FakeApp` therefore needs no edit.
- `KorvidApp._impact_preview` (two call sites: `action_delete_resource`, `action_rollout_restart`) and `KorvidApp._impact_scope` (one call site: `_impact_preview`) are **new private methods**: no existing code calls them, so they add no blast radius of their own.
- Test modules that construct `ConfirmScreen` or drive a confirm dialog and must keep passing untouched: `tests/ui/test_confirm_screen.py` (extended by Task 3), `tests/ui/test_write_ops.py`, `tests/ui/test_dryrun_preview.py`, `tests/ui/test_write_confirm_characterization.py`, `tests/ui/test_protected_contexts.py`, `tests/ui/test_node_ops.py`, `tests/ui/test_node_shell.py`, `tests/ui/test_shell.py`, `tests/ui/test_helm_actions.py`, `tests/ui/test_olm_view.py`, `tests/ui/test_operator_uninstall.py`, `tests/ui/test_agent_write.py`, `tests/ui/test_agent_interrupt.py`, `tests/ui/test_proposals_ui.py`, `tests/ui/test_mcp_follow.py`, `tests/ui/test_resize_flow.py`, `tests/ui/test_transfer.py`, `tests/ui/test_ctx_switch.py`, `tests/ui/test_helm_view.py`.
- No `WriteOps`, `UIBridge`, or `WriteGate` fake gains a method or parameter in this plan.

---

### Task 1: Pure impact model and bounded reverse traversal

**Files:**
- Create: `src/korvid/core/impact.py`
- Create: `tests/core/test_impact.py`

**Interfaces:**
- Consumes (already exist, do not modify):
  - `korvid.core.relationships.RelationshipGraph(nodes, edges, coverage, limits, truncated)`, its `incomplete` property, and its `walk_dependents` traversal (used by the parity test only).
  - `korvid.core.relationships.RelationshipEdge(subject, target, relation, confidence, evidence, resolution, explanation="")`.
  - `korvid.core.relationships.GraphResource(group, kind, namespace, name, uid=None)`, `EvidencePointer(resource, field)`, `CoverageRecord(group, resource, scope, state, detail="")`, `CoverageState`, `EdgeResolution`.
  - `korvid.k8s.relationship_facts.RelationKind`, `korvid.k8s.relationship_facts.FactConfidence`.
- Produces:
  - `ImpactAction(StrEnum)` with exactly `DELETE = "delete"` and `ROLLOUT_RESTART = "rollout_restart"`.
  - `ACTION_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind]]`.
  - `ImpactLimits(max_depth: int = 3, max_nodes: int = 50)` — frozen, slots.
  - `_DEFAULT_LIMITS: ImpactLimits` — the module-level frozen singleton used as the keyword default (a name lookup, not a `ImpactLimits()` call in the signature; ruff B008).
  - `ImpactItem(resource: GraphResource, path: tuple[RelationshipEdge, ...])` with `inferred: bool` property — frozen, slots.
  - `ImpactSummary(action: ImpactAction, target: GraphResource, target_present: bool, scope: str | None, direct: tuple[ImpactItem, ...], transitive: tuple[ImpactItem, ...], cycles: tuple[RelationshipEdge, ...], revisits: tuple[RelationshipEdge, ...], unresolved: tuple[RelationshipEdge, ...], coverage: tuple[CoverageRecord, ...], traversal_capped: bool, graph_truncated: bool)` with `incomplete: bool` property — frozen, slots. Field order is fixed here and every construction in this plan is keyword-only.
  - Exactly this signature (the same default name appears in the code, not a re-spelled `ImpactLimits()`):

    ```python
    def summarize_impact(
        graph: RelationshipGraph,
        action: ImpactAction,
        target: GraphResource,
        *,
        scope: str | None = None,
        limits: ImpactLimits = _DEFAULT_LIMITS,
    ) -> ImpactSummary: ...
    ```

  - `scope` is the namespace the snapshot covered (`None` = every namespace). It is recorded verbatim on the summary; the model never derives it from the graph, so a namespaced snapshot can never be rendered as a cluster-wide answer.
  - `target_present` is `target in graph.nodes` — full identity including UID, so a stale/replaced target is reported instead of silently summarized as dependency-free.
- Path contract: `item.path[0].target == summary.target`, `item.path[-1].subject == item.resource`, `len(item.path) == 1` for every `direct` item and `>= 2` for every `transitive` item.
- **Deliberate duplication (do not "fix" by extending `RelationshipGraph`):** this module keeps its own traversal because a write dialog needs four things the graph screen's walk must not grow: closed per-action relation filtering *before* the walk, the full edge path behind every dependent (not just the tree edge), unresolved-reference filtering against the affected set the walk produced, and its own much smaller caps. What the two must agree on is pinned by `test_cycle_and_revisit_classification_matches_the_graph_walk`: on edges the action filter leaves untouched, a genuine loop is a cycle and a converging or parallel repeat is a revisit, in both. The same rationale is repeated in the module docstring so a future reader hits it before the diff.

- [ ] **Step 1: Write the failing core tests**

```python
# tests/core/test_impact.py
"""Deterministic blast-radius summaries over one relationship snapshot (#283).

`summarize_impact` is pure: one immutable graph plus one proposed action in,
one immutable `ImpactSummary` out. These tests pin the closed action/relation
semantics on the resource pairs those relations really occur between, the
direct/transitive split, deterministic paths, cycle versus revisit
classification (including parity with `RelationshipGraph.walk_dependents`),
both caps, a target the snapshot never saw, the recorded scope, and the
bounded unresolved-reference warning.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError

import pytest

from korvid.core.impact import (
    ACTION_RELATIONS,
    ImpactAction,
    ImpactLimits,
    summarize_impact,
)
from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    EvidencePointer,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
)
from korvid.k8s.relationship_facts import FactConfidence, RelationKind

_COMPLETE_COVERAGE = (
    CoverageRecord(group="", resource="pods", scope="prod", state=CoverageState.COMPLETE),
)


def _res(
    kind: str,
    name: str,
    *,
    group: str = "",
    namespace: str = "prod",
    uid: str | None = None,
) -> GraphResource:
    return GraphResource(group=group, kind=kind, namespace=namespace, name=name, uid=uid)


def _edge(
    subject: GraphResource,
    target: GraphResource,
    relation: RelationKind,
    *,
    confidence: FactConfidence = FactConfidence.DECLARED,
    resolution: EdgeResolution = EdgeResolution.RESOLVED,
    field: str = "metadata.ownerReferences[0]",
    evidence_resource: GraphResource | None = None,
) -> RelationshipEdge:
    """One edge, with the graph's own dependent -> dependency direction.

    `evidence_resource` defaults to the subject, which is what every
    reference-derived fact produces; selector-derived facts (`managed_by`,
    `protected_by`) keep the *declaring* object as evidence while the
    matched object becomes the subject, so those cases pass it explicitly.
    """
    return RelationshipEdge(
        subject=subject,
        target=target,
        relation=relation,
        confidence=confidence,
        evidence=EvidencePointer(resource=evidence_resource or subject, field=field),
        resolution=resolution,
    )


def _graph(
    *edges: RelationshipEdge,
    coverage: tuple[CoverageRecord, ...] = _COMPLETE_COVERAGE,
    truncated: bool = False,
    extra_nodes: tuple[GraphResource, ...] = (),
) -> RelationshipGraph:
    nodes: list[GraphResource] = []
    for resource in (*extra_nodes, *(r for edge in edges for r in (edge.subject, edge.target))):
        if resource not in nodes:
            nodes.append(resource)
    return RelationshipGraph(
        nodes=tuple(nodes), edges=tuple(edges), coverage=coverage, truncated=truncated
    )


#: One realistic edge per relation a delete may follow, built from the
#: resource pair `korvid.k8s.relationship_facts` actually produces it
#: between. A synthetic pair (every relation between the same Pod and
#: ConfigMap) would pass while proving nothing about the semantics.
_DELETE_CASES = [
    pytest.param(
        _edge(
            _res("ReplicaSet", "web-abc", group="apps", uid="rs-1"),
            _res("Deployment", "web", group="apps", uid="deploy-1"),
            RelationKind.OWNED_BY,
        ),
        id="owned_by-replicaset-owned-by-deployment",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("Deployment", "web", group="apps", uid="deploy-1"),
            RelationKind.MANAGED_BY,
            field="spec.selector",
            evidence_resource=_res("Deployment", "web", group="apps", uid="deploy-1"),
        ),
        id="managed_by-pod-managed-by-deployment",
    ),
    pytest.param(
        _edge(
            _res("Ingress", "web", group="networking.k8s.io", uid="ing-1"),
            _res("Service", "web", uid="svc-1"),
            RelationKind.ROUTES_TO,
            field="spec.rules[0].http.paths[0].backend.service",
        ),
        id="routes_to-ingress-routes-to-service",
    ),
    pytest.param(
        _edge(
            _res("EndpointSlice", "web-xyz", group="discovery.k8s.io", uid="eps-1"),
            _res("Pod", "web-abc-1", uid="pod-1"),
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[0].targetRef",
        ),
        id="routes_to-endpointslice-routes-to-pod",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("PersistentVolumeClaim", "data", uid="pvc-1"),
            RelationKind.USES_VOLUME,
            field="spec.volumes[0].persistentVolumeClaim",
        ),
        id="uses_volume-pod-uses-claim",
    ),
    pytest.param(
        _edge(
            _res("Deployment", "web", group="apps", uid="deploy-1"),
            _res("ConfigMap", "app-config", uid="cm-1"),
            RelationKind.USES_CONFIG,
            field="spec.template.spec.volumes[0].configMap",
        ),
        id="uses_config-workload-uses-configmap",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("PodDisruptionBudget", "web", group="policy", uid="pdb-1"),
            RelationKind.PROTECTED_BY,
            field="spec.selector",
            evidence_resource=_res("PodDisruptionBudget", "web", group="policy", uid="pdb-1"),
        ),
        id="protected_by-pod-protected-by-pdb",
    ),
    pytest.param(
        _edge(
            _res("Pod", "web-abc-1", uid="pod-1"),
            _res("Node", "worker-1", namespace="", uid="node-1"),
            RelationKind.SCHEDULED_ON,
            confidence=FactConfidence.OBSERVED,
            field="spec.nodeName",
        ),
        id="scheduled_on-pod-scheduled-on-node",
    ),
    pytest.param(
        _edge(
            _res("PersistentVolumeClaim", "data", uid="pvc-1"),
            _res("PersistentVolume", "pv-data", namespace="", uid="pv-1"),
            RelationKind.BOUND_TO,
            field="spec.volumeName",
        ),
        id="bound_to-claim-bound-to-volume",
    ),
]


def test_only_delete_and_rollout_restart_carry_action_semantics() -> None:
    assert [action.value for action in ImpactAction] == ["delete", "rollout_restart"]
    assert set(ACTION_RELATIONS) == set(ImpactAction)
    assert RelationKind.SELECTS not in ACTION_RELATIONS[ImpactAction.DELETE]
    assert RelationKind.SELECTS not in ACTION_RELATIONS[ImpactAction.ROLLOUT_RESTART]
    assert {edge.relation for param in _DELETE_CASES for edge in param.values} == (
        ACTION_RELATIONS[ImpactAction.DELETE]
    )


@pytest.mark.parametrize("edge", _DELETE_CASES)
def test_delete_follows_every_supported_relation(edge: RelationshipEdge) -> None:
    """Deleting the dependency reports the dependent that declared it."""
    summary = summarize_impact(_graph(edge), ImpactAction.DELETE, edge.target)
    assert [item.resource for item in summary.direct] == [edge.subject]
    assert summary.transitive == ()
    assert summary.target_present is True
    assert summary.incomplete is False


def test_delete_never_claims_a_selecting_service_fails() -> None:
    """Deleting one selected Pod must not claim the Service selecting it breaks."""
    pod = _res("Pod", "web-1")
    service = _res("Service", "web")
    summary = summarize_impact(
        _graph(_edge(service, pod, RelationKind.SELECTS, field="spec.selector")),
        ImpactAction.DELETE,
        pod,
    )
    assert summary.direct == ()
    assert summary.transitive == ()
    assert summary.unresolved == ()


@pytest.mark.parametrize("relation", [RelationKind.OWNED_BY, RelationKind.MANAGED_BY])
def test_rollout_restart_follows_owner_and_manager_relations(relation: RelationKind) -> None:
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    summary = summarize_impact(
        _graph(_edge(pod, deployment, relation)), ImpactAction.ROLLOUT_RESTART, deployment
    )
    assert [item.resource for item in summary.direct] == [pod]


@pytest.mark.parametrize(
    "relation",
    [
        RelationKind.SELECTS,
        RelationKind.ROUTES_TO,
        RelationKind.USES_VOLUME,
        RelationKind.USES_CONFIG,
        RelationKind.PROTECTED_BY,
        RelationKind.SCHEDULED_ON,
        RelationKind.BOUND_TO,
    ],
)
def test_rollout_restart_ignores_every_relation_outside_its_closed_set(
    relation: RelationKind,
) -> None:
    """Counterfactual on purpose: no extractor emits a `scheduled_on` or
    `bound_to` edge *into* a Deployment today. The closed set must hold
    anyway - if a CRD fact, a future extractor, or a hand-built summary ever
    offers one, a restart must still claim nothing about it."""
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    other = _res("Service", "web", uid="svc-1")
    summary = summarize_impact(
        _graph(_edge(other, deployment, relation, field="spec.selector")),
        ImpactAction.ROLLOUT_RESTART,
        deployment,
    )
    assert summary.direct == ()
    assert summary.transitive == ()


def test_direct_and_transitive_dependents_stay_separate_with_their_paths() -> None:
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    graph = _graph(
        _edge(replicaset, deployment, RelationKind.OWNED_BY),
        _edge(pod, replicaset, RelationKind.OWNED_BY),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, deployment)
    assert [item.resource for item in summary.direct] == [replicaset]
    assert [item.resource for item in summary.transitive] == [pod]
    assert len(summary.direct[0].path) == 1
    assert summary.transitive[0].path[0].target == deployment
    assert summary.transitive[0].path[-1].subject == pod


def test_unresolved_edges_are_never_traversed_as_dependents() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    graph = _graph(
        _edge(
            pod,
            configmap,
            RelationKind.USES_CONFIG,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].configMap",
        )
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, configmap)
    assert summary.direct == ()
    assert summary.unresolved == ()  # its subject is neither the target nor impacted


def test_unresolved_references_are_limited_to_the_affected_set() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    unrelated = _res("Pod", "unrelated-9")
    missing_claim = _res("PersistentVolumeClaim", "data", uid="pvc-gone")
    graph = _graph(
        _edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"),
        _edge(
            pod,
            missing_claim,
            RelationKind.USES_VOLUME,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[1].persistentVolumeClaim",
        ),
        _edge(
            unrelated,
            missing_claim,
            RelationKind.USES_VOLUME,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].persistentVolumeClaim",
        ),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, configmap)
    assert [item.resource for item in summary.direct] == [pod]
    assert [(edge.subject, edge.target) for edge in summary.unresolved] == [(pod, missing_claim)]


def test_unresolved_reference_declared_by_the_target_itself_is_reported() -> None:
    pod = _res("Pod", "web-1")
    missing_config = _res("ConfigMap", "gone")
    graph = _graph(
        _edge(
            pod,
            missing_config,
            RelationKind.USES_CONFIG,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].configMap",
        )
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, pod)
    assert summary.direct == ()
    assert [edge.target for edge in summary.unresolved] == [missing_config]


def test_unresolved_references_are_reported_whatever_their_relation() -> None:
    """A rolling restart follows only owner/manager edges, but the Pod it
    replaces still has to mount its ConfigMap: a dangling `uses_config`
    reference inside the affected set is exactly what makes the recreated
    Pod fail to start, so it is reported even though `uses_config` is not a
    restart relation."""
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    missing_config = _res("ConfigMap", "app-config")
    graph = _graph(
        _edge(pod, deployment, RelationKind.MANAGED_BY, field="spec.selector"),
        _edge(
            pod,
            missing_config,
            RelationKind.USES_CONFIG,
            resolution=EdgeResolution.MISSING,
            field="spec.volumes[0].configMap",
        ),
    )
    summary = summarize_impact(graph, ImpactAction.ROLLOUT_RESTART, deployment)
    assert [item.resource for item in summary.direct] == [pod]
    assert [(edge.relation, edge.target) for edge in summary.unresolved] == [
        (RelationKind.USES_CONFIG, missing_config)
    ]


def test_a_genuine_loop_is_classified_as_a_cycle_and_walked_once() -> None:
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-1")
    graph = _graph(
        _edge(replicaset, deployment, RelationKind.OWNED_BY),
        _edge(deployment, replicaset, RelationKind.OWNED_BY),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, deployment)
    assert [item.resource for item in summary.direct] == [replicaset]
    assert summary.transitive == ()
    assert [(edge.subject, edge.target) for edge in summary.cycles] == [(deployment, replicaset)]
    assert summary.revisits == ()
    assert summary.traversal_capped is False


def test_a_parallel_non_looping_edge_is_a_revisit_not_a_cycle_or_a_duplicate() -> None:
    configmap = _res("ConfigMap", "app-config", uid="cm-1")
    pod = _res("Pod", "web-1", uid="pod-1")
    first = _edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")
    second = _edge(
        pod,
        configmap,
        RelationKind.USES_CONFIG,
        field="spec.containers[0].envFrom[0].configMapRef",
    )
    summary = summarize_impact(_graph(first, second), ImpactAction.DELETE, configmap)
    assert [item.resource for item in summary.direct] == [pod]
    assert summary.cycles == ()
    assert summary.revisits == (second,)
    assert summary.traversal_capped is False


def test_the_first_graph_ordered_path_wins_and_repeats_identically() -> None:
    """Two routes reach the EndpointSlice; breadth-first order picks the
    first-reached dependent's path, the other becomes a counted revisit, and
    a second call returns the same value."""
    configmap = _res("ConfigMap", "app-config", uid="cm-1")
    pod_a = _res("Pod", "a-1", uid="pod-a")
    pod_b = _res("Pod", "b-1", uid="pod-b")
    slice_ = _res("EndpointSlice", "web-xyz", group="discovery.k8s.io", uid="eps-1")
    graph = _graph(
        _edge(pod_a, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"),
        _edge(pod_b, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"),
        _edge(
            slice_,
            pod_b,
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[1].targetRef",
        ),
        _edge(
            slice_,
            pod_a,
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[0].targetRef",
        ),
    )
    summary = summarize_impact(graph, ImpactAction.DELETE, configmap)
    assert [item.resource for item in summary.direct] == [pod_a, pod_b]
    assert [item.resource for item in summary.transitive] == [slice_]
    # `pod_a` entered the frontier first, so its route is the reported path
    # and `pod_b`'s is the revisit - regardless of the graph's edge order.
    assert summary.transitive[0].path[0].subject == pod_a
    assert summary.transitive[0].path[-1].evidence.field == "endpoints[0].targetRef"
    assert [edge.evidence.field for edge in summary.revisits] == ["endpoints[1].targetRef"]
    assert summarize_impact(graph, ImpactAction.DELETE, configmap) == summary


def test_cycle_and_revisit_classification_matches_the_graph_walk() -> None:
    """The impact walk is a deliberate second traversal (closed action
    filtering, full paths, affected-set filtering, its own much smaller
    caps). On edges the action filter leaves untouched it must still
    classify exactly like `RelationshipGraph.walk_dependents`: a genuine
    loop is a cycle, a converging or parallel repeat is a revisit, and each
    dependent is reached once, in the same breadth-first order."""
    deployment = _res("Deployment", "web", group="apps", uid="deploy-1")
    replicaset = _res("ReplicaSet", "web-abc", group="apps", uid="rs-1")
    pod = _res("Pod", "web-abc-1", uid="pod-1")
    slice_ = _res("EndpointSlice", "web-xyz", group="discovery.k8s.io", uid="eps-1")
    graph = _graph(
        _edge(replicaset, deployment, RelationKind.OWNED_BY),
        _edge(pod, deployment, RelationKind.OWNED_BY, field="metadata.ownerReferences[1]"),
        # Reached first straight from the Deployment above, so the walk
        # through the ReplicaSet converges instead of adding a second item.
        _edge(pod, replicaset, RelationKind.OWNED_BY),
        _edge(
            slice_,
            pod,
            RelationKind.ROUTES_TO,
            confidence=FactConfidence.OBSERVED,
            field="endpoints[0].targetRef",
        ),
        # A corrupted owner loop back into the traversal root.
        _edge(deployment, pod, RelationKind.OWNED_BY, field="metadata.ownerReferences[2]"),
    )
    summary = summarize_impact(
        graph, ImpactAction.DELETE, deployment, limits=ImpactLimits(max_depth=5, max_nodes=500)
    )
    walk = graph.walk_dependents(deployment, max_depth=5, max_nodes=500)
    assert summary.cycles == walk.cycles
    assert summary.revisits == walk.revisits
    assert [item.resource for item in (*summary.direct, *summary.transitive)] == [
        edge.subject for edge in walk.edges
    ]
    assert summary.traversal_capped is walk.truncated


def test_depth_cap_stops_the_walk_and_is_reported() -> None:
    root = _res("ConfigMap", "app-config")
    edges: list[RelationshipEdge] = []
    previous = root
    for index in range(5):
        current = _res("Pod", f"hop-{index}", uid=f"pod-{index}")
        edges.append(_edge(current, previous, RelationKind.OWNED_BY))
        previous = current
    summary = summarize_impact(
        _graph(*edges),
        ImpactAction.DELETE,
        root,
        limits=ImpactLimits(max_depth=2, max_nodes=50),
    )
    assert [item.resource.name for item in summary.direct] == ["hop-0"]
    assert [item.resource.name for item in summary.transitive] == ["hop-1"]
    assert summary.traversal_capped is True
    assert summary.incomplete is True


def test_node_cap_stops_the_walk_and_is_reported() -> None:
    configmap = _res("ConfigMap", "app-config")
    edges = [
        _edge(
            _res("Pod", f"web-{index}", uid=f"pod-{index}"),
            configmap,
            RelationKind.USES_CONFIG,
            field="spec.volumes[0].configMap",
        )
        for index in range(4)
    ]
    summary = summarize_impact(
        _graph(*edges),
        ImpactAction.DELETE,
        configmap,
        limits=ImpactLimits(max_depth=3, max_nodes=2),
    )
    assert [item.resource.name for item in summary.direct] == ["web-0", "web-1"]
    assert summary.traversal_capped is True
    assert summary.incomplete is True


def test_inferred_edges_are_labelled_and_still_listed() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    declared = summarize_impact(
        _graph(_edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")),
        ImpactAction.DELETE,
        configmap,
    )
    inferred = summarize_impact(
        _graph(
            _edge(
                pod,
                configmap,
                RelationKind.USES_CONFIG,
                confidence=FactConfidence.INFERRED,
                field="spec.volumes[0].configMap",
            )
        ),
        ImpactAction.DELETE,
        configmap,
    )
    assert declared.direct[0].inferred is False
    assert inferred.direct[0].inferred is True
    assert inferred.direct[0].resource == pod


def test_incomplete_is_false_only_for_a_complete_uncapped_snapshot() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    edge = _edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")
    complete = summarize_impact(_graph(edge), ImpactAction.DELETE, configmap)
    forbidden = summarize_impact(
        _graph(
            edge,
            coverage=(
                CoverageRecord(
                    group="",
                    resource="secrets",
                    scope="prod",
                    state=CoverageState.FORBIDDEN,
                    detail="secrets is forbidden",
                ),
            ),
        ),
        ImpactAction.DELETE,
        configmap,
    )
    truncated = summarize_impact(_graph(edge, truncated=True), ImpactAction.DELETE, configmap)
    assert complete.incomplete is False
    assert forbidden.incomplete is True
    assert truncated.incomplete is True
    assert truncated.graph_truncated is True


def test_a_target_missing_from_the_snapshot_is_reported_instead_of_answered() -> None:
    """A row deleted and recreated under the same name carries a new UID, so
    the identity the write targets is simply not in this graph. Reporting
    "no dependents" for it would be a lie by omission."""
    live = _res("ConfigMap", "app-config", uid="cm-1")
    replaced = _res("ConfigMap", "app-config", uid="cm-0")
    pod = _res("Pod", "web-1", uid="pod-1")
    graph = _graph(_edge(pod, live, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap"))
    stale = summarize_impact(graph, ImpactAction.DELETE, replaced)
    assert stale.target_present is False
    assert stale.direct == ()
    assert stale.transitive == ()
    assert stale.incomplete is True
    assert summarize_impact(graph, ImpactAction.DELETE, live).target_present is True


def test_the_snapshot_scope_is_recorded_verbatim() -> None:
    """The scope is the caller's, never inferred from the graph: only the
    caller knows whether it listed one namespace or all of them."""
    configmap = _res("ConfigMap", "app-config", uid="cm-1")
    graph = _graph(extra_nodes=(configmap,))
    assert summarize_impact(graph, ImpactAction.DELETE, configmap, scope="prod").scope == "prod"
    assert summarize_impact(graph, ImpactAction.DELETE, configmap).scope is None
    assert summarize_impact(graph, ImpactAction.DELETE, configmap).target_present is True


def test_summary_items_and_limits_are_immutable() -> None:
    configmap = _res("ConfigMap", "app-config")
    pod = _res("Pod", "web-1")
    summary = summarize_impact(
        _graph(_edge(pod, configmap, RelationKind.USES_CONFIG, field="spec.volumes[0].configMap")),
        ImpactAction.DELETE,
        configmap,
    )
    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        summary.traversal_capped = True  # type: ignore[misc]  # frozen by design
    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        summary.direct[0].resource = configmap  # type: ignore[misc]  # frozen by design
    with pytest.raises(FrozenInstanceError, match="cannot assign to field"):
        ImpactLimits().max_nodes = 1  # type: ignore[misc]  # frozen by design


def test_the_impact_model_imports_no_textual() -> None:
    """`korvid.core` is Textual-free (AGENTS.md layer rules)."""
    probe = (
        "import sys\n"
        "import korvid.core.impact  # noqa: F401\n"
        "leaked = [m for m in sys.modules if m == 'textual' or m.startswith('textual.')]\n"
        "if leaked:\n"
        "    raise SystemExit(f'core.impact leaked Textual: {leaked}')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/core/test_impact.py -q
```

Expected:
- Collection fails with `ModuleNotFoundError: No module named 'korvid.core.impact'`

- [ ] **Step 3: Add the pure impact model**

```python
# src/korvid/core/impact.py
"""Deterministic, advisory blast-radius summaries (issue #283).

`summarize_impact` is a pure function over one immutable `RelationshipGraph`
snapshot and one proposed destructive action. It answers a deliberately
bounded question - "which resources korvid already observed depend on this
one, through relationships whose action semantics are explicitly tested?" -
and reports everything it could not answer: a target the snapshot never
saw, unresolved references inside the affected set, relationship loops, the
namespace scope the snapshot covered, that snapshot's coverage, and both
traversal caps.

Direction is fixed by `korvid.core.relationships`: an edge is always
dependent -> dependency, so this traversal walks edges *backwards* (into
`edge.target`) and every resource it reaches is a known dependent of the
action's target.

This is deliberately a *second* traversal rather than an extension of
`RelationshipGraph.walk_dependents`, which stays the graph view's own
unfiltered walk. Four requirements here are specific to a write dialog and
would distort that shared API if pushed into it:

- edges are filtered by the closed per-action relation set *before* the
  walk, so no action can claim a relationship whose semantics are untested;
- every dependent carries the full edge path that reached it, not just the
  tree edge, so a dialog can show the chain it is claiming;
- unresolved references are filtered by the affected set the walk produced;
- the caps (`ImpactLimits`) are an order of magnitude smaller than the
  graph view's and must stay independent of them - an approval dialog is
  not a graph screen.

What the two walks must agree on is pinned by
`tests/core/test_impact.py::test_cycle_and_revisit_classification_matches_the_graph_walk`:
on edges the action filter leaves untouched, a genuine loop is a cycle and a
converging or parallel repeat is a revisit, in both.

The result is advisory. It never claims a dependent will fail, never blocks
a write, and cannot approve, execute, or reserve one: this module holds no
cluster client, imports nothing from `korvid.ui`, and returns immutable
values only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    GraphResource,
    RelationshipEdge,
    RelationshipGraph,
)
from korvid.k8s.relationship_facts import FactConfidence, RelationKind


class ImpactAction(StrEnum):
    """The destructive actions with explicit, tested relationship semantics.

    Closed on purpose: an action without a tested semantic mapping gets no
    impact claim at all rather than a plausible-looking guess.
    """

    DELETE = "delete"
    ROLLOUT_RESTART = "rollout_restart"


#: Relations a delete may follow in reverse (target -> its dependents).
#:
#: `SELECTS` is deliberately absent. A Service selecting many Pods does not
#: fail because one selected Pod is deleted, so following it would make
#: every Pod delete read as catastrophic. Every relation listed here has a
#: dedicated test in `tests/core/test_impact.py`.
_DELETE_RELATIONS: frozenset[RelationKind] = frozenset(
    {
        RelationKind.OWNED_BY,
        RelationKind.MANAGED_BY,
        RelationKind.ROUTES_TO,
        RelationKind.USES_VOLUME,
        RelationKind.USES_CONFIG,
        RelationKind.PROTECTED_BY,
        RelationKind.SCHEDULED_ON,
        RelationKind.BOUND_TO,
    }
)

#: A rolling restart replaces pods of the restarted workload; only the
#: ownership/management chain below it is affected. Nothing else - a mounted
#: ConfigMap, a routing backend, a PDB - is destroyed or detached by it.
_ROLLOUT_RESTART_RELATIONS: frozenset[RelationKind] = frozenset(
    {RelationKind.OWNED_BY, RelationKind.MANAGED_BY}
)

ACTION_RELATIONS: Mapping[ImpactAction, frozenset[RelationKind]] = {
    ImpactAction.DELETE: _DELETE_RELATIONS,
    ImpactAction.ROLLOUT_RESTART: _ROLLOUT_RESTART_RELATIONS,
}


@dataclass(frozen=True, slots=True)
class ImpactLimits:
    """Traversal caps. Smaller than the graph screen's own caps on purpose:
    an approval dialog must stay readable in one screenful."""

    max_depth: int = 3
    max_nodes: int = 50


@dataclass(frozen=True, slots=True)
class ImpactItem:
    """One known dependent plus the deterministic path that reached it."""

    resource: GraphResource
    path: tuple[RelationshipEdge, ...]

    @property
    def inferred(self) -> bool:
        """True when any hop was derived by a heuristic rather than read.

        Labelling exists so an inferred hop can be shown and discounted by
        the reader; it never blocks a write.
        """
        return any(edge.confidence is FactConfidence.INFERRED for edge in self.path)


@dataclass(frozen=True, slots=True)
class ImpactSummary:
    """One immutable, advisory blast-radius answer.

    `target_present` is False when the exact identity the action targets
    (UID included) is not a node in this snapshot - the object was replaced
    since the row was watched, or was never listed. `scope` is the
    namespace the snapshot covered, `None` meaning every namespace; it is
    the caller's value, recorded verbatim, so a namespaced snapshot can
    never be rendered as a cluster-wide answer.
    """

    action: ImpactAction
    target: GraphResource
    target_present: bool
    scope: str | None
    direct: tuple[ImpactItem, ...]
    transitive: tuple[ImpactItem, ...]
    cycles: tuple[RelationshipEdge, ...]
    revisits: tuple[RelationshipEdge, ...]
    unresolved: tuple[RelationshipEdge, ...]
    coverage: tuple[CoverageRecord, ...]
    traversal_capped: bool
    graph_truncated: bool

    @property
    def incomplete(self) -> bool:
        """True when this answer cannot be read as exhaustive.

        A target the snapshot never saw, incomplete coverage, a truncated
        snapshot, or a traversal cap all mean the same thing for a reader:
        the absence of a dependent here is not proof that none exists.
        """
        return (
            not self.target_present
            or self.traversal_capped
            or self.graph_truncated
            or any(record.state is not CoverageState.COMPLETE for record in self.coverage)
        )


#: Frozen singleton so the keyword default below is a name lookup, not a
#: call (ruff B008), while still behaving as `limits=ImpactLimits()`.
_DEFAULT_LIMITS = ImpactLimits()

#: `(items, cycles, revisits, capped)` - one traversal's whole answer.
_WalkResult = tuple[
    tuple[ImpactItem, ...], tuple[RelationshipEdge, ...], tuple[RelationshipEdge, ...], bool
]


def summarize_impact(
    graph: RelationshipGraph,
    action: ImpactAction,
    target: GraphResource,
    *,
    scope: str | None = None,
    limits: ImpactLimits = _DEFAULT_LIMITS,
) -> ImpactSummary:
    """Summarize which observed resources depend on `target` for `action`.

    Args:
        graph: One immutable relationship snapshot.
        action: The proposed destructive action.
        target: The exact resource identity (group/kind/namespace/name/uid)
            the action will run against.
        scope: The namespace the snapshot covered, or None when it covered
            every namespace. Recorded, never inferred: only the caller
            knows what it asked the loader for.
        limits: Traversal caps; both are reported when reached.

    Returns:
        An immutable `ImpactSummary`. Only resolved edges whose relation has
        tested semantics for `action` are traversed; membership of `target`
        in `graph.nodes` is checked by full identity, so a same-named
        replacement is reported as a missing target rather than summarized
        as if it were the object on screen.
    """
    relations = ACTION_RELATIONS[action]
    index = _dependents_index(graph.edges, relations)
    items, cycles, revisits, capped = _walk(index, target, limits)
    affected = {target, *(item.resource for item in items)}
    # Deliberately *not* filtered by `relations`: a dangling reference of
    # any relation held by the target or by something it takes down is a
    # real reason the action may not land the way the reader expects (a
    # restarted workload whose Pod mounts a deleted ConfigMap will not come
    # back). Only the affected set bounds this.
    unresolved = tuple(
        edge
        for edge in graph.edges
        if edge.resolution is not EdgeResolution.RESOLVED and edge.subject in affected
    )
    return ImpactSummary(
        action=action,
        target=target,
        target_present=target in graph.nodes,
        scope=scope,
        direct=tuple(item for item in items if len(item.path) == 1),
        transitive=tuple(item for item in items if len(item.path) > 1),
        cycles=cycles,
        revisits=revisits,
        unresolved=unresolved,
        coverage=graph.coverage,
        traversal_capped=capped,
        graph_truncated=graph.truncated,
    )


def _dependents_index(
    edges: Sequence[RelationshipEdge], relations: frozenset[RelationKind]
) -> dict[GraphResource, list[RelationshipEdge]]:
    """Group in-scope resolved edges by the resource they depend on.

    Indexed once per summary, so one traversal costs a single pass over the
    graph's edges no matter how many resources it reaches. The graph's own
    deterministic edge order is preserved inside each bucket.
    """
    index: dict[GraphResource, list[RelationshipEdge]] = {}
    for edge in edges:
        if edge.resolution is not EdgeResolution.RESOLVED or edge.relation not in relations:
            continue
        index.setdefault(edge.target, []).append(edge)
    return index


def _walk(
    index: Mapping[GraphResource, Sequence[RelationshipEdge]],
    target: GraphResource,
    limits: ImpactLimits,
) -> _WalkResult:
    """Breadth-first walk of dependents, excluding `target` itself.

    Each resource is reached once, by the first path breadth-first order
    offers, so the reported path is deterministic. An edge into an
    already-reached resource is classified rather than traversed: a cycle
    when the dependent is an ancestor of the resource it depends on along
    the path taken to get there, and otherwise a revisit - a converging or
    parallel repeat that is a real relationship but adds no new impacted
    resource. This mirrors `RelationshipGraph.walk_dependents`'s
    classification exactly; only the filtering, the retained paths, and the
    caps differ (see the module docstring).
    """
    paths: dict[GraphResource, tuple[RelationshipEdge, ...]] = {target: ()}
    items: list[ImpactItem] = []
    cycles: list[RelationshipEdge] = []
    revisits: list[RelationshipEdge] = []
    frontier = [target]
    depth = 0
    capped = False

    while frontier and depth < limits.max_depth and not capped:
        depth += 1
        next_frontier: list[GraphResource] = []
        for current in frontier:
            for edge in index.get(current, ()):
                if edge.subject in paths:
                    bucket = cycles if _is_ancestor(edge.subject, current, paths) else revisits
                    bucket.append(edge)
                    continue
                if len(items) >= limits.max_nodes:
                    capped = True
                    break
                path = (*paths[current], edge)
                paths[edge.subject] = path
                items.append(ImpactItem(resource=edge.subject, path=path))
                next_frontier.append(edge.subject)
            if capped:
                break
        frontier = next_frontier

    if frontier and not capped:
        depth_cycles, depth_revisits, capped = _classify_depth_frontier(index, frontier, paths)
        cycles.extend(depth_cycles)
        revisits.extend(depth_revisits)
    return tuple(items), tuple(cycles), tuple(revisits), capped


def _classify_depth_frontier(
    index: Mapping[GraphResource, Sequence[RelationshipEdge]],
    frontier: Sequence[GraphResource],
    paths: Mapping[GraphResource, tuple[RelationshipEdge, ...]],
) -> tuple[tuple[RelationshipEdge, ...], tuple[RelationshipEdge, ...], bool]:
    """Classify what sits just past the depth cap without traversing it.

    A loop or a second path back into an already-reached resource is a
    cycle or a revisit, not hidden work; only a genuinely unreached
    dependent means the cap truncated a real answer.
    """
    cycles: list[RelationshipEdge] = []
    revisits: list[RelationshipEdge] = []
    capped = False
    for current in frontier:
        for edge in index.get(current, ()):
            if edge.subject not in paths:
                capped = True
                continue
            bucket = cycles if _is_ancestor(edge.subject, current, paths) else revisits
            bucket.append(edge)
    return tuple(cycles), tuple(revisits), capped


def _is_ancestor(
    candidate: GraphResource,
    node: GraphResource,
    paths: Mapping[GraphResource, tuple[RelationshipEdge, ...]],
) -> bool:
    """True when `candidate` lies on the path that reached `node`.

    `node` itself counts: a resource depending on itself is a genuine
    one-step loop. Costs at most the depth cap, never the graph's size.
    """
    path = paths[node]
    if not path:
        return candidate == node  # `node` is the traversal root
    if candidate == path[0].target:
        return True
    return any(edge.subject == candidate for edge in path)
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/core/test_impact.py -q
uv run ruff check --fix src/korvid/core/impact.py tests/core/test_impact.py
uv run ruff format src/korvid/core/impact.py tests/core/test_impact.py
uv run mypy src/korvid/core/impact.py
uv run tach check
```

Expected:
- `pytest`: PASS (37 tests: the 9 + 2 + 7 parametrized cases plus 19 single cases)
- `ruff check --fix`: `All checks passed!`
- `ruff format`: unchanged or formatting-only changes in the two touched files
- `mypy`: `Success: no issues found in 1 source file`
- `tach check`: PASS (`korvid.core` may import `korvid.k8s`)

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/impact.py tests/core/test_impact.py
git commit -m "feat: add graph-derived impact summaries" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 2: Bounded, literal impact preview renderer

**Files:**
- Create: `src/korvid/ui/impact_preview.py`
- Create: `tests/ui/test_impact_preview.py`

**Interfaces:**
- Consumes (Task 1): `ImpactAction`, `ImpactItem`, `ImpactSummary` from `korvid.core.impact`; `CoverageState`, `GraphResource`, `RelationshipEdge` from `korvid.core.relationships`.
- Produces:
  - `render_impact_lines(summary: ImpactSummary) -> tuple[str, ...]`.
  - `IMPACT_TITLE: str` — `"graph-derived impact (advisory):"`.
  - `ADVISORY_LINE: str`.
  - `IMPACT_UNAVAILABLE_LINES: tuple[str, ...]` — the static two-line advisory the app renders when the snapshot could not be loaded.
  - `_MAX_LINE: int = 240` — the total per-line bound applied *after* a line is composed (the test imports it by name so the constant and its test cannot drift).
- Line grammar (exact, machine-defined; no cluster text may reach a heading):
  - `graph-derived impact (advisory):`
  - `  <action label> <group/kind/namespace/name>` (`delete` or `rollout restart`)
  - `  target not found in this snapshot - dependents unknown` (only when `target_present` is False, directly under the action line: it changes how every count below reads)
  - `  known direct dependents (may be affected): <n>` or `: none in this snapshot`
  - `    - <resource> via <relation> (<confidence>) at <field>[ -> ...][ [inferred]]`
  - `  known transitive dependents (may be affected): <n>` or `: none in this snapshot`
  - `  inferred relationships are labelled and never block this write` (only when some item, cycle, revisit, or unresolved reference is inferred)
  - `  relationship cycles: <n> (loop edges classified, not expanded)` (only when non-empty)
  - `  additional known paths: <n> (already-listed dependents reached again)` (only when `revisits` is non-empty: converging or parallel edges are counted, never expanded into a second item)
  - `  unresolved references in the affected set: <n>` plus `    - <subject> <relation> (<confidence>) -> <target> (<resolution>) at <field>`
  - `  scope: <namespace>` or `  scope: all namespaces` — **always**, including for `complete` coverage, which is only ever complete within that scope
  - `  graph coverage: complete` or `  graph coverage: incomplete - a missing dependent here does not prove none exists` plus `    - <group>/<resource>[ @<scope>]: <state>`
  - `  traversal capped: ...` and/or `  snapshot truncated: ...`
  - `ADVISORY_LINE`
- Bounds: at most 10 item lines per dependent section, 5 unresolved lines, 5 coverage lines, 3 rendered hops per path; every overflow adds one `    ... <n> more ... (preview capped)` line. Cluster-controlled fragments are flattened of control characters and truncated at 120 characters, and every composed line is then capped at `_MAX_LINE` (240) characters — a path line concatenates several fragments, so the per-fragment bound alone does not bound the line in a 70-column modal.

- [ ] **Step 1: Write the failing renderer tests**

```python
# tests/ui/test_impact_preview.py
"""Advisory blast-radius text for write approval dialogs (issue #283).

The renderer is pure and Textual-free: `ImpactSummary` in, bounded literal
lines out. These tests pin the exact line grammar (so nothing unexpected can
ever leak into an approval dialog), both bounds (per cluster-derived
fragment and per composed line), the scope/target-presence statements, and
the advisory wording.
"""

from __future__ import annotations

from korvid.core.impact import ImpactAction, ImpactItem, ImpactSummary
from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    EvidencePointer,
    GraphResource,
    RelationshipEdge,
)
from korvid.k8s.relationship_facts import FactConfidence, RelationKind
from korvid.ui.impact_preview import (
    _MAX_LINE,
    ADVISORY_LINE,
    IMPACT_TITLE,
    IMPACT_UNAVAILABLE_LINES,
    render_impact_lines,
)

_DEPLOY = GraphResource(group="apps", kind="Deployment", namespace="prod", name="web", uid="d-1")
_RS = GraphResource(group="apps", kind="ReplicaSet", namespace="prod", name="web-abc", uid="rs-1")
_POD = GraphResource(group="", kind="Pod", namespace="prod", name="web-abc-1", uid="pod-1")
_SECRET = GraphResource(group="", kind="Secret", namespace="prod", name="db", uid=None)

_OWNS_DEPLOY = RelationshipEdge(
    subject=_RS,
    target=_DEPLOY,
    relation=RelationKind.OWNED_BY,
    confidence=FactConfidence.DECLARED,
    evidence=EvidencePointer(resource=_RS, field="metadata.ownerReferences[0]"),
    resolution=EdgeResolution.RESOLVED,
)
_OWNS_RS = RelationshipEdge(
    subject=_POD,
    target=_RS,
    relation=RelationKind.OWNED_BY,
    confidence=FactConfidence.DECLARED,
    evidence=EvidencePointer(resource=_POD, field="metadata.ownerReferences[0]"),
    resolution=EdgeResolution.RESOLVED,
)
_FORBIDDEN_SECRETS = CoverageRecord(
    group="",
    resource="secrets",
    scope="prod",
    state=CoverageState.FORBIDDEN,
    detail="secrets is forbidden",
)


def _summary(
    *,
    direct: tuple[ImpactItem, ...] = (),
    transitive: tuple[ImpactItem, ...] = (),
    cycles: tuple[RelationshipEdge, ...] = (),
    revisits: tuple[RelationshipEdge, ...] = (),
    unresolved: tuple[RelationshipEdge, ...] = (),
    coverage: tuple[CoverageRecord, ...] = (),
    traversal_capped: bool = False,
    graph_truncated: bool = False,
    action: ImpactAction = ImpactAction.DELETE,
    target: GraphResource = _DEPLOY,
    target_present: bool = True,
    scope: str | None = "prod",
) -> ImpactSummary:
    return ImpactSummary(
        action=action,
        target=target,
        target_present=target_present,
        scope=scope,
        direct=direct,
        transitive=transitive,
        cycles=cycles,
        revisits=revisits,
        unresolved=unresolved,
        coverage=coverage,
        traversal_capped=traversal_capped,
        graph_truncated=graph_truncated,
    )


def test_render_produces_the_exact_deterministic_line_sequence() -> None:
    """Exact-match on purpose: nothing beyond identity, relation, confidence,
    evidence field, scope, and coverage state may ever reach an approval
    dialog."""
    summary = _summary(
        direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
        transitive=(ImpactItem(resource=_POD, path=(_OWNS_DEPLOY, _OWNS_RS)),),
        coverage=(_FORBIDDEN_SECRETS,),
    )
    assert render_impact_lines(summary) == (
        "graph-derived impact (advisory):",
        "  delete apps/Deployment/prod/web",
        "  known direct dependents (may be affected): 1",
        "    - apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]",
        "  known transitive dependents (may be affected): 1",
        "    - Pod/prod/web-abc-1 via owned_by (declared) at metadata.ownerReferences[0]"
        " -> owned_by (declared) at metadata.ownerReferences[0]",
        "  scope: prod",
        "  graph coverage: incomplete - a missing dependent here does not prove none exists",
        "    - core/secrets @prod: forbidden",
        ADVISORY_LINE,
    )


def test_empty_sections_and_complete_coverage_are_stated_explicitly() -> None:
    lines = render_impact_lines(
        _summary(
            coverage=(
                CoverageRecord(
                    group="", resource="pods", scope="prod", state=CoverageState.COMPLETE
                ),
            ),
            action=ImpactAction.ROLLOUT_RESTART,
        )
    )
    assert lines[0] == IMPACT_TITLE
    assert lines[1] == "  rollout restart apps/Deployment/prod/web"
    assert "  known direct dependents (may be affected): none in this snapshot" in lines
    assert "  known transitive dependents (may be affected): none in this snapshot" in lines
    assert "  graph coverage: complete" in lines
    assert lines[-1] == ADVISORY_LINE


def test_caps_and_cycles_are_reported_as_their_own_lines() -> None:
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            cycles=(_OWNS_DEPLOY,),
            traversal_capped=True,
            graph_truncated=True,
        )
    )
    assert "  relationship cycles: 1 (loop edges classified, not expanded)" in lines
    assert (
        "  traversal capped: more dependents exist beyond the traversal limits"
        " and are not listed" in lines
    )
    assert (
        "  snapshot truncated: the relationship snapshot hit an input cap, so some"
        " resources were never joined" in lines
    )


def test_revisited_paths_are_counted_never_expanded() -> None:
    """A dependent reached twice is one item plus a count, not two items:
    "2 dependents" when there is one would overstate the blast radius."""
    lines = render_impact_lines(
        _summary(direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),), revisits=(_OWNS_RS,))
    )
    assert "  known direct dependents (may be affected): 1" in lines
    assert "  additional known paths: 1 (already-listed dependents reached again)" in lines
    assert sum(1 for line in lines if line.startswith("    - apps/ReplicaSet/")) == 1


def test_the_snapshot_scope_is_always_stated_even_when_coverage_is_complete() -> None:
    """`complete` is only ever complete *within* the listed scope: a
    namespaced snapshot must not read as a cluster-wide answer."""
    namespaced = render_impact_lines(_summary(scope="prod"))
    cluster_wide = render_impact_lines(_summary(scope=None))
    assert "  scope: prod" in namespaced
    assert "  graph coverage: complete" in namespaced
    assert "  scope: all namespaces" in cluster_wide
    assert namespaced.index("  scope: prod") < namespaced.index("  graph coverage: complete")


def test_a_target_missing_from_the_snapshot_says_dependents_are_unknown() -> None:
    """Without the target in the graph, "none in this snapshot" below is a
    statement about the snapshot, not about the object being deleted."""
    lines = render_impact_lines(_summary(target_present=False))
    assert lines[2] == "  target not found in this snapshot - dependents unknown"
    assert "  known direct dependents (may be affected): none in this snapshot" in lines
    present = render_impact_lines(_summary())
    assert not any(line.startswith("  target not found") for line in present)


def test_every_composed_line_stays_within_the_total_line_bound() -> None:
    """Per-fragment caps do not bound a line that concatenates an identity
    and three hops; the modal is 70 columns wide, so the composed line is
    capped too."""
    hostile = GraphResource(group="", kind="Pod", namespace="prod", name="w" * 400)
    edge = RelationshipEdge(
        subject=hostile,
        target=_DEPLOY,
        relation=RelationKind.OWNED_BY,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=hostile, field="f" * 400),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=hostile, path=(edge, edge, edge)),),
            unresolved=(edge,),
            scope="n" * 400,
        )
    )
    assert max(len(line) for line in lines) <= _MAX_LINE
    assert lines[-1] == ADVISORY_LINE


def test_unresolved_references_are_listed_and_bounded() -> None:
    unresolved = tuple(
        RelationshipEdge(
            subject=_POD,
            target=GraphResource(
                group="", kind="ConfigMap", namespace="prod", name=f"gone-{index}"
            ),
            relation=RelationKind.USES_CONFIG,
            confidence=FactConfidence.DECLARED,
            evidence=EvidencePointer(resource=_POD, field=f"spec.volumes[{index}].configMap"),
            resolution=EdgeResolution.MISSING,
        )
        for index in range(7)
    )
    lines = render_impact_lines(_summary(unresolved=unresolved))
    assert "  unresolved references in the affected set: 7" in lines
    assert (
        "    - Pod/prod/web-abc-1 uses_config (declared) -> ConfigMap/prod/gone-0 (missing)"
        " at spec.volumes[0].configMap" in lines
    )
    assert "    ... 2 more not shown (preview capped)" in lines
    assert sum(1 for line in lines if line.startswith("    - Pod/prod/web-abc-1 uses_config")) == 5


def test_dependent_lists_are_bounded_with_an_explicit_more_line() -> None:
    items = tuple(
        ImpactItem(
            resource=GraphResource(group="", kind="Pod", namespace="prod", name=f"web-{index}"),
            path=(_OWNS_DEPLOY,),
        )
        for index in range(12)
    )
    lines = render_impact_lines(_summary(direct=items))
    assert "  known direct dependents (may be affected): 12" in lines
    assert sum(1 for line in lines if line.startswith("    - Pod/prod/web-")) == 10
    assert "    ... 2 more not shown (preview capped)" in lines


def test_inferred_items_are_labelled_and_declared_never_blocks() -> None:
    inferred_edge = RelationshipEdge(
        subject=_POD,
        target=_DEPLOY,
        relation=RelationKind.MANAGED_BY,
        confidence=FactConfidence.INFERRED,
        evidence=EvidencePointer(resource=_POD, field="spec.selector"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(direct=(ImpactItem(resource=_POD, path=(inferred_edge,)),))
    )
    assert "    - Pod/prod/web-abc-1 via managed_by (inferred) at spec.selector [inferred]" in lines
    assert "  inferred relationships are labelled and never block this write" in lines


def test_cluster_text_stays_literal_and_control_characters_are_flattened() -> None:
    hostile = GraphResource(group="", kind="Pod", namespace="prod", name="[bold red]web\nrogue[/]")
    edge = RelationshipEdge(
        subject=hostile,
        target=_DEPLOY,
        relation=RelationKind.OWNED_BY,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=hostile, field="metadata\townerReferences[0]"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(_summary(direct=(ImpactItem(resource=hostile, path=(edge,)),)))
    body = "\n".join(lines)
    assert "[bold red]web rogue[/]" in body
    assert "metadata ownerReferences[0]" in body
    assert not any("\n" in line or "\t" in line for line in lines)


def test_no_line_claims_a_guaranteed_failure_and_the_advisory_is_always_last() -> None:
    lines = render_impact_lines(
        _summary(
            direct=(ImpactItem(resource=_RS, path=(_OWNS_DEPLOY,)),),
            coverage=(_FORBIDDEN_SECRETS,),
            traversal_capped=True,
        )
    )
    body = " ".join(lines).lower()
    for claim in ("will fail", "will break", "guaranteed", "definitely", "certain to"):
        assert claim not in body
    assert "advisory" in lines[0]
    assert lines[-1] == ADVISORY_LINE
    assert "never a block on approval" in ADVISORY_LINE


def test_secret_identity_is_rendered_without_any_value_field() -> None:
    edge = RelationshipEdge(
        subject=_POD,
        target=_SECRET,
        relation=RelationKind.USES_CONFIG,
        confidence=FactConfidence.DECLARED,
        evidence=EvidencePointer(resource=_POD, field="spec.volumes[0].secret.secretName"),
        resolution=EdgeResolution.RESOLVED,
    )
    lines = render_impact_lines(
        _summary(direct=(ImpactItem(resource=_POD, path=(edge,)),), target=_SECRET)
    )
    assert lines[1] == "  delete Secret/prod/db"
    assert (
        "    - Pod/prod/web-abc-1 via uses_config (declared) at"
        " spec.volumes[0].secret.secretName" in lines
    )
    assert not any("data" in line and "=" in line for line in lines)


def test_unavailable_lines_are_static_and_keep_approval_available() -> None:
    assert IMPACT_UNAVAILABLE_LINES == (
        IMPACT_TITLE,
        "  impact unavailable; approval remains available",
    )
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_impact_preview.py -q
```

Expected:
- Collection fails with `ModuleNotFoundError: No module named 'korvid.ui.impact_preview'`

- [ ] **Step 3: Add the renderer**

```python
# src/korvid/ui/impact_preview.py
"""Bounded, literal text for the advisory blast-radius section (issue #283).

Pure and Textual-free on purpose: `ImpactSummary` in, `tuple[str, ...]` out,
so the exact wording an approval dialog shows is testable without a Pilot and
cannot drift between the dialog and the tests.

Every heading is machine-defined here; the only cluster-derived text that
reaches a line is a resource identity, a relation/confidence/resolution enum
value, an evidence field path, and a namespace/coverage scope - each
flattened of control characters and length-capped, with the composed line
capped again at `_MAX_LINE` because one line concatenates several of them.
Nothing is formatted as Rich markup: `ConfirmScreen` appends these lines to
a `rich.text.Text`, so a resource named `[bold red]web[/]` renders
literally.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from korvid.core.impact import ImpactAction, ImpactItem, ImpactSummary
from korvid.core.relationships import CoverageState, GraphResource, RelationshipEdge
from korvid.k8s.relationship_facts import FactConfidence

IMPACT_TITLE = "graph-derived impact (advisory):"

#: Always the last line: the summary describes known relationships, not a
#: prediction, and it never gates the approval the user asked for.
ADVISORY_LINE = (
    "  advisory only: known relationships from one bounded snapshot - not a prediction of"
    " failure, no replacement for the server dry-run, and never a block on approval."
)

#: What the app renders when the snapshot could not be loaded at all (a
#: timeout or an unexpected failure). Static text: an exception message can
#: embed a response body, which must never reach the dialog.
IMPACT_UNAVAILABLE_LINES: tuple[str, ...] = (
    IMPACT_TITLE,
    "  impact unavailable; approval remains available",
)

_DIRECT_TITLE = "known direct dependents (may be affected)"
_TRANSITIVE_TITLE = "known transitive dependents (may be affected)"
_COVERAGE_COMPLETE_LINE = "  graph coverage: complete"
_COVERAGE_INCOMPLETE_LINE = (
    "  graph coverage: incomplete - a missing dependent here does not prove none exists"
)
_TRAVERSAL_CAPPED_LINE = (
    "  traversal capped: more dependents exist beyond the traversal limits and are not listed"
)
_SNAPSHOT_TRUNCATED_LINE = (
    "  snapshot truncated: the relationship snapshot hit an input cap, so some resources"
    " were never joined"
)
_INFERRED_NOTE_LINE = "  inferred relationships are labelled and never block this write"
_TARGET_MISSING_LINE = "  target not found in this snapshot - dependents unknown"
_ALL_NAMESPACES_LABEL = "all namespaces"

_ACTION_LABEL = {
    ImpactAction.DELETE: "delete",
    ImpactAction.ROLLOUT_RESTART: "rollout restart",
}

#: Hard output bounds: an approval dialog must stay reviewable, and a
#: pathological cluster must not be able to grow it.
_MAX_ITEM_LINES = 10
_MAX_UNRESOLVED_LINES = 5
_MAX_COVERAGE_LINES = 5
_MAX_PATH_HOPS = 3
_MAX_TEXT = 120
#: Total bound per composed line. `_MAX_TEXT` bounds each cluster-derived
#: *fragment*, but an item line concatenates an identity and up to three
#: hops; the dialog body is 70 columns wide, so a line that would wrap into
#: a screenful on its own is truncated here instead.
_MAX_LINE = 240

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def render_impact_lines(summary: ImpactSummary) -> tuple[str, ...]:
    """Render one `ImpactSummary` as bounded, literal preview lines."""
    lines = [
        IMPACT_TITLE,
        f"  {_ACTION_LABEL[summary.action]} {_resource_label(summary.target)}",
    ]
    if not summary.target_present:
        # Directly under the action line: every count below is about the
        # snapshot, and without the target in it they say nothing about
        # the object the user is about to act on.
        lines.append(_TARGET_MISSING_LINE)
    lines.extend(_section(_DIRECT_TITLE, summary.direct))
    lines.extend(_section(_TRANSITIVE_TITLE, summary.transitive))
    lines.extend(_inferred_lines(summary))
    lines.extend(_cycle_lines(summary))
    lines.extend(_revisit_lines(summary))
    lines.extend(_unresolved_lines(summary))
    lines.append(_scope_line(summary))
    lines.extend(_coverage_lines(summary))
    lines.extend(_cap_lines(summary))
    lines.append(ADVISORY_LINE)
    return tuple(line[:_MAX_LINE] for line in lines)


def _section(title: str, items: Sequence[ImpactItem]) -> list[str]:
    """One dependent section: an explicit count, bounded rows, an overflow
    note. "none in this snapshot" is information - distinct from a section
    that was omitted."""
    if not items:
        return [f"  {title}: none in this snapshot"]
    lines = [f"  {title}: {len(items)}"]
    lines.extend(_item_line(item) for item in items[:_MAX_ITEM_LINES])
    if len(items) > _MAX_ITEM_LINES:
        lines.append(f"    ... {len(items) - _MAX_ITEM_LINES} more not shown (preview capped)")
    return lines


def _item_line(item: ImpactItem) -> str:
    hops = " -> ".join(_hop(edge) for edge in item.path[:_MAX_PATH_HOPS])
    if len(item.path) > _MAX_PATH_HOPS:
        hops = f"{hops} -> ... {len(item.path) - _MAX_PATH_HOPS} more hops"
    marker = " [inferred]" if item.inferred else ""
    return f"    - {_resource_label(item.resource)} via {hops}{marker}"


def _hop(edge: RelationshipEdge) -> str:
    return f"{edge.relation.value} ({edge.confidence.value}) at {_safe(edge.evidence.field)}"


def _inferred_lines(summary: ImpactSummary) -> list[str]:
    """Whether any edge anywhere in this summary was heuristically derived.

    Checked across every edge collection the summary carries - not just the
    listed dependent paths - because a cycle, a revisit, or an unresolved
    reference renders its own line regardless of confidence, and an inferred
    edge folded into one of those must still surface the same warning an
    inferred dependent path gets.
    """
    listed_inferred = any(item.inferred for item in (*summary.direct, *summary.transitive))
    aggregate_inferred = any(
        edge.confidence is FactConfidence.INFERRED
        for edge in (*summary.cycles, *summary.revisits, *summary.unresolved)
    )
    if listed_inferred or aggregate_inferred:
        return [_INFERRED_NOTE_LINE]
    return []


def _cycle_lines(summary: ImpactSummary) -> list[str]:
    if not summary.cycles:
        return []
    return [f"  relationship cycles: {len(summary.cycles)} (loop edges classified, not expanded)"]


def _revisit_lines(summary: ImpactSummary) -> list[str]:
    """Converging or parallel edges into an already-listed dependent.

    Counted, never expanded: each dependent is listed once with the first
    path that reached it, and this line says how many further known paths
    the summary folded away - so "1 dependent" cannot be misread as "only
    one relationship".
    """
    if not summary.revisits:
        return []
    return [
        f"  additional known paths: {len(summary.revisits)}"
        " (already-listed dependents reached again)"
    ]


def _scope_line(summary: ImpactSummary) -> str:
    """The namespace this snapshot covered, always stated.

    `graph coverage: complete` means complete *within this scope*; a
    namespaced snapshot that never listed another namespace must not read
    as a cluster-wide answer.
    """
    scope = _ALL_NAMESPACES_LABEL if summary.scope is None else _safe(summary.scope)
    return f"  scope: {scope}"


def _unresolved_lines(summary: ImpactSummary) -> list[str]:
    if not summary.unresolved:
        return []
    lines = [f"  unresolved references in the affected set: {len(summary.unresolved)}"]
    lines.extend(_unresolved_line(edge) for edge in summary.unresolved[:_MAX_UNRESOLVED_LINES])
    if len(summary.unresolved) > _MAX_UNRESOLVED_LINES:
        omitted = len(summary.unresolved) - _MAX_UNRESOLVED_LINES
        lines.append(f"    ... {omitted} more not shown (preview capped)")
    return lines


def _unresolved_line(edge: RelationshipEdge) -> str:
    """One dangling reference, with its own confidence next to its relation.

    A cycle or a revisit only ever gets the aggregate `_INFERRED_NOTE_LINE`
    because those are counted, never individually listed; an unresolved
    reference *is* individually listed, so its confidence goes right after
    the relation - matching `_hop`'s `relation (confidence) at field`
    grammar - rather than folding an inferred one into that same generic
    note with no way to tell which listed reference was heuristic.
    """
    return (
        f"    - {_resource_label(edge.subject)} {edge.relation.value}"
        f" ({edge.confidence.value}) -> {_resource_label(edge.target)}"
        f" ({edge.resolution.value}) at {_safe(edge.evidence.field)}"
    )


def _coverage_lines(summary: ImpactSummary) -> list[str]:
    incomplete = [
        record for record in summary.coverage if record.state is not CoverageState.COMPLETE
    ]
    if not incomplete:
        return [_COVERAGE_COMPLETE_LINE]
    lines = [_COVERAGE_INCOMPLETE_LINE]
    for record in incomplete[:_MAX_COVERAGE_LINES]:
        scope = f" @{_safe(record.scope)}" if record.scope else ""
        target = f"{_safe(record.group or 'core')}/{_safe(record.resource)}"
        lines.append(f"    - {target}{scope}: {record.state.value}")
    if len(incomplete) > _MAX_COVERAGE_LINES:
        omitted = len(incomplete) - _MAX_COVERAGE_LINES
        lines.append(f"    ... {omitted} more coverage records not shown (preview capped)")
    return lines


def _cap_lines(summary: ImpactSummary) -> list[str]:
    lines: list[str] = []
    if summary.traversal_capped:
        lines.append(_TRAVERSAL_CAPPED_LINE)
    if summary.graph_truncated:
        lines.append(_SNAPSHOT_TRUNCATED_LINE)
    return lines


def _resource_label(resource: GraphResource) -> str:
    """`group/kind/namespace/name`, blank parts dropped (the graph screen's
    own convention), flattened and capped."""
    parts = (resource.group, resource.kind, resource.namespace, resource.name)
    return _safe("/".join(part for part in parts if part))


def _safe(text: str) -> str:
    """Flatten control characters (including newlines/tabs) and cap length,
    so cluster-controlled text can neither break the dialog layout nor grow
    the preview unboundedly."""
    return _CONTROL_CHARS.sub(" ", text)[:_MAX_TEXT]
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_impact_preview.py tests/core/test_impact.py -q
uv run ruff check --fix src/korvid/ui/impact_preview.py tests/ui/test_impact_preview.py
uv run ruff format src/korvid/ui/impact_preview.py tests/ui/test_impact_preview.py
uv run mypy src/korvid/ui/impact_preview.py
uv run tach check
```

Expected:
- `pytest`: PASS (51 tests: the 14 renderer tests plus Task 1's 37, which must not regress — the renderer's exact-sequence test and the model share every field name)
- `ruff check --fix`: `All checks passed!`
- `ruff format`: unchanged or formatting-only changes in the two touched files
- `mypy`: `Success: no issues found in 1 source file`
- `tach check`: PASS (`korvid.ui` may import `korvid.core`)

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/impact_preview.py tests/ui/test_impact_preview.py
git commit -m "feat: add advisory impact preview renderer" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: ConfirmScreen impact section

**Files:**
- Modify: `src/korvid/ui/widgets/confirm_screen.py` (CSS block at `:23-50`, `__init__` at `:104-132`, `compose` at `:134-166`)
- Modify: `tests/ui/test_confirm_screen.py` (append the new tests)

**Interfaces:**
- Consumes (Task 2): nothing at import time — the screen receives already-rendered lines, so the widget stays independent of `ImpactSummary`.
- Produces:
  - `ConfirmScreen.__init__(..., impact_lines: tuple[str, ...] | None = None)` — a new trailing keyword-only parameter with a default; every existing call site keeps working unchanged.
  - A `Static` with CSS class `confirm-impact`, mounted **above** the `confirm-preview` widget and below `confirm-operation` / `confirm-managed`.
  - `ConfirmScreen._impact_text() -> Text` — builds the section as a `rich.text.Text` by appending, never by parsing markup.
- Unchanged and pinned by the tests below: `created_time` stale-key cutoff, the `y`/`n` gate, the typed-name (`require_name`) gate, the protected-context gate, and the existing `confirm-preview` rendering.

- [ ] **Step 1: Write the failing ConfirmScreen tests**

```python
# tests/ui/test_confirm_screen.py  (append at the end of the file)

# ---------------------------------------------------------------------------
# Graph-derived impact section (issue #283)
# ---------------------------------------------------------------------------

_IMPACT_LINES = (
    "graph-derived impact (advisory):",
    "  delete apps/Deployment/prod/web",
    "  known direct dependents (may be affected): 1",
    "    - apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]",
    "  graph coverage: complete",
)


async def test_impact_section_renders_above_the_dry_run_preview() -> None:
    """The advisory section is additional context for the dry-run diff, so it
    must read before it, not replace or follow it."""
    from textual.containers import VerticalScroll

    app = HostApp()
    async with app.run_test() as pilot:
        screen = ConfirmScreen(
            "Delete deployments/web?",
            "DELETE apps/deployments/web in prod",
            preview=["- apps/Deployment prod/web"],
            impact_lines=_IMPACT_LINES,
        )
        await app.push_screen(screen)
        await pilot.pause()
        children = list(screen.query_one(VerticalScroll).children)
        impact = screen.query_one(".confirm-impact", Static)
        preview = screen.query_one(".confirm-preview", Static)
        rendered = str(impact.render())
        assert children.index(impact) < children.index(preview)
        assert "graph-derived impact (advisory):" in rendered
        assert "known direct dependents (may be affected): 1" in rendered
        assert "graph coverage: complete" in rendered
        assert "dry-run" in str(preview.render())


async def test_no_impact_widget_without_impact_lines() -> None:
    """No snapshot means no section at all - distinct from an empty one."""
    app = HostApp()
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen("Delete pod", "DELETE pods/web-1", preview=["- pod prod/web-1"])
        )
        await pilot.pause()
        assert not app.screen.query(".confirm-impact")


async def test_impact_lines_render_cluster_markup_literally() -> None:
    """A resource named `[bold red]web[/]` must not style the dialog."""
    app = HostApp()
    async with app.run_test() as pilot:
        screen = ConfirmScreen(
            "Delete deployments/web?",
            "DELETE apps/deployments/web in prod",
            impact_lines=(
                "graph-derived impact (advisory):",
                "    - Pod/prod/[bold red]web[/] via owned_by (declared) at"
                " metadata.ownerReferences[0]",
            ),
        )
        await app.push_screen(screen)
        await pilot.pause()
        rendered = str(screen.query_one(".confirm-impact", Static).render())
        assert "[bold red]web[/]" in rendered


async def test_impact_section_does_not_relax_the_typed_name_gate() -> None:
    """An impact section is context, not consent: the typed-name gate still
    owns the decision."""
    app = HostApp()
    results: list[bool | None] = []
    async with app.run_test() as pilot:
        await app.push_screen(
            ConfirmScreen(
                "Delete node",
                "delete nodes/worker-1",
                require_name="worker-1",
                impact_lines=_IMPACT_LINES,
            ),
            results.append,
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert results == []
        for ch in "worker-1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert results == [True]


async def test_impact_section_does_not_relax_the_stale_key_cutoff() -> None:
    """A `y` created while the impact snapshot was loading predates the
    dialog and must never approve it."""
    from textual import events

    app = HostApp()
    results: list[bool | None] = []
    async with app.run_test() as pilot:
        stale = events.Key("y", "y")
        await pilot.pause()
        dialog = ConfirmScreen(
            "Delete deployments/web?",
            "DELETE apps/deployments/web in prod",
            impact_lines=_IMPACT_LINES,
        )
        await app.push_screen(dialog, results.append)
        await pilot.pause()
        dialog.post_message(stale)
        await pilot.pause()
        assert results == []
        await pilot.press("y")
        await pilot.pause()
        assert results == [True]
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_confirm_screen.py -q
```

Expected:
- 4 failures with `TypeError: ConfirmScreen.__init__() got an unexpected keyword argument 'impact_lines'` — the four tests that pass `impact_lines`
- `test_no_impact_widget_without_impact_lines` already passes: it asserts an absence and constructs no new keyword
- every pre-existing test in the file still passes

- [ ] **Step 3: Add the impact section to ConfirmScreen**

Add the CSS rule to `_DIALOG_CSS` (after the `.confirm-managed` rule):

```css
ConfirmScreen .confirm-impact {
    color: $text-muted;
}
```

Extend the class docstring (after the `managed_note` paragraph):

```text
    ``impact_lines`` (issue #283) renders a graph-derived blast-radius
    section above the dry-run preview: which observed resources depend on
    this one, how completely korvid could see the cluster, and where the
    traversal stopped. Advisory only - it is already-rendered text, carries
    no decision, and changes no gate. The dialog never parses it as Rich
    markup, so a resource name containing markup stays literal.
```

Extend `__init__`:

```python
    def __init__(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        protected_context: str | None = None,
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._operation = operation
        self._require_name = require_name
        self._preview = preview
        self._preview_title = preview_title
        self._protected_context = protected_context
        self._managed_note = managed_note
        self._impact_lines = impact_lines
```

Insert the section in `compose`, between the `managed_note` banner and the preview:

```python
            if self._impact_lines:
                yield Static(self._impact_text(), classes="confirm-impact")
            if self._preview is not None:
                yield Static(self._preview_text(), classes="confirm-preview")
```

Add the renderer beside `_preview_text`:

```python
    def _impact_text(self) -> Text:
        """Graph-derived blast radius (issue #283), one line per fact.

        Built by appending to a `Text` rather than parsing markup: the lines
        embed cluster-controlled names and evidence paths, and a resource
        called `[bold red]web[/]` must render literally instead of styling
        (or silently disappearing from) an approval dialog.
        """
        lines = self._impact_lines or ()
        if not lines:
            return Text()
        text = Text(lines[0], style="bold")
        for line in lines[1:]:
            text.append(f"\n{line}")
        return text
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_confirm_screen.py tests/ui/test_dryrun_preview.py tests/ui/test_protected_contexts.py tests/ui/test_write_confirm_characterization.py -q
uv run ruff check --fix src/korvid/ui/widgets/confirm_screen.py tests/ui/test_confirm_screen.py
uv run ruff format src/korvid/ui/widgets/confirm_screen.py tests/ui/test_confirm_screen.py
uv run mypy src/korvid/ui/widgets/confirm_screen.py
```

Expected:
- `pytest`: PASS (new impact tests plus every pre-existing confirm/preview/protected/characterization test)
- `ruff check --fix`: `All checks passed!`
- `ruff format`: unchanged or formatting-only changes in the two touched files
- `mypy`: `Success: no issues found in 1 source file`

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/widgets/confirm_screen.py tests/ui/test_confirm_screen.py
git commit -m "feat: show a graph-derived impact section in write confirmations" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: App integration for delete and rollout restart

**Files:**
- Modify: `src/korvid/ui/app.py` (constant beside `_PREVIEW_TIMEOUT` at `:266`; new `_impact_scope` + `_impact_preview` after `_dry_run_preview` at `:5059-5067`; `_push_write_confirmation` at `:5069-5111`; `action_delete_resource` at `:5157-5220`; `action_rollout_restart` at `:5222-5267`; `_confirm_screen` at `:6372-6394`)
- Create: `tests/ui/test_impact_flow.py`

**Interfaces:**
- Consumes:
  - `korvid.core.impact.ImpactAction`, `korvid.core.impact.summarize_impact` (Task 1).
  - `korvid.ui.impact_preview.render_impact_lines`, `korvid.ui.impact_preview.IMPACT_UNAVAILABLE_LINES` (Task 2).
  - `ConfirmScreen(..., impact_lines=...)` (Task 3).
  - Existing, unchanged: `KorvidApp._relationship_loader` (`RelationshipSnapshotLoader`, built at `src/korvid/ui/app.py:776`), `RelationshipSnapshotLoader.load(root, namespace, aliases)` (which already lists cluster-scoped kinds cluster-wide and namespaced kinds in `namespace`, or everywhere when it is `None`), `korvid.core.relationships.GraphResource`, `korvid.core.store.ALL_NAMESPACES`, `KorvidApp._pane.scope`, `KorvidApp._write_context_intact`, `KorvidApp._write_target`, `KorvidApp._precheck_keybinding_write`, `KorvidApp._managed_note`, `KorvidApp._dry_run_preview`.
- Produces:
  - `_IMPACT_TIMEOUT: float = 5.0` module constant in `src/korvid/ui/app.py`.
  - `KorvidApp._impact_scope(self, meta: ResourceMeta) -> str | None` — the one place that decides the snapshot's namespace: the pane's scope for a namespaced target, `None` (every namespace) for a cluster-scoped one or an all-namespaces pane. Its return value is passed to **both** `loader.load(...)` and `summarize_impact(..., scope=...)`, so the rendered scope can never disagree with what was listed.
  - `KorvidApp._impact_preview(self, action: ImpactAction, meta: ResourceMeta, ns: str | None, name: str, uid: str | None) -> tuple[str, ...] | None`.
  - `KorvidApp._push_write_confirmation(..., impact_lines: tuple[str, ...] | None = None)`.
  - `KorvidApp._confirm_screen(..., impact_lines: tuple[str, ...] | None = None)`.
  - `tests/ui/test_impact_flow.py::ImpactEnv`, `::RecordingLister`, `::RecordingOps`, `::to_view`, `::open_delete_dialog`, `::impact_text`, `::CATALOG_ALIASES` — the shared harness Task 5 imports by name.
- Ordering contract inside both actions: RBAC pre-check → dry-run preview → managed note → existing `_write_context_intact(phase="the dry-run preview")` → impact load → **new** `_write_context_intact(phase="the impact summary")` → dialog.

- [ ] **Step 1: Write the failing app-integration tests**

```python
# tests/ui/test_impact_flow.py
"""Graph-derived impact previews in the delete/rollout-restart flows (#283).

The app reuses the relationship snapshot loader it already owns for `g`: no
new LIST/GET interface, no new constructor parameter, no composition-root
change. What this module pins beyond "the section renders" is the wiring
that only shows up end to end: the snapshot's scope is chosen by the target
(cluster-scoped kinds cover every namespace), the row's exact UID reaches
the summary, unsupported write flows are untouched, and both awaited gaps
still revalidate. This module owns the shared harness (`ImpactEnv`) that
`tests/ui/test_impact_security.py` reuses for the security invariants.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any
from unittest import mock

from textual.widgets import Static

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta, build_alias_map
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    SelectorFact,
    TargetReference,
)
from korvid.k8s.selectors import LabelSelector
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

#: The loader's fixed source catalog, so a test snapshot's only non-complete
#: coverage records are the ones a test asks for (plus the always-absent
#: Gateway API group).
CATALOG_METAS = [
    ResourceMeta("Pod", "pods", "", "v1", True, ("po",)),
    ResourceMeta("Service", "services", "", "v1", True, ("svc",)),
    ResourceMeta("ConfigMap", "configmaps", "", "v1", True, ("cm",)),
    ResourceMeta("Secret", "secrets", "", "v1", True),
    ResourceMeta("PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True, ("pvc",)),
    ResourceMeta("PersistentVolume", "persistentvolumes", "", "v1", False, ("pv",)),
    ResourceMeta("Node", "nodes", "", "v1", False, ("no",)),
    ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",)),
    ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True, ("rs",)),
    ResourceMeta("StatefulSet", "statefulsets", "apps", "v1", True, ("sts",)),
    ResourceMeta("DaemonSet", "daemonsets", "apps", "v1", True, ("ds",)),
    ResourceMeta("Job", "jobs", "batch", "v1", True),
    ResourceMeta("CronJob", "cronjobs", "batch", "v1", True, ("cj",)),
    ResourceMeta("EndpointSlice", "endpointslices", "discovery.k8s.io", "v1", True),
    ResourceMeta("Ingress", "ingresses", "networking.k8s.io", "v1", True, ("ing",)),
    ResourceMeta("PodDisruptionBudget", "poddisruptionbudgets", "policy", "v1", True, ("pdb",)),
]
CATALOG_ALIASES = build_alias_map(CATALOG_METAS)


def _owner(kind: str, name: str, uid: str, *, group: str) -> ReferenceFact:
    return ReferenceFact(
        relation=RelationKind.OWNED_BY,
        target=TargetReference(group=group, kind=kind, namespace="prod", name=name, uid=uid),
        confidence=FactConfidence.DECLARED,
        field="metadata.ownerReferences[0]",
    )


def _deployment(name: str, uid: str) -> GenericSummary:
    return GenericSummary(
        name=name, namespace="prod", kind="Deployment", created="", desired=3, uid=uid
    )


def _replicaset() -> GenericSummary:
    return GenericSummary(
        name="web-abc",
        namespace="prod",
        kind="ReplicaSet",
        created="",
        uid="rs-1",
        relationships=RelationshipFacts(
            api_group="apps",
            references=(_owner("Deployment", "web", "deploy-1", group="apps"),),
        ),
    )


def _pod() -> PodSummary:
    return PodSummary(
        name="web-abc-1",
        namespace="prod",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-1",
        labels=(("app", "web"),),
        relationships=RelationshipFacts(
            references=(
                _owner("ReplicaSet", "web-abc", "rs-1", group="apps"),
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference(
                        group="", kind="ConfigMap", namespace="prod", name="app-config"
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="spec.volumes[0].configMap",
                ),
            )
        ),
    )


def _configmap() -> GenericSummary:
    return GenericSummary(
        name="app-config", namespace="prod", kind="ConfigMap", created="", uid="cm-1"
    )


def _service() -> GenericSummary:
    """A Service selecting the Pod: deleting that Pod must never claim the
    Service fails (`selects` has no action semantics)."""
    return GenericSummary(
        name="web",
        namespace="prod",
        kind="Service",
        created="",
        uid="svc-1",
        relationships=RelationshipFacts(
            selectors=(
                SelectorFact(
                    relation=RelationKind.SELECTS,
                    target_group="",
                    target_kind="Pod",
                    selector=LabelSelector(match_labels=(("app", "web"),), present=True),
                    confidence=FactConfidence.DECLARED,
                    field="spec.selector",
                ),
            )
        ),
    )


def _node() -> GenericSummary:
    """A cluster-scoped Node: `namespace` is always empty for these."""
    return GenericSummary(name="worker-1", namespace="", kind="Node", created="", uid="node-1")


def _scheduled_pod(name: str, namespace: str, uid: str) -> PodSummary:
    """A Pod running on `worker-1`, in whichever namespace it belongs to."""
    return PodSummary(
        name=name,
        namespace=namespace,
        phase="Running",
        ready="1/1",
        restarts=0,
        node="worker-1",
        uid=uid,
        relationships=RelationshipFacts(
            references=(
                ReferenceFact(
                    relation=RelationKind.SCHEDULED_ON,
                    target=TargetReference(group="", kind="Node", namespace="", name="worker-1"),
                    confidence=FactConfidence.OBSERVED,
                    field="spec.nodeName",
                ),
            )
        ),
    )


class RecordingLister:
    """Replays snapshot LIST results by plural; records order and calls.

    A real LIST is namespace-scoped, so this one is too: a request for
    namespace `prod` returns only `prod` rows. Without that, a snapshot
    wrongly scoped to one namespace would still see every namespace here
    and the scope tests would prove nothing.
    """

    def __init__(self, rows: dict[str, list[Any]], order: list[str]) -> None:
        self._rows = rows
        self._order = order
        self.calls: list[tuple[str, str | None]] = []
        self.errors: dict[str, Exception] = {}
        self.delay = 0.0
        #: Fired once, inside the first LIST: how a test simulates a context
        #: switch or selection change landing while the snapshot is loading.
        self.on_first_call: Callable[[], None] | None = None

    async def __call__(self, meta: ResourceMeta, namespace: str | None) -> list[Any]:
        self.calls.append((meta.plural, namespace))
        if not self._order or self._order[-1] != "list":
            self._order.append("list")
        hook, self.on_first_call = self.on_first_call, None
        if hook is not None:
            hook()
        if self.delay:
            await asyncio.sleep(self.delay)
        if meta.plural in self.errors:
            raise self.errors[meta.plural]
        rows = self._rows.get(meta.plural, [])
        if namespace is None:
            return list(rows)
        return [row for row in rows if row.namespace == namespace]


class RecordingOps(WriteOps):
    """Records mutations and dry-run previews; performs none."""

    def __init__(self, order: list[str]) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._order = order

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("delete", meta.plural, namespace, name, uid))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("scale", meta.plural, namespace, name, replicas))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.calls.append(("restart", meta.plural, namespace, name, uid))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self.calls.append(("replace", meta.plural, namespace, name))

    async def preview_delete(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> list[str] | None:
        self._order.append("preview")
        return [f"- {meta.plural} prod/{name}"]

    async def preview_rollout_restart(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        *,
        uid: str | None = None,
        restarted_at: str | None = None,
    ) -> list[str] | None:
        self._order.append("preview")
        return ["~ spec.template.metadata.annotations.kubectl.kubernetes.io/restartedAt"]


class ImpactEnv:
    """App plus recording fakes for the impact-preview integration path."""

    def __init__(
        self,
        audit_path: Path,
        *,
        with_lister: bool = True,
        rows: dict[str, list[Any]] | None = None,
        list_rows: dict[str, list[Any]] | None = None,
    ) -> None:
        self.order: list[str] = []
        self.ops = RecordingOps(self.order)
        #: Rows the watch stream feeds the store, i.e. what is on screen.
        #: `web` sorts before `zz-api`, and the store orders by
        #: `(namespace, name)`, so the default cursor row is always `web` -
        #: the row every delete/restart assertion below names. The second
        #: row exists so a test can *move* the selection during the load.
        self.rows: dict[str, list[Any]] = (
            {
                "pods": [_pod()],
                "deployments": [_deployment("web", "deploy-1"), _deployment("zz-api", "deploy-2")],
                "replicasets": [_replicaset()],
                "configmaps": [_configmap()],
                "services": [_service()],
            }
            if rows is None
            else rows
        )
        #: What the snapshot LISTs return; the watched rows unless a test
        #: needs them to diverge (an object replaced under the same name
        #: between the watch and the snapshot carries a new uid).
        self.list_rows = self.rows if list_rows is None else list_rows
        self.lister = RecordingLister(self.list_rows, self.order)
        store = ResourceStore()
        watched = self.rows

        async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
            for row in watched.get(kind, []):
                yield ("ADDED", row)
            while True:
                await asyncio.sleep(0.01)

        async def check_permission(
            verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
        ) -> bool:
            self.order.append("rbac")
            return True

        self.app = KorvidApp(
            config=KorvidConfig(namespace="prod"),
            store=store,
            watch_manager=WatchManager(store, source),
            aliases=dict(CATALOG_ALIASES),
            write_ops=self.ops,
            audit=AuditLog(audit_path),
            check_permission=check_permission,
            list_relationship_objects=self.lister if with_lister else None,
        )


async def to_view(pilot: Any, view: str, *, expect: str | None = None) -> None:
    """Navigate to `view` through the command bar and wait for its rows.

    `expect` waits for a specific first row rather than for any row: rows
    from the previous view can still be on screen for a tick after the
    navigation, and every impact assertion depends on which row the cursor
    is on.
    """
    await pilot.press("colon")
    for ch in view:
        await pilot.press(ch)
    await pilot.press("enter")

    def ready() -> bool:
        table = pilot.app.query_one(ResourceTable)
        if table.row_count == 0:
            return False
        return expect is None or str(table.get_row_at(0)[0]) == expect

    await until(pilot, ready, label=f"{view} rows visible")


def impact_text(app: KorvidApp) -> str:
    """The rendered impact section of the open ConfirmScreen."""
    screen = app.screen
    assert isinstance(screen, ConfirmScreen)
    return str(screen.query_one(".confirm-impact", Static).render())


async def open_delete_dialog(
    env: ImpactEnv, pilot: Any, view: str, *, expect: str | None = None
) -> None:
    await to_view(pilot, view, expect=expect)
    await pilot.press("ctrl+d")
    await until(
        pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="confirm dialog opened"
    )


async def test_delete_dialog_shows_direct_and_transitive_dependents(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "delete apps/Deployment/prod/web" in text
        assert "known direct dependents (may be affected): 1" in text
        assert (
            "apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]"
            in text
        )
        assert "known transitive dependents (may be affected): 1" in text
        assert "Pod/prod/web-abc-1 via owned_by (declared)" in text
        assert "scope: prod" in text
        assert env.ops.calls == []


async def test_delete_of_a_pod_never_claims_the_selecting_service_fails(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "pods", expect="web-abc-1")
        text = impact_text(env.app)
        assert "delete Pod/prod/web-abc-1" in text
        assert "Service/prod/web" not in text
        assert "known direct dependents (may be affected): none in this snapshot" in text


async def test_rollout_restart_dialog_shows_the_owner_chain_only(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="restart dialog"
        )
        text = impact_text(env.app)
        assert "rollout restart apps/Deployment/prod/web" in text
        assert "apps/ReplicaSet/prod/web-abc via owned_by (declared)" in text
        assert "ConfigMap/prod/app-config" not in text
        assert env.ops.calls == []


async def test_rollout_restart_warns_about_an_unresolved_config_reference(tmp_path: Path) -> None:
    """`uses_config` is not a restart relation, but the Pod the restart
    replaces still has to mount its ConfigMap: a dangling reference inside
    the affected set is reported whatever its relation."""
    rows: dict[str, list[Any]] = {
        "deployments": [_deployment("web", "deploy-1"), _deployment("zz-api", "deploy-2")],
        "replicasets": [_replicaset()],
        "pods": [_pod()],  # its ConfigMap is not in this snapshot
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="restart dialog"
        )
        text = impact_text(env.app)
        assert "unresolved references in the affected set: 1" in text
        assert (
            "Pod/prod/web-abc-1 uses_config (declared) -> ConfigMap/prod/app-config (missing)"
            " at spec.volumes[0].configMap" in text
        )
        assert env.ops.calls == []


async def test_deleting_a_cluster_scoped_node_covers_every_namespace(tmp_path: Path) -> None:
    """The pane is scoped to `prod`, but a Node is cluster-scoped: scoping
    its snapshot to the pane would hide the `staging` Pod it also runs and
    let the dialog claim complete coverage of `prod` as if that were the
    whole answer."""
    rows: dict[str, list[Any]] = {
        "nodes": [_node()],
        "pods": [
            _scheduled_pod("web-1", "prod", "pod-1"),
            _scheduled_pod("api-1", "staging", "pod-2"),
        ],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "nodes", expect="worker-1")
        text = impact_text(env.app)
        assert "delete Node/worker-1" in text
        assert "known direct dependents (may be affected): 2" in text
        assert "Pod/prod/web-1 via scheduled_on (observed) at spec.nodeName" in text
        assert "Pod/staging/api-1 via scheduled_on (observed) at spec.nodeName" in text
        assert "scope: all namespaces" in text
        assert "scope: prod" not in text
        assert ("pods", None) in env.lister.calls


async def test_a_target_replaced_since_the_watch_is_reported_as_unknown(tmp_path: Path) -> None:
    """The row on screen carries uid `deploy-1`; the snapshot only knows a
    `web` with uid `deploy-9`. The write targets the incarnation the user
    saw, so the summary must say it cannot see it - not "no dependents"."""
    env = ImpactEnv(
        tmp_path / "audit.jsonl",
        list_rows={
            "deployments": [_deployment("web", "deploy-9"), _deployment("zz-api", "deploy-2")],
            "replicasets": [_replicaset()],
        },
    )
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "target not found in this snapshot - dependents unknown" in text
        assert "known direct dependents (may be affected): none in this snapshot" in text
        assert env.ops.calls == []


async def test_no_impact_section_without_a_relationship_loader(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl", with_lister=False)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert not env.app.screen.query(".confirm-impact")
        assert env.app.screen.query(".confirm-preview")
        assert env.lister.calls == []


async def test_incomplete_graph_still_renders_a_summary_with_the_coverage_warning(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    env.lister.errors["configmaps"] = ApiStatusError(403, "configmaps is forbidden")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert (
            "graph coverage: incomplete - a missing dependent here does not prove none exists"
            in text
        )
        assert "core/configmaps @prod: forbidden" in text
        assert "apps/ReplicaSet/prod/web-abc via owned_by (declared)" in text


async def test_impact_timeout_renders_the_static_unavailable_advisory(tmp_path: Path) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    env.lister.delay = 5.0
    with mock.patch("korvid.ui.app._IMPACT_TIMEOUT", 0.01):
        async with env.app.run_test() as pilot:
            await open_delete_dialog(env, pilot, "deploy", expect="web")
            text = impact_text(env.app)
            assert "impact unavailable; approval remains available" in text
            assert "known direct dependents" not in text
            assert env.app.screen.query(".confirm-preview")


async def test_unexpected_loader_failure_renders_the_static_unavailable_advisory(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    env.lister.errors["deployments"] = RuntimeError("parser exploded")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "impact unavailable; approval remains available" in impact_text(env.app)


async def test_scale_dialog_has_no_impact_section(tmp_path: Path) -> None:
    """Only delete and rollout restart have tested action semantics."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(
            pilot, lambda: isinstance(env.app.screen, ReplicasPrompt), label="replicas prompt"
        )
        await pilot.press("5")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="scale confirm")
        assert not env.app.screen.query(".confirm-impact")
        assert env.lister.calls == []


async def test_cordon_dialog_has_no_impact_section(tmp_path: Path) -> None:
    """A second unsupported flow, on a cluster-scoped kind: the delivery
    boundary is delete/rollout restart, and everything else stays exactly
    as it was (see the roadmap deviation in Global Constraints)."""
    rows: dict[str, list[Any]] = {"nodes": [_node()]}
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "nodes", expect="worker-1")
        await pilot.press("c")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="cordon confirm"
        )
        assert not env.app.screen.query(".confirm-impact")
        assert env.lister.calls == []
        assert env.ops.calls == []


async def test_context_switch_during_the_impact_load_aborts_before_the_dialog(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def bump_epoch() -> None:
        app._ctx_epoch += 1

    env.lister.on_first_call = bump_epoch
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await app.action_delete_resource()
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert any(
            "the kube context changed during the impact summary" in n.message
            for n in app._notifications
        )


async def test_selection_change_during_the_impact_load_aborts_before_the_dialog(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    app = env.app

    def move_cursor() -> None:
        app.query_one(ResourceTable).move_cursor(row=1)

    env.lister.on_first_call = move_cursor
    async with app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        # Row 1 is the second deployment: moving there during the load
        # means the dialog would describe a row the user is not on.
        assert str(app.query_one(ResourceTable).get_row_at(1)[0]) == "zz-api"
        await app.action_delete_resource()
        assert len(app.screen_stack) == 1
        assert env.ops.calls == []
        assert any(
            "the selection changed during the impact summary" in n.message
            for n in app._notifications
        )


async def test_impact_loads_after_the_permission_check_and_the_dry_run_preview(
    tmp_path: Path,
) -> None:
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert env.order[:3] == ["rbac", "preview", "list"]
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_impact_flow.py -q
```

Expected (15 tests: 12 fail, 3 pass):
- `test_impact_timeout_renders_the_static_unavailable_advisory` fails with `AttributeError: <module 'korvid.ui.app' ...> does not have the attribute '_IMPACT_TIMEOUT'` (raised by `mock.patch` before the app even starts)
- `test_delete_dialog_shows_direct_and_transitive_dependents`, `test_delete_of_a_pod_never_claims_the_selecting_service_fails`, `test_rollout_restart_dialog_shows_the_owner_chain_only`, `test_rollout_restart_warns_about_an_unresolved_config_reference`, `test_deleting_a_cluster_scoped_node_covers_every_namespace`, `test_a_target_replaced_since_the_watch_is_reported_as_unknown`, `test_incomplete_graph_still_renders_a_summary_with_the_coverage_warning`, and `test_unexpected_loader_failure_renders_the_static_unavailable_advisory` fail with `textual.css.query.NoMatches: No nodes match '.confirm-impact'`
- `test_context_switch_during_the_impact_load_aborts_before_the_dialog` and `test_selection_change_during_the_impact_load_aborts_before_the_dialog` fail on `assert len(app.screen_stack) == 1` (no impact load runs, so nothing fires `on_first_call` and the dialog opens)
- `test_impact_loads_after_the_permission_check_and_the_dry_run_preview` fails on `assert env.order[:3] == ["rbac", "preview", "list"]` (the list phase never happens)
- `test_no_impact_section_without_a_relationship_loader`, `test_scale_dialog_has_no_impact_section`, and `test_cordon_dialog_has_no_impact_section` already pass (they assert an absence)

- [ ] **Step 3: Wire the impact preview into the two supported write flows**

Add the imports beside the existing `korvid.core.relationships` / widget imports in `src/korvid/ui/app.py`:

```python
from korvid.core.impact import ImpactAction, summarize_impact
from korvid.ui.impact_preview import IMPACT_UNAVAILABLE_LINES, render_impact_lines
```

Add the deadline constant next to `_PREVIEW_TIMEOUT` (`src/korvid/ui/app.py:266`):

```python
#: Upper bound on the advisory blast-radius snapshot (issue #283). The
#: section is display support, so it gets its own hard deadline: a slow or
#: hung snapshot must never wedge an approval the user already asked for.
#: Larger than `_PREVIEW_TIMEOUT` because the snapshot is a bounded LIST
#: fan-out, not a single dry-run round trip.
_IMPACT_TIMEOUT = 5.0
```

Add the scope helper and the loader wrapper immediately after `_dry_run_preview` (`src/korvid/ui/app.py:5059-5067`):

```python
    def _impact_scope(self, meta: ResourceMeta) -> str | None:
        """The namespace an impact snapshot must cover for this target.

        The pane's namespace for a namespaced target, and *every* namespace
        for a cluster-scoped one (or an all-namespaces pane). A Node or
        PersistentVolume is reachable from every namespace: scoping its
        snapshot to the pane the user happens to be in would both hide the
        Pods it runs elsewhere and let the dialog report complete coverage
        of a namespace that was never the whole question. The same value is
        handed to the loader and to `summarize_impact`, so the scope the
        text states is always the scope that was listed.
        """
        scope = self._pane.scope
        if not meta.namespaced or scope == ALL_NAMESPACES:
            return None
        return scope

    async def _impact_preview(
        self,
        action: ImpactAction,
        meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
    ) -> tuple[str, ...] | None:
        """Advisory blast-radius lines for a write dialog (issue #283).

        Reuses the relationship snapshot loader `g` already owns - no new
        LIST/GET interface and no per-node fan-out - with the exact
        group/kind/namespace/name/uid identity the write will target, so a
        recreated same-named object is reported as absent from the snapshot
        rather than summarized as the one on screen. Display support only,
        and fail-open in three distinct ways:

        - no loader wired (no cluster connection) -> None, no section at all;
        - a timeout or unexpected failure -> the static "impact unavailable"
          advisory, because an API error message can embed a response body
          (for a Secret, its data) and must never reach the dialog;
        - cancellation (a `:ctx` switch tearing the client down) propagates
          untouched, exactly like every other awaited read here.

        The summary itself can never approve, execute, or reserve a write:
        it returns text.
        """
        loader = self._relationship_loader
        if loader is None:
            return None
        root = GraphResource(
            group=meta.group, kind=meta.kind, namespace=ns or "", name=name, uid=uid
        )
        scope = self._impact_scope(meta)
        try:
            async with asyncio.timeout(_IMPACT_TIMEOUT):
                graph = await loader.load(root, scope, self.aliases)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Type only: never the message (CodeQL py/clear-text-logging-
            # sensitive-data), and never anything derived from a manifest.
            logger.debug("impact summary unavailable for %s: %s", action, type(exc).__name__)
            return IMPACT_UNAVAILABLE_LINES
        return render_impact_lines(summarize_impact(graph, action, root, scope=scope))
```

Extend `_push_write_confirmation` (`src/korvid/ui/app.py:5069-5111`) with the new keyword and pass it through:

```python
    async def _push_write_confirmation(
        self,
        title: str,
        operation: str,
        *,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        op_factory: Callable[[], Awaitable[None]],
        detail: str = "",
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
    ) -> None:
```

```python
        await self.push_screen(
            self._confirm_screen(
                title,
                operation,
                require_name=require_name,
                preview=preview,
                preview_title=preview_title,
                managed_note=managed_note,
                impact_lines=impact_lines,
            ),
            _done,
        )
```

Extend `_confirm_screen` (`src/korvid/ui/app.py:6372-6394`) the same way:

```python
    def _confirm_screen(
        self,
        title: str,
        operation: str,
        *,
        require_name: str | None = None,
        preview: list[str] | None = None,
        preview_title: str = "server dry-run preview:",
        managed_note: str | None = None,
        impact_lines: tuple[str, ...] | None = None,
    ) -> ConfirmScreen:
```

```python
        return ConfirmScreen(
            title,
            operation,
            require_name=require_name,
            preview=preview,
            preview_title=preview_title,
            protected_context=self._protected_context,
            managed_note=managed_note,
            impact_lines=impact_lines,
        )
```

In `action_delete_resource` (`src/korvid/ui/app.py:5201-5220`), load the summary after the existing dry-run revalidation and revalidate again before the dialog:

```python
        preview = await self._dry_run_preview(ops.preview_delete(meta, ns, name, uid=uid))
        note = await self._managed_note(kind_alias, ns, name)
        if not self._write_context_intact(
            "delete", meta, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return
        # The snapshot is another awaited gap: a `:ctx` switch or a moved
        # selection during it must abort before a dialog describes the row
        # the user is no longer on (issue #283).
        impact = await self._impact_preview(ImpactAction.DELETE, meta, ns, name, uid)
        if not self._write_context_intact(
            "delete", meta, ns, name, phase="the impact summary", epoch=epoch
        ):
            return
        operation = f"DELETE {self._gvr_label(meta)}/{name}{self._write_locus(ns)}"
        require = None if meta.namespaced else name
        await self._push_write_confirmation(
            f"Delete {self._gvr_label(meta)}/{name}?",
            operation,
            action="delete",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.delete_object(meta, ns, name, uid=uid),
            require_name=require,
            preview=preview,
            managed_note=note,
            impact_lines=impact,
        )
```

In `action_rollout_restart` (`src/korvid/ui/app.py:5245-5267`), do the same with the restart action:

```python
        note = await self._managed_note(kind_alias, ns, name)
        if not self._write_context_intact(
            "rollout_restart", meta, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return
        # Same awaited-gap revalidation as delete - see action_delete_resource.
        impact = await self._impact_preview(ImpactAction.ROLLOUT_RESTART, meta, ns, name, uid)
        if not self._write_context_intact(
            "rollout_restart", meta, ns, name, phase="the impact summary", epoch=epoch
        ):
            return

        await self._push_write_confirmation(
            f"Rollout restart {self._gvr_label(meta)}/{name}?",
            f"PATCH {self._gvr_label(meta)}/{name} pod template (restartedAt annotation)"
            f"{self._write_locus(ns)}",
            action="rollout_restart",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.rollout_restart_with_stamp(
                meta, ns, name, uid=uid, restarted_at=stamp
            ),
            preview=preview,
            managed_note=note,
            impact_lines=impact,
        )
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_impact_flow.py tests/ui/test_write_ops.py tests/ui/test_dryrun_preview.py tests/ui/test_ctx_switch.py tests/ui/test_write_confirm_characterization.py tests/ui/test_relationship_flow.py -q
uv run ruff check --fix src/korvid/ui/app.py tests/ui/test_impact_flow.py
uv run ruff format src/korvid/ui/app.py tests/ui/test_impact_flow.py
uv run mypy src/korvid/ui/app.py tests/ui/test_impact_flow.py
uv run tach check
```

Expected:
- `pytest`: PASS (the 15 new flow tests plus every pre-existing write/preview/ctx-switch/relationship test)
- `ruff check --fix`: `All checks passed!` — in particular no `C901` on `action_delete_resource` (measured today at 8; it gains one branch, to 9, against the limit of 10) and none on `action_rollout_restart` (6 → 7)
- `ruff format`: unchanged or formatting-only changes in the two touched files
- `mypy`: `Success: no issues found in 2 source files`
- `tach check`: PASS

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/app.py tests/ui/test_impact_flow.py
git commit -m "feat: add impact previews to delete and rollout restart" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Security invariants, docs, and the full gate

**Files:**
- Create: `tests/ui/test_impact_security.py`
- Modify: `docs/tui.md`
- Modify: `docs/resource-relationships.md`

**Interfaces:**
- Consumes (Task 4 harness, imported directly): `tests.ui.test_impact_flow.ImpactEnv`, `CATALOG_ALIASES`, `to_view`, `open_delete_dialog`, `impact_text`.
- Consumes (Tasks 1-4, unchanged): `KorvidApp._active_cluster_writes` (the write reservation counter set by `_tracks_cluster_write`, `src/korvid/ui/app.py:373-401`), `ConfirmScreen`, `AuditLog`.
- Produces: no production code. This task pins the invariants the feature must never break and documents the user-visible behavior.
- These are regression pins, so most of them pass against Task 4's implementation. Step 2 proves they actually bite by mutating the source, observing the failure, and reverting.

- [ ] **Step 1: Write the security-invariant tests**

```python
# tests/ui/test_impact_security.py
"""Security invariants around the advisory impact preview (issue #283).

The preview adds text to an existing dialog. It must not become a new way to
approve, execute, reserve, or unblock a cluster write, and a graph failure
must not take a legitimate confirmation away from the user. Every test here
drives the real `Ctrl-D` / `r` flow through the Task 4 harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from textual import events

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    TargetReference,
)
from korvid.ui.widgets.confirm_screen import ConfirmScreen

from .test_impact_flow import CATALOG_ALIASES, ImpactEnv, impact_text, open_delete_dialog, to_view
from .waits import until


def _markup_replicaset() -> GenericSummary:
    """A ReplicaSet whose name is Rich markup: cluster-controlled text must
    never be interpreted as styling in an approval dialog."""
    return GenericSummary(
        name="[bold red]web-abc[/]",
        namespace="prod",
        kind="ReplicaSet",
        created="",
        uid="rs-1",
        relationships=RelationshipFacts(
            api_group="apps",
            references=(
                ReferenceFact(
                    relation=RelationKind.OWNED_BY,
                    target=TargetReference(
                        group="apps", kind="Deployment", namespace="prod", name="web", uid="d-1"
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="metadata.ownerReferences[0]",
                ),
            ),
        ),
    )


def _secret_row() -> GenericSummary:
    """A Secret summary carries identity only - never `data`/`stringData`."""
    return GenericSummary(name="db", namespace="prod", kind="Secret", created="", uid="secret-1")


def _pod_using_secret() -> PodSummary:
    return PodSummary(
        name="web-abc-1",
        namespace="prod",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-1",
        relationships=RelationshipFacts(
            references=(
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference(group="", kind="Secret", namespace="prod", name="db"),
                    confidence=FactConfidence.DECLARED,
                    field="spec.volumes[0].secret.secretName",
                ),
            )
        ),
    )


async def test_declined_delete_with_an_impact_section_runs_no_operation(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "known direct dependents" in impact_text(env.app)
        await pilot.press("n")
        await pilot.pause()
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_keystroke_buffered_during_the_impact_load_cannot_approve(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        stale = events.Key("y", "y")  # created before the dialog existed
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="confirm dialog"
        )
        env.app.screen.post_message(stale)
        await pilot.pause()
        assert env.ops.calls == []
        assert not audit_path.exists()
        await pilot.press("y")
        await until(pilot, lambda: env.ops.calls, label="approved delete ran")
        assert env.ops.calls == [("delete", "deployments", "prod", "web", "deploy-1")]


async def test_the_impact_load_never_writes_reserves_or_audits(tmp_path: Path) -> None:
    """Loading a snapshot is a read: it must take no write reservation (which
    would block `:ctx`), run no operation, and write no audit record."""
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert env.lister.calls != []
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_audit_failure_still_blocks_the_operation_factory(tmp_path: Path) -> None:
    """Fail-closed auditing is unchanged: an unwritable audit log blocks the
    write even though the dialog showed an impact summary."""
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # a directory at the log path makes appends fail
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "known direct dependents" in impact_text(env.app)
        await pilot.press("y")
        await pilot.pause(0.3)  # the write path must stay blocked
        assert env.ops.calls == []


async def test_graph_failure_does_not_block_a_legitimate_confirmation(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    env.lister.errors["deployments"] = RuntimeError("parser exploded")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "impact unavailable; approval remains available" in impact_text(env.app)
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="write audited",
        )
        assert env.ops.calls == [("delete", "deployments", "prod", "web", "deploy-1")]
        entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
        assert entries[0]["outcome"] == "intent"
        assert entries[-1]["outcome"] == "success"


async def test_rich_markup_in_a_resource_name_renders_literally(tmp_path: Path) -> None:
    rows: dict[str, list[Any]] = {
        "deployments": [
            GenericSummary(
                name="web", namespace="prod", kind="Deployment", created="", desired=1, uid="d-1"
            )
        ],
        "replicasets": [_markup_replicaset()],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "apps/ReplicaSet/prod/[bold red]web-abc[/]" in impact_text(env.app)


async def test_impact_preview_works_with_the_agent_disabled(tmp_path: Path) -> None:
    """No LLM, no provider: the summary is a deterministic graph query."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        assert env.app.config.agent_enabled is False
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "known direct dependents (may be affected): 1" in impact_text(env.app)


async def test_no_secret_value_or_manifest_content_reaches_the_dialog(tmp_path: Path) -> None:
    rows: dict[str, list[Any]] = {
        "secrets": [_secret_row()],
        "pods": [_pod_using_secret()],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "secrets", expect="db")
        text = impact_text(env.app)
        assert "delete Secret/prod/db" in text
        assert (
            "Pod/prod/web-abc-1 via uses_config (declared) at spec.volumes[0].secret.secretName"
            in text
        )
        for leak in ("stringData", "data:", "apiVersion", "kind: Secret"):
            assert leak not in text


async def test_rollout_restart_declined_with_an_impact_section_runs_no_operation(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="restart dialog"
        )
        assert "rollout restart apps/Deployment/prod/web" in impact_text(env.app)
        await pilot.press("n")
        await pilot.pause()
        assert env.ops.calls == []
        assert not audit_path.exists()


def test_the_catalog_aliases_cover_every_supported_write_kind() -> None:
    """A guard on the harness itself: the flows under test address real
    discovered kinds, not synthetic views."""
    deployment = CATALOG_ALIASES["deployments"]
    assert isinstance(deployment, ResourceMeta)
    assert deployment.group == "apps"
    assert deployment.synthetic is False
```

- [ ] **Step 2: Run the pins, then prove they bite with one reverted mutation**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_impact_security.py -q
```

Expected:
- PASS — these are regression pins over Task 4's implementation.

Now prove the revalidation pin is real. Temporarily delete the second revalidation in `action_delete_resource` (the block added in Task 4):

```python
        impact = await self._impact_preview(ImpactAction.DELETE, meta, ns, name, uid)
        if not self._write_context_intact(
            "delete", meta, ns, name, phase="the impact summary", epoch=epoch
        ):
            return
```

Run:

```bash
uv run pytest -p no:tach tests/ui/test_impact_flow.py -q -k "during_the_impact_load"
```

Expected:
- 2 failures (`test_context_switch_during_the_impact_load_aborts_before_the_dialog`, `test_selection_change_during_the_impact_load_aborts_before_the_dialog`) on `assert len(app.screen_stack) == 1`

Restore the file and confirm the tree is clean again:

```bash
git checkout -- src/korvid/ui/app.py
git diff --stat src/korvid/ui/app.py
uv run pytest -p no:tach tests/ui/test_impact_flow.py -q -k "during_the_impact_load"
```

Expected:
- `git diff --stat`: no output
- `pytest`: PASS

- [ ] **Step 3: Document the behavior**

Append to `docs/tui.md` (after the "Session timeline" section):

```markdown
## Write impact preview

Destructive writes that have tested relationship semantics — delete
(`Ctrl-D`) and rollout restart (`r`) — show a graph-derived impact section
above the server dry-run preview in the approval dialog. It answers one
bounded question: which resources korvid has already observed depend on this
one?

    graph-derived impact (advisory):
      delete apps/Deployment/prod/web
      known direct dependents (may be affected): 1
        - apps/ReplicaSet/prod/web-abc via owned_by (declared) at metadata.ownerReferences[0]
      known transitive dependents (may be affected): 1
        - Pod/prod/web-abc-1 via owned_by (declared) at metadata.ownerReferences[0] -> owned_by (declared) at metadata.ownerReferences[0]
      additional known paths: 1 (already-listed dependents reached again)
      scope: prod
      graph coverage: incomplete - a missing dependent here does not prove none exists
        - gateway.networking.k8s.io/*: unavailable
      advisory only: known relationships from one bounded snapshot - not a prediction of failure, no replacement for the server dry-run, and never a block on approval.

The section is **advisory**. It never predicts failure, never replaces the
server dry-run, and never blocks approval: the y/typed-name gate, the UID
precondition, the RBAC pre-check, and the fail-closed audit log are exactly
what they were. Scale, edit, resize, cordon/uncordon, drain, Helm, and
operator flows do not show it — they have no tested per-relation semantics
yet, and korvid would rather show nothing than a plausible guess.

Reading it:

- **direct** dependents are one hop from the target, **transitive** are two
  or more; each line names the relation, how the fact was derived, and the
  manifest field it came from.
- `additional known paths` counts relationships that reach a dependent
  already listed above (a second route, a second mount). They are counted
  rather than repeated, so a count of dependents is never inflated.
- `[inferred]` marks a hop derived by a heuristic rather than read from a
  manifest. It is labelled, never a blocker.
- `unresolved references in the affected set` lists dangling references
  held by the target or by something it takes down — a mounted ConfigMap
  that no longer exists, say — whatever relation they use. Each line names
  its own confidence (`declared`, `observed`, or `inferred`) next to the
  relation, the same way a dependent path does, so a heuristically-derived
  dangling reference is identifiable on its own line, not only through the
  `[inferred]`/aggregate note above.
- `scope` is the namespace the snapshot covered. `all namespaces` appears
  for a cluster-scoped target (a Node, a PersistentVolume) or an
  all-namespaces view; otherwise the coverage below it is only ever
  complete *within that namespace*.
- `target not found in this snapshot - dependents unknown` means the exact
  object (UID included) was not in the snapshot — usually deleted and
  recreated under the same name. The counts below it then describe the
  snapshot, not your object.
- `graph coverage: incomplete` means some source could not be listed
  (RBAC, an absent API, a cap): a missing dependent is then *unknown*, not
  *absent*.
- `traversal capped` / `snapshot truncated` mean the answer was bounded on
  purpose (3 hops, 50 resources) rather than complete.

The snapshot is the same bounded, read-only LIST fan-out the relationship
view (`g`) performs — scoped to the current namespace for a namespaced
target, and cluster-wide for a cluster-scoped one so a dependent in another
namespace cannot be quietly missed — with a 5-second deadline. If it times
out or fails, the dialog says `impact unavailable; approval remains
available` and the approval proceeds normally. If the context switches or
the selection moves while it loads, the write is cancelled before any
dialog opens.
```

Append to `docs/resource-relationships.md` (at the end of the file):

```markdown
## Blast radius in write previews

The same snapshot feeds the approval dialogs for `Ctrl-D` (delete) and `r`
(rollout restart). Only relationships with explicitly tested action
semantics participate:

| Action | Relations followed (target → its dependents) |
|---|---|
| delete | `owned_by`, `managed_by`, `routes_to`, `uses_volume`, `uses_config`, `protected_by`, `scheduled_on`, `bound_to` |
| rollout restart | `owned_by`, `managed_by` |

`selects` is deliberately excluded from both. A Service selecting many Pods
does not fail because one selected Pod is deleted, so korvid never claims it
does — the same reasoning that keeps `missing` from meaning "absent".

Only **resolved** edges are traversed; an unresolved reference is reported
as a warning instead. That warning is bounded by *the affected set*, not by
the relations above: any dangling reference held by the target or by a
resource it takes down is reported — a restarted workload whose Pod mounts a
deleted ConfigMap is exactly the case worth seeing — while an unrelated
dangling reference elsewhere in the cluster never lands in your approval
dialog. The walk is breadth-first and deterministic (each dependent is
listed once, with the first path that reached it; further paths to the same
dependent are counted as `additional known paths`), bounded to 3 hops and 50
resources, and classifies a genuine loop as a cycle rather than expanding it
twice.

The snapshot is scoped like the graph view's: the current namespace for a
namespaced target, and cluster-wide for a cluster-scoped one such as a Node
or PersistentVolume, so a dependent in another namespace is not silently
omitted. The preview always states which scope it used.

Everything the answer does not know is stated: a target that was not in the
snapshot at all (an object recreated under the same name has a new UID),
coverage that is not `complete`, a truncated snapshot, and either traversal
cap. The summary is advisory — see [Write impact
preview](tui.md#write-impact-preview) for how it appears and what it never
does.

Only `Ctrl-D` and `r` show this section today. The remaining write types
(scale, edit, resize, cordon/uncordon, drain, Helm, operator) have no tested
per-relation semantics yet and deliberately show nothing rather than a
plausible guess.
```

- [ ] **Step 4: Run the full gate**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_impact_security.py tests/ui/test_impact_flow.py -q
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
make check
uv run deptry src
uv run pytest --cov -q
```

Expected:
- targeted `pytest`: PASS
- `ruff check --fix`: `All checks passed!`
- `ruff format`: no changes outside the files this plan touched
- `make check`: `ruff check` clean, `mypy` `Success: no issues found`, full `pytest` PASS, `tach check` PASS
- `deptry src`: no issues (no dependency was added)
- `pytest --cov`: PASS with total coverage at or above the 80% gate

- [ ] **Step 5: Commit**

```bash
git add tests/ui/test_impact_security.py docs/tui.md docs/resource-relationships.md
git commit -m "test: pin impact preview security invariants and document it" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

- [ ] **Step 6: Open the follow-up issue for the remaining write types**

Issue #283 asks for the summary in destructive previews; this delivery
covers `delete` and `rollout restart` only (see the deviation recorded in
Global Constraints). The remaining write types therefore need their own
tracked work item **before** #283 is closed.

Run:

```bash
gh issue create \
  --title "Blast-radius previews for the remaining write types" \
  --body "Follow-up to #283, which shipped graph-derived \`ImpactSummary\` text in the delete (\`Ctrl-D\`) and rollout-restart (\`r\`) approval dialogs.

Still without an impact section, because none of them has tested per-relation action semantics yet: scale, edit, resize, cordon/uncordon, drain, Helm install/upgrade/uninstall, and OLM uninstall.

Each needs its own closed relation set decided and tested before it may make a claim (what does 'scale to 0' assert about a \`routes_to\` backend? what does 'drain' assert beyond the existing PDB-aware plan?). \`korvid.core.impact.ACTION_RELATIONS\` is the extension point: add an \`ImpactAction\` member plus its relation set, with a dedicated test per relation, then pass it through the flow's \`_impact_preview\` call.

Until then those dialogs deliberately show nothing rather than a plausible guess."
```

Expected:
- the command prints the new issue's URL; record that number in #283's closing comment
- if `gh` cannot create it (no network, no auth), **leave #283 open** and say why in its comment — the deviation must stay tracked somewhere, and this is the only place the plan allows it to live

---

## Self-review

**Spec coverage** (Slice 3 lines 213-234, safety lines 236-248, testing lines 277-284, issue #283 acceptance criteria):

| Requirement | Where |
|---|---|
| Immutable `ImpactSummary` from one action + one snapshot | Task 1 (`summarize_impact`, frozen `slots=True` dataclasses) |
| Direct target, direct dependents, transitive dependents reported separately | Task 1 (`target`/`direct`/`transitive`, path-length split) + Task 2 (`_section`) |
| Edge paths supporting every item | Task 1 (`ImpactItem.path`) + Task 2 (`_item_line` / `_hop`) |
| Explicit action semantics for every participating relation | Task 1 (`ACTION_RELATIONS`, parametrized tests over all 8 delete relations — 9 cases, both `routes_to` shapes — on the realistic resource pair each occurs between, plus both restart relations) |
| Deleting a Pod never claims every selecting Service fails | Task 1 (`selects` excluded, `test_delete_never_claims_a_selecting_service_fails`) + Task 4 (`test_delete_of_a_pod_never_claims_the_selecting_service_fails`) |
| Direct and transitive distinct and deterministically ordered | Task 1 (BFS order, `test_the_first_graph_ordered_path_wins_and_repeats_identically`) |
| Cycles explicit | Task 1 (`cycles`, ancestor check, `test_cycle_and_revisit_classification_matches_the_graph_walk` parity with `walk_dependents`) + Task 2 (cycle line) |
| Converging/parallel paths counted, not duplicated | Task 1 (`revisits`) + Task 2 (`additional known paths: <n>`) |
| Inferred edges labelled, never blocking | Task 1 (`ImpactItem.inferred`) + Task 2 (`[inferred]`, note line) |
| Unresolved targets explicit and relevant | Task 1 (affected-set filter, *not* relation-filtered: `test_unresolved_references_are_reported_whatever_their_relation`) + Task 2 (`_unresolved_lines`) + Task 4 (`test_rollout_restart_warns_about_an_unresolved_config_reference`) |
| A target the snapshot never saw is reported, never answered | Task 1 (`target_present`, feeds `incomplete`) + Task 2 (`target not found in this snapshot - dependents unknown`) + Task 4 (`test_a_target_replaced_since_the_watch_is_reported_as_unknown`) |
| The snapshot's namespace scope is chosen by the target and always stated | Task 4 (`_impact_scope`, one value for both the load and the summary; `test_deleting_a_cluster_scoped_node_covers_every_namespace`) + Task 1 (`scope` recorded) + Task 2 (`scope:` line, stated even for `complete` coverage) |
| Graph completeness reported | Task 1 (`coverage`, `incomplete`) + Task 2 (`_coverage_lines`) |
| Caps reported, never silent | Task 1 (`traversal_capped`, `graph_truncated`) + Task 2 (`_cap_lines`) |
| Rendered beside the existing server dry-run preview | Task 3 (`.confirm-impact` above `.confirm-preview`) |
| Approval possible while the graph is incomplete, and the preview says so | Task 2 + Task 4 (`test_incomplete_graph_still_renders_a_summary_with_the_coverage_warning`) |
| No impact summary can approve, execute, reserve, or bypass a write | Task 5 (`test_the_impact_load_never_writes_reserves_or_audits`, `test_declined_delete_with_an_impact_section_runs_no_operation`, `test_keystroke_buffered_during_the_impact_load_cannot_approve`) |
| Fresh-keystroke, typed-name, context epoch, UID, RBAC, dry-run unchanged | Task 3 (gate tests) + Task 4 (ordering + both revalidation tests) + the untouched suites re-run in Tasks 3-5 |
| Audit failure still prevents the operation factory | Task 5 (`test_audit_failure_still_blocks_the_operation_factory`) |
| No Secret value or manifest content anywhere | Task 2 (`test_secret_identity_is_rendered_without_any_value_field`) + Task 5 (`test_no_secret_value_or_manifest_content_reaches_the_dialog`) |
| No per-node API GET fan-out / no new interface | Task 4 (`_impact_preview` reuses `RelationshipSnapshotLoader`; no `KorvidApp.__init__` or composition-root change) |
| Deterministic and fully usable without an LLM | Task 5 (`test_impact_preview_works_with_the_agent_disabled`) |
| Textual confined to `ui/` | Task 1 (`test_the_impact_model_imports_no_textual`) + `tach check` in every task |
| Docs for supported actions/relations, advisory/incomplete semantics, scope, timeout/failure | Task 5 (`docs/tui.md`, `docs/resource-relationships.md`) |
| Incomplete-graph warning in a destructive preview (design line 282) | **Partially covered, deliberately.** Task 2 + Task 4 deliver it for `delete` and `rollout restart`; scale, edit, resize, cordon/uncordon, drain, Helm, and OLM keep no impact section at all, proved unchanged by Task 4 (`test_scale_dialog_has_no_impact_section`, `test_cordon_dialog_has_no_impact_section`). The deviation and its reason are recorded in Global Constraints, in `docs/resource-relationships.md`, and in the follow-up issue Task 5 Step 6 opens before #283 is closed. This plan does **not** claim every destructive preview. |
| Out of scope respected: no new mutation, no write blocked by an inferred edge, no new dependency, no complete causal graph | Tasks 1-5; `deptry src` in Task 5 Step 4 |

**Placeholder scan:** no `TODO`, `TBD`, "similar to Task N", "handle edge cases", or unnamed interface remains. Every code step contains the code; every command states its expected output.

**Type consistency:**
- `ImpactAction`, `ImpactLimits`, `ImpactItem`, `ImpactSummary`, `summarize_impact`, and `ACTION_RELATIONS` are defined once in Task 1 and used with identical names and signatures in Tasks 2, 4, and 5.
- `summarize_impact`'s signature is written once, identically, in Task 1's Interfaces and Task 1 Step 3: keyword-only `scope: str | None = None` and `limits: ImpactLimits = _DEFAULT_LIMITS` (the module singleton, never a re-spelled `ImpactLimits()` in the signature).
- `ImpactSummary`'s twelve fields appear in one order (Task 1) and every construction — Task 1's code, Task 2's `_summary` helper — passes them by keyword, so the added `target_present`, `scope`, and `revisits` cannot silently shift a positional argument.
- `render_impact_lines(summary) -> tuple[str, ...]`, `IMPACT_TITLE`, `ADVISORY_LINE`, `IMPACT_UNAVAILABLE_LINES`, and `_MAX_LINE` are defined once in Task 2 and consumed unchanged in Tasks 3-5 (`_MAX_LINE` only by Task 2's own bound test).
- `impact_lines: tuple[str, ...] | None = None` is the same keyword-only parameter with the same type on `ConfirmScreen.__init__` (Task 3), `KorvidApp._confirm_screen`, and `KorvidApp._push_write_confirmation` (Task 4); `KorvidApp._impact_preview` returns exactly that type.
- `KorvidApp._impact_scope(meta) -> str | None` returns the single value passed to both `RelationshipSnapshotLoader.load(root, namespace, aliases)` and `summarize_impact(..., scope=...)`, so the loaded scope and the rendered scope are the same object by construction.
- The harness names `ImpactEnv`, `RecordingLister`, `RecordingOps`, `CATALOG_ALIASES`, `to_view`, `open_delete_dialog`, and `impact_text` are defined once in Task 4 and imported by name in Task 5; `to_view`/`open_delete_dialog` take the same optional `expect` keyword in both.
- `ImpactSummary.incomplete` (Task 1) and the renderer's separate target/coverage/cap lines (Task 2) are deliberately different views of completeness: `incomplete` is the single boolean, the lines say *which* reason applies.
- The impact traversal deliberately duplicates `RelationshipGraph.walk_dependents` rather than extending it (rationale in Task 1's Interfaces and the module docstring); the parity test keeps the one shared invariant honest.

**Sample verification (run while writing this plan, re-runnable by the implementer):** every ` ```python ` block was extracted and parsed (`ast.parse`); the two `_push_write_confirmation` / `_confirm_screen` blocks are signature-only fragments by design, everything else parses standalone. Each block was then checked with `ruff format --diff` (line-length 100, py311) at its real indentation and with `ruff check` under the repo's rule set — clean. Task 1's and Task 2's modules were type-checked with `mypy --strict` (`Success: no issues found in 2 source files`) and their test blocks were executed against them: **37 core tests and 14 renderer tests pass**, with only `test_the_impact_model_imports_no_textual` unrunnable outside the real package (its subprocess imports `korvid.core.impact` by name). The Task 4 expectations that depend on the graph builder — the delete/restart line text, the cluster-scoped Node scope, the stale-UID target, the unresolved `uses_config` warning, and the Service that must not appear — were reproduced against `build_relationship_graph` with the same fixtures and match exactly. The `action_delete_resource` (8 → 9) and `action_rollout_restart` (6 → 7) complexity numbers were measured with `ruff --select C901` against today's `src/korvid/ui/app.py`, and every `src/korvid/ui/app.py` line reference in the blast-radius list was re-read at HEAD.
