# Operational Relationship Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver issue #281: a deterministic, metadata-only Kubernetes
relationship snapshot and a bounded Textual graph view that works without an
LLM.

**Architecture:** The Kubernetes layer extracts immutable relationship facts
while it already holds list/watch manifests. A pure core builder joins those
facts into an immutable dependent-to-dependency graph with explicit coverage
and resolution states. A UI controller performs bounded, concurrency-limited
LISTs and opens an adjacency-table screen; it never performs a GET per graph
node.

**Tech Stack:** Python 3.11+, frozen dataclasses, `enum.StrEnum`, asyncio,
Textual, kubernetes_asyncio, pytest/Pilot, Ruff, mypy strict, tach.

## Global Constraints

- Follow `docs/dev/specs/2026-08-14-operational-relationships-roadmap-design.md`
  Slice 1 and GitHub issue #281.
- `ui/` may import `core` and `k8s`; `core/` may import `k8s`; Textual imports
  remain confined to `ui/`.
- No new dependency and no `uv lock` invocation.
- No per-node Kubernetes GET. The feature may call `list_objects` once per
  discovered graph source and scope.
- Never retain or render Secret `data`, `stringData`, environment literal
  values, commands, arguments, or annotations.
- Every edge direction is dependent to dependency.
- Every edge carries a closed relation, `declared`/`observed`/`inferred`
  confidence, evidence pointer, and resolution state.
- Missing RBAC, unavailable APIs, failed LISTs, and caps remain visible; absence
  is never proof of no relationship.
- Default bounds are `4` concurrent LISTs, `10_000` input resources,
  `50_000` edges, transitive depth `5`, and rendered nodes `500`.
- The graph remains read-only and fully usable without an agent provider.
- Use TDD for every task. Run a test RED before production changes, then GREEN.
- Run `uv run tach check` whenever an import crosses package boundaries.
- Commit each task after its targeted tests, Ruff, formatting, mypy for touched
  source, and tach are green. Never bypass hooks.

---

## File map

### New files

- `src/korvid/k8s/selectors.py` — shared Kubernetes LabelSelector parsing and
  matching.
- `src/korvid/k8s/relationship_facts.py` — metadata-only manifest extraction.
- `src/korvid/core/relationships.py` — immutable graph model, builder, queries,
  coverage, and bounds.
- `src/korvid/ui/relationship_controller.py` — source selection, bounded LIST
  orchestration, and coverage classification.
- `src/korvid/ui/widgets/relationship_screen.py` — adjacency table and bounded
  transitive expansion.
- `tests/k8s/test_selectors.py`
- `tests/k8s/test_relationship_facts.py`
- `tests/core/test_relationships.py`
- `tests/ui/test_relationship_controller.py`
- `tests/ui/test_relationship_screen.py`
- `tests/ui/test_relationship_flow.py`
- `docs/resource-relationships.md`

### Modified files

- `src/korvid/k8s/drain.py` — consume the shared selector matcher.
- `src/korvid/k8s/models.py` — carry `RelationshipFacts` in list/watch summaries.
- `src/korvid/ui/app.py` — `g` action, controller ownership, result navigation.
- `src/korvid/__main__.py` — inject `kube.list_objects`.
- `tests/k8s/test_drain.py`
- `tests/k8s/test_models.py`
- `tests/test_main_wiring.py`
- `tests/ui/test_app.py`
- `README.md`

---

### Task 1: Shared Kubernetes selector semantics

**Files:**
- Create: `src/korvid/k8s/selectors.py`
- Create: `tests/k8s/test_selectors.py`
- Modify: `src/korvid/k8s/drain.py`
- Modify: `tests/k8s/test_drain.py`

**Interfaces:**
- Produces:
  - `SelectorExpression(key: str, operator: str, values: tuple[str, ...])`
  - `LabelSelector(match_labels, match_expressions, present)`.
  - `parse_label_selector(raw: object) -> LabelSelector`
  - `matches_selector(selector, labels, *, empty_matches: bool) -> bool`
- `LabelSelector.present` distinguishes an absent selector from an explicit
  empty selector.
- `drain.py` keeps policy-version-specific PDB empty-selector behavior by
  passing `empty_matches=True` for `policy/v1` and `False` for `policy/v1beta1`.

- [ ] **Step 1: Add failing parser and matcher tests**

```python
# tests/k8s/test_selectors.py
from korvid.k8s.selectors import matches_selector, parse_label_selector


def test_absent_and_empty_selectors_remain_distinct() -> None:
    absent = parse_label_selector(None)
    empty = parse_label_selector({})
    assert absent.present is False
    assert empty.present is True
    assert matches_selector(absent, {}, empty_matches=True) is False
    assert matches_selector(empty, {}, empty_matches=True) is True
    assert matches_selector(empty, {}, empty_matches=False) is False


def test_match_labels_and_expressions_follow_kubernetes_semantics() -> None:
    selector = parse_label_selector(
        {
            "matchLabels": {"app": "api"},
            "matchExpressions": [
                {"key": "tier", "operator": "In", "values": ["backend"]},
                {"key": "debug", "operator": "DoesNotExist"},
            ],
        }
    )
    assert matches_selector(
        selector, {"app": "api", "tier": "backend"}, empty_matches=False
    )
    assert not matches_selector(
        selector,
        {"app": "api", "tier": "backend", "debug": "true"},
        empty_matches=False,
    )


def test_unknown_operator_never_matches() -> None:
    selector = parse_label_selector(
        {"matchExpressions": [{"key": "app", "operator": "Equals", "values": ["api"]}]}
    )
    assert not matches_selector(selector, {"app": "api"}, empty_matches=True)
```

- [ ] **Step 2: Run the new test RED**

Run:

```bash
uv run pytest -p no:tach tests/k8s/test_selectors.py -q
```

Expected: collection fails with
`ModuleNotFoundError: No module named 'korvid.k8s.selectors'`.

- [ ] **Step 3: Implement the immutable selector model**

```python
# src/korvid/k8s/selectors.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectorExpression:
    key: str
    operator: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LabelSelector:
    match_labels: tuple[tuple[str, str], ...] = ()
    match_expressions: tuple[SelectorExpression, ...] = ()
    present: bool = False


def parse_label_selector(raw: object) -> LabelSelector:
    if not isinstance(raw, Mapping):
        return LabelSelector()
    raw_labels = raw.get("matchLabels")
    labels = (
        tuple(sorted((str(key), str(value)) for key, value in raw_labels.items()))
        if isinstance(raw_labels, Mapping)
        else ()
    )
    raw_expressions = raw.get("matchExpressions")
    expressions: list[SelectorExpression] = []
    if isinstance(raw_expressions, list):
        for item in raw_expressions:
            if not isinstance(item, Mapping):
                continue
            key = item.get("key")
            operator = item.get("operator")
            values = item.get("values")
            if not isinstance(key, str) or not isinstance(operator, str):
                continue
            expressions.append(
                SelectorExpression(
                    key,
                    operator,
                    tuple(str(value) for value in values)
                    if isinstance(values, list)
                    else (),
                )
            )
    return LabelSelector(labels, tuple(expressions), present=True)
```

Add `_expression_matches` and `matches_selector` with these exact operators:
`In`, `NotIn`, `Exists`, and `DoesNotExist`. `NotIn` returns true when the key
is absent, matching Kubernetes selector behavior. An absent selector always
returns false. An explicit empty selector returns `empty_matches`.

