# Deployment Operation Outcome Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track an accepted direct-TUI Deployment scale or rollout restart until Kubernetes evidence proves convergence, a concrete blocker, supersession, replacement, or an incomplete observation.

**Architecture:** A pure `core/` state model evaluates immutable Deployment observations. A read-only `k8s/` adapter gathers one bounded observation. `WriteCoordinator` emits an accepted-write receipt only after the mutation and success audit complete, and a dedicated `ui/` controller supervises bounded polling and exposes immutable snapshots to a modal screen.

**Tech Stack:** Python 3.11+, asyncio, Textual, kubernetes async client, pytest, mypy strict, ruff, tach.

## Global Constraints

- Direct TUI `apps/Deployment` scale and rollout restart only.
- Keep API acceptance and later convergence as separate states.
- Preserve approval, UID/context revalidation, protected contexts, exact dry-run replay, and fail-closed audit-before-mutation.
- Observation is read-only, session-only, capped at three trackers, and limited to five minutes per tracker.
- A missing field, denied read, timeout, disconnect, or context switch must never become success.
- Do not import `korvid.evals` from the shipped TUI.
- Do not add a new globally reserved key.
- No automatic retry of writes, rollback, remediation, or privilege escalation.

## File Structure

- Create `src/korvid/core/deployment_outcome.py`: immutable operation intents, observations, outcomes, and pure completion predicates.
- Create `tests/core/test_deployment_outcome.py`: exhaustive state-model tests.
- Create `src/korvid/k8s/deployment_outcomes.py`: read-only ABC and `KubeClient` adapter producing bounded observations.
- Create `tests/k8s/test_deployment_outcomes.py`: reader identity, cap, and error tests.
- Modify `src/korvid/ui/write_gate.py`: accepted-write receipt and optional success observer contract.
- Modify `src/korvid/ui/write_coordinator.py`: invoke observer after successful mutation and success audit without changing write outcome.
- Modify `src/korvid/ui/resource_write_controller.py`: attach Deployment scale/restart intents.
- Modify `tests/ui/test_write_confirm_characterization.py`: perimeter callback ordering and refusal tests.
- Create `src/korvid/ui/deployment_outcome_controller.py`: tracker registry, polling, cancellation, and context lifecycle.
- Create `tests/ui/test_deployment_outcome_controller.py`: deterministic controller lifecycle tests.
- Create `src/korvid/ui/widgets/deployment_outcome_screen.py`: live outcome modal and typed navigation results.
- Create `tests/ui/test_deployment_outcome_screen.py`: rendering and local-key tests.
- Modify `src/korvid/ui/app.py`, `src/korvid/__main__.py`, `src/korvid/ui/app_surfaces.py`, `tests/app_factory.py`, and `tests/test_main_wiring.py`: composition and lifecycle wiring.
- Modify `src/korvid/ui/action_policy.py`, `src/korvid/ui/app_runtime.py`, and palette/help tests: reopen latest tracker without a global key.
- Modify `docs/tui.md`, `docs/keybindings.md`, `docs/dev/capabilities.md`, and `docs/release-notes/unreleased.md`: user-facing behavior and limitations.

---

### Task 1: Pure Deployment Outcome Model

**Files:**
- Create: `src/korvid/core/deployment_outcome.py`
- Create: `tests/core/test_deployment_outcome.py`

**Interfaces:**
- Produces:
  - `DeploymentOperationTarget`
  - `DeploymentScaleIntent`
  - `DeploymentRestartIntent`
  - `DeploymentObservation`
  - `DeploymentPodEvidence`
  - `DeploymentOutcome`
  - `DeploymentOutcomePhase`
  - `evaluate_deployment_outcome(intent, observation) -> DeploymentOutcome`

- [ ] **Step 1: Write failing scale predicate tests**

```python
def test_scale_requires_all_controller_counts_and_current_generation() -> None:
    intent = scale_intent(replicas=3)
    observation = deployment_observation(
        generation=7,
        observed_generation=6,
        desired=3,
        current=3,
        updated=3,
        ready=3,
        available=3,
        unavailable=0,
    )

    outcome = evaluate_deployment_outcome(intent, observation)

    assert outcome.phase is DeploymentOutcomePhase.OBSERVING
    assert "generation" in outcome.summary


def test_scale_to_zero_completes_without_defaulting_missing_fields() -> None:
    complete = evaluate_deployment_outcome(
        scale_intent(replicas=0),
        deployment_observation(
            desired=0,
            current=0,
            updated=0,
            ready=0,
            available=0,
            unavailable=0,
        ),
    )
    incomplete = evaluate_deployment_outcome(
        scale_intent(replicas=0),
        deployment_observation(desired=0, current=None, ready=None),
    )

    assert complete.phase is DeploymentOutcomePhase.COMPLETED
    assert incomplete.phase is DeploymentOutcomePhase.OBSERVING
```

