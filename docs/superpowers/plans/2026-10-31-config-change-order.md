# ConfigMap and Secret Change Order (#410 P2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In the `:consumers` view, say whether a change to the selected ConfigMap or Secret, seen this session, followed each environment-variable or `subPath` consumer's container start (#513, slice P2 of #410).

**Architecture:** The Pod projection (`core/config_consumers.py`) gains each container's current `state.running.startedAt`. A new pure module, `core/config_changes.py`, bounds a change between two resource-version sightings and orders it against a start with a one-minute skew allowance. `explain_propagation` reports the run's sighting, the controller keeps a bounded `ChangeLog` across runs, and the view adds an **Object change** column and a note.

**Tech Stack:** Python 3.11+, Textual, Rich `Text`, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-28-config-propagation-design.md`, section "P2: when a change is known to follow a start".
- **Start times:** `state.running.startedAt` only, from the container's own status list (`containerStatuses`, `initContainerStatuses`, `ephemeralContainerStatuses`), matched by name. Waiting, terminated or missing status has no start. `lastState` and `terminated.startedAt` are never read; no other status field reaches the result.
- **Bounds:** a change is bounded by the old sighting's send time and the new sighting's arrival. Resource versions are compared only for equality; `creationTimestamp` is never a change time.
- **Ordering:** "changed after this container started" only when `sent_old >= started_at + 1 minute`; "changed before this container started" only when `arrived_new <= started_at - 1 minute`; otherwise "not established".
- **Wording:** the object changed, never the configuration changed.
- **No values, no writes:** only resource versions and times are kept.
- **Code rules:** no `assert` in `src/`; every `pytest.raises` takes `match=`; maximum complexity 10; Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Where the sightings come from

The design says a change is seen as two sightings but not which reads make them. Each `:consumers` run already GETs the object's metadata, and `PAYLOAD_METADATA_FIELDS` keeps its `resourceVersion`, so each run is one sighting:

- its send time is `clock()` taken before the reads start, earlier than the real send;
- its arrival is the object observation's `observed_at`, taken after the response.

Both errors widen the bound, which can only turn an ordering into "not established". The cost is that korvid sees a change only if the view was opened before and after it, in the same session. The note and `docs/tui.md` say so.

The log is keyed by the run token's context epoch and session generation and by the object's kind, namespace, name and observed UID:

- a context switch, new credentials or a recreated object starts afresh;
- it holds at most 256 objects, least recently recorded first out;
- a sighting sent before the last one recorded is ignored.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/core/config_consumers.py` | modify | `Consumer.started_at` from the container's running state |
| `src/korvid/core/config_changes.py` | create | `Sighting`, `ObservedChange`, `ObjectKey`, `ChangeLog`, `Ordering`, `order`, `SKEW` |
| `src/korvid/core/config_propagation.py` | modify | `PropagationReport.sighting` |
| `src/korvid/ui/config_propagation_controller.py` | modify | owns the `ChangeLog`, passes the change to the view |
| `src/korvid/ui/widgets/config_propagation_screen.py` | modify | **Object change** column and the change note |
| `tests/core/test_config_consumers.py` | modify | start-time projection |
| `tests/core/test_config_changes.py` | create | bounds, log and ordering |
| `tests/core/test_config_propagation.py` | modify | the report's sighting |
| `tests/ui/test_config_propagation_controller.py` | modify | changes across runs and scopes |
| `tests/ui/test_config_propagation_screen.py` | modify | the column and the notes |
| `tests/ui/test_config_propagation_workflow.py` | modify | reopening after a change, end to end |
| `tests/test_optional_extras.py` | modify | the new module imports without extras |
| `docs/tui.md`, `docs/release-notes/unreleased.md` | modify | what the column means and its limits |

No version-matrix scenario is added: a matrix cluster's Pods are not guaranteed to be running when the scenario reads them, and nothing in P2 depends on the API version.

---

### Task 1: Start times in the projection

**Files:**
- Modify: `src/korvid/core/config_consumers.py`
- Test: `tests/core/test_config_consumers.py`