- [ ] **Step 4: Run selector tests GREEN**

Run:

```bash
uv run pytest -p no:tach tests/k8s/test_selectors.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Characterize PDB behavior before refactoring drain**

Add to `tests/k8s/test_drain.py`:

```python
def test_policy_v1_empty_pdb_selector_matches_all_pods() -> None:
    plan = build_drain_plan(
        [_pod("api-0", labels={"app": "api"})],
        [_pdb("all", selector={}, disruptions_allowed=0, api_version="policy/v1")],
    )
    assert plan.targets[0].pdb_blocked == "all"


def test_policy_v1beta1_empty_pdb_selector_matches_no_pods() -> None:
    plan = build_drain_plan(
        [_pod("api-0", labels={"app": "api"})],
        [_pdb("legacy", selector={}, disruptions_allowed=0, api_version="policy/v1beta1")],
    )
    assert plan.targets[0].pdb_blocked is None
```

Adapt the existing `_pdb` fixture signature rather than adding a second fixture.

- [ ] **Step 6: Run the characterization tests GREEN before refactoring**

Run:

```bash
uv run pytest -p no:tach tests/k8s/test_drain.py -q
```

Expected: the existing implementation passes both tests.

- [ ] **Step 7: Replace drain's private selector evaluator**

Delete `_expression_matches`, `_selector_matches`, and
`_pdb_selector_matches`. Parse each PDB selector with
`parse_label_selector`; call `matches_selector` with
`empty_matches=api_version == "policy/v1"`. Preserve terminal-pod and allowance
allocation behavior unchanged.

- [ ] **Step 8: Verify and commit Task 1**

Run:

```bash
uv run pytest -p no:tach tests/k8s/test_selectors.py tests/k8s/test_drain.py -q
uv run ruff check --fix src/korvid/k8s/selectors.py src/korvid/k8s/drain.py tests/k8s/test_selectors.py tests/k8s/test_drain.py
uv run ruff format src/korvid/k8s/selectors.py src/korvid/k8s/drain.py tests/k8s/test_selectors.py tests/k8s/test_drain.py
uv run mypy src/korvid/k8s/selectors.py src/korvid/k8s/drain.py
```

Expected: all commands exit 0.

Commit:

```bash
git add src/korvid/k8s/selectors.py src/korvid/k8s/drain.py tests/k8s/test_selectors.py tests/k8s/test_drain.py
git commit -m "refactor(k8s): share Kubernetes selector semantics"
```

---

### Task 2: Metadata-only relationship fact extraction

**Files:**
- Create: `src/korvid/k8s/relationship_facts.py`
- Create: `tests/k8s/test_relationship_facts.py`
- Modify: `src/korvid/k8s/models.py`
- Modify: `tests/k8s/test_models.py`

**Interfaces:**
- Consumes: `LabelSelector` and `parse_label_selector` from Task 1.
- Produces:
  - `RelationKind` values `owned_by`, `selects`, `routes_to`, `uses_volume`,
    `uses_config`, `managed_by`, `protected_by`, `scheduled_on`, `bound_to`.
  - `FactConfidence` values `declared`, `observed`, `inferred`.
  - `TargetReference(group, kind, namespace, name, uid)`.
  - `ReferenceFact(relation, target, confidence, field)`.
  - `SelectorFact(relation, target_group, target_kind, selector, confidence, field, empty_matches, match_is_subject)`.
  - `ReferenceGrantFact(from_group, from_kind, to_group, to_kind, namespace, field)`.
  - `RelationshipFacts(api_group, references, selectors, grants)`.
  - `extract_relationship_facts(kind, group, api_version, manifest)`.
- `GenericSummary.relationships` and `PodSummary.relationships` default to an
  empty `RelationshipFacts`.

- [ ] **Step 1: Add RED tests for owner, storage, config, and node facts**

```python
# tests/k8s/test_relationship_facts.py
from korvid.k8s.relationship_facts import (
    FactConfidence,
    RelationKind,
    extract_relationship_facts,
)


def test_pod_facts_include_metadata_only_references() -> None:
    manifest = {
        "metadata": {
            "name": "api-0",
            "namespace": "prod",
            "uid": "pod-1",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "api-abc",
                    "uid": "rs-1",
                }
            ],
        },
        "spec": {
            "nodeName": "node-a",
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "api-data"}},
                {"name": "cfg", "configMap": {"name": "api-config"}},
                {"name": "tls", "secret": {"secretName": "api-tls"}},
            ],
            "containers": [
                {
                    "name": "api",
                    "env": [
                        {
                            "name": "TOKEN",
                            "valueFrom": {"secretKeyRef": {"name": "api-token", "key": "token"}},
                        },
                        {"name": "LITERAL", "value": "must-not-be-retained"},
                    ],
                    "command": ["must-not-be-retained"],
                }
            ],
        },
    }
    facts = extract_relationship_facts("Pod", "", "v1", manifest)
    pairs = {(fact.relation, fact.target.kind, fact.target.name) for fact in facts.references}
    assert (RelationKind.OWNED_BY, "ReplicaSet", "api-abc") in pairs
    assert (RelationKind.USES_VOLUME, "PersistentVolumeClaim", "api-data") in pairs
    assert (RelationKind.USES_CONFIG, "ConfigMap", "api-config") in pairs
    assert (RelationKind.USES_CONFIG, "Secret", "api-tls") in pairs
    assert (RelationKind.USES_CONFIG, "Secret", "api-token") in pairs
    assert (RelationKind.SCHEDULED_ON, "Node", "node-a") in pairs
    assert all(fact.confidence is not FactConfidence.INFERRED for fact in facts.references)
    assert "must-not-be-retained" not in repr(facts)
    assert "token" not in repr(facts)
```

- [ ] **Step 2: Add RED tests for selectors and routing**

Add these named tests to the same file:

```python
def test_service_absent_and_empty_selectors_emit_no_fact() -> None:
    absent = extract_relationship_facts(
        "Service", "", "v1", {"metadata": {"name": "external", "namespace": "prod"}}
    )
    empty = extract_relationship_facts(
        "Service",
        "",
        "v1",
        {"metadata": {"name": "external", "namespace": "prod"}, "spec": {"selector": {}}},
    )
    assert absent.selectors == ()
    assert empty.selectors == ()


def test_policy_v1_empty_pdb_selector_matches_all() -> None:
    facts = extract_relationship_facts(
        "PodDisruptionBudget",
        "policy",
        "v1",
        {"metadata": {"name": "all", "namespace": "prod"}, "spec": {"selector": {}}},
    )
    assert facts.selectors[0].target_kind == "Pod"
    assert facts.selectors[0].empty_matches is True
    assert facts.selectors[0].match_is_subject is True
    assert facts.selectors[0].field == "spec.selector"


def test_endpoint_slice_target_ref_is_observed() -> None:
    facts = extract_relationship_facts(
        "EndpointSlice",
        "discovery.k8s.io",
        "v1",
        {
            "metadata": {"name": "api-abc", "namespace": "prod"},
            "endpoints": [
                {
                    "targetRef": {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "namespace": "prod",
                        "name": "api-0",
                        "uid": "pod-1",
                    }
                }
            ],
        },
    )
    target = facts.references[0]
    assert target.target.kind == "Pod"
    assert target.target.uid == "pod-1"
    assert target.confidence is FactConfidence.OBSERVED
    assert target.field == "endpoints[0].targetRef"