- [ ] **Step 2: Run scale tests and verify RED**

Run: `uv run pytest -p no:tach tests/core/test_deployment_outcome.py -q`

Expected: collection fails because `korvid.core.deployment_outcome` does not exist.

- [ ] **Step 3: Implement immutable scale types and evaluator**

```python
class DeploymentOutcomePhase(StrEnum):
    ACCEPTED = "accepted"
    OBSERVING = "observing"
    COMPLETED = "completed"
    STALLED = "stalled"
    SUPERSEDED = "superseded"
    REPLACED = "replaced"
    INCOMPLETE = "incomplete"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class DeploymentOperationTarget:
    epoch: int
    cluster_id: str
    namespace: str
    name: str
    uid: str


@dataclass(frozen=True, slots=True)
class DeploymentScaleIntent:
    target: DeploymentOperationTarget
    replicas: int


def evaluate_deployment_outcome(
    intent: DeploymentScaleIntent | DeploymentRestartIntent,
    observation: DeploymentObservation,
) -> DeploymentOutcome:
    if observation.uid != intent.target.uid:
        return DeploymentOutcome.replaced(observation.uid)
    if isinstance(intent, DeploymentScaleIntent):
        return _evaluate_scale(intent, observation)
    return _evaluate_restart(intent, observation)
```

Implement explicit optional integer parsing semantics: `None` remains unknown,
`bool` is rejected, and completion requires every required count.

- [ ] **Step 4: Add failing restart, blocker, replacement, and supersession tests**

```python
def test_restart_requires_exact_marker_and_observed_generation() -> None:
    intent = restart_intent(stamp="2026-10-09T12:00:00+00:00")
    observation = deployment_observation(
        restart_stamp=intent.restarted_at,
        generation=8,
        observed_generation=8,
        desired=2,
        updated=2,
        ready=2,
        available=2,
        unavailable=0,
    )

    assert evaluate_deployment_outcome(intent, observation).phase is DeploymentOutcomePhase.COMPLETED


def test_later_restart_is_superseded() -> None:
    outcome = evaluate_deployment_outcome(
        restart_intent(stamp="accepted"),
        deployment_observation(restart_stamp="later"),
    )

    assert outcome.phase is DeploymentOutcomePhase.SUPERSEDED


def test_progress_deadline_and_non_ready_pod_are_stalled() -> None:
    observation = deployment_observation(
        conditions=(condition("Progressing", "False", "ProgressDeadlineExceeded"),),
        pods=(pod_evidence("web-new-1", uid="pod-1", reason="ImagePullBackOff"),),
    )

    outcome = evaluate_deployment_outcome(scale_intent(replicas=3), observation)

    assert outcome.phase is DeploymentOutcomePhase.STALLED
    assert outcome.pods[0].uid == "pod-1"
```

- [ ] **Step 5: Implement restart and failure predicates**

Normalize only `Progressing`, `ReplicaFailure`, and `Available` conditions.
Treat a changed requested field as superseded, an explicit controller failure
as stalled, and missing evidence as observing or incomplete.

- [ ] **Step 6: Run core tests and lint**

Run: `uv run pytest -p no:tach tests/core/test_deployment_outcome.py -q`

Expected: all tests pass.

Run: `uv run ruff check src/korvid/core/deployment_outcome.py tests/core/test_deployment_outcome.py`

