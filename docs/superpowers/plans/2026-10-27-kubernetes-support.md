# Kubernetes Support Policy and Version Matrix (#409 S5) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** State which Kubernetes versions korvid supports and verify each one against a live API server, using read-only scenarios for the capability modules S1–S4 added (#489, slice S5 of #409).

**Architecture:** A gated pytest suite, `tests/version_matrix/`, runs against a throwaway kind cluster that `setup.sh` seeds with fixtures and restricted principals. A workflow runs that suite on one kind cluster per supported minor version. `docs/kubernetes-support.md` defines "verified" and "fixture-compatible" and lists the versions, and the release runbook copies the verified ones into each release's notes. The design's Events fallback, from `events.k8s.io` to core v1, needs a reader that tries a logical source's candidates in order; S2 and S3 left it out, so Task 1 adds it.

**Tech Stack:** Python 3.11+, kubernetes_asyncio, pytest with `asyncio_mode = "auto"`, kind v0.32.0, GitHub Actions, MkDocs.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`, section "Kubernetes support policy". Where this plan departs from it, the section below says why.
- **Verified:** integration tests ran against a live API server of that minor version, a kind cluster in CI or the AKS contract cluster at 1.35.
- **Fixture-compatible:** unit tests only, against recorded or hand-written discovery and API responses.
- **Release notes:** each release lists the versions verified for it.
- **Read-only:** the scenarios only read. The fixtures they read are created by `setup.sh` before the suite runs, on a cluster that exists only for the job.
- **Never against a real cluster:** the suite is skipped unless `KORVID_VERSION_MATRIX_CONTEXT` names a context, and only the workflow sets it.
- **Workflow security:** `permissions: contents: read`, actions pinned by commit SHA, `persist-credentials: false`, the kind binary and node images pinned by digest.
- **Layers:** `core` imports no Textual and no optional extra; `core` may import `k8s`.
- **Code rules:** no `assert` in `src/`; every `pytest.raises` takes `match=`; maximum complexity 10; Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **The logical-source fall-through is added here.** The design reads a logical source's first resolved candidate and tries the next on `DENIED` or `UNSUPPORTED`. S2 resolved the candidates and S3 read one source, but neither tried the next, so the Events scenario had nothing to exercise. Task 1 adds `read_logical_list`.
- **Denied discovery uses an anonymous principal.** The design's scenario denies discovery of one group. RBAC only grants, and `system:discovery` is bound to every authenticated user, which the cluster's own controllers rely on, so one group's discovery cannot be taken away. The suite denies all discovery to an unauthenticated principal instead; S2's unit tests cover a single group failing discovery.
- **The kind binary is installed directly.** The workflow downloads kind v0.32.0 and checks its SHA-256 rather than adding a third-party action. kind v0.33.0 publishes no 1.33 node image; v0.32.0 publishes 1.33, 1.34, 1.35 and 1.36.
- **The oldest supported version is 1.33.** The design leaves it to the maintainer. 1.33 is the oldest node image kind v0.32.0 publishes, so the matrix can verify every version from it up; the PR asks the maintainer to confirm it.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/core/evidence_reads.py` | modify | `read_logical_list`: LIST a logical source through its candidates |
| `tests/core/test_logical_reads.py` | create | the fall-through, its outcomes, and the reported failure |
| `tests/version_matrix/__init__.py` | create | package marker |
| `tests/version_matrix/fixtures.yaml` | create | namespace, CRDs, ConfigMap, Secret, Event, principals |
| `tests/version_matrix/apiservice.yaml` | create | an aggregated API with no backend |
| `tests/version_matrix/setup.sh` | create | seed the cluster and add the principals' contexts |
| `tests/version_matrix/conftest.py` | create | the opt-in gate, contexts, and client fixture |
| `tests/version_matrix/test_scenarios.py` | create | the read-only scenarios |
| `pyproject.toml` | modify | the `version_matrix` marker |
| `.github/workflows/k8s-version-matrix.yml` | create | one kind cluster per supported minor version |
| `docs/kubernetes-support.md` | create | the support policy and version table |
| `mkdocs.yml` | modify | the page under Administration |
| `docs/release.md` | modify | list the verified versions before publishing |
| `docs/release-notes/unreleased.md` | modify | the support page and matrix |

---

### Task 1: Read a logical source through its candidates

**Files:**
- Modify: `src/korvid/core/evidence_reads.py` (after `read_object`)
- Test: `tests/core/test_logical_reads.py`