def test_http_route_and_reference_grant_retain_cross_namespace_policy() -> None:
    route = extract_relationship_facts(
        "HTTPRoute",
        "gateway.networking.k8s.io",
        "v1",
        {
            "metadata": {"name": "public", "namespace": "edge"},
            "spec": {
                "rules": [
                    {
                        "backendRefs": [
                            {
                                "group": "",
                                "kind": "Service",
                                "namespace": "prod",
                                "name": "api",
                            }
                        ]
                    }
                ]
            },
        },
    )
    grant = extract_relationship_facts(
        "ReferenceGrant",
        "gateway.networking.k8s.io",
        "v1beta1",
        {
            "metadata": {"name": "edge-to-services", "namespace": "prod"},
            "spec": {
                "from": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "HTTPRoute",
                        "namespace": "edge",
                    }
                ],
                "to": [{"group": "", "kind": "Service"}],
            },
        },
    )
    assert route.references[0].target.namespace == "prod"
    assert grant.grants[0].from_namespace == "edge"
    assert grant.grants[0].to_kind == "Service"


def test_malformed_relationship_lists_are_ignored() -> None:
    facts = extract_relationship_facts(
        "Ingress",
        "networking.k8s.io",
        "v1",
        {"metadata": {"name": "bad", "namespace": "prod"}, "spec": {"rules": "bad"}},
    )
    assert facts.references == ()
```

Also add `test_deployment_selector_targets_pods`,
`test_ingress_default_and_path_backends_target_services`,
`test_pvc_and_pv_binding_references`, and assert their complete dataclass
values, field paths, confidence values, and selector direction. Deployment and
PDB selectors set `match_is_subject=True`; Service selectors set it to `False`.

- [ ] **Step 3: Run extraction tests RED**

Run:

```bash
uv run pytest -p no:tach tests/k8s/test_relationship_facts.py -q
```

Expected: collection fails because `relationship_facts.py` does not exist.

- [ ] **Step 4: Implement focused extraction helpers**

Create `relationship_facts.py` with:

```python
class RelationKind(StrEnum):
    OWNED_BY = "owned_by"
    SELECTS = "selects"
    ROUTES_TO = "routes_to"
    USES_VOLUME = "uses_volume"
    USES_CONFIG = "uses_config"
    MANAGED_BY = "managed_by"
    PROTECTED_BY = "protected_by"
    SCHEDULED_ON = "scheduled_on"
    BOUND_TO = "bound_to"


class FactConfidence(StrEnum):
    DECLARED = "declared"
    OBSERVED = "observed"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class TargetReference:
    group: str
    kind: str
    namespace: str
    name: str
    uid: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceFact:
    relation: RelationKind
    target: TargetReference
    confidence: FactConfidence
    field: str


@dataclass(frozen=True, slots=True)
class SelectorFact:
    relation: RelationKind
    target_group: str
    target_kind: str
    selector: LabelSelector
    confidence: FactConfidence
    field: str
    empty_matches: bool = False
    match_is_subject: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceGrantFact:
    from_group: str
    from_kind: str
    from_namespace: str
    to_group: str
    to_kind: str
    namespace: str
    field: str


@dataclass(frozen=True, slots=True)
class RelationshipFacts:
    api_group: str = ""
    references: tuple[ReferenceFact, ...] = ()
    selectors: tuple[SelectorFact, ...] = ()
    grants: tuple[ReferenceGrantFact, ...] = ()
```

Keep extractors separate:
`_owner_references`, `_pod_spec`, `_selector`, `_endpoint_targets`,
`_ingress_backends`, `_gateway_backends`, `_reference_grants`, and
`_volume_binding`.

`_pod_spec` receives only a PodSpec mapping. Call it for:

- Pod: `spec`;
- Deployment/ReplicaSet/StatefulSet/DaemonSet/Job:
  `spec.template.spec`;
- CronJob: `spec.jobTemplate.spec.template.spec`.

It reads only volume references, projected volume sources, `envFrom`,
`env[].valueFrom`, `imagePullSecrets`, and `nodeName`. It never copies the
manifest or retains unrelated values.

Parse `apiVersion` with `partition("/")`: core `v1` produces group `""`;
`apps/v1` produces group `"apps"`. Owner namespace defaults to the subject
namespace. Cluster-scoped Node and PV targets use namespace `""`.

- [ ] **Step 5: Run extraction tests GREEN**

Run:

```bash
uv run pytest -p no:tach tests/k8s/test_relationship_facts.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Add summary propagation tests RED**

Add to `tests/k8s/test_models.py`:

```python
def test_generic_summary_carries_relationship_facts() -> None:
    summary = summary_for(
        "Service",
        {
            "apiVersion": "v1",
            "metadata": {"name": "api", "namespace": "prod", "uid": "svc-1"},
            "spec": {"selector": {"app": "api"}},
        },
        group="",
    )
    assert summary.relationships.selectors[0].target_kind == "Pod"


def test_pod_summary_never_retains_secret_values() -> None:
    summary = PodSummary.from_manifest(
        {
            "metadata": {"name": "api", "namespace": "prod", "uid": "pod-1"},
            "spec": {
                "containers": [{"name": "api"}],
                "volumes": [{"name": "s", "secret": {"secretName": "api-tls"}}],
            },
            "data": {"token": "forbidden-value"},
        }
    )
    assert summary.relationships.references[0].target.name == "api-tls"
    assert "forbidden-value" not in repr(summary)
```

- [ ] **Step 7: Add `relationships` to summaries**

Add:

```python
relationships: RelationshipFacts = RelationshipFacts()
```

to `GenericSummary` and `PodSummary`. In
`GenericSummary.from_manifest`, pass:

```python
relationships=extract_relationship_facts(
    kind,
    _api_group(manifest.get("apiVersion")) if group is None else group,
    _api_version(manifest.get("apiVersion")),
    manifest,
)
```

Update `GenericSummary.from_manifest` to accept keyword-only
`group: str | None = None`, and have `summary_for` pass its authoritative group.
`PodSummary.from_manifest` passes core group and `v1`. Because specialized
summaries use `**vars(base)`, they inherit facts automatically.

- [ ] **Step 8: Verify and commit Task 2**

Run:

```bash
uv run pytest -p no:tach tests/k8s/test_relationship_facts.py tests/k8s/test_models.py -q
uv run ruff check --fix src/korvid/k8s/relationship_facts.py src/korvid/k8s/models.py tests/k8s/test_relationship_facts.py tests/k8s/test_models.py
uv run ruff format src/korvid/k8s/relationship_facts.py src/korvid/k8s/models.py tests/k8s/test_relationship_facts.py tests/k8s/test_models.py
uv run mypy src/korvid/k8s/relationship_facts.py src/korvid/k8s/models.py
```

Expected: all commands exit 0.

Commit:

```bash
git add src/korvid/k8s/relationship_facts.py src/korvid/k8s/models.py tests/k8s/test_relationship_facts.py tests/k8s/test_models.py
git commit -m "feat(k8s): extract safe resource relationship facts"
```

---

### Task 3: Immutable graph model and named-reference resolution

**Files:**
- Create: `src/korvid/core/relationships.py`
- Create: `tests/core/test_relationships.py`