Expected: no diagnostics.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/core/deployment_outcome.py tests/core/test_deployment_outcome.py
git commit -m "feat(core): model Deployment operation outcomes"
```

### Task 2: Bounded Kubernetes Observation Reader

**Files:**
- Create: `src/korvid/k8s/deployment_outcomes.py`
- Create: `tests/k8s/test_deployment_outcomes.py`
- Modify: `src/korvid/k8s/client.py`

**Interfaces:**
- Consumes: Task 1 immutable observation types.
- Produces:
  - `DeploymentOutcomeReader(ABC)`
  - `KubeDeploymentOutcomeReader`
  - `async snapshot(target) -> DeploymentObservation`

- [ ] **Step 1: Write failing exact-identity and bounded-read tests**

```python
async def test_snapshot_reads_exact_deployment_and_caps_pod_evidence() -> None:
    api = FakeOutcomeApi(
        deployment=deployment(uid="deploy-1"),
        replica_sets=[replica_set(uid="rs-1", owner_uid="deploy-1")],
        pods=[pod(f"web-{index}", uid=f"pod-{index}", owner_uid="rs-1") for index in range(20)],
    )
    reader = KubeDeploymentOutcomeReader(api, max_pod_evidence=5)

    observation = await reader.snapshot(target(uid="deploy-1"))

    assert observation.uid == "deploy-1"
    assert len(observation.pods) == 5
    assert observation.partial_evidence is True
    assert api.namespaces_read == {"default"}
```

- [ ] **Step 2: Run reader tests and verify RED**

Run: `uv run pytest -p no:tach tests/k8s/test_deployment_outcomes.py -q`

Expected: module import failure.

- [ ] **Step 3: Add the read-only ABC and adapter**

```python
class DeploymentOutcomeReader(ABC):
    @abstractmethod
    async def snapshot(
        self, target: DeploymentOperationTarget
    ) -> DeploymentObservation:
        """Read one bounded authoritative observation."""


class KubeDeploymentOutcomeReader(DeploymentOutcomeReader):
    def __init__(self, client: KubeClient, *, max_pod_evidence: int = 5) -> None:
        self._client = client
        self._max_pod_evidence = max_pod_evidence
```

Add a private `KubeClient.list_raw_objects` method that accepts a fixed
namespace and optional label selector, records read telemetry, and returns raw
manifests. The adapter lists only `apps/v1 ReplicaSet` and core `v1 Pod`
objects in the target namespace and filters controller owner UIDs before
normalization.

- [ ] **Step 4: Add failure and malformed-payload tests**

Cover 403 propagation, 404 propagation, cancellation, same-name replacement,
missing selector, malformed status, pod cap, and pods owned by another
ReplicaSet.

- [ ] **Step 5: Implement defensive extraction**

Use mapping/list guards for every nested field. Preserve missing integer fields
as `None`; never coerce malformed values or booleans to zero. Redact and cap
condition and Pod reason strings using existing core redaction helpers.

- [ ] **Step 6: Run reader tests, mypy, and tach**

Run: `uv run pytest -p no:tach tests/k8s/test_deployment_outcomes.py -q`

Run: `uv run mypy src/korvid/core/deployment_outcome.py src/korvid/k8s/deployment_outcomes.py`

Run: `uv run tach check`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/k8s/client.py src/korvid/k8s/deployment_outcomes.py tests/k8s/test_deployment_outcomes.py
git commit -m "feat(k8s): read bounded Deployment outcome evidence"
```

### Task 3: Accepted-Write Observer Handoff

**Files:**
- Modify: `src/korvid/ui/write_gate.py`
- Modify: `src/korvid/ui/write_coordinator.py`
- Modify: `src/korvid/ui/resource_write_controller.py`
- Modify: `tests/ui/test_write_confirm_characterization.py`
- Modify: `tests/ui/test_write_ops.py`

**Interfaces:**
- Consumes: Task 1 operation intents.
- Produces:
  - `AcceptedWriteReceipt`
  - `AcceptedWriteObserver = Callable[[AcceptedWriteReceipt], Awaitable[None]]`
  - optional `on_accepted` keyword on `WriteGate.confirm` and
    `WriteCoordinator.run`

- [ ] **Step 1: Write failing perimeter ordering tests**

```python
async def test_observer_runs_only_after_success_audit() -> None:
    events: list[str] = []

    async def operation() -> None:
        events.append("mutation")

    async def accepted(receipt: AcceptedWriteReceipt) -> None:
        events.append("observer")
        assert receipt.action == "scale"

    await coordinator.run(
        "scale",
        DEPLOYMENT,
        "default",
        "web",
        operation,
        on_accepted=accepted,
    )

    assert events == ["mutation", "observer"]
    assert audit_outcomes() == ["intent", "success"]
```

Add parameterized cases proving no observer call after intent-audit failure,
mutation failure, success-audit failure, precondition refusal, cancellation,
or declined confirmation.

- [ ] **Step 2: Run handoff tests and verify RED**

Run: `uv run pytest -p no:tach tests/ui/test_write_confirm_characterization.py -q`

Expected: `on_accepted` is not accepted by the interface.