**Interfaces:**
- Consumes: `Resolution` and `CandidateOutcome` from `korvid.core.api_versions` (S2); `read_list`, `ListEvidence`, `EvidenceReader` from `korvid.core.evidence_reads` (S3); `FAILURE_PRECEDENCE` from `korvid.core.capability` (S1).
- Produces: `LogicalListEvidence(items, observation, outcomes)` and `async read_logical_list(reader, resolution, *, page_size, max_pages, deadline, max_bytes, token, clock) -> LogicalListEvidence`.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_logical_reads.py` with:

```python
"""A logical source's LIST, falling through its candidates (#409, slice S5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from korvid.core.api_versions import EVENTS, DiscoverySnapshot, observe_discovery, resolve
from korvid.core.capability import (
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    SourceScope,
    Verb,
)
from korvid.core.evidence_reads import read_logical_list
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
EVENT = {"metadata": {"name": "e1", "namespace": "team-a", "uid": "uid-e1"}}
FORBIDDEN = ApiStatusError(403, "Forbidden")


def _clock() -> datetime:
    return NOW


def _version(group: str, *resources: str) -> GroupVersionOutcome:
    served = tuple(ServedResource(name, frozenset({"get", "list"})) for name in resources)
    return GroupVersionOutcome(group, "v1", served)


def _snapshot(*, events_group: bool = True, core_events: bool = True) -> DiscoverySnapshot:
    core = _version("", "pods", *(("events",) if core_events else ()))
    named = (_version("events.k8s.io", "events"),) if events_group else ()
    raw = RawDiscovery(
        1,
        NOW,
        0.0,
        RootOutcome("/api", (("", "v1"),)),
        RootOutcome("/apis", tuple((outcome.group, "v1") for outcome in named)),
        (core, *named),
    )
    return observe_discovery(raw)


class FakeReader(EvidenceReader):
    """Answers each LIST with the next scripted outcome, and records which group was read."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.groups: list[str] = []
        self.limits: list[int] = []

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.groups.append(target.group)
        self.limits.append(limit)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, EvidencePage)
        return outcome

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        raise AssertionError("a logical LIST never reads one object")


def _group(observation: Observation) -> str:
    assert isinstance(observation.source, ApiSource)
    return observation.source.group


def _state(observation: Observation) -> tuple[EvidenceState, Basis]:
    return observation.state, observation.basis


def _events(snapshot: DiscoverySnapshot) -> Any:
    return resolve(EVENTS, Verb.LIST, snapshot, scope=TEAM)


async def test_the_first_candidate_that_answers_is_the_only_one_read() -> None:
    reader = FakeReader(EvidencePage((EVENT,)))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io"]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert _group(result.observation) == "events.k8s.io"
    assert len(result.items) == 1
    assert [_group(outcome) for outcome in result.outcomes] == ["events.k8s.io"]


async def test_a_denied_read_falls_through_to_the_next_candidate() -> None:
    reader = FakeReader(FORBIDDEN, EvidencePage((EVENT,)))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io", ""]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert _group(result.observation) == ""
    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.DENIED, Basis.FORBIDDEN),
        (EvidenceState.AVAILABLE, Basis.OBSERVED),
    ]


async def test_an_unsupported_read_falls_through_to_the_next_candidate() -> None:
    reader = FakeReader(ApiStatusError(406, "Not Acceptable"), EvidencePage(()))
    resolution = resolve(EVENTS, Verb.LIST, _snapshot(), scope=TEAM, projection=Projection.METADATA)

    result = await read_logical_list(reader, resolution, clock=_clock)

    assert reader.groups == ["events.k8s.io", ""]
    assert _state(result.outcomes[0]) == (
        EvidenceState.UNSUPPORTED,
        Basis.PROJECTION_UNSUPPORTED,
    )
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)


async def test_an_unavailable_read_does_not_fall_through() -> None:
    reader = FakeReader(ApiStatusError(503, "Service Unavailable"), EvidencePage(()))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io"]
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.SERVER_ERROR)


async def test_a_partial_read_does_not_fall_through() -> None:
    truncated = [EvidencePage((EVENT,), "p2"), EvidencePage((EVENT,), "p3")]
    reader = FakeReader(*truncated, EvidencePage(()))

    result = await read_logical_list(reader, _events(_snapshot()), clock=_clock)

    assert reader.groups == ["events.k8s.io", "events.k8s.io"]
    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
    assert len(result.items) == 2


async def test_a_candidate_discovery_did_not_resolve_is_skipped_and_kept() -> None:
    reader = FakeReader(EvidencePage((EVENT,)))

    result = await read_logical_list(reader, _events(_snapshot(events_group=False)), clock=_clock)

    assert reader.groups == [""]
    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED),
        (EvidenceState.AVAILABLE, Basis.OBSERVED),
    ]
    assert _group(result.observation) == ""


async def test_when_no_candidate_answers_the_strongest_failure_is_reported() -> None:
    reader = FakeReader(FORBIDDEN)

    result = await read_logical_list(reader, _events(_snapshot(core_events=False)), clock=_clock)

    assert reader.groups == ["events.k8s.io"]
    assert result.items == ()
    assert _state(result.observation) == (EvidenceState.DENIED, Basis.FORBIDDEN)
    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.DENIED, Basis.FORBIDDEN),
        (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED),
    ]


async def test_when_no_candidate_resolves_nothing_is_read() -> None:
    reader = FakeReader()

    result = await read_logical_list(
        reader, _events(_snapshot(events_group=False, core_events=False)), clock=_clock
    )

    assert reader.groups == []
    assert _state(result.observation) == (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED)
    assert len(result.outcomes) == 2


async def test_every_read_gets_the_budgets_given() -> None:
    reader = FakeReader(FORBIDDEN, EvidencePage(()))

    result = await read_logical_list(
        reader, _events(_snapshot()), page_size=10, max_pages=1, clock=_clock
    )

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert reader.limits == [10, 10]
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest -p no:tach tests/core/test_logical_reads.py -q`
Expected: collection error, `ImportError: cannot import name 'read_logical_list'`.

- [ ] **Step 3: Add the reader**

In `src/korvid/core/evidence_reads.py`, import `Resolution` from `korvid.core.api_versions` and `FAILURE_PRECEDENCE` from `korvid.core.capability`, then add after `read_object`:

```python
#: A logical source tries its next candidate after a read in one of these
#: states: RBAC can refuse one API group and allow another that serves the
#: same evidence, and one API can refuse a projection that another serves.
_FALL_THROUGH = frozenset({EvidenceState.DENIED, EvidenceState.UNSUPPORTED})