**Interfaces:**
- Consumes: `RelationshipFacts`, `ReferenceFact`, and `ResourceMeta`.
- Produces:
  - `GraphResource(group, kind, namespace, name, uid)`.
  - `EvidencePointer(resource, field)`.
  - `EdgeResolution`: `resolved`, `missing`, `invalid`.
  - `RelationshipEdge(subject, target, relation, confidence, evidence, resolution, explanation)`.
  - `CoverageState`: `complete`, `forbidden`, `unavailable`, `failed`, `capped`.
  - `CoverageRecord(group, resource, scope, state, detail)`.
  - `GraphInput(meta, summary)`.
  - `GraphLimits(max_resources=10_000, max_edges=50_000, max_depth=5, max_nodes=500)`.
  - `RelationshipGraph(nodes, edges, coverage, limits, truncated)`.
  - `build_relationship_graph(inputs, coverage, limits=GraphLimits())`.

- [ ] **Step 1: Add RED tests for identity and UID-safe resolution**

```python
# tests/core/test_relationships.py
from korvid.core.relationships import (
    CoverageRecord,
    CoverageState,
    EdgeResolution,
    GraphInput,
    build_relationship_graph,
)


def test_owner_uid_does_not_reconnect_to_replacement_with_same_name() -> None:
    deployment = _input(
        "Deployment", "apps", "prod", "api", "deploy-new", relationships=_facts()
    )
    replica_set = _input(
        "ReplicaSet",
        "apps",
        "prod",
        "api-abc",
        "rs-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "prod",
                    "api",
                    uid="deploy-old",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    graph = build_relationship_graph([deployment, replica_set], [_complete("deployments")])
    edge = graph.edges[0]
    assert edge.target.uid == "deploy-old"
    assert edge.resolution is EdgeResolution.MISSING
    assert "deploy-new" not in repr(edge)


def test_named_reference_resolves_current_uid() -> None:
    pod = _input(
        "Pod",
        "",
        "prod",
        "api-0",
        "pod-1",
        relationships=_facts(
            references=(
                _ref(
                    "uses_config",
                    "",
                    "ConfigMap",
                    "prod",
                    "api-config",
                    field="spec.volumes[0].configMap.name",
                ),
            )
        ),
    )
    config = _input("ConfigMap", "", "prod", "api-config", "cm-1")
    graph = build_relationship_graph([pod, config], [_complete("pods"), _complete("configmaps")])
    assert graph.edges[0].target.uid == "cm-1"
    assert graph.edges[0].resolution is EdgeResolution.RESOLVED
```

Test helpers build real `GenericSummary` or `PodSummary` instances; do not use
`Any` or dictionaries in the graph API.

- [ ] **Step 2: Add RED tests for invalid namespace, coverage, dedup, and caps**

```python
def test_cross_namespace_namespaced_owner_is_invalid() -> None:
    child = _input(
        "ReplicaSet",
        "apps",
        "prod",
        "api-abc",
        "rs-1",
        relationships=_facts(
            references=(
                _ref(
                    "owned_by",
                    "apps",
                    "Deployment",
                    "other",
                    "api",
                    uid="deploy-1",
                    field="metadata.ownerReferences[0]",
                ),
            )
        ),
    )
    graph = build_relationship_graph([child], [_complete("replicasets")])
    assert graph.edges[0].resolution is EdgeResolution.INVALID
    assert "cross-namespace owner" in graph.edges[0].explanation


def test_forbidden_coverage_keeps_graph_incomplete() -> None:
    record = CoverageRecord("", "secrets", "prod", CoverageState.FORBIDDEN, "RBAC denied")
    graph = build_relationship_graph([], [record])
    assert graph.incomplete
    assert graph.coverage == (record,)


def test_caps_are_deterministic_and_visible() -> None:
    inputs = [
        _input("ConfigMap", "", "prod", "z", "uid-z"),
        _input("ConfigMap", "", "prod", "a", "uid-a"),
    ]
    graph = build_relationship_graph(
        inputs,
        [_complete("configmaps")],
        limits=GraphLimits(max_resources=1, max_edges=1),
    )
    assert [node.name for node in graph.nodes] == ["a"]
    assert graph.truncated
    assert any(record.state is CoverageState.CAPPED for record in graph.coverage)


def test_coverage_detail_is_flattened_and_bounded() -> None:
    record = CoverageRecord("", "pods", "prod", CoverageState.FAILED, "line1\nline2" + "x" * 600)
    assert "\n" not in record.detail
    assert len(record.detail) == 512
```

Also add `test_cluster_scoped_owner_is_valid`,
`test_identical_edges_deduplicate`, `test_absent_named_target_is_missing`, and
`test_unavailable_coverage_keeps_graph_incomplete`; each asserts the complete
edge or coverage value rather than only a count.

- [ ] **Step 3: Run graph tests RED**

Run:

```bash
uv run pytest -p no:tach tests/core/test_relationships.py -q
```

Expected: collection fails because `korvid.core.relationships` does not exist.

- [ ] **Step 4: Implement graph values and indexes**

Use frozen, slotted dataclasses. Sort inputs by
`(group, kind, namespace, name, uid)` before applying caps. Build both indexes:

```python
by_uid: dict[tuple[str, str], GraphResource]
by_name: dict[tuple[str, str, str, str], GraphResource]
```

The UID key is `(group, uid)`; never match only by name when a reference carries
a UID. The name key is `(group, kind, namespace, name)`. For namespaced
references, reject a target namespace different from the subject namespace
unless the relation is a Gateway backend authorized by Task 4.

Construct unresolved `GraphResource` values from the reference itself so
missing and stale targets remain renderable. Flatten control characters in
coverage detail with spaces and cap at 512 characters.

Read only `name`, `namespace`, `uid`, `labels`, and `relationships` from each
summary. Never copy `custom`, status text, or the summary object into graph
nodes or edges.

- [ ] **Step 5: Implement immutable queries**

`RelationshipGraph` exposes:

```python
def dependencies_of(self, resource: GraphResource) -> tuple[RelationshipEdge, ...]
def dependents_of(self, resource: GraphResource) -> tuple[RelationshipEdge, ...]
def walk_dependents(
    self,
    resource: GraphResource,
    *,
    max_depth: int | None = None,
    max_nodes: int | None = None,
) -> TraversalResult
```

`TraversalResult(edges, cycles, truncated)` uses breadth-first traversal,
deduplicates resources by `(group, kind, namespace, name, uid)`, records an edge
that returns to an active/visited node in `cycles`, and excludes the root from
results.

Add:

```python
def test_walk_dependents_is_breadth_first_and_cycle_safe() -> None:
    graph = _owner_graph(
        ("Deployment", "deploy-1"),
        ("ReplicaSet", "rs-1"),
        ("Pod", "pod-1"),
        cycle_to="deploy-1",
    )
    root = _resource(graph, "Deployment", "deploy-1")
    result = graph.walk_dependents(root)
    assert [(edge.subject.kind, edge.target.kind) for edge in result.edges] == [
        ("ReplicaSet", "Deployment"),
        ("Pod", "ReplicaSet"),
    ]
    assert len(result.cycles) == 1
    assert root not in {edge.subject for edge in result.edges}


def test_walk_dependents_reports_depth_and_node_caps() -> None:
    graph = _wide_owner_graph()
    root = _resource(graph, "Deployment", "deploy-1")
    by_depth = graph.walk_dependents(root, max_depth=1, max_nodes=500)
    by_nodes = graph.walk_dependents(root, max_depth=5, max_nodes=1)
    assert all(edge.target == root for edge in by_depth.edges)
    assert len(by_nodes.edges) == 1
    assert by_nodes.truncated
```