- [ ] **Step 3: Add receipt and observer contract**

```python
@dataclass(frozen=True, slots=True)
class AcceptedWriteReceipt:
    action: str
    meta: ResourceMeta
    namespace: str | None
    name: str
    accepted_at: str


AcceptedWriteObserver = Callable[[AcceptedWriteReceipt], Awaitable[None]]
```

Call the observer after the success audit. Catch only observer exceptions at
the observational boundary, log them, and notify:
`"<action> accepted, but outcome verification could not start: <error>"`.
Return `"done"` because observer failure cannot rewrite the accepted mutation.

- [ ] **Step 4: Attach exact Deployment intents**

In `ResourceWriteController`, pass `on_accepted` only when
`(meta.group, meta.plural) == ("apps", "deployments")`.

```python
on_accepted=self._deployment_outcomes.accept_scale(
    target=target,
    replicas=replicas,
)
```

For restart, carry the exact `stamp` already shared by preview and mutation.
Do not start a tracker for StatefulSet, DaemonSet, or ReplicaSet.

- [ ] **Step 5: Update every `WriteGate` fake**

Add the keyword with its exact type and default to fakes in:

- `tests/ui/test_write_confirm_characterization.py`
- `tests/ui/test_resource_write_controller.py` if present
- `tests/test_main_wiring.py`
- any file reported by mypy or `rg "class .*WriteGate" tests`

- [ ] **Step 6: Run targeted tests and mypy**

Run: `uv run pytest -p no:tach tests/ui/test_write_confirm_characterization.py tests/ui/test_write_ops.py -q`

Run: `uv run mypy src/korvid/ui/write_gate.py src/korvid/ui/write_coordinator.py src/korvid/ui/resource_write_controller.py`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/ui/write_gate.py src/korvid/ui/write_coordinator.py src/korvid/ui/resource_write_controller.py tests/ui
git commit -m "feat(ui): hand accepted Deployment writes to observers"
```

### Task 4: Tracker Controller

**Files:**
- Create: `src/korvid/ui/deployment_outcome_controller.py`
- Create: `tests/ui/test_deployment_outcome_controller.py`

**Interfaces:**
- Consumes: Task 1 intents/evaluator and Task 2 reader.
- Produces:
  - `DeploymentTrackerSnapshot`
  - `DeploymentOutcomeController.accept_scale(...)`
  - `DeploymentOutcomeController.accept_restart(...)`
  - `latest()`, `stop(tracker_id)`, `stop_all(reason)`, and `shutdown()`

- [ ] **Step 1: Write failing deterministic lifecycle tests**

```python
async def test_tracker_reads_immediately_and_completes() -> None:
    reader = ScriptedReader([observing(), completed()])
    controller = make_controller(reader)

    await controller.accept(scale_intent(replicas=3))
    await controller.poll_once_for_test()
    await controller.poll_once_for_test()

    assert controller.latest().outcome.phase is DeploymentOutcomePhase.COMPLETED
    assert reader.calls == 2


async def test_context_switch_stops_without_reading_new_cluster() -> None:
    controller = make_controller(ScriptedReader([observing()]))
    await controller.accept(scale_intent(replicas=3))

    await controller.stop_all("context changed")

    assert controller.latest().outcome.phase is DeploymentOutcomePhase.STOPPED
```

Use an injected async sleeper/clock so tests never assert wall-clock timing.

- [ ] **Step 2: Run controller tests and verify RED**

Run: `uv run pytest -p no:tach tests/ui/test_deployment_outcome_controller.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement bounded registry and polling**

```python
MAX_TRACKERS = 3
OBSERVATION_DEADLINE_SECONDS = 300.0
POLL_DELAYS_SECONDS = (0.0, 1.0, 2.0, 5.0, 10.0)
OUTCOME_WORKER_GROUP = "deployment-outcomes"
```

Use `UiSurface.run_worker`; do not create bare asyncio tasks. Each read checks
tracker ID and epoch before applying a result. 403, timeout, disconnect, and
404 map to explicit incomplete/replaced outcomes and preserve prior evidence.

- [ ] **Step 4: Add cap, cancellation, late-result, and shutdown tests**

Verify:

- fourth tracker evicts/stops the oldest;
- explicit stop is terminal;
- closing a screen does not stop;
- late result after stop/context change is ignored;
- shutdown cancels the worker group and waits;
- reader failure cannot change accepted write state.

- [ ] **Step 5: Implement notifications and immutable snapshots**