@dataclass(frozen=True, slots=True)
class LogicalListEvidence:
    """What a logical source's LIST read, and how each candidate it tried fared.

    `observation` is the read that answered, or, when none did, the failure
    the baseline precedence picks. `outcomes` holds each candidate's outcome
    in order, up to the one that answered: why discovery did not resolve it,
    or what its read established.
    """

    items: tuple[dict[str, Any], ...]
    observation: Observation
    outcomes: tuple[Observation, ...]


async def read_logical_list(
    reader: EvidenceReader,
    resolution: Resolution,
    *,
    page_size: int = LIST_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    deadline: float = DEFAULT_DEADLINE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> LogicalListEvidence:
    """LIST a logical source through its candidates, in order.

    A candidate discovery did not resolve is skipped. A read that is `DENIED`
    or `UNSUPPORTED` falls through to the next candidate. Any other outcome,
    `PARTIAL` and `UNAVAILABLE` included, is the answer. Each read has its own
    budgets, those given here.
    """
    outcomes: list[Observation] = []
    for outcome in resolution.outcomes:
        if outcome.observation is not None:
            outcomes.append(outcome.observation)
            continue
        result = await read_list(
            reader,
            outcome.source,
            page_size=page_size,
            max_pages=max_pages,
            deadline=deadline,
            max_bytes=max_bytes,
            token=token,
            clock=clock,
        )
        outcomes.append(result.observation)
        if result.observation.state not in _FALL_THROUGH:
            return LogicalListEvidence(result.items, result.observation, tuple(outcomes))
    failure = min(outcomes, key=lambda failed: FAILURE_PRECEDENCE.index(failed.state))
    return LogicalListEvidence((), failure, tuple(outcomes))
```

- [ ] **Step 4: Run the tests to verify they pass, and check the mutations**

Run: `uv run --frozen pytest -p no:tach tests/core/test_logical_reads.py -q`
Expected: `9 passed`.

Each of these changes to `evidence_reads.py` must make at least one test fail: drop `EvidenceState.UNSUPPORTED` from `_FALL_THROUGH`; replace `min(` with `max(` in the failure pick; remove the `continue` after an unresolved candidate.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/evidence_reads.py tests/core/test_logical_reads.py
git commit -m "core: read a logical source through its candidates"
```

### Task 2: The version-matrix scenarios

**Files:**
- Create: `tests/version_matrix/__init__.py` (empty)
- Create: `tests/version_matrix/fixtures.yaml`, `tests/version_matrix/apiservice.yaml`, `tests/version_matrix/setup.sh`, `tests/version_matrix/conftest.py`, `tests/version_matrix/test_scenarios.py`
- Modify: `pyproject.toml` (`[tool.pytest.ini_options] markers`)

**Interfaces:**
- Consumes: `read_logical_list` (Task 1); `observe_discovery`, `resolve`, `EVENTS` and `LogicalSource` from `korvid.core.api_versions` (S2); `read_list`, `read_object` and `LAST_APPLIED_ANNOTATION` from `korvid.core.evidence_reads` (S3); `KubeClient` from `korvid.k8s.client`, which is the `EvidenceReader` and serves `discovery_snapshot()`.
- Produces: the `version_matrix` marker, `KORVID_VERSION_MATRIX_CONTEXT`, and `tests/version_matrix/setup.sh CONTEXT`, which Task 3's workflow runs.

These tests need a live cluster, so they cannot fail first locally: without `KORVID_VERSION_MATRIX_CONTEXT` they are collected and skipped. Their first real run is Task 3's workflow on the PR. Do not run `setup.sh` or the suite against a cluster you use, and do not create a kind cluster locally: kind rewrites the current kubeconfig.

- [ ] **Step 1: Seed fixtures**

Create `tests/version_matrix/fixtures.yaml` with:

```yaml
# Read-only fixtures for the Kubernetes version matrix (#409, slice S5).
#
# Applied with `kubectl apply` by setup.sh, so the ConfigMap and the Secret
# carry the `last-applied-configuration` annotation, payload included, that
# metadata reads must never pass on. Everything lives in one namespace or
# under the korvid.dev API groups, in a kind cluster that exists only for
# the workflow run.
apiVersion: v1
kind: Namespace
metadata:
  name: korvid-matrix
---
# Served in a preferred (v1) and a non-preferred (v1beta1) version.
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: widgets.matrix.korvid.dev
spec:
  group: matrix.korvid.dev
  scope: Namespaced
  names:
    plural: widgets
    singular: widget
    kind: Widget
  versions:
    - name: v1
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
    - name: v1beta1
      served: true
      storage: false
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
---
# Served only in a version no adapter knows.
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata:
  name: gadgets.future.korvid.dev
spec:
  group: future.korvid.dev
  scope: Namespaced
  names:
    plural: gadgets
    singular: gadget
    kind: Gadget
  versions:
    - name: v9
      served: true
      storage: true
      schema:
        openAPIV3Schema:
          type: object
          x-kubernetes-preserve-unknown-fields: true
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: settings
  namespace: korvid-matrix
  labels:
    app: matrix
  annotations:
    note: configuration-payload
data:
  password: matrix-configmap-payload
---
apiVersion: v1
kind: Secret
metadata:
  name: credentials
  namespace: korvid-matrix
  labels:
    app: matrix
type: Opaque
stringData:
  password: matrix-secret-payload
---
# A recorded event, readable through events.k8s.io/v1 and core v1 alike.
apiVersion: v1
kind: Event
metadata:
  name: matrix-scenario
  namespace: korvid-matrix
involvedObject:
  apiVersion: v1
  kind: ConfigMap
  name: settings
  namespace: korvid-matrix
reason: MatrixScenario
message: Recorded by the Kubernetes version matrix.
type: Normal
---
# Principals for the denial scenarios. Every authenticated principal can
# read discovery through the cluster's own system:discovery binding.
apiVersion: v1
kind: ServiceAccount
metadata:
  name: events-core
  namespace: korvid-matrix
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: events-group
  namespace: korvid-matrix
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: events-core
  namespace: korvid-matrix
rules:
  - apiGroups: [""]
    resources: [events]
    verbs: [list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: events-group
  namespace: korvid-matrix
rules:
  - apiGroups: [events.k8s.io]
    resources: [events]
    verbs: [list]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: events-core
  namespace: korvid-matrix
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: events-core
subjects:
  - kind: ServiceAccount
    name: events-core
    namespace: korvid-matrix
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: events-group
  namespace: korvid-matrix
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: events-group
subjects:
  - kind: ServiceAccount
    name: events-group
    namespace: korvid-matrix
```

Create `tests/version_matrix/apiservice.yaml` with:

```yaml
# An aggregated API that is registered but never available: its backing
# Service does not exist, so discovery of its group version fails with 503.
# Applied after fixtures.yaml, once kubectl no longer needs full discovery.
apiVersion: apiregistration.k8s.io/v1
kind: APIService
metadata:
  name: v1.unavailable.korvid.dev
spec:
  group: unavailable.korvid.dev
  version: v1
  groupPriorityMinimum: 1000
  versionPriority: 15
  insecureSkipTLSVerify: true
  service:
    name: missing-backend
    namespace: korvid-matrix
```

Create `tests/version_matrix/setup.sh` with:

```sh
#!/usr/bin/env bash
# Install the version-matrix fixtures into a disposable cluster and add one
# kubeconfig context per principal the scenarios read as (#409, slice S5).
#
# Usage: setup.sh ADMIN_CONTEXT
#
# Only ever run against a cluster created for the run: it installs CRDs, an
# APIService and RBAC, and writes contexts into the current kubeconfig.
set -euo pipefail

admin=${1:?usage: setup.sh ADMIN_CONTEXT}
here=$(cd "$(dirname "$0")" && pwd)
kube=(kubectl --context "$admin")
cluster=$("${kube[@]}" config view -o jsonpath="{.contexts[?(@.name=='$admin')].context.cluster}")

"${kube[@]}" apply -f "$here/fixtures.yaml"
"${kube[@]}" wait --for=condition=Established --timeout=120s \
  crd/widgets.matrix.korvid.dev crd/gadgets.future.korvid.dev

"${kube[@]}" apply -f "$here/apiservice.yaml"
"${kube[@]}" wait --for=condition=Available=false --timeout=120s \
  apiservice/v1.unavailable.korvid.dev

for account in events-core events-group; do
  token=$("${kube[@]}" -n korvid-matrix create token "$account" --duration=1h)
  kubectl config set-credentials "matrix-$account" --token="$token" >/dev/null
  kubectl config set-context "matrix-$account" --cluster="$cluster" \
    --user="matrix-$account" --namespace=korvid-matrix >/dev/null
done

# A principal with no credentials: the API server treats it as anonymous,
# which may read no discovery at all.
kubectl config set-credentials matrix-anonymous >/dev/null
kubectl config set-context matrix-anonymous --cluster="$cluster" \
  --user=matrix-anonymous >/dev/null
```

Make it executable: `chmod +x tests/version_matrix/setup.sh`.

- [ ] **Step 2: The gate and the client fixture**

Create `tests/version_matrix/conftest.py` with:

```python
"""Fixtures for the Kubernetes version-matrix scenarios (#409, slice S5).

The suite is opt-in: without `KORVID_VERSION_MATRIX_CONTEXT`, every test is
skipped at collection, so the ordinary test run never reaches a cluster. The
workflow sets it to the admin context of a kind cluster it created for the
run, after `setup.sh` has installed the fixtures and the principals'
contexts.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from korvid.core.api_versions import DiscoverySnapshot, observe_discovery
from korvid.k8s.client import KubeClient

ADMIN_CONTEXT = os.environ.get("KORVID_VERSION_MATRIX_CONTEXT", "")
#: Lists only core events; denied events.k8s.io events and everything else.
EVENTS_CORE_CONTEXT = "matrix-events-core"
#: Lists only events.k8s.io events; denied core events and everything else.
EVENTS_GROUP_CONTEXT = "matrix-events-group"
#: No credentials, so the API server treats it as anonymous.
ANONYMOUS_CONTEXT = "matrix-anonymous"
NAMESPACE = "korvid-matrix"

Connect = Callable[[str], Awaitable[KubeClient]]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if ADMIN_CONTEXT:
        return
    skip = pytest.mark.skip(
        reason="KORVID_VERSION_MATRIX_CONTEXT not set; the version matrix is opt-in"
    )
    for item in items:
        if item.get_closest_marker("version_matrix"):
            item.add_marker(skip)


@pytest.fixture
async def connect() -> AsyncIterator[Connect]:
    """Connect a client to one of the matrix contexts; every one is closed afterwards."""
    clients: list[KubeClient] = []

    async def open_client(context: str) -> KubeClient:
        kube = KubeClient()
        await kube.connect(context)
        clients.append(kube)
        return kube

    yield open_client
    for kube in clients:
        await kube.close()


async def snapshot_of(kube: KubeClient) -> DiscoverySnapshot:
    """The observed discovery of `kube`'s current session."""
    return observe_discovery(await kube.discovery_snapshot())
```

- [ ] **Step 3: The scenarios**

Create `tests/version_matrix/test_scenarios.py` with:

```python
"""Read-only capability scenarios against a live API server (#409, slice S5).

Each Kubernetes version in the matrix runs the same scenarios: version
resolution from real discovery, an unavailable aggregated API, metadata reads
of configuration payloads, denied principals, and Events through either API
group. They read; the only writes are the fixtures `setup.sh` installed.
"""

from __future__ import annotations

import json

import pytest

from korvid.core.api_versions import EVENTS, Candidate, LogicalSource, Resolution, resolve
from korvid.core.capability import (
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    SourceScope,
    Verb,
)
from korvid.core.evidence_reads import (
    LAST_APPLIED_ANNOTATION,
    read_list,
    read_logical_list,
    read_object,
)
from korvid.k8s.discovery import ResourceMeta
from tests.version_matrix.conftest import (
    ADMIN_CONTEXT,
    ANONYMOUS_CONTEXT,
    EVENTS_CORE_CONTEXT,
    EVENTS_GROUP_CONTEXT,
    NAMESPACE,
    Connect,
    snapshot_of,
)

pytestmark = pytest.mark.version_matrix

SCOPE = SourceScope.namespace(NAMESPACE)
WIDGETS = LogicalSource("widgets", (Candidate("matrix.korvid.dev", "widgets", ("v1", "v1beta1")),))
OLD_WIDGETS = LogicalSource(
    "old-widgets", (Candidate("matrix.korvid.dev", "widgets", ("v1beta1",)),)
)
GADGETS = LogicalSource("gadgets", (Candidate("future.korvid.dev", "gadgets", ("v1",)),))
ABSENT = LogicalSource("absent", (Candidate("absent.korvid.dev", "things", ("v1",)),))
UNAVAILABLE = LogicalSource(
    "unavailable", (Candidate("unavailable.korvid.dev", "things", ("v1",)),)
)
PODS = LogicalSource("pods", (Candidate("", "pods", ("v1",)),))
PAYLOADS = {
    "configmaps": ("settings", "matrix-configmap-payload"),
    "secrets": ("credentials", "matrix-secret-payload"),
}
CONFIGMAP_META = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)
SECRET_META = ResourceMeta("Secret", "secrets", "", "v1", True)


def _state(observation: Observation | None) -> tuple[EvidenceState, Basis]:
    assert observation is not None
    return observation.state, observation.basis


def _only_source(resolution: Resolution) -> ApiSource:
    (source,) = resolution.sources
    return source


def _failure(resolution: Resolution) -> tuple[EvidenceState, Basis]:
    (outcome,) = resolution.outcomes
    return _state(outcome.observation)


def _group(observation: Observation) -> str:
    assert isinstance(observation.source, ApiSource)
    return observation.source.group


async def test_the_preferred_version_is_read_when_known(connect: Connect) -> None:
    kube = await connect(ADMIN_CONTEXT)
    resolution = resolve(WIDGETS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_list(kube, _only_source(resolution))

    assert _only_source(resolution).version == "v1"
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)


async def test_a_non_preferred_version_is_read_when_only_it_is_known(connect: Connect) -> None:
    kube = await connect(ADMIN_CONTEXT)
    resolution = resolve(OLD_WIDGETS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_list(kube, _only_source(resolution))

    assert _only_source(resolution).version == "v1beta1"
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)


async def test_a_resource_served_only_in_an_unknown_version_is_never_read(
    connect: Connect,
) -> None:
    kube = await connect(ADMIN_CONTEXT)

    resolution = resolve(GADGETS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    assert _failure(resolution) == (EvidenceState.UNSUPPORTED, Basis.NO_KNOWN_VERSION)


async def test_an_installed_optional_api_resolves_and_an_absent_one_is_not_served(
    connect: Connect,
) -> None:
    kube = await connect(ADMIN_CONTEXT)
    snapshot = await snapshot_of(kube)

    assert resolve(WIDGETS, Verb.LIST, snapshot, scope=SCOPE).sources
    assert _failure(resolve(ABSENT, Verb.LIST, snapshot, scope=SCOPE)) == (
        EvidenceState.UNSUPPORTED,
        Basis.NOT_SERVED,
    )


async def test_an_unavailable_aggregated_api_is_unavailable_not_unsupported(
    connect: Connect,
) -> None:
    kube = await connect(ADMIN_CONTEXT)

    resolution = resolve(UNAVAILABLE, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    assert _failure(resolution) == (EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED)
    assert "pods" in [meta.plural for meta in await kube.discover_resources()]


@pytest.mark.parametrize("resource", sorted(PAYLOADS))
async def test_no_configuration_payload_reaches_a_metadata_list(
    connect: Connect, resource: str
) -> None:
    kube = await connect(ADMIN_CONTEXT)
    name, payload = PAYLOADS[resource]
    source = LogicalSource(resource, (Candidate("", resource, ("v1",)),))
    resolution = resolve(
        source, Verb.LIST, await snapshot_of(kube), scope=SCOPE, projection=Projection.METADATA
    )

    result = await read_list(kube, _only_source(resolution))

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert name in [item["metadata"]["name"] for item in result.items]
    assert all(set(item) == {"metadata"} for item in result.items)
    assert all("annotations" not in item["metadata"] for item in result.items)
    assert payload not in json.dumps(result.items)


@pytest.mark.parametrize("resource", sorted(PAYLOADS))
async def test_no_configuration_payload_reaches_a_metadata_get(
    connect: Connect, resource: str
) -> None:
    kube = await connect(ADMIN_CONTEXT)
    name, payload = PAYLOADS[resource]
    meta = CONFIGMAP_META if resource == "configmaps" else SECRET_META
    on_the_wire = await kube.get_object(meta, NAMESPACE, name)
    source = LogicalSource(resource, (Candidate("", resource, ("v1",)),))
    resolution = resolve(
        source,
        Verb.GET,
        await snapshot_of(kube),
        scope=SCOPE,
        projection=Projection.METADATA,
        name=name,
    )

    result = await read_object(kube, _only_source(resolution))

    assert LAST_APPLIED_ANNOTATION in on_the_wire["metadata"]["annotations"]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert result.item is not None
    assert set(result.item) == {"metadata"}
    assert "annotations" not in result.item["metadata"]
    assert payload not in json.dumps(result.item)


async def test_a_principal_denied_a_list_is_denied(connect: Connect) -> None:
    kube = await connect(EVENTS_GROUP_CONTEXT)
    resolution = resolve(PODS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_list(kube, _only_source(resolution))

    assert _state(result.observation) == (EvidenceState.DENIED, Basis.FORBIDDEN)


async def test_a_principal_denied_discovery_is_denied_not_unsupported(connect: Connect) -> None:
    kube = await connect(ANONYMOUS_CONTEXT)
    snapshot = await snapshot_of(kube)

    assert _failure(resolve(WIDGETS, Verb.LIST, snapshot, scope=SCOPE)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )
    assert _failure(resolve(PODS, Verb.LIST, snapshot, scope=SCOPE)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )


async def test_events_are_read_through_both_groups_from_the_first(connect: Connect) -> None:
    kube = await connect(ADMIN_CONTEXT)
    resolution = resolve(EVENTS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_logical_list(kube, resolution)

    assert [source.group for source in resolution.sources] == ["events.k8s.io", ""]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert [_group(outcome) for outcome in result.outcomes] == ["events.k8s.io"]
    assert "matrix-scenario" in [item["metadata"]["name"] for item in result.items]


async def test_events_are_read_through_events_k8s_io_alone(connect: Connect) -> None:
    kube = await connect(EVENTS_GROUP_CONTEXT)
    resolution = resolve(EVENTS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_logical_list(kube, resolution)

    assert _group(result.observation) == "events.k8s.io"
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert "matrix-scenario" in [item["metadata"]["name"] for item in result.items]


async def test_events_fall_through_to_core_when_events_k8s_io_is_denied(
    connect: Connect,
) -> None:
    kube = await connect(EVENTS_CORE_CONTEXT)
    resolution = resolve(EVENTS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_logical_list(kube, resolution)

    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.DENIED, Basis.FORBIDDEN),
        (EvidenceState.AVAILABLE, Basis.OBSERVED),
    ]
    assert _group(result.observation) == ""
    assert "matrix-scenario" in [item["metadata"]["name"] for item in result.items]
```

- [ ] **Step 4: Register the marker**

In `pyproject.toml`, add to `[tool.pytest.ini_options] markers`:

```toml
    "version_matrix: read-only scenarios against a kind cluster (opt-in via KORVID_VERSION_MATRIX_CONTEXT)",
```

- [ ] **Step 5: Check they are collected and skipped**

Run: `uv run --frozen pytest -p no:tach tests/version_matrix/ -q -rs`
Expected: `14 skipped`, each naming `KORVID_VERSION_MATRIX_CONTEXT`.

Run: `bash -n tests/version_matrix/setup.sh && uv run --frozen ruff check tests/version_matrix/ && uv run --frozen mypy tests/version_matrix/`
Expected: no output from `bash -n`, and both tools clean.

- [ ] **Step 6: Commit**

```bash
git add tests/version_matrix/ pyproject.toml
git commit -m "tests: read-only version-matrix scenarios"
```

### Task 3: The workflow

**Files:**
- Create: `.github/workflows/k8s-version-matrix.yml`

**Interfaces:**
- Consumes: `tests/version_matrix/setup.sh CONTEXT` and the `version_matrix` marker (Task 2).
- Produces: a check per Kubernetes version, named `Kubernetes <version>`, and a job summary naming the server's `gitVersion`, which the release runbook (Task 4) reads.

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/k8s-version-matrix.yml` with:

```yaml
name: Kubernetes version matrix
# Read-only capability scenarios against kind clusters of several Kubernetes
# minor versions (#409, slice S5). A version is "verified" only when its job
# passes here; see docs/kubernetes-support.md. GitHub-hosted runners and kind
# only: no cloud resources, no secrets, nothing persistent.

on:
  workflow_dispatch:
  pull_request:
    paths:
      - src/korvid/core/api_versions.py
      - src/korvid/core/capability.py
      - src/korvid/core/capability_classifier.py
      - src/korvid/core/evidence_reads.py
      - src/korvid/core/run_tokens.py
      - src/korvid/k8s/bounded_transport.py
      - src/korvid/k8s/discovery.py
      - src/korvid/k8s/discovery_snapshot.py
      - src/korvid/k8s/errors.py
      - src/korvid/k8s/evidence_requests.py
      - tests/version_matrix/**
      - .github/workflows/k8s-version-matrix.yml
  push:
    branches: [main]
    paths:
      - src/korvid/core/api_versions.py
      - src/korvid/core/capability.py
      - src/korvid/core/capability_classifier.py
      - src/korvid/core/evidence_reads.py
      - src/korvid/core/run_tokens.py
      - src/korvid/k8s/bounded_transport.py
      - src/korvid/k8s/discovery.py
      - src/korvid/k8s/discovery_snapshot.py
      - src/korvid/k8s/errors.py
      - src/korvid/k8s/evidence_requests.py
      - tests/version_matrix/**
      - .github/workflows/k8s-version-matrix.yml

permissions:
  contents: read

concurrency:
  group: korvid-version-matrix-${{ github.ref }}
  cancel-in-progress: true

env:
  KIND_VERSION: v0.32.0
  # kind-linux-amd64.sha256sum from the kind v0.32.0 release.
  KIND_SHA256: 50030de23cf40a18505f20426f6a8506bedf13c6e509244bd1fa9463721b0f54
  CLUSTER: korvid-matrix

jobs:
  scenarios:
    name: Kubernetes ${{ matrix.kubernetes }}
    runs-on: ubuntu-latest
    timeout-minutes: 30
    strategy:
      fail-fast: false
      matrix:
        # Images built for kind v0.32.0, pinned by digest. The set is what
        # the matrix runs, not a support claim.
        include:
          - kubernetes: "1.33"
            image: kindest/node:v1.33.12@sha256:3f5c8443c620245e4d355cfe09e96a91ead32ceaa569d3f1ca9edf0cb2fe2ff4
          - kubernetes: "1.34"
            image: kindest/node:v1.34.8@sha256:02722c2dedddcfc00febf5d27fbeb9b7b2c14294c82109ff4a85d89ac9ba3256
          - kubernetes: "1.35"
            image: kindest/node:v1.35.5@sha256:ce977ae6d65918d0b58a5f8b5e940429c2ce42fa3a5619ec2bbc60b949c0ac95
          - kubernetes: "1.36"
            image: kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false

      - name: Install kind
        run: |
          curl -fsSLo "$RUNNER_TEMP/kind" \
            "https://github.com/kubernetes-sigs/kind/releases/download/$KIND_VERSION/kind-linux-amd64"
          echo "$KIND_SHA256  $RUNNER_TEMP/kind" | sha256sum --check --strict
          sudo install "$RUNNER_TEMP/kind" /usr/local/bin/kind

      - name: Create the cluster
        env:
          IMAGE: ${{ matrix.image }}
        run: kind create cluster --name "$CLUSTER" --image "$IMAGE" --wait 120s

      - uses: astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7 # v10.2.0
        with:
          python-version: "3.13"
          enable-cache: true

      - run: uv sync --locked --dev --all-extras

      - name: Install the fixtures and the principals' contexts
        run: tests/version_matrix/setup.sh "kind-$CLUSTER"

      - name: Run the scenarios
        env:
          KORVID_VERSION_MATRIX_CONTEXT: kind-${{ env.CLUSTER }}
        run: uv run pytest -p no:randomly -m version_matrix tests/version_matrix/ -v

      - name: Record the verified version
        env:
          MINOR: ${{ matrix.kubernetes }}
        run: |
          server=$(kubectl --context "kind-$CLUSTER" version -o json | jq -r .serverVersion.gitVersion)
          echo "Kubernetes $MINOR verified: API server $server" >> "$GITHUB_STEP_SUMMARY"

      - name: Collect diagnostics
        if: failure()
        run: |
          mkdir -p diag
          kubectl --context "kind-$CLUSTER" get apiservices,crds -o wide > diag/apis.txt || true
          kubectl --context "kind-$CLUSTER" -n korvid-matrix get all,events -o wide > diag/fixtures.txt || true
          kind export logs --name "$CLUSTER" diag/kind || true

      - name: Upload diagnostics
        if: failure()
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: version-matrix-${{ matrix.kubernetes }}
          path: diag/
          retention-days: 14
```

The node image digests come from the kind v0.32.0 release notes, and `KIND_SHA256` from the release's `kind-linux-amd64.sha256sum`.

- [ ] **Step 2: Check it parses**

Run: `uv run --frozen python -c "import yaml; yaml.safe_load(open('.github/workflows/k8s-version-matrix.yml'))"`
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/k8s-version-matrix.yml
git commit -m "ci: verify each supported Kubernetes version on kind"
```

The workflow's own path filter includes it, so the PR runs every version. A job that fails there is fixed before review: its `version-matrix-<version>` artifact holds the cluster's events, the APIService and the server version.

### Task 4: The support policy

**Files:**
- Create: `docs/kubernetes-support.md`
- Modify: `mkdocs.yml` (Administration nav), `docs/release.md` (before "Publish `$TAG`"), `docs/release-notes/unreleased.md`

**Interfaces:**
- Consumes: the workflow's name and job summary (Task 3).
- Produces: the page `kubernetes-support.md`, which the release runbook and release notes link to.

- [ ] **Step 1: Write the page**

Create `docs/kubernetes-support.md` with:

```markdown
# Kubernetes support

korvid supports the Kubernetes versions its version matrix verifies, and it
claims nothing for any other version. This page says what "verified" means,
which versions are verified, and how korvid copes with APIs that a cluster
does or does not serve.

## Supported versions

The oldest supported version is **Kubernetes 1.33**.

| Minor version | Status | Verified by |
| --- | --- | --- |
| 1.36 | Verified | the version matrix |
| 1.35 | Verified | the version matrix and the AKS contract cluster |
| 1.34 | Verified | the version matrix |
| 1.33 | Verified | the version matrix |

Each release's notes list the versions verified for that release.

## What "verified" means

- **Verified:** korvid's integration scenarios ran against a live API server
  of that minor version, and passed. The server is a
  [kind](https://kind.sigs.k8s.io/) cluster created by the version matrix, or
  the AKS cluster the write-path contract tests run against.
- **Fixture-compatible:** covered only by unit tests that use constructed API
  responses. Nothing has checked it against a live server.

A version the matrix only *runs* is not a support claim until it passes. No
version is currently listed as only fixture-compatible.

## Newer and missing APIs

korvid never decides what a cluster can do from its version number. It asks
the cluster's API discovery which resources each API group version serves,
and reads only versions korvid knows how to read:

- a resource served in several versions is read in the most preferred one
  korvid knows, even if that is not the cluster's preferred version;
- a resource served only in versions korvid does not know is reported as
  unsupported, never read as if it were a known one;
- an optional API that is not installed is reported as not served;
- when discovery itself fails or is refused, korvid reports the evidence as
  unavailable or denied, never as unsupported.

So a feature that needs a newer API keeps working on any cluster that serves
it, and says why it cannot run on one that does not.

## How versions are verified

The **Kubernetes version matrix** workflow creates one kind cluster per minor
version on GitHub-hosted runners, so it adds no cloud cost, and runs the same
read-only scenarios against each:

- a resource served in a preferred and in a non-preferred version;
- a resource served only in a version korvid does not know;
- an optional API that is installed, and one that is not;
- an aggregated API that is registered but unavailable;
- metadata-only reads of ConfigMaps and Secrets created with `kubectl apply`,
  which must never return their data or the `last-applied-configuration`
  annotation;
- a principal denied a list, and a principal denied discovery;
- Events read through `events.k8s.io` alone, through core `v1` alone, and
  through both.

The workflow runs on demand, and whenever a pull request or a push to `main`
changes the modules those scenarios exercise. Each passing job records the
API server version it verified in the run's summary.

The matrix runs every minor version from the oldest supported one up. 1.35 is
also the AKS contract cluster's version, and 1.36 is the API generation
korvid's Kubernetes client targets.
```

- [ ] **Step 2: Link it**

In `mkdocs.yml`, under Administration, before `Air-gapped operation`, add:

```yaml
      - Kubernetes support: kubernetes-support.md
```

In `docs/release.md`, before `## Publish $TAG`, add a section "List the verified Kubernetes versions" telling the release manager to take the versions whose job passed from the latest version-matrix run on the reviewed commit, starting one with `gh workflow run k8s-version-matrix.yml --ref main` when none has run since the last change it covers.

In `docs/release-notes/unreleased.md`, add a "Kubernetes support" section naming the page, 1.33 as the oldest supported version, and the versions the matrix verifies.

- [ ] **Step 3: Build the docs**

Run: `uv run --frozen mkdocs build --strict -d /tmp/korvid-site && uv run --frozen python scripts/check_docs_site.py /tmp/korvid-site`
Expected: the build succeeds and the check prints `publication artifacts look correct`.

Run: `uv run --frozen pytest -p no:tach tests/test_docs_*.py tests/test_check_docs_site.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add docs/kubernetes-support.md mkdocs.yml docs/release.md docs/release-notes/unreleased.md
git commit -m "docs: Kubernetes support policy"
```

### Task 5: Run the full gate

- [ ] **Step 1: Run every check**

Run: `make check`
Expected: ruff, format, mypy, tach, the source-size check and the test suite all pass, with coverage at least 80%.

- [ ] **Step 2: Push and watch the matrix**

Push the branch and open the PR. Every `Kubernetes <version>` job must pass before review; the PR lists the versions that passed.