- [ ] **Step 6: Run graph tests GREEN**

Run:

```bash
uv run pytest -p no:tach tests/core/test_relationships.py -q
uv run ruff check --fix src/korvid/core/relationships.py tests/core/test_relationships.py
uv run ruff format src/korvid/core/relationships.py tests/core/test_relationships.py
uv run mypy src/korvid/core/relationships.py
uv run tach check
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit Task 3**

```bash
git add src/korvid/core/relationships.py tests/core/test_relationships.py
git commit -m "feat(core): build immutable relationship snapshots"
```

---

### Task 4: Selector, routing, and ReferenceGrant joins

**Files:**
- Modify: `src/korvid/core/relationships.py`
- Modify: `tests/core/test_relationships.py`

**Interfaces:**
- Consumes: `SelectorFact`, `ReferenceGrantFact`, `matches_selector`.
- Extends `build_relationship_graph` to resolve selector-derived and
  Gateway cross-namespace edges.
- Adds no public type beyond Task 3.

- [ ] **Step 1: Add RED tests for selector joins**

```python
def test_service_selector_creates_declared_pod_dependencies() -> None:
    service = _service_input("api", {"app": "api"})
    api = _pod_input("api-0", labels={"app": "api"})
    worker = _pod_input("worker-0", labels={"app": "worker"})
    graph = build_relationship_graph(
        [service, api, worker], [_complete("services"), _complete("pods")]
    )
    edges = graph.dependencies_of(_resource(graph, "Service", "api"))
    assert [(edge.target.kind, edge.target.name) for edge in edges] == [("Pod", "api-0")]
    assert edges[0].relation.value == "selects"
    assert edges[0].confidence.value == "declared"
```

Add:

```python
def test_duplicate_selectors_create_edges_for_each_subject() -> None:
    api = _pod_input("api-0", labels={"app": "api"})
    graph = build_relationship_graph(
        [
            _service_input("public", {"app": "api"}),
            _service_input("internal", {"app": "api"}),
            api,
        ],
        [_complete("services"), _complete("pods")],
    )
    assert [(edge.subject.name, edge.target.name) for edge in graph.edges] == [
        ("internal", "api-0"),
        ("public", "api-0"),
    ]


def test_selectors_do_not_cross_namespaces() -> None:
    graph = build_relationship_graph(
        [
            _service_input("api", {"app": "api"}, namespace="prod"),
            _pod_input("api-0", labels={"app": "api"}, namespace="other"),
        ],
        [_complete("services"), _complete("pods")],
    )
    assert graph.edges == ()


def test_policy_v1_empty_pdb_selector_matches_every_pod_in_namespace() -> None:
    graph = build_relationship_graph(
        [_pdb_input("availability", selector={}, empty_matches=True), _pod_input("api-0")],
        [_complete("poddisruptionbudgets"), _complete("pods")],
    )
    assert graph.edges[0].relation.value == "protected_by"
    assert graph.edges[0].subject.kind == "Pod"
    assert graph.edges[0].target.kind == "PodDisruptionBudget"
```

Also add `test_service_absent_and_empty_selectors_create_no_edges`,
`test_workload_and_pdb_match_expressions`, and
`test_unmatched_selector_creates_no_edge_without_changing_coverage`.

- [ ] **Step 2: Add RED tests for EndpointSlice and routing joins**

```python
def test_cross_namespace_route_requires_matching_reference_grant() -> None:
    route = _http_route_input("public", "edge", backend_namespace="prod")
    service = _service_input("api", {}, namespace="prod")
    without_grant = build_relationship_graph(
        [route, service], [_complete("httproutes"), _complete("services")]
    )
    assert without_grant.edges[0].resolution is EdgeResolution.INVALID

    with_grant = build_relationship_graph(
        [route, service, _reference_grant_input("edge", "prod")],
        [
            _complete("httproutes"),
            _complete("services"),
            _complete("referencegrants"),
        ],
    )
    assert with_grant.edges[0].resolution is EdgeResolution.RESOLVED
    assert with_grant.edges[0].target.uid == service.summary.uid


@pytest.mark.parametrize(
    ("from_group", "from_kind", "to_kind"),
    [
        ("wrong.example", "HTTPRoute", "Service"),
        ("gateway.networking.k8s.io", "GRPCRoute", "Service"),
        ("gateway.networking.k8s.io", "HTTPRoute", "ConfigMap"),
    ],
)
def test_reference_grant_constraints_are_exact(
    from_group: str, from_kind: str, to_kind: str
) -> None:
    graph = build_relationship_graph(
        [
            _http_route_input("public", "edge", backend_namespace="prod"),
            _service_input("api", {}, namespace="prod"),
            _reference_grant_input(
                "edge",
                "prod",
                from_group=from_group,
                from_kind=from_kind,
                to_kind=to_kind,
            ),
        ],
        [_complete("httproutes"), _complete("services"), _complete("referencegrants")],
    )
    assert graph.edges[0].resolution is EdgeResolution.INVALID
```

Also add `test_endpoint_slice_target_ref_resolves_pod`,
`test_ingress_backend_is_same_namespace_only`,
`test_http_route_same_namespace_backend_resolves`, and
`test_optional_gateway_unavailable_coverage_does_not_abort_build`.

- [ ] **Step 3: Run focused tests RED**

Run:

```bash
uv run pytest -p no:tach tests/core/test_relationships.py -q
```

Expected: selector/routing assertions fail because Task 3 resolves only
`ReferenceFact` values.

- [ ] **Step 4: Implement deterministic joins**

Build a namespace-indexed Pod label table. For each `SelectorFact`, call
`matches_selector` with the fact's extracted `empty_matches` policy and create
one edge per matching target sorted by resource identity. When
`match_is_subject=False`, the selector-declaring object is the edge subject
(Service -> Pod, `selects`). When `match_is_subject=True`, the matched Pod is
the edge subject (Pod -> workload, `managed_by`; Pod -> PDB, `protected_by`).
The evidence resource remains the selector-declaring object in both cases.

For Gateway backend references where target namespace differs:

1. collect `ReferenceGrantFact` values in the target namespace;
2. require one grant whose `from_group`/`from_kind` match the subject and whose
   `to_group`/`to_kind` match the target;
3. resolve when authorized;
4. otherwise retain the edge with `invalid` resolution and explanation
   `"cross-namespace backend has no matching ReferenceGrant"`.

Do not infer a grant from object presence. Apply the global edge cap after
sorting all candidate edges by subject, relation, target, evidence field.

- [ ] **Step 5: Verify and commit Task 4**

Run:

```bash
uv run pytest -p no:tach tests/core/test_relationships.py -q
uv run ruff check --fix src/korvid/core/relationships.py tests/core/test_relationships.py
uv run ruff format src/korvid/core/relationships.py tests/core/test_relationships.py
uv run mypy src/korvid/core/relationships.py
uv run tach check
```

Expected: all commands exit 0.

Commit:

```bash
git add src/korvid/core/relationships.py tests/core/test_relationships.py
git commit -m "feat(core): join selector and routing relationships"
```

---

### Task 5: Bounded graph snapshot controller

**Files:**
- Create: `src/korvid/ui/relationship_controller.py`
- Create: `tests/ui/test_relationship_controller.py`

**Interfaces:**
- Consumes:
  - `list_objects(meta: ResourceMeta, namespace: str | None) -> Awaitable[list[GenericSummary]]`
  - discovered aliases;
  - `build_relationship_graph`.
- Produces:
  - `GraphSourceSpec(group, kind, plural, optional)`.
  - `GraphLoadLimits(max_concurrency=4, max_resources=10_000)`.
  - `RelationshipSnapshotLoader.load(root, namespace, aliases) -> RelationshipGraph`.
  - `graph_source_metas(root, namespace, aliases) -> tuple[ResourceMeta, ...]`.
- The loader performs no Textual operations; the app owns worker lifecycle.

- [ ] **Step 1: Add RED source-selection tests**

```python
# tests/ui/test_relationship_controller.py
def test_graph_sources_dedupe_aliases_by_gvr() -> None:
    aliases = {
        "pods": PODS_META,
        "pod": PODS_META,
        "po": PODS_META,
        "services": SERVICE_META,
    }
    sources = graph_source_metas(_root("Pod", "prod"), "prod", aliases)
    assert [(meta.group, meta.plural) for meta in sources].count(("", "pods")) == 1