**Interfaces:**
- Produces: `Consumer.started_at: datetime | None`, timezone-aware, `None` unless the container is running.

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_config_consumers.py` (and import `from datetime import UTC, datetime`):

```python
def _with_status(spec: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    pod = _pod(spec)
    pod["status"] = status
    return pod


def _running(name: str, started: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "state": {"running": {"startedAt": started}}, **extra}
```

Then these tests, each building a Pod with `_with_status` and reading `started_at` from `pod_consumers(pod, "ConfigMap", "settings")`:

- `test_each_container_gets_its_running_start_from_its_own_status_list`: an app container, a sidecar (`initContainers` with `restartPolicy: Always`) and an ephemeral container, each with a start in its own list.
- `test_a_status_from_another_list_or_container_is_not_used`: the app's name in `initContainerStatuses` and another name in `containerStatuses` give `None`.
- `test_only_a_running_container_has_a_start`: waiting, terminated (with a running `lastState`), an unparseable time, and no status give four `None`s.
- `test_a_restarted_container_has_its_current_start`: `running.startedAt` wins over `lastState.terminated.startedAt`.
- `test_nothing_else_of_the_status_reaches_the_result`: a status `message` and a status `image` are absent from `repr(consumers)`.
- `test_a_start_without_a_time_zone_is_not_used`: `"2026-10-30T09:00:00"` gives `None`.
- `test_a_mounting_container_gets_its_start`: a volume row carries the mounting container's start.

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_consumers.py -q`
Expected: FAIL with `AttributeError: 'Consumer' object has no attribute 'started_at'`.

- [ ] **Step 3: Implement**

Pair each container list with its status list, read only running starts, and pass them through `_Container` to every row that names a container:

```python
_CONTAINER_LISTS = (
    ("containers", ContainerCategory.CONTAINER, "containerStatuses"),
    ("initContainers", ContainerCategory.INIT, "initContainerStatuses"),
    ("ephemeralContainers", ContainerCategory.EPHEMERAL, "ephemeralContainerStatuses"),
)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _running_starts(statuses: object) -> dict[str, datetime]:
    """Each running container's current start, by name; nothing else of a status."""
    starts: dict[str, datetime] = {}
    for status in statuses if isinstance(statuses, list) else []:
        name = _text(_mapping(status).get("name"))
        running = _mapping(_mapping(_mapping(status).get("state")).get("running"))
        started = _timestamp(running.get("startedAt"))
        if name is not None and started is not None:
            starts[name] = started
    return starts
```

`_containers(spec, status)` looks each container's name up in `_running_starts(status.get(status_field))`; `_env_consumers` and `_volume_consumers` pass `started_at=container.started_at`; `pod_consumers` passes `_mapping(pod.get("status"))`.

- [ ] **Step 4: Run them to see them pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_consumers.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/config_consumers.py tests/core/test_config_consumers.py
git commit -m "Read each consuming container's running start time"
```

### Task 2: Bounding and ordering a change

**Files:**
- Create: `src/korvid/core/config_changes.py`
- Create: `tests/core/test_config_changes.py`
- Modify: `tests/test_optional_extras.py` (add `"korvid.core.config_changes"` to the base-import list)

**Interfaces:**
- Produces: `Sighting(resource_version: str, sent_at: datetime, arrived_at: datetime)`; `ObservedChange(after: datetime, before: datetime)`; `ObjectKey(epoch: int, session_generation: int, kind: str, namespace: str, name: str, uid: str)`; `ChangeLog(capacity: int = 256).record(key, sighting) -> ObservedChange | None`; `Ordering.{AFTER_START, BEFORE_START, NOT_ESTABLISHED}`; `order(change, started_at: datetime | None) -> Ordering`; `SKEW = timedelta(minutes=1)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_config_changes.py` with:

```python
"""Tests for bounding a ConfigMap's or Secret's change by two sightings (#410 P2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from korvid.core.config_changes import (
    SKEW,
    ChangeLog,
    ObjectKey,
    ObservedChange,
    Ordering,
    Sighting,
    order,
)

START = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
KEY = ObjectKey(1, 0, "ConfigMap", "shop", "settings", "uid-1")


def _at(minutes: float) -> datetime:
    return START + timedelta(minutes=minutes)


def _sighting(version: str, sent: float, arrived: float | None = None) -> Sighting:
    return Sighting(version, _at(sent), _at(sent if arrived is None else arrived))


def test_a_change_is_bounded_by_the_old_send_and_the_new_arrival() -> None:
    log = ChangeLog()

    assert log.record(KEY, _sighting("1", -60, -59)) is None
    change = log.record(KEY, _sighting("2", 60, 61))

    assert change == ObservedChange(_at(-60), _at(61))


def test_a_repeated_version_moves_the_bound_forward_and_keeps_the_change() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))
    log.record(KEY, _sighting("1", -30))
    first = log.record(KEY, _sighting("2", 30, 31))

    again = log.record(KEY, _sighting("2", 90, 91))

    assert first == ObservedChange(_at(-30), _at(31))
    assert again == first


def test_the_latest_change_is_the_one_reported() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))
    log.record(KEY, _sighting("2", 30))

    change = log.record(KEY, _sighting("3", 90, 92))

    assert change == ObservedChange(_at(30), _at(92))


def test_a_sighting_sent_before_the_last_one_is_ignored() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))
    log.record(KEY, _sighting("2", 30))

    late = log.record(KEY, _sighting("1", 0, 40))

    assert late == ObservedChange(_at(-60), _at(30))
    assert log.record(KEY, _sighting("2", 50)) == ObservedChange(_at(-60), _at(30))


@pytest.mark.parametrize(
    "key",
    [
        ObjectKey(1, 0, "ConfigMap", "shop", "settings", "uid-2"),
        ObjectKey(2, 0, "ConfigMap", "shop", "settings", "uid-1"),
        ObjectKey(1, 1, "ConfigMap", "shop", "settings", "uid-1"),
        ObjectKey(1, 0, "Secret", "shop", "settings", "uid-1"),
        ObjectKey(1, 0, "ConfigMap", "other", "settings", "uid-1"),
        ObjectKey(1, 0, "ConfigMap", "shop", "other", "uid-1"),
    ],
)
def test_another_incarnation_or_scope_starts_without_a_sighting(key: ObjectKey) -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -60))

    assert log.record(key, _sighting("2", 60)) is None


def test_the_least_recently_recorded_object_is_forgotten_past_capacity() -> None:
    log = ChangeLog(capacity=2)
    other = ObjectKey(1, 0, "ConfigMap", "shop", "other", "uid-2")
    third = ObjectKey(1, 0, "ConfigMap", "shop", "third", "uid-3")
    log.record(KEY, _sighting("1", -60))
    log.record(other, _sighting("1", -50))
    log.record(KEY, _sighting("1", -40))

    log.record(third, _sighting("1", -30))

    assert log.record(KEY, _sighting("2", 60)) == ObservedChange(_at(-40), _at(60))
    assert log.record(other, _sighting("2", 60)) is None


@pytest.mark.parametrize(
    ("after", "before", "expected"),
    [
        (1, 3, Ordering.AFTER_START),
        (5, 10, Ordering.AFTER_START),
        (-10, -1, Ordering.BEFORE_START),
        (-10, -5, Ordering.BEFORE_START),
        (-60, 60, Ordering.NOT_ESTABLISHED),
        (0.5, 3, Ordering.NOT_ESTABLISHED),
        (-3, -0.5, Ordering.NOT_ESTABLISHED),
    ],
    ids=[
        "after",
        "well-after",
        "before",
        "well-before",
        "straddles",
        "within-skew-after",
        "within-skew-before",
    ],
)
def test_a_change_is_ordered_against_a_start_only_outside_the_skew_allowance(
    after: float, before: float, expected: Ordering
) -> None:
    assert order(ObservedChange(_at(after), _at(before)), START) is expected


def test_a_delayed_old_response_does_not_place_the_change_after_the_start() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", -5, 30))
    change = log.record(KEY, _sighting("2", 60))

    assert change is not None
    assert order(change, START) is Ordering.NOT_ESTABLISHED


def test_without_a_start_the_order_is_not_established() -> None:
    assert order(ObservedChange(_at(5), _at(10)), None) is Ordering.NOT_ESTABLISHED


def test_the_skew_allowance_is_one_minute() -> None:
    assert timedelta(minutes=1) == SKEW


@pytest.mark.parametrize(
    ("version", "sent", "arrived", "message"),
    [
        ("", START, START, "resource version"),
        ("1", _at(1), START, "arrives after it is sent"),
        ("1", START.replace(tzinfo=None), START.replace(tzinfo=None), "aware"),
    ],
)
def test_a_sighting_is_checked(
    version: str, sent: datetime, arrived: datetime, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        Sighting(version, sent, arrived)


def test_a_capacity_is_positive() -> None:
    with pytest.raises(ValueError, match="capacity"):
        ChangeLog(capacity=0)


def test_a_sighting_sent_at_the_same_moment_still_counts() -> None:
    log = ChangeLog()
    log.record(KEY, _sighting("1", 0, 1))

    assert log.record(KEY, _sighting("2", 0, 2)) == ObservedChange(_at(0), _at(2))
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_changes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'korvid.core.config_changes'`.

- [ ] **Step 3: Implement**

Create `src/korvid/core/config_changes.py` with:

```python
"""When a ConfigMap or Secret is known to have changed, relative to a container's start (#410 P2).

korvid sees a change to an object only as two sightings of it with different
resource versions. A sighting is not an instant: the server answered at some
point between the request being sent and the response arriving. So a change
is bounded by the old sighting's send time and the new sighting's arrival,
and a delayed old-version response cannot push the bound past the change.

Resource versions are compared only for equality, never ordered, and
`creationTimestamp` is never used as a change time. A container's start
comes from the node's clock and the sightings from korvid's, so the two are
ordered only with a fixed allowance for skew between them.

Only the changes korvid saw are known: one between two sightings in this
session, of the same incarnation, under the same context and credentials.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

#: How far the node's clock and korvid's may disagree.
SKEW = timedelta(minutes=1)
#: How many objects a `ChangeLog` remembers.
DEFAULT_CAPACITY = 256


@dataclass(frozen=True, slots=True)
class Sighting:
    """One read of an object's resource version: sent at `sent_at`, back by `arrived_at`.

    Both times are korvid's. `sent_at` may be earlier than the request was
    really sent, and `arrived_at` later than the response really arrived: a
    wider sighting only widens the change it bounds.
    """

    resource_version: str
    sent_at: datetime
    arrived_at: datetime

    def __post_init__(self) -> None:
        if not self.resource_version:
            raise ValueError("a sighting has a resource version")
        if self.sent_at.tzinfo is None or self.arrived_at.tzinfo is None:
            raise ValueError("sighting times are timezone-aware")
        if self.arrived_at < self.sent_at:
            raise ValueError("a sighting arrives after it is sent")


@dataclass(frozen=True, slots=True)
class ObservedChange:
    """A change known to have happened after `after` and before `before`."""

    after: datetime
    before: datetime


@dataclass(frozen=True, slots=True)
class ObjectKey:
    """One incarnation of an object, under one context epoch and session generation."""

    epoch: int
    session_generation: int
    kind: str
    namespace: str
    name: str
    uid: str


class Ordering(Enum):
    """Where a change falls relative to a container's start."""

    AFTER_START = "after"
    BEFORE_START = "before"
    NOT_ESTABLISHED = "not-established"


def order(change: ObservedChange, started_at: datetime | None) -> Ordering:
    """Whether `change` is known to follow or precede a start at `started_at`.

    The whole bound must clear the start by `SKEW`; a bound that straddles it,
    or no start at all, establishes neither.
    """
    if started_at is None:
        return Ordering.NOT_ESTABLISHED
    if change.after >= started_at + SKEW:
        return Ordering.AFTER_START
    if change.before <= started_at - SKEW:
        return Ordering.BEFORE_START
    return Ordering.NOT_ESTABLISHED


@dataclass(frozen=True, slots=True)
class _Entry:
    last: Sighting
    change: ObservedChange | None


class ChangeLog:
    """The last sighting and the latest change of each recently read object.

    It is bounded: past `capacity` objects, the least recently recorded is
    forgotten, and its next sighting starts afresh.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("a change log's capacity is at least one")
        self._capacity = capacity
        self._entries: OrderedDict[ObjectKey, _Entry] = OrderedDict()

    def record(self, key: ObjectKey, sighting: Sighting) -> ObservedChange | None:
        """Record `sighting` of `key`, and return the latest change known for it.

        A sighting sent before the last one recorded is ignored: it can only
        be older news.
        """
        entry = self._entries.pop(key, None)
        if entry is None:
            entry = _Entry(sighting, None)
        elif sighting.sent_at < entry.last.sent_at:
            pass
        elif sighting.resource_version == entry.last.resource_version:
            entry = _Entry(sighting, entry.change)
        else:
            entry = _Entry(sighting, ObservedChange(entry.last.sent_at, sighting.arrived_at))
        self._entries[key] = entry
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)
        return entry.change
```

- [ ] **Step 4: Run them to see them pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_changes.py tests/test_optional_extras.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/config_changes.py tests/core/test_config_changes.py tests/test_optional_extras.py
git commit -m "Bound an observed ConfigMap or Secret change by two sightings"
```

### Task 3: The report's sighting

**Files:**
- Modify: `src/korvid/core/config_propagation.py`
- Test: `tests/core/test_config_propagation.py`

**Interfaces:**
- Consumes: `Sighting` from Task 2.
- Produces: `PropagationReport.sighting: Sighting | None = None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/core/test_config_propagation.py`:

```python
class _Ticking:
    """A clock that moves a second forward each time it is read."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return NOW + timedelta(seconds=self.calls)


def _versioned(version: object) -> dict[str, Any]:
    metadata = {**SETTINGS_META["metadata"], "resourceVersion": version}
    return {"metadata": metadata}


async def test_an_observed_object_is_sighted_from_before_the_reads_to_its_arrival() -> None:
    clock = _Ticking()

    report = await explain_propagation(
        FakeReader(get=_versioned("812")), _snapshot(), TARGET, token=TOKEN, clock=clock
    )

    assert report.sighting == Sighting("812", NOW + timedelta(seconds=1), report.object.observed_at)
    assert report.sighting.arrived_at > report.sighting.sent_at


@pytest.mark.parametrize(
    "get",
    [
        SETTINGS_META,
        _versioned(""),
        _versioned(812),
        _status(404, "NotFound", name="settings", kind="configmaps"),
        _status(403, "Forbidden"),
    ],
    ids=["no-version", "empty-version", "non-string-version", "absent", "denied"],
)
async def test_without_an_observed_resource_version_there_is_no_sighting(get: object) -> None:
    report = await _explain(FakeReader(get=get))

    assert report.sighting is None
```

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_propagation.py -q`
Expected: FAIL with `AttributeError: 'PropagationReport' object has no attribute 'sighting'`.

- [ ] **Step 3: Implement**

`_read_object` also returns the observed object's `metadata.resourceVersion` when it is a non-empty string. `explain_propagation` takes `sent_at = clock()` just before `asyncio.gather` and builds the sighting:

```python
def _sighting(observation: Observation, version: str | None, sent_at: datetime) -> Sighting | None:
    """The run's sighting; only an observed object has a version, and so a time."""
    observed_at = observation.observed_at
    if version is None or observed_at is None:
        return None
    return Sighting(version, sent_at, observed_at)
```

- [ ] **Step 4: Run them to see them pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_propagation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/config_propagation.py tests/core/test_config_propagation.py
git commit -m "Report the selected object's resource-version sighting"
```

### Task 4: The controller remembers sightings

**Files:**
- Modify: `src/korvid/ui/config_propagation_controller.py`
- Modify: `src/korvid/ui/widgets/config_propagation_screen.py` (constructor only: `change: ObservedChange | None = None`, kept as `self.change`)
- Test: `tests/ui/test_config_propagation_controller.py`

**Interfaces:**
- Consumes: `ChangeLog`, `ObjectKey`, `ObservedChange` (Task 2); `PropagationReport.sighting` (Task 3).
- Produces: `ConfigPropagationScreen(report, clock, *, change=None, refresh_seconds=5.0)` and its `change` attribute.

- [ ] **Step 1: Write the failing tests**

Give `FakeClient` a settable `version`, `uid` and `get_error`, returned in the object's metadata, and give `Harness` a settable `now` used as its clock. Then append:

```python
async def _open(harness: Harness, *, minutes: int, version: str = "1") -> ConfigPropagationScreen:
    harness.now = NOW + timedelta(minutes=minutes)
    harness.client.version = version
    harness.controller.open_selected()
    await harness.run()
    screen = harness.ui.screens[-1]
    assert isinstance(screen, ConfigPropagationScreen)
    harness.ui.screens.clear()
    return screen


async def test_a_new_resource_version_is_shown_as_a_change_since_the_last_run() -> None:
    harness = Harness()

    first = await _open(harness, minutes=0)
    unchanged = await _open(harness, minutes=5)
    changed = await _open(harness, minutes=10, version="2")

    assert first.change is None
    assert unchanged.change is None
    assert changed.change == ObservedChange(NOW + timedelta(minutes=5), NOW + timedelta(minutes=10))
```

Also `test_a_run_that_does_not_see_the_object_keeps_the_last_sighting` (a 403 run between two sightings) and `test_another_incarnation_context_or_session_starts_without_a_change` (a new UID, a new epoch and a new session generation each start afresh, and the next sighting in the same scope finds the change).

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --frozen pytest -p no:tach tests/ui/test_config_propagation_controller.py -q`
Expected: FAIL with `AttributeError: 'ConfigPropagationScreen' object has no attribute 'change'`.

- [ ] **Step 3: Implement**

The controller creates `self._changes = ChangeLog()`, records each report before the "user moved on" check, and hands the change to the screen:

```python
    def _record(self, report: PropagationReport, token: RunToken) -> ObservedChange | None:
        """The object's latest change seen this session, after this run's sighting."""
        uid = report.object.observed_uid
        if report.sighting is None or uid is None:
            return None
        target = report.target
        key = ObjectKey(
            token.epoch, token.session_generation, target.kind, target.namespace, target.name, uid
        )
        return self._changes.record(key, report.sighting)
```

- [ ] **Step 4: Run them to see them pass**

Run: `uv run --frozen pytest -p no:tach tests/ui/test_config_propagation_controller.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/config_propagation_controller.py src/korvid/ui/widgets/config_propagation_screen.py tests/ui/test_config_propagation_controller.py
git commit -m "Remember each run's sighting and pass the latest change to the view"
```

### Task 5: The column and the notes

**Files:**
- Modify: `src/korvid/ui/widgets/config_propagation_screen.py`
- Modify: `tests/ui/test_config_propagation_screen.py`, `tests/ui/test_config_propagation_workflow.py`

**Interfaces:**
- Consumes: `order`, `Ordering` (Task 2); `Consumer.started_at` (Task 1); `report.sighting` (Task 3).
- Produces: `table_rows(consumers, change=None)`, whose rows gain a fourth cell when `change` is given; `report_notes(report, now, change=None)`.

- [ ] **Step 1: Write the failing tests**

In `tests/ui/test_config_propagation_screen.py`, with `CHANGE = ObservedChange(NOW - 30 min, NOW - 20 min)`:

- `test_a_read_at_start_row_says_where_the_change_falls_against_its_start`: starts 60 and 31 minutes ago give "changed after this container started" (31 is the boundary), 25 minutes ago straddles and gives "not established", a container without a start gives "not established", and a volume row gives an empty cell.
- `test_a_change_before_the_start_is_said`: a start 10 minutes ago gives "changed before this container started".
- `test_without_a_change_rows_have_three_cells`.
- `test_a_seen_change_is_described_as_an_object_change_this_session`: the note reads "The ConfigMap changed between 2026-10-30 08:30:00 UTC and 2026-10-30 08:40:00 UTC, as seen this session: it has a new resource version, which may change only its metadata. An earlier change is not known."
- `test_without_a_seen_change_the_note_says_how_one_is_seen`: with a sighting and an environment consumer, "Not established: whether the ConfigMap changed after its consumers started: a change is seen only between two openings of this view in this session"; not with only volume consumers, nor without a sighting.
- `test_the_change_column_is_shown_only_when_a_change_is_known`, parametrized over no change (three columns) and `CHANGE` (four, every row as wide, the change note shown).

In `tests/ui/test_config_propagation_workflow.py`, give the Pod a running status since 2020 and the client a settable `resourceVersion`, and add `test_reopening_after_the_object_changed_orders_the_change_against_the_start`: open `:consumers`, close it, bump the version, reopen, and the row's fourth cell reads "changed after this container started".

- [ ] **Step 2: Run them to see them fail**

Run: `uv run --frozen pytest -p no:tach tests/ui/test_config_propagation_screen.py tests/ui/test_config_propagation_workflow.py -q`
Expected: FAIL with `TypeError: table_rows() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Implement**

```python
_CHANGE_COLUMN = "Object change"
_ORDERINGS = {
    Ordering.AFTER_START: "changed after this container started",
    Ordering.BEFORE_START: "changed before this container started",
    Ordering.NOT_ESTABLISHED: "not established",
}


def _change_cell(consumer: Consumer, change: ObservedChange) -> str:
    if consumer.mode not in _READ_AT_START:
        return ""
    return _ORDERINGS[order(change, consumer.started_at)]


def _time(at: datetime) -> str:
    return f"{at.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC"


def _change_note(report: PropagationReport, change: ObservedChange | None) -> Text | None:
    kind = report.target.kind
    if change is not None:
        return Text(
            f"The {kind} changed between {_time(change.after)} and {_time(change.before)}, "
            "as seen this session: it has a new resource version, which may change only its "
            "metadata. An earlier change is not known."
        )
    if report.sighting is None or not any(c.mode in _READ_AT_START for c in report.consumers):
        return None
    return not_established(
        f"whether the {kind} changed after its consumers started: a change is seen only "
        "between two openings of this view in this session"
    )
```

`report_notes` puts the change note right after the object notes. `on_mount` adds `_CHANGE_COLUMN` only when `self.change` is set; a heading row's missing fourth cell is left empty by the table.

- [ ] **Step 4: Run them to see them pass**

Run: `uv run --frozen pytest -p no:tach tests/ui/test_config_propagation_screen.py tests/ui/test_config_propagation_workflow.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/widgets/config_propagation_screen.py tests/ui/test_config_propagation_screen.py tests/ui/test_config_propagation_workflow.py
git commit -m "Say whether a seen change followed each consumer's start"
```

### Task 6: Document it

**Files:**
- Modify: `docs/tui.md` ("Configuration propagation"), `docs/release-notes/unreleased.md`

- [ ] **Step 1:** In `docs/tui.md`, after "Enter on a row opens that Pod.", describe the **Object change** column, its three answers, the start time it uses, the one-minute skew allowance, and its limits: only changes seen between two openings in one context and session against one incarnation, a new resource version may be only a metadata change, and no history across restarts.
- [ ] **Step 2:** Add one bullet to the "Configuration propagation" entry in `docs/release-notes/unreleased.md`.
- [ ] **Step 3: Commit**

```bash
git add docs/tui.md docs/release-notes/unreleased.md docs/superpowers/plans/2026-10-31-config-change-order.md
git commit -m "Document the configuration change order"
```

### Task 7: Run the full gate

- [ ] **Step 1:** Run `make check`, or behind the mirror the same chain with the venv's tools: `scripts/check_source_size.py`, `ruff check`, `ruff format --check`, `mypy src/`, `tach check`, `pytest`.
- [ ] **Step 2:** Mutation-check each new condition: the skew comparisons (`>=` and `<=`), the out-of-order guard, the same-version replacement, the eviction order, the status list each category reads, the timezone check, and the screen's change cell, column and note. Every mutation must fail a test, except where the code exists only for the type checker (an observed object always has a UID).