Notify only on phase changes. Messages must say `"accepted"` separately from
`"converged"`, `"stalled"`, `"superseded"`, `"replaced"`, or
`"verification incomplete"`.

- [ ] **Step 6: Run tests and lint**

Run: `uv run pytest -p no:tach tests/ui/test_deployment_outcome_controller.py -q`

Run: `uv run ruff check src/korvid/ui/deployment_outcome_controller.py tests/ui/test_deployment_outcome_controller.py`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/ui/deployment_outcome_controller.py tests/ui/test_deployment_outcome_controller.py
git commit -m "feat(ui): supervise bounded Deployment outcome trackers"
```

### Task 5: Outcome Screen and Identity-Safe Navigation

**Files:**
- Create: `src/korvid/ui/widgets/deployment_outcome_screen.py`
- Create: `tests/ui/test_deployment_outcome_screen.py`
- Modify: `src/korvid/ui/object_navigation.py`
- Modify: `tests/ui/test_object_navigation.py`

**Interfaces:**
- Consumes: Task 4 immutable tracker snapshots.
- Produces:
  - `DeploymentOutcomeScreen`
  - `OutcomeScreenAction`
  - UID-bearing Pod navigation request

- [ ] **Step 1: Write failing screen rendering tests**

```python
async def test_screen_keeps_api_acceptance_separate_from_convergence() -> None:
    screen = DeploymentOutcomeScreen(snapshot(phase="observing"))

    async with screen_app(screen).run_test() as pilot:
        text = screen.query_one(".outcome-status").renderable.plain

    assert "API request accepted" in text
    assert "Deployment convergence: observing" in text
    assert "completed" not in text.lower()
```

- [ ] **Step 2: Run screen tests and verify RED**

Run: `uv run pytest -p no:tach tests/ui/test_deployment_outcome_screen.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement modal structure and local bindings**

Use a `VerticalScroll` body with `height: auto; max-height: 80%`. Add local
bindings for Escape, stop, refresh, Pod selection, events, describe, and logs.
Return typed actions through `dismiss`; do not call the app directly.

- [ ] **Step 4: Add identity-safe navigation tests**

Prove Pod actions are disabled when UID is missing and that a changed UID is
refused before existing events/describe/logs routes open.

- [ ] **Step 5: Implement navigation adapter**

Extend existing object navigation with an optional expected UID. Fetch the
Pod manifest immediately before navigation and compare
`metadata.uid`. Report replacement rather than opening a same-name Pod.

- [ ] **Step 6: Run screen/navigation tests and format**

Run: `uv run pytest -p no:tach tests/ui/test_deployment_outcome_screen.py tests/ui/test_object_navigation.py -q`

Run: `uv run ruff format src/korvid/ui/widgets/deployment_outcome_screen.py tests/ui/test_deployment_outcome_screen.py`

Expected: all tests pass; formatter reports files unchanged or reformatted.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/ui/widgets/deployment_outcome_screen.py src/korvid/ui/object_navigation.py tests/ui
git commit -m "feat(ui): show Deployment outcome evidence and navigation"
```

### Task 6: Composition, Context Lifecycle, and Reopen Action

**Files:**
- Modify: `src/korvid/ui/app.py`
- Modify: `src/korvid/__main__.py`
- Modify: `src/korvid/ui/app_runtime.py`
- Modify: `src/korvid/ui/app_surfaces.py`
- Modify: `src/korvid/ui/action_policy.py`
- Modify: `tests/app_factory.py`
- Modify: `tests/test_main_wiring.py`
- Modify: `tests/ui/test_action_policy.py`
- Create: `tests/ui/test_deployment_outcome_app.py`

**Interfaces:**
- Consumes: Tasks 2, 4, and 5.
- Produces: a fully wired application journey and Action Palette entry.

- [ ] **Step 1: Write failing wiring and journey tests**

```python
async def test_confirmed_scale_opens_tracker_and_converges() -> None:
    app = build_outcome_app(script=[observing(), completed()])

    async with app.run_test() as pilot:
        await select_deployment(pilot)
        await pilot.press("S")
        await enter_replicas_and_confirm(pilot, 3)
        await until(
            pilot,
            lambda: app.latest_deployment_outcome.phase is DeploymentOutcomePhase.COMPLETED,
            label="Deployment scale converged",
        )

    assert app.write_ops.calls == [("scale", "web", 3)]