def test_optional_gateway_routes_are_selected_when_discovered() -> None:
    aliases = _aliases(HTTP_ROUTE_META, REFERENCE_GRANT_META)
    sources = graph_source_metas(_root("Ingress", "prod"), "prod", aliases)
    assert HTTP_ROUTE_META in sources
    assert REFERENCE_GRANT_META in sources
```

The fixed source identities are:

- core: Pod, Service, ConfigMap, Secret, PersistentVolumeClaim,
  PersistentVolume, Node;
- apps: Deployment, ReplicaSet, StatefulSet, DaemonSet;
- batch: Job, CronJob;
- discovery.k8s.io: EndpointSlice;
- networking.k8s.io: Ingress;
- policy: PodDisruptionBudget;
- discovered `gateway.networking.k8s.io` Gateway, `*Route`, and ReferenceGrant
  resources.

`graph_source_metas` returns `(metas, missing_specs)`. A missing fixed source
becomes `unavailable`; missing Gateway specs are grouped into one optional
`gateway.networking.k8s.io/*` unavailable record.

- [ ] **Step 2: Add RED loader tests**

Use an async fake that records `(group, plural, namespace)` and raises
`ApiStatusError`:

```python
async def test_loader_classifies_forbidden_without_failing_other_sources() -> None:
    lister = _Lister(
        results={("", "pods"): [_pod_summary("api-0")]},
        errors={("", "secrets"): ApiStatusError(403, "Forbidden")},
    )
    graph = await RelationshipSnapshotLoader(lister).load(
        _root("Pod", "prod"), "prod", _aliases(PODS_META, SECRETS_META)
    )
    assert any(record.state is CoverageState.COMPLETE for record in graph.coverage)
    assert any(record.state is CoverageState.FORBIDDEN for record in graph.coverage)
    assert graph.incomplete
```

Add tests that:

```python
async def test_missing_gateway_discovery_is_visible_as_unavailable() -> None:
    graph = await RelationshipSnapshotLoader(_Lister()).load(
        _root("Pod", "prod"), "prod", _aliases(PODS_META)
    )
    record = next(
        item for item in graph.coverage if item.group == "gateway.networking.k8s.io"
    )
    assert record.resource == "*"
    assert record.state is CoverageState.UNAVAILABLE


async def test_loader_uses_namespace_only_for_namespaced_sources() -> None:
    lister = _Lister()
    await RelationshipSnapshotLoader(lister).load(
        _root("Pod", "prod"), "prod", _aliases(PODS_META, NODES_META)
    )
    assert lister.calls == [
        ("", "nodes", None),
        ("", "pods", "prod"),
    ]


async def test_loader_respects_concurrency_limit() -> None:
    lister = _BlockingLister()
    task = asyncio.create_task(
        RelationshipSnapshotLoader(
            lister, limits=GraphLoadLimits(max_concurrency=2)
        ).load(_root("Pod", "prod"), "prod", _many_aliases())
    )
    await lister.wait_until_started(2)
    assert lister.peak_concurrency == 2
    lister.release_all()
    await task


async def test_resource_cap_is_visible_and_deterministic() -> None:
    lister = _Lister(
        results={
            ("", "configmaps"): [_generic_summary(f"cfg-{index:02}") for index in range(3)],
            ("", "pods"): [_pod_summary("api-0")],
        }
    )
    graph = await RelationshipSnapshotLoader(
        lister, limits=GraphLoadLimits(max_resources=2)
    ).load(_root("Pod", "prod"), "prod", _aliases(CONFIG_MAPS_META, PODS_META))
    assert [node.name for node in graph.nodes] == ["cfg-00", "cfg-01"]
    assert any(record.state is CoverageState.CAPPED for record in graph.coverage)
```

Also add `test_unexpected_api_failure_is_flattened_as_failed`,
`test_all_namespaces_root_lists_namespaced_sources_with_none`, and
`test_source_order_is_group_plural_sorted`.

- [ ] **Step 3: Run controller tests RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_relationship_controller.py -q
```

Expected: collection fails because `relationship_controller.py` does not exist.

- [ ] **Step 4: Implement bounded LIST orchestration**

Use one `asyncio.Semaphore(max_concurrency)` around each call. Convert errors:

- `ApiStatusError.status == 403` -> `forbidden`;
- `ApiStatusError.status in {404, 405}` for optional Gateway sources ->
  `unavailable`;
- all other `ApiStatusError` and declared network exceptions -> `failed`.

Do not catch `BaseException` or `asyncio.CancelledError`. Each successful result
becomes `GraphInput(meta, summary)`. Apply the global resource cap in sorted
source order before calling `build_relationship_graph`.

- [ ] **Step 5: Verify and commit Task 5**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_relationship_controller.py -q
uv run ruff check --fix src/korvid/ui/relationship_controller.py tests/ui/test_relationship_controller.py
uv run ruff format src/korvid/ui/relationship_controller.py tests/ui/test_relationship_controller.py
uv run mypy src/korvid/ui/relationship_controller.py
uv run tach check
```

Expected: all commands exit 0.

Commit:

```bash
git add src/korvid/ui/relationship_controller.py tests/ui/test_relationship_controller.py
git commit -m "feat(ui): load bounded relationship snapshots"
```

---

### Task 6: Keyboard-navigable relationship screen

**Files:**
- Create: `src/korvid/ui/widgets/relationship_screen.py`
- Create: `tests/ui/test_relationship_screen.py`

**Interfaces:**
- Consumes: `RelationshipGraph`, root `GraphResource`.
- Produces:
  - `RelationshipScreen(graph, root)`.
  - Dismiss result `("goto", group, kind, namespace, name) | None`.
- Bindings:
  - `escape`: close;
  - `enter`: navigate resolved row;
  - `d`: toggle bounded dependent expansion;
  - `c`: toggle coverage details.

- [ ] **Step 1: Add RED rendering tests**

```python
# tests/ui/test_relationship_screen.py
async def test_screen_separates_dependencies_and_dependents() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        table = app.screen.query_one(DataTable)
        rows = [
            str(cell)
            for index in range(table.row_count)
            for cell in table.get_row_at(index)
        ]
        assert "Dependencies" in rows
        assert "Dependents" in rows
        assert "owned_by" in rows
        assert "declared" in rows
        assert "metadata.ownerReferences[0]" in rows
        await pilot.press("escape")


async def test_incomplete_banner_names_coverage_state() -> None:
    app = HostApp()
    screen = RelationshipScreen(_incomplete_graph(), _resource("Service", "api"))
    async with app.run_test():
        await app.push_screen(screen)
        banner = app.screen.query_one("#relationship-coverage", Static)
        assert "incomplete" in banner.renderable.plain.lower()
        assert "forbidden" in banner.renderable.plain.lower()
```

Use repository `until()` helpers for asynchronous Textual state; never use
wall-clock sleeps.

- [ ] **Step 2: Add RED interaction tests**

```python
async def test_enter_on_resolved_row_returns_exact_goto() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen, lambda value: setattr(app, "result", value))
        await pilot.press("down", "enter")
        assert app.result == ("goto", "", "Pod", "prod", "api-0")


async def test_enter_on_missing_row_keeps_screen_open() -> None:
    app = HostApp()
    screen = RelationshipScreen(_graph_with_missing_target(), _resource("Pod", "api-0"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("down", "enter")
        assert app.screen is screen
        assert "missing" in app.screen.query_one("#relationship-status", Static).renderable.plain


async def test_expansion_and_coverage_remain_bounded() -> None:
    app = HostApp()
    screen = RelationshipScreen(_cyclic_capped_graph(), _resource("Deployment", "api"))
    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.press("d")
        table = app.screen.query_one(DataTable)
        text = "\n".join(
            str(cell)
            for index in range(table.row_count)
            for cell in table.get_row_at(index)
        )
        assert "cycle" in text.lower()
        assert "capped" in text.lower()
        assert table.row_count <= screen.graph.limits.max_nodes + 4
        await pilot.press("c")
        assert "forbidden" in app.screen.query_one("#relationship-coverage", Static).renderable.plain


async def test_markup_names_and_secret_metadata_render_literally() -> None:
    app = HostApp()
    screen = RelationshipScreen(_secret_graph("[red]tls[/]"), _resource("Pod", "api-0"))
    async with app.run_test():
        await app.push_screen(screen)
        rendered = app.screen.query_one(DataTable).get_row_at(1)
        assert "[red]tls[/]" in {str(cell) for cell in rendered}
        assert "secret-value" not in repr(screen)
```

- [ ] **Step 3: Run screen tests RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_relationship_screen.py -q
```

Expected: collection fails because the screen module does not exist.

- [ ] **Step 4: Implement the adjacency table**

Use `ModalScreen`, `DataTable`, `Static`, and `Footer`. Keep row payload in a
mapping keyed by `RowKey`; never parse display strings to navigate. Render
columns:

```text
DIRECTION | RELATION | RESOURCE | CONFIDENCE | STATE | EVIDENCE
```

Set `markup=False` for user-controlled names and details. Direct dependencies
and dependents are separate labelled rows. Expansion adds a `DEPTH` prefix and
never exceeds `GraphLimits.max_nodes`.

- [ ] **Step 5: Verify and commit Task 6**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_relationship_screen.py -q
uv run ruff check --fix src/korvid/ui/widgets/relationship_screen.py tests/ui/test_relationship_screen.py
uv run ruff format src/korvid/ui/widgets/relationship_screen.py tests/ui/test_relationship_screen.py
uv run mypy src/korvid/ui/widgets/relationship_screen.py
```

Expected: all commands exit 0.

Commit:

```bash
git add src/korvid/ui/widgets/relationship_screen.py tests/ui/test_relationship_screen.py
git commit -m "feat(ui): add the operational relationship graph view"
```

---

### Task 7: App action, composition wiring, and navigation

**Files:**
- Modify: `src/korvid/ui/app.py`
- Modify: `src/korvid/__main__.py`
- Create: `tests/ui/test_relationship_flow.py`
- Modify: `tests/ui/test_app.py`
- Modify: `tests/test_main_wiring.py`

**Interfaces:**
- `KorvidApp.__init__` gains keyword-only-compatible dependency:
  `list_relationship_objects: Callable[[ResourceMeta, str | None], Awaitable[list[GenericSummary]]] | None = None`.
- Composition root wires `kube.list_objects`.
- App action `action_relationships()` is bound to `g`.
- The action uses the selected row's exact group/kind/namespace/name/UID,
  captures `_ctx_epoch`, runs the loader in an exclusive worker group, refuses
  stale results after context switch, and opens `RelationshipScreen`.
- Goto results reuse `_jump_to_object`; no second navigation implementation.

- [ ] **Step 1: Add RED app-flow tests**

```python
# tests/ui/test_relationship_flow.py
async def test_g_opens_relationships_for_selected_resource(app_env: AppEnv) -> None:
    app_env.relationship_lister.add(PODS_META, [_pod_summary("api-0", uid="pod-1")])
    async with app_env.app.run_test() as pilot:
        await app_env.show_pods(pilot)
        await pilot.press("g")
        await until(
            pilot,
            lambda: isinstance(app_env.app.screen, RelationshipScreen),
            label="relationship screen opened",
        )
        screen = cast(RelationshipScreen, app_env.app.screen)
        assert screen.root.uid == "pod-1"


async def test_context_switch_discards_inflight_graph(app_env: AppEnv) -> None:
    app_env.relationship_lister.pause()
    async with app_env.app.run_test() as pilot:
        await app_env.show_pods(pilot)
        await pilot.press("g")
        await app_env.switch_context("other")
        app_env.relationship_lister.resume()
        await until(pilot, lambda: not app_env.app.workers, label="graph worker reaped")
        assert not isinstance(app_env.app.screen, RelationshipScreen)
```

Add tests for no selected row, missing lister, a failed root source, and goto
navigation from the graph screen:

```python
async def test_g_without_selected_row_does_not_start_loader(app_env: AppEnv) -> None:
    async with app_env.app.run_test() as pilot:
        await pilot.press("g")
        assert app_env.relationship_lister.calls == []
        assert not isinstance(app_env.app.screen, RelationshipScreen)


async def test_g_is_unavailable_without_relationship_lister(app_env: AppEnv) -> None:
    app_env.app._relationship_loader = None
    async with app_env.app.run_test() as pilot:
        await app_env.show_pods(pilot)
        await pilot.press("g")
        assert not isinstance(app_env.app.screen, RelationshipScreen)


async def test_graph_goto_reuses_normal_navigation(app_env: AppEnv) -> None:
    async with app_env.app.run_test() as pilot:
        await app_env.show_pods(pilot)
        await pilot.press("g")
        await until(
            pilot,
            lambda: isinstance(app_env.app.screen, RelationshipScreen),
            label="relationship screen opened",
        )
        await pilot.press("down", "enter")
        await until(
            pilot,
            lambda: app_env.app.current_kind == "pods",
            label="pod view restored",
        )
        assert app_env.app.current_namespace == "prod"
        assert app_env.app._selected_name() == "api-0"
```

`test_failed_root_source_shows_incomplete_graph` configures the fake Pod LIST to
raise `ApiStatusError(403, "Forbidden")`, opens the screen, and asserts the
coverage banner names `forbidden`.

- [ ] **Step 2: Run app-flow tests RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_relationship_flow.py -q
```

Expected: `g` has no relationship action and the tests fail.

- [ ] **Step 3: Wire the controller and screen**

Add:

```python
Binding("g", "relationships", "Relationships", id="relationships")
```

Create one `RelationshipSnapshotLoader` in `KorvidApp.__init__` when the lister
is available. `action_relationships` captures root identity from the selected
summary and `ResourceMeta`, starts a worker in group `"relationships"`, checks
the captured epoch before opening the screen, and handles its goto result with
the existing jump path.

Keep API calls in the worker. Do not access Textual widgets from the loader.

The worker structure is:

```python
def action_relationships(self) -> None:
    target = self._selected_relationship_root()
    if target is None or self._relationship_loader is None:
        return
    self.run_worker(
        self._load_relationships(target, self._ctx_epoch),
        exclusive=True,
        group="relationships",
    )


async def _load_relationships(self, target: GraphResource, epoch: int) -> None:
    assert self._relationship_loader is not None
    graph = await self._relationship_loader.load(
        target,
        None if self.current_namespace == ALL_NAMESPACES else self.current_namespace,
        self.aliases,
    )
    if self._ctx_switch_crossed(epoch):
        return
    await self.push_screen(
        RelationshipScreen(graph, target),
        self._on_relationship_result,
    )
```

Invalid selection follows the existing action-unavailable notification pattern;
do not silently return when a row is selected but its identity cannot be built.

- [ ] **Step 4: Add composition-root wiring test RED then GREEN**

Extend the existing fake Kube client in `tests/test_main_wiring.py` with:

```python
async def list_objects(
    self, meta: ResourceMeta, namespace: str | None
) -> list[GenericSummary]:
    self.relationship_calls.append((meta, namespace))
    return []
```

Capture the `KorvidApp` constructor kwargs in the existing app fake and assert:

```python
assert captured_app_kwargs["list_relationship_objects"] == kube.list_objects
```

Update direct `KorvidApp` construction only where strict constructor signatures
require it; the dependency defaults to `None`.

Wire in `src/korvid/__main__.py`:

```python
list_relationship_objects=kube.list_objects,
```

- [ ] **Step 5: Verify and commit Task 7**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_relationship_flow.py tests/ui/test_app.py tests/test_main_wiring.py -q
uv run ruff check --fix src/korvid/ui/app.py src/korvid/__main__.py tests/ui/test_relationship_flow.py tests/ui/test_app.py tests/test_main_wiring.py
uv run ruff format src/korvid/ui/app.py src/korvid/__main__.py tests/ui/test_relationship_flow.py tests/ui/test_app.py tests/test_main_wiring.py
uv run mypy src/korvid/ui/app.py src/korvid/__main__.py
uv run tach check
```

Expected: all commands exit 0.

Commit:

```bash
git add src/korvid/ui/app.py src/korvid/__main__.py tests/ui/test_relationship_flow.py tests/ui/test_app.py tests/test_main_wiring.py
git commit -m "feat(ui): wire relationship navigation into the app"
```

---

### Task 8: Documentation and complete verification

**Files:**
- Create: `docs/resource-relationships.md`
- Modify: `README.md`
- Modify: `docs/dev/specs/2026-08-12-korvid-architecture.md`
- Modify: `AGENTS.md` only if the implementation changes the documented layer
  map; otherwise leave it untouched.

**Interfaces:**
- Documents the `g` binding, graph meanings, coverage states, bounds,
  cross-namespace behavior, and Secret-safety guarantee.
- No runtime interface changes.

- [ ] **Step 1: Write user documentation**

Document:

- open the view with `g` on a selected resource;
- dependent-to-dependency edge direction;
- relation, confidence, resolution, and evidence columns;
- direct versus bounded transitive expansion;
- `complete`, `forbidden`, `unavailable`, `failed`, and `capped` coverage;
- why an incomplete graph cannot prove no dependency;
- why a stale owner UID remains missing after same-name replacement;
- Gateway `ReferenceGrant` behavior;
- exact default bounds;
- explicit statement that Secret values are never read.

Link the new document from the README documentation section.

- [ ] **Step 2: Update architecture documentation**

Add the data flow:

```text
k8s manifest -> RelationshipFacts -> core RelationshipGraph
             -> ui RelationshipSnapshotLoader -> RelationshipScreen
```

State that the controller performs bounded LISTs and the core builder performs
no I/O. Do not add a new package layer.

- [ ] **Step 3: Run focused formatting and affected tests**

Run:

```bash
uv run ruff check --fix src/korvid/k8s/selectors.py src/korvid/k8s/relationship_facts.py src/korvid/k8s/models.py src/korvid/k8s/drain.py src/korvid/core/relationships.py src/korvid/ui/relationship_controller.py src/korvid/ui/widgets/relationship_screen.py src/korvid/ui/app.py src/korvid/__main__.py tests/k8s/test_selectors.py tests/k8s/test_relationship_facts.py tests/k8s/test_models.py tests/k8s/test_drain.py tests/core/test_relationships.py tests/ui/test_relationship_controller.py tests/ui/test_relationship_screen.py tests/ui/test_relationship_flow.py tests/ui/test_app.py tests/test_main_wiring.py
uv run ruff format src/korvid/k8s/selectors.py src/korvid/k8s/relationship_facts.py src/korvid/k8s/models.py src/korvid/k8s/drain.py src/korvid/core/relationships.py src/korvid/ui/relationship_controller.py src/korvid/ui/widgets/relationship_screen.py src/korvid/ui/app.py src/korvid/__main__.py tests/k8s/test_selectors.py tests/k8s/test_relationship_facts.py tests/k8s/test_models.py tests/k8s/test_drain.py tests/core/test_relationships.py tests/ui/test_relationship_controller.py tests/ui/test_relationship_screen.py tests/ui/test_relationship_flow.py tests/ui/test_app.py tests/test_main_wiring.py
uv run pytest -p no:tach tests/k8s/test_selectors.py tests/k8s/test_relationship_facts.py tests/k8s/test_models.py tests/k8s/test_drain.py tests/core/test_relationships.py tests/ui/test_relationship_controller.py tests/ui/test_relationship_screen.py tests/ui/test_relationship_flow.py tests/ui/test_app.py tests/test_main_wiring.py -q
uv run mypy src/
uv run tach check
```

Expected: all commands exit 0.

- [ ] **Step 4: Run the complete repository gate**

Run:

```bash
make check
```

Expected: Ruff, formatting, mypy, tach, deptry, tests, coverage, lock guard, and
all repository release checks exit 0.

- [ ] **Step 5: Manually verify the exact outcome**

With a cluster containing a Deployment, Service, EndpointSlice, Pod, ConfigMap,
Secret, PVC, PDB, and Ingress:

1. select the Deployment and press `g`;
2. confirm dependencies and dependents are separate;
3. expand dependents and confirm Deployment -> ReplicaSet -> Pod is bounded and
   cycle-safe;
4. select the Pod row and press Enter; confirm the normal Pod view opens;
5. remove `list secrets` RBAC and reopen; confirm `forbidden` is visible and no
   Secret value appears;
6. recreate the Deployment with the same name and confirm the old owner UID is
   shown as missing rather than attached to the replacement.

Record the cluster version and fixture manifests in the PR body. Do not claim
manual verification when no suitable cluster is available; automated coverage
remains required.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/resource-relationships.md README.md docs/dev/specs/2026-08-12-korvid-architecture.md
git commit -m "docs: explain operational resource relationships"
```

- [ ] **Step 7: Request review**

Use the requesting-code-review skill with the merge-base SHA and current HEAD.
Fix every Critical or Important finding with a RED regression test. Then push,
open a PR that closes #281 and references parent #194, and follow the AGENTS.md
review loop through all successful required checks before squash merge.