```

Add a context-switch test proving the old tracker stops before the old client
closes and no new-cluster read occurs.

- [ ] **Step 2: Run app tests and verify RED**

Run: `uv run pytest -p no:tach tests/test_main_wiring.py tests/ui/test_deployment_outcome_app.py -q`

Expected: missing outcome controller wiring.

- [ ] **Step 3: Compose reader and controller**

Construct `KubeDeploymentOutcomeReader` in `__main__.py`, inject it into
`DeploymentOutcomeController`, and inject the controller into
`ResourceWriteController`. Base installs with no Kubernetes client leave the
feature unavailable without importing optional extras.

- [ ] **Step 4: Wire lifecycle**

On context switch, await `stop_all("context changed")` before client teardown.
On app unmount, await `shutdown()`. Add the worker group to explicit lifecycle
tests.

- [ ] **Step 5: Add Action Palette reopening**

Register `"Show latest Deployment outcome"` with an availability reason when
no tracker exists. The action opens the latest immutable snapshot and carries
screen results back to the controller. Do not add a global keybinding.

- [ ] **Step 6: Update fakes and run integration checks**

Run: `uv run pytest -p no:tach tests/test_main_wiring.py tests/ui/test_action_policy.py tests/ui/test_deployment_outcome_app.py -q`

Run: `uv run mypy src/korvid`

Run: `uv run tach check`

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/__main__.py src/korvid/ui tests/app_factory.py tests/test_main_wiring.py tests/ui
git commit -m "feat: wire Deployment outcome tracking into the TUI"
```

### Task 7: Documentation and Complete Verification

**Files:**
- Modify: `docs/tui.md`
- Modify: `docs/keybindings.md`
- Modify: `docs/dev/capabilities.md`
- Modify: `docs/release-notes/unreleased.md`

**Interfaces:**
- Consumes: completed behavior from Tasks 1-6.
- Produces: maintained documentation and exact verification evidence.

- [ ] **Step 1: Update user documentation**

Document:

- automatic tracker start after accepted direct-TUI Deployment scale/restart;
- accepted vs observing vs converged/stalled/incomplete;
- modal-local keys and Action Palette reopen path;
- five-minute deadline, three-tracker cap, session-only retention;
- Deployment-only scope and no application-health guarantee.

- [ ] **Step 2: Update capability ledger and release notes**

Mark only implemented behavior. Do not claim agent/MCP writes, persistence,
automatic rollback, other workload kinds, or release publication.

- [ ] **Step 3: Run formatting and targeted tests**

Run:

```bash
uv run ruff check --fix \
  src/korvid/core/deployment_outcome.py \
  src/korvid/k8s/deployment_outcomes.py \
  src/korvid/ui/deployment_outcome_controller.py \
  src/korvid/ui/widgets/deployment_outcome_screen.py \
  tests/core/test_deployment_outcome.py \
  tests/k8s/test_deployment_outcomes.py \
  tests/ui/test_deployment_outcome_controller.py \
  tests/ui/test_deployment_outcome_screen.py \
  tests/ui/test_deployment_outcome_app.py
uv run ruff format <same paths>
uv run pytest -p no:tach \
  tests/core/test_deployment_outcome.py \
  tests/k8s/test_deployment_outcomes.py \
  tests/ui/test_write_confirm_characterization.py \
  tests/ui/test_write_ops.py \
  tests/ui/test_deployment_outcome_controller.py \
  tests/ui/test_deployment_outcome_screen.py \
  tests/ui/test_deployment_outcome_app.py -q
```

Expected: no lint diagnostics and all targeted tests pass.

- [ ] **Step 4: Run repository gates**

Run: `make check`

Expected: source-size, ruff, strict mypy, pytest, and tach all pass.

Run: `pre-commit run --all-files`

Expected: every hook passes without bypass.

- [ ] **Step 5: Review the complete diff**

Run:

```bash
git diff --check origin/main...HEAD
git status --short
git diff --stat origin/main...HEAD
```

Expected: no whitespace errors; only intended files are changed; the
pre-existing `uv.lock` workspace modification is not staged or committed.

- [ ] **Step 6: Commit documentation**

```bash
git add docs/tui.md docs/keybindings.md docs/dev/capabilities.md docs/release-notes/unreleased.md
git commit -m "docs: explain Deployment operation outcome tracking"
```

- [ ] **Step 7: Request final code review**

Invoke the repository code-review workflow against the complete branch. Fix
credible correctness, security, data-loss, architecture, or required-check
findings with a failing regression test first, then rerun `make check`.
