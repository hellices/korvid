# Bounded Session Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver issue #282: a bounded in-memory session timeline with typed watch / Warning event / context-switch / write entries, deterministic filters, and a Textual navigation screen that stays safe without an LLM.

**Architecture:** Add a pure `korvid.core.session_timeline` model that owns entry envelopes, count+byte bounds, projection/redaction for Warning events, and deterministic snapshot filtering. `KorvidApp` and `WatchManager` become producers; the app also owns a dedicated Warning-event worker, context-switch/write recording, and a modal `SessionTimelineScreen` that renders filtered snapshots and reuses existing `_jump_to_object` navigation.

**Tech Stack:** Python 3.11+, frozen dataclasses, `deque`, `enum.StrEnum`, asyncio, Textual `ModalScreen`/`DataTable`, kubernetes_asyncio raw watches, pytest/Pilot, Ruff, mypy strict, tach.

## Global Constraints

- Follow `docs/dev/specs/2026-08-14-operational-relationships-roadmap-design.md` Slice 2 (`SessionTimeline` / view) plus Timeline testing lines 258-265 and GitHub issue #282.
- `SessionTimeline` stays in `src/korvid/core/`; Textual imports remain confined to `src/korvid/ui/`.
- Enforce both `max_entries` and `max_bytes` on **every append**; eviction is oldest-first and visible in the timeline view.
- Reject any single entry whose UTF-8 JSON encoding exceeds `max_bytes`; report a visible diagnostic and never truncate it into a misleading record.
- Timeline entries carry only bounded metadata summaries: no full manifests, no Secret values, no arbitrary audit payloads, and no unbounded Event messages.
- Warning Event text must pass the existing redaction/control-character boundary before storage; redaction never substitutes for the whole-entry byte cap, so an oversized redacted Event is refused rather than truncated.
- Timeline failures, producer callback failures, and screen/render failures must not kill the main resource watches or weaken the audit fail-closed invariant.
- The default timeline filter is the current context epoch only; older epochs remain stored (subject to bounds), clearly labeled, and opt-in.
- The feature must work with `KorvidConfig(agent_enabled=False)`; no LLM/provider dependency is allowed.
- No new dependency and no `uv lock` invocation.
- Use TDD for every task: RED on the targeted tests first, then GREEN.
- Run `uv run tach check` whenever imports cross packages.
- Adopt explicit defaults in config and docs: `timeline.max_entries = 500`, `timeline.max_bytes = 262144`.
- Commit after each task once targeted pytest, Ruff, mypy, and (when imports cross packages) tach pass.
- Every commit includes `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.

---

## File map

### New files

- `src/korvid/core/session_timeline.py` — bounded deque model, typed payloads, resource matching, UTF-8 byte accounting, Warning-event projection/redaction, and filtered snapshots.
- `src/korvid/ui/widgets/session_timeline_screen.py` — modal `DataTable` view for timeline snapshots, fixed filter toggles, banners, and Enter-to-navigate results.
- `tests/core/test_session_timeline.py`
- `tests/ui/test_session_timeline_screen.py`
- `tests/ui/test_session_timeline_flow.py`

### Modified files

- `src/korvid/core/config.py` — parse `timeline.max_entries` / `timeline.max_bytes` into `KorvidConfig`.
- `src/korvid/core/watch.py` — post-store-accept delta callback (`on_event`) with callback-failure isolation.
- `src/korvid/k8s/client.py` — live Warning-event watch seeded from a list resourceVersion without backfilling old events.
- `src/korvid/ui/app.py` — timeline producer hooks, Warning-event worker group, `T` binding, modal open/close, and goto reuse.
- `src/korvid/ui/widgets/help_screen.py` — classify the new `timeline` binding under Table help.
- `src/korvid/__main__.py` — construct `SessionTimeline` from config and wire `watch_warning_events` into `KorvidApp`.
- `tests/core/test_config.py`
- `tests/core/test_watch.py`
- `tests/k8s/test_client.py`
- `tests/ui/test_app.py` — extend `make_app` with `session_timeline=` and `watch_warning_events=` so new tests stay small.
- `tests/ui/test_ctx_switch.py` — extend `_CtxEnv` with timeline collaborators and assert start/completion/failure epochs.
- `tests/ui/test_write_ops.py` — assert write timeline intent/outcome sequencing and blocked-write non-recording.
- `tests/ui/test_help_screen.py`
- `tests/ui/test_keybindings.py`
- `tests/test_main_wiring.py` — `_FakeKubeForWiring.watch_warning_events`, plus captured kwargs assertions.
- `docs/keybindings.md`
- `docs/tui.md`
- `README.md`

### Required fake / wiring surfaces

- `tests/ui/test_app.py::make_app` must accept `session_timeline: SessionTimeline | None` and `watch_warning_events: Callable[[str | None], AsyncIterator[dict[str, Any]]] | None`.
- `tests/ui/test_ctx_switch.py::_CtxEnv` must forward those same collaborators when timeline-specific context-switch assertions are added.
- `tests/test_main_wiring.py::_FakeKubeForWiring` must grow `watch_warning_events(self, namespace: str | None) -> AsyncIterator[dict[str, Any]]` so `_wire_and_run` can hand the real bound method to the app.
- Because `WatchManager` gains a new optional callback attribute rather than a required constructor arg, the many existing `WatchManager` call sites in tests should not need bulk edits.

---

### Task 1: Core bounded timeline model and config

**Files:**
- Create: `src/korvid/core/session_timeline.py`
- Create: `tests/core/test_session_timeline.py`
- Modify: `src/korvid/core/config.py`
- Modify: `tests/core/test_config.py`

**Interfaces:**
- Produces:
  - `TimelineSource(StrEnum)` with values `watch`, `event`, `context`, `write`.
  - `TimelineResourceRef(kind_alias: str | None, display_kind: str, namespace: str, name: str, uid: str | None = None)`.
  - `WatchDeltaPayload(verb: Literal["ADDED", "MODIFIED", "DELETED"])`.
  - `WarningEventPayload(reason: str, note: str, count: int)`.
  - `ContextSwitchPayload(phase: Literal["started", "completed", "failed"], from_context: str | None, to_context: str | None, note: str = "")`.
  - `WriteAuditPayload(action: str, outcome: str)`.
  - `TimelineEntry(sequence: int, occurred_at: str, epoch: int, source: TimelineSource, resource: TimelineResourceRef | None, payload: WatchDeltaPayload | WarningEventPayload | ContextSwitchPayload | WriteAuditPayload)`.
  - `TimelineStats(entry_count: int, encoded_bytes: int, evicted: int, refused: int)`.
  - `TimelineSnapshot(entries: tuple[TimelineEntry, ...], stats: TimelineStats)`.
  - `AppendResult(accepted: bool, diagnostic: str | None, evicted: int)`.
  - `SessionTimeline(max_entries: int, max_bytes: int)`.
  - `SessionTimeline.append_watch(*, epoch: int, kind_alias: str, display_kind: str, namespace: str, name: str, uid: str | None, verb: Literal["ADDED", "MODIFIED", "DELETED"]) -> AppendResult`.
  - `SessionTimeline.append_warning_event(*, epoch: int, event: dict[str, Any], kind_alias: str | None) -> AppendResult`.
  - `SessionTimeline.append_context_switch(*, epoch: int, phase: Literal["started", "completed", "failed"], from_context: str | None, to_context: str | None, note: str = "") -> AppendResult`.
  - `SessionTimeline.append_write(*, epoch: int, action: str, kind_alias: str, display_kind: str, namespace: str | None, name: str, uid: str | None, outcome: str) -> AppendResult`.
  - `SessionTimeline.snapshot(*, epoch: int | None, source: TimelineSource | None, resource: TimelineResourceRef | None) -> TimelineSnapshot`.
- `TimelineResourceRef` matching rule: same `kind_alias`/`namespace`/`name` always required; `uid` must match only when **both** sides carry one, so same-name recreated objects stay distinguishable without hiding write entries that lack a UID.
- `KorvidConfig` gains `timeline_max_entries: int` and `timeline_max_bytes: int`.

- [ ] **Step 1: Write the failing core/config tests**

```python
# tests/core/test_session_timeline.py
from korvid.core.session_timeline import SessionTimeline, TimelineResourceRef, TimelineSource


def _warning(message: str, *, uid: str = "pod-uid") -> dict[str, object]:
    return {
        "type": "Warning",
        "reason": "BackOff",
        "message": message,
        "count": 4,
        "lastTimestamp": "2026-08-15T00:00:00Z",
        "involvedObject": {
            "apiVersion": "v1",
            "kind": "Pod",
            "namespace": "default",
            "name": "api-1",
            "uid": uid,
        },
    }


def test_append_evicts_oldest_entries_to_hold_count() -> None:
    timeline = SessionTimeline(max_entries=2, max_bytes=4096)
    assert timeline.append_watch(epoch=0, kind_alias="pods", display_kind="Pod", namespace="default", name="a", uid="uid-a", verb="ADDED").accepted is True
    assert timeline.append_watch(epoch=0, kind_alias="pods", display_kind="Pod", namespace="default", name="b", uid="uid-b", verb="MODIFIED").accepted is True
    result = timeline.append_write(epoch=0, action="delete", kind_alias="pods", display_kind="Pod", namespace="default", name="c", uid=None, outcome="success")
    snap = timeline.snapshot(epoch=None, source=None, resource=None)
    assert result.accepted is True
    assert result.evicted == 1
    assert [entry.resource.name for entry in snap.entries if entry.resource is not None] == ["b", "c"]
    assert snap.stats.entry_count == 2
    assert snap.stats.evicted == 1


def test_append_evicts_oldest_entries_to_hold_encoded_bytes() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=512)
    timeline.append_watch(epoch=0, kind_alias="pods", display_kind="Pod", namespace="default", name="a" * 100, uid="uid-a", verb="ADDED")
    result = timeline.append_watch(epoch=0, kind_alias="pods", display_kind="Pod", namespace="default", name="b" * 100, uid="uid-b", verb="MODIFIED")
    snap = timeline.snapshot(epoch=None, source=None, resource=None)
    assert result.accepted is True
    assert result.evicted > 0
    assert snap.stats.encoded_bytes <= 512


def test_oversized_entry_is_refused_without_mutating_existing_history() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=320)
    kept = timeline.append_watch(epoch=0, kind_alias="pods", display_kind="Pod", namespace="default", name="steady", uid="uid-steady", verb="ADDED")
    refused = timeline.append_warning_event(epoch=0, event=_warning("Authorization: Bearer secret-token" + "x" * 200), kind_alias="pods")
    snap = timeline.snapshot(epoch=None, source=None, resource=None)
    assert kept.accepted is True
    assert refused.accepted is False
    assert "too large" in str(refused.diagnostic)
    assert [entry.resource.name for entry in snap.entries if entry.resource is not None] == ["steady"]
    assert snap.stats.refused == 1


def test_snapshot_filters_source_epoch_and_recreated_resource_uid_deterministically() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(epoch=0, kind_alias="pods", display_kind="Pod", namespace="default", name="api", uid="old", verb="DELETED")
    timeline.append_watch(epoch=1, kind_alias="pods", display_kind="Pod", namespace="default", name="api", uid="new", verb="ADDED")
    timeline.append_context_switch(epoch=1, phase="completed", from_context="ctx-a", to_context="ctx-b", note="switched")
    resource = TimelineResourceRef(kind_alias="pods", display_kind="Pod", namespace="default", name="api", uid="new")
    snap = timeline.snapshot(epoch=1, source=TimelineSource.WATCH, resource=resource)
    assert [(entry.epoch, entry.resource.uid, entry.payload.verb) for entry in snap.entries] == [(1, "new", "ADDED")]


def test_warning_projection_stores_only_normalized_text() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)
    result = timeline.append_warning_event(
        epoch=0,
        event=_warning("Authorization: Bearer secret-token\nready=false " + "x" * 400),
        kind_alias="pods",
    )
    entry = timeline.snapshot(epoch=None, source=TimelineSource.EVENT, resource=None).entries[0]
    assert result.accepted is True
    assert entry.payload.reason == "BackOff"
    assert "secret-token" not in entry.payload.note
    assert "••••••" in entry.payload.note
    assert "\n" not in entry.payload.note


def test_warning_projection_redacts_credentials_before_storage() -> None:
    timeline = SessionTimeline(max_entries=4, max_bytes=4096)
    result = timeline.append_warning_event(
        epoch=0,
        event=_warning("Authorization: secret-token\nBack-off"),
        kind_alias="pods",
    )
    entry = timeline.snapshot(epoch=None, source=TimelineSource.EVENT, resource=None).entries[0]
    assert result.accepted is True
    assert "secret-token" not in entry.payload.note
    assert "••••••" in entry.payload.note
    assert "\n" not in entry.payload.note
```

```python
# tests/core/test_config.py
from korvid.core.config import KorvidConfig, load_config


def test_timeline_config_defaults(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")
    cfg = load_config(cfg_file)
    assert cfg.timeline_max_entries == 500
    assert cfg.timeline_max_bytes == 262144


def test_timeline_config_parses_nested_values(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("timeline:\n  max_entries: 32\n  max_bytes: 8192\n")
    cfg = load_config(cfg_file)
    assert cfg.timeline_max_entries == 32
    assert cfg.timeline_max_bytes == 8192


def test_timeline_config_invalid_values_warn_and_fallback(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("timeline:\n  max_entries: 0\n  max_bytes: nope\n")
    cfg = load_config(cfg_file)
    assert cfg.timeline_max_entries == 500
    assert cfg.timeline_max_bytes == 262144
    assert any("timeline.max_entries" in warning for warning in cfg.warnings)
    assert any("timeline.max_bytes" in warning for warning in cfg.warnings)
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/core/test_session_timeline.py tests/core/test_config.py -q
```

Expected:
- `ModuleNotFoundError: No module named 'korvid.core.session_timeline'`
- and/or `AttributeError: 'KorvidConfig' object has no attribute 'timeline_max_entries'`

- [ ] **Step 3: Add the core model and config parsing**

```python
# src/korvid/core/session_timeline.py
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from korvid.core.redaction import RedactionRecord, redact_text

class TimelineSource(StrEnum):
    WATCH = "watch"
    EVENT = "event"
    CONTEXT = "context"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class TimelineResourceRef:
    kind_alias: str | None
    display_kind: str
    namespace: str
    name: str
    uid: str | None = None

    def matches(self, other: "TimelineResourceRef") -> bool:
        if (self.kind_alias, self.namespace, self.name) != (
            other.kind_alias,
            other.namespace,
            other.name,
        ):
            return False
        if self.uid and other.uid:
            return self.uid == other.uid
        return True


@dataclass(frozen=True, slots=True)
class WatchDeltaPayload:
    verb: Literal["ADDED", "MODIFIED", "DELETED"]


@dataclass(frozen=True, slots=True)
class WarningEventPayload:
    reason: str
    note: str
    count: int


@dataclass(frozen=True, slots=True)
class ContextSwitchPayload:
    phase: Literal["started", "completed", "failed"]
    from_context: str | None
    to_context: str | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class WriteAuditPayload:
    action: str
    outcome: str


TimelinePayload = WatchDeltaPayload | WarningEventPayload | ContextSwitchPayload | WriteAuditPayload


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    sequence: int
    occurred_at: str
    epoch: int
    source: TimelineSource
    resource: TimelineResourceRef | None
    payload: TimelinePayload


@dataclass(frozen=True, slots=True)
class TimelineStats:
    entry_count: int
    encoded_bytes: int
    evicted: int
    refused: int


@dataclass(frozen=True, slots=True)
class TimelineSnapshot:
    entries: tuple[TimelineEntry, ...]
    stats: TimelineStats


@dataclass(frozen=True, slots=True)
class AppendResult:
    accepted: bool
    diagnostic: str | None
    evicted: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _redacted_text(raw: object, path: str) -> str:
    records: list[RedactionRecord] = []
    return redact_text(str(raw or "").strip(), path, records)


class SessionTimeline:
    def __init__(self, max_entries: int, max_bytes: int) -> None:
        if isinstance(max_entries, bool) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        if isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._entries: deque[tuple[TimelineEntry, int]] = deque()
        self._next_sequence = 1
        self._encoded_bytes = 0
        self._evicted = 0
        self._refused = 0

    def append_watch(self, *, epoch: int, kind_alias: str, display_kind: str, namespace: str, name: str, uid: str | None, verb: Literal["ADDED", "MODIFIED", "DELETED"]) -> AppendResult:
        resource = TimelineResourceRef(kind_alias=kind_alias, display_kind=display_kind, namespace=namespace, name=name, uid=uid)
        return self._append(epoch=epoch, source=TimelineSource.WATCH, resource=resource, payload=WatchDeltaPayload(verb=verb))

    def append_warning_event(self, *, epoch: int, event: dict[str, Any], kind_alias: str | None) -> AppendResult:
        involved_value = event.get("involvedObject")
        involved = involved_value if isinstance(involved_value, dict) else {}
        metadata_value = event.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, dict) else {}
        count_value = event.get("count")
        count = (
            count_value
            if isinstance(count_value, int) and not isinstance(count_value, bool) and count_value > 0
            else 1
        )
        resource = TimelineResourceRef(
            kind_alias=kind_alias,
            display_kind=str(involved.get("kind") or "Event"),
            namespace=str(involved.get("namespace") or ""),
            name=str(involved.get("name") or ""),
            uid=str(involved.get("uid") or "") or None,
        )
        payload = WarningEventPayload(
            reason=_redacted_text(event.get("reason") or "Warning", "timeline.event.reason"),
            note=_redacted_text(event.get("message") or "", "timeline.event.message"),
            count=count,
        )
        occurred_at = str(event.get("lastTimestamp") or event.get("eventTime") or event.get("firstTimestamp") or metadata.get("creationTimestamp") or _utc_now())
        return self._append(epoch=epoch, source=TimelineSource.EVENT, resource=resource, payload=payload, occurred_at=occurred_at)

    def append_context_switch(self, *, epoch: int, phase: Literal["started", "completed", "failed"], from_context: str | None, to_context: str | None, note: str = "") -> AppendResult:
        return self._append(epoch=epoch, source=TimelineSource.CONTEXT, resource=None, payload=ContextSwitchPayload(phase=phase, from_context=from_context, to_context=to_context, note=note[:160]))

    def append_write(self, *, epoch: int, action: str, kind_alias: str, display_kind: str, namespace: str | None, name: str, uid: str | None, outcome: str) -> AppendResult:
        resource = TimelineResourceRef(kind_alias=kind_alias, display_kind=display_kind, namespace=namespace or "", name=name, uid=uid)
        return self._append(epoch=epoch, source=TimelineSource.WRITE, resource=resource, payload=WriteAuditPayload(action=action, outcome=outcome[:160]))

    def snapshot(self, *, epoch: int | None, source: TimelineSource | None, resource: TimelineResourceRef | None) -> TimelineSnapshot:
        entries = tuple(
            entry
            for entry, _size in self._entries
            if (epoch is None or entry.epoch == epoch)
            and (source is None or entry.source is source)
            and (resource is None or (entry.resource is not None and entry.resource.matches(resource)))
        )
        return TimelineSnapshot(
            entries=entries,
            stats=TimelineStats(
                entry_count=len(self._entries),
                encoded_bytes=self._encoded_bytes,
                evicted=self._evicted,
                refused=self._refused,
            ),
        )

    def _append(self, *, epoch: int, source: TimelineSource, resource: TimelineResourceRef | None, payload: TimelinePayload, occurred_at: str | None = None) -> AppendResult:
        entry = TimelineEntry(sequence=self._next_sequence, occurred_at=occurred_at or _utc_now(), epoch=epoch, source=source, resource=resource, payload=payload)
        encoded = json.dumps(self._entry_dict(entry), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > self._max_bytes:
            self._refused += 1
            return AppendResult(False, f"timeline entry too large ({len(encoded)} > {self._max_bytes} bytes)", 0)
        self._next_sequence += 1
        self._entries.append((entry, len(encoded)))
        self._encoded_bytes += len(encoded)
        evicted = 0
        while len(self._entries) > self._max_entries or self._encoded_bytes > self._max_bytes:
            _old, size = self._entries.popleft()
            self._encoded_bytes -= size
            self._evicted += 1
            evicted += 1
        return AppendResult(True, None, evicted)

    @staticmethod
    def _entry_dict(entry: TimelineEntry) -> dict[str, Any]:
        return {
            "sequence": entry.sequence,
            "occurred_at": entry.occurred_at,
            "epoch": entry.epoch,
            "source": entry.source.value,
            "resource": None if entry.resource is None else asdict(entry.resource),
            "payload": asdict(entry.payload),
        }
```

```python
# src/korvid/core/config.py -- add these two fields to KorvidConfig
timeline_max_entries: int = 500
timeline_max_bytes: int = 262144


# Rename the existing `_observability_int` helper to `_mapping_positive_int`
# and update its observability call sites; timeline uses the same validated
# mapping/key/default contract instead of introducing a duplicate parser.
def _mapping_positive_int(
    raw: Mapping[str, Any],
    key: str,
    default: int,
    label: str,
    warnings: list[str],
) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        warnings.append(f"{label}.{key}: must be a positive integer — using the default {default}")
        return default
    return value


# src/korvid/core/config.py -- add before the KorvidConfig constructor call
timeline_raw = raw.get("timeline") if isinstance(raw.get("timeline"), dict) else {}

# src/korvid/core/config.py -- add inside the existing KorvidConfig constructor call
timeline_max_entries=_mapping_positive_int(timeline_raw, "max_entries", 500, "timeline", warnings),
timeline_max_bytes=_mapping_positive_int(timeline_raw, "max_bytes", 262144, "timeline", warnings),
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/core/test_session_timeline.py tests/core/test_config.py -q
uv run ruff check --fix src/korvid/core/session_timeline.py src/korvid/core/config.py tests/core/test_session_timeline.py tests/core/test_config.py
uv run ruff format src/korvid/core/session_timeline.py src/korvid/core/config.py tests/core/test_session_timeline.py tests/core/test_config.py
uv run mypy src/korvid/core/session_timeline.py src/korvid/core/config.py
```

Expected:
- `pytest`: PASS
- `ruff check --fix`: `All checks passed!`
- `ruff format`: `4 files left unchanged` (or equivalent)
- `mypy`: `Success: no issues found`

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/session_timeline.py src/korvid/core/config.py tests/core/test_session_timeline.py tests/core/test_config.py
git commit -m "feat: add bounded session timeline core" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 2: Watch delta emission and Warning-event watch source

**Files:**
- Modify: `src/korvid/core/watch.py`
- Modify: `src/korvid/k8s/client.py`
- Modify: `tests/core/test_watch.py`
- Modify: `tests/k8s/test_client.py`

**Interfaces:**
- Consumes:
  - `SessionTimeline.append_watch(*, epoch: int, kind_alias: str, display_kind: str, namespace: str, name: str, uid: str | None, verb: Literal["ADDED", "MODIFIED", "DELETED"])`
  - `SessionTimeline.append_warning_event(*, epoch: int, event: dict[str, Any], kind_alias: str | None)`
- Produces:
  - `WatchManager.on_event: Callable[[str, str, str, Summary], None] | None`.
  - `KubeClient.watch_warning_events(namespace: str | None) -> AsyncIterator[dict[str, Any]]`.
- `WatchManager.on_event` fires only **after** `ResourceStore.apply_event` returns.
- Callback exceptions are logged and swallowed exactly like store subscriber failures.
- `watch_warning_events` must issue an initial LIST only to capture a resourceVersion, then yield **live** Warning Event raw objects from the watch stream; it must not backfill pre-session Warning history into the timeline.

- [ ] **Step 1: Add the failing watch/client tests**

```python
# tests/core/test_watch.py
async def test_on_event_runs_after_store_accepts_delta() -> None:
    store = ResourceStore()
    seen: list[tuple[str, list[str]]] = []
    mgr = WatchManager(store, make_source([("ADDED", _pod("api-1"))]))
    mgr.on_event = lambda kind, scope, event_type, obj: seen.append(
        (event_type, [pod.name for pod in store.get(kind, scope)])
    )
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert seen == [("ADDED", ["api-1"])]
    await mgr.stop_all()


async def test_on_event_failure_does_not_kill_watch() -> None:
    store = ResourceStore()
    mgr = WatchManager(store, make_source([("ADDED", _pod("api-1"))]))
    calls = 0

    def boom(kind: str, scope: str, event_type: str, obj: Summary) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("timeline sink exploded")

    mgr.on_event = boom
    await mgr.start("pods", "default")
    await asyncio.sleep(0.05)
    assert calls == 1
    assert mgr.active == {("pods", "default")}
    assert [pod.name for pod in store.get("pods", "default")] == ["api-1"]
    await mgr.stop_all()
```

```python
# tests/k8s/test_client.py
async def test_watch_warning_events_uses_warning_selector_without_backfill() -> None:
    client = KubeClient()
    fake_watch = _FakeWatch([
        {"type": "ADDED", "raw_object": {"type": "Warning", "message": "new warning", "reason": "BackOff"}},
    ])
    mock_api = MagicMock()
    request_json = AsyncMock(
        return_value={
            "metadata": {"resourceVersion": "77"},
            "items": [{"type": "Warning", "message": "old warning", "reason": "BackOff"}],
        }
    )

    async def _capture_watch_call(*args: Any, **kwargs: Any) -> Any:
        return _raw_response(200, "OK", b"")

    mock_api.call_api = _capture_watch_call

    with (
        patch.object(client, "_api", mock_api),
        patch.object(client, "_request_json", request_json),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        seen = [event async for event in client.watch_warning_events(None)]

    assert [event["message"] for event in seen] == ["new warning"]
    request_json.assert_awaited_once_with("/api/v1/events", query_params=[("fieldSelector", "type=Warning")])
    assert fake_watch.captured_kwargs["resource_version"] == "77"


async def test_watch_warning_events_namespaced_path_and_selector() -> None:
    client = KubeClient()
    fake_watch = _FakeWatch([])
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value={"metadata": {"resourceVersion": "1"}, "items": []})),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_warning_events("team-a"):
            pass
    assert fake_watch.captured_kwargs["resource_version"] == "1"
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/core/test_watch.py tests/k8s/test_client.py -q
```

Expected:
- `AttributeError: 'WatchManager' object has no attribute 'on_event'`
- and/or `AttributeError: 'KubeClient' object has no attribute 'watch_warning_events'`

- [ ] **Step 3: Add the watch callback and Warning-event watch source**

```python
# src/korvid/core/watch.py -- add this attribute inside WatchManager.__init__
self.on_event: Callable[[str, str, str, Summary], None] | None = None

# src/korvid/core/watch.py -- add this call inside _watch_loop(), immediately
# after self._store.apply_event
self._emit_event(kind, scope, event_type, obj)

# src/korvid/core/watch.py -- new helper method
    def _emit_event(self, kind: str, scope: str, event_type: str, obj: Summary) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, scope, event_type, obj)
        except Exception:
            logger.exception("watch event sink failed")
```

```python
# src/korvid/k8s/client.py
async def watch_warning_events(self, namespace: str | None) -> AsyncIterator[dict[str, Any]]:
    if self._api is None:
        raise RuntimeError("connect() first")
    path = "/api/v1/events" if namespace is None else f"/api/v1/namespaces/{quote(namespace, safe='')}/events"
    query = [("fieldSelector", "type=Warning")]
    data = await self._request_json(path, query_params=query)
    resource_version = str((data.get("metadata") or {}).get("resourceVersion") or "") or None
    watch_func = self._make_raw_watch_callable(path, extra_query=query)
    watch_kwargs: dict[str, Any] = {}
    if resource_version is not None:
        watch_kwargs["resource_version"] = resource_version
    w = k8s_watch.Watch()
    self._observe_read("watch_open", path)
    try:
        async with w.stream(watch_func, **watch_kwargs) as stream:
            async for event in stream:
                raw_object = event["raw_object"]
                self._observe_read("watch_event", path, payload=raw_object, object_count=1)
                yield raw_object
    except (k8s_client.exceptions.ApiException, ApiStatusError) as exc:
        self._observe_read_error(path, exc)
        if isinstance(exc, ApiStatusError):
            raise
        raise ApiStatusError(int(exc.status or 0), str(exc.reason or "")) from exc
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/core/test_watch.py tests/k8s/test_client.py -q
uv run ruff check --fix src/korvid/core/watch.py src/korvid/k8s/client.py tests/core/test_watch.py tests/k8s/test_client.py
uv run ruff format src/korvid/core/watch.py src/korvid/k8s/client.py tests/core/test_watch.py tests/k8s/test_client.py
uv run mypy src/korvid/core/watch.py src/korvid/k8s/client.py
```

Expected:
- `pytest`: PASS
- `ruff check --fix`: `All checks passed!`
- `ruff format`: unchanged or reformatted only the touched files
- `mypy`: `Success: no issues found`

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/watch.py src/korvid/k8s/client.py tests/core/test_watch.py tests/k8s/test_client.py
git commit -m "feat: add timeline watch producers" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 3: App and composition-root timeline producers

**Files:**
- Modify: `src/korvid/ui/app.py`
- Modify: `src/korvid/__main__.py`
- Modify: `tests/ui/test_app.py`
- Modify: `tests/ui/test_ctx_switch.py`
- Modify: `tests/ui/test_write_ops.py`
- Modify: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes:
  - `SessionTimeline.append_watch(*, epoch: int, kind_alias: str, display_kind: str, namespace: str, name: str, uid: str | None, verb: Literal["ADDED", "MODIFIED", "DELETED"])`
  - `SessionTimeline.append_warning_event(*, epoch: int, event: dict[str, Any], kind_alias: str | None)`
  - `SessionTimeline.append_context_switch(*, epoch: int, phase: Literal["started", "completed", "failed"], from_context: str | None, to_context: str | None, note: str = "")`
  - `SessionTimeline.append_write(*, epoch: int, action: str, kind_alias: str, display_kind: str, namespace: str | None, name: str, uid: str | None, outcome: str)`
  - `KubeClient.watch_warning_events(namespace: str | None) -> AsyncIterator[dict[str, Any]]`
- Produces:
  - `KorvidApp.__init__` gains trailing keyword parameters `session_timeline: SessionTimeline | None = None` and `watch_warning_events: Callable[[str | None], AsyncIterator[dict[str, Any]]] | None = None`.
  - `KorvidApp._record_timeline_result(label: str, result: AppendResult | None) -> None`.
  - `KorvidApp._record_timeline_watch_event(kind: str, scope: str, event_type: str, obj: Summary) -> None`.
  - `KorvidApp._run_timeline_warning_watch() -> Coroutine[Any, Any, None]`.
  - `KorvidApp` worker group constant for Warning-event watching (for example `_TIMELINE_EVENT_GROUP = "timeline-warning-events"`).
- `on_mount` owns the live wiring: set `watch_manager.on_event`, start the Warning-event worker if both timeline and callback exist, and keep the feature inert otherwise.
- `_audit_write` is the single post-durable append chokepoint for write-gate and drain-controller records; it must append the write timeline entry only after `audit.append` returns.
- Context-switch timeline rules:
  - `started` records on the current epoch after guards pass and before the probe.
  - `failed` records on the current epoch when no new cluster was applied.
  - `completed` records after `_ctx_epoch += 1`, so the new epoch owns the successful switch entry.

- [ ] **Step 1: Add the failing app/wiring tests**

```python
# tests/ui/test_ctx_switch.py
async def test_successful_switch_records_started_then_completed_in_distinct_epochs() -> None:
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
    env = _CtxEnv(timeline=timeline)
    async with env.app.run_test() as pilot:
        env.app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: env.app.config.kube_context == "ctx-b", label="switched")
    entries = timeline.snapshot(epoch=None, source=TimelineSource.CONTEXT, resource=None).entries
    assert [(entry.epoch, entry.payload.phase) for entry in entries] == [(0, "started"), (1, "completed")]


async def test_probe_failure_records_context_failure_without_bumping_epoch() -> None:
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
    env = _CtxEnv(probe_error=RuntimeError("Unauthorized"), timeline=timeline)
    async with env.app.run_test() as pilot:
        env.app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: any("Unauthorized" in n.message for n in env.app._notifications), label="probe failure")
    entries = timeline.snapshot(epoch=None, source=TimelineSource.CONTEXT, resource=None).entries
    assert [(entry.epoch, entry.payload.phase) for entry in entries] == [(0, "started"), (0, "failed")]


async def test_mid_swap_failure_records_context_failure_before_restore() -> None:
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
    env = _CtxEnv(switch_error=RuntimeError("socket closed"), timeline=timeline)
    async with env.app.run_test() as pilot:
        env.app.post_message(SwitchContextCommand("ctx-b"))
        await until(pilot, lambda: any("Restored context" in n.message for n in env.app._notifications), label="restore notification")
    entries = timeline.snapshot(epoch=None, source=TimelineSource.CONTEXT, resource=None).entries
    assert [(entry.epoch, entry.payload.phase) for entry in entries] == [(0, "started"), (0, "failed")]
```

```python
# tests/ui/test_write_ops.py
async def test_run_write_records_timeline_after_intent_and_success_audit(tmp_path: Path) -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    app = make_app([_pod("web-1")], audit=AuditLog(tmp_path / "audit.jsonl"), session_timeline=timeline)

    async def op() -> None:
        return None

    result = await app._run_write("delete", _PODS_META, "default", "web-1", op)
    entries = timeline.snapshot(epoch=0, source=TimelineSource.WRITE, resource=None).entries
    assert result == "done"
    assert [(entry.payload.action, entry.payload.outcome) for entry in entries] == [
        ("delete", "intent"),
        ("delete", "success"),
    ]


async def test_blocked_intent_does_not_record_write_timeline(tmp_path: Path) -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()
    app = make_app([_pod("web-1")], audit=AuditLog(audit_path), session_timeline=timeline)

    async def op() -> None:
        raise AssertionError("must not run")

    result = await app._run_write("delete", _PODS_META, "default", "web-1", op)
    assert "blocked" in result
    assert timeline.snapshot(epoch=None, source=TimelineSource.WRITE, resource=None).entries == ()
```

```python
# tests/ui/test_app.py
async def test_resource_watch_records_post_store_delta() -> None:
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
    app = make_app([_pod("web-1")], session_timeline=timeline)
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: bool(timeline.snapshot(epoch=0, source=TimelineSource.WATCH, resource=None).entries),
            label="watch delta recorded",
        )
        entry = timeline.snapshot(epoch=0, source=TimelineSource.WATCH, resource=None).entries[0]
        assert entry.resource is not None
        assert (entry.resource.kind_alias, entry.resource.name, entry.payload.verb) == (
            "pods",
            "web-1",
            "ADDED",
        )
        assert [pod.name for pod in app.store.get("pods", app.current_scope)] == ["web-1"]


async def test_warning_watch_redacts_before_timeline_storage() -> None:
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
    hold = asyncio.Event()

    async def warnings(_namespace: str | None) -> AsyncIterator[dict[str, Any]]:
        yield _warning_event("Authorization: secret-token\nBack-off")
        await hold.wait()

    app = make_app(
        [_pod("web-1")],
        session_timeline=timeline,
        watch_warning_events=warnings,
    )
    async with app.run_test() as pilot:
        await until(
            pilot,
            lambda: bool(timeline.snapshot(epoch=0, source=TimelineSource.EVENT, resource=None).entries),
            label="Warning event recorded",
        )
        entry = timeline.snapshot(epoch=0, source=TimelineSource.EVENT, resource=None).entries[0]
        assert "secret-token" not in entry.payload.note
        assert "••••••" in entry.payload.note
        assert "\n" not in entry.payload.note
```

```python
# tests/test_main_wiring.py
async def test_wire_and_run_passes_session_timeline_and_warning_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    import korvid.__main__ as main_mod

    monkeypatch.setattr(main_mod, "KorvidApp", _FakeAppCapturesKwargs)
    _FakeAppCapturesKwargs.instances.clear()
    kube = _FakeKubeForWiring()
    state = main_mod._RunState()
    await main_mod._wire_and_run(KorvidConfig(readonly=True), cast("Any", kube), state)
    if state.discovery_box:
        await state.discovery_box[0]
    captured = _FakeAppCapturesKwargs.instances[0].captured
    assert isinstance(captured["session_timeline"], SessionTimeline)
    assert captured["watch_warning_events"] is kube.watch_warning_events
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_app.py tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py tests/test_main_wiring.py -q
```

Expected:
- `TypeError: KorvidApp.__init__() got an unexpected keyword argument 'session_timeline'`
- and/or missing timeline entries / missing `watch_warning_events` wiring assertions

- [ ] **Step 3: Wire the app, write path, and composition root**

```python
# src/korvid/ui/app.py
_TIMELINE_EVENT_GROUP = "timeline-warning-events"

# src/korvid/ui/app.py -- append these keyword params to KorvidApp.__init__
session_timeline: SessionTimeline | None = None,
watch_warning_events: Callable[[str | None], AsyncIterator[dict[str, Any]]] | None = None,

# src/korvid/ui/app.py -- store them in __init__
self._session_timeline = session_timeline
self._watch_warning_events = watch_warning_events

# src/korvid/ui/app.py -- add inside on_mount(), after self.watch_manager.on_error = _on_watch_error
self.watch_manager.on_event = self._record_timeline_watch_event
if self._session_timeline is not None and self._watch_warning_events is not None:
    self.run_worker(
        self._run_timeline_warning_watch(),
        exclusive=False,
        group=_TIMELINE_EVENT_GROUP,
        exit_on_error=False,
    )

    def _record_timeline_result(self, label: str, result: AppendResult | None) -> None:
        if result is not None and not result.accepted and result.diagnostic is not None:
            self.notify(
                f"Timeline skipped {label}: {result.diagnostic}",
                severity="warning",
                markup=False,
            )

    def _record_timeline_watch_event(self, kind: str, scope: str, event_type: str, obj: Summary) -> None:
        if self._session_timeline is None:
            return
        result = self._session_timeline.append_watch(
            epoch=self._ctx_epoch,
            kind_alias=kind,
            display_kind=getattr(obj, "kind", kind),
            namespace=str(getattr(obj, "namespace", "") or ""),
            name=str(getattr(obj, "name", "")),
            uid=str(getattr(obj, "uid", "") or "") or None,
            verb=cast("Literal['ADDED', 'MODIFIED', 'DELETED']", event_type),
        )
        if not result.accepted and result.diagnostic is not None:
            self.notify(f"Timeline skipped watch entry: {result.diagnostic}", severity="warning", markup=False)

    async def _run_timeline_warning_watch(self) -> None:
        watch = self._watch_warning_events
        timeline = self._session_timeline
        if watch is None or timeline is None:
            return
        epoch = self._ctx_epoch
        failures = 0
        while epoch == self._ctx_epoch:
            try:
                async for event in watch(None):
                    if epoch != self._ctx_epoch:
                        return
                    failures = 0
                    kind_alias = self._event_kind_alias(event)
                    self._record_timeline_result(
                        "Warning event",
                        timeline.append_warning_event(
                            epoch=epoch,
                            event=event,
                            kind_alias=kind_alias,
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except ApiStatusError as exc:
                if exc.status in {403, 405}:
                    self.notify(
                        explain_api_error(exc.status, exc.reason, "events", None),
                        severity="warning",
                    )
                    return
                failures += 1
            except Exception:
                logger.exception("Warning-event timeline feed failed")
                failures += 1
            if failures >= 5:
                self.notify(
                    "Warning-event timeline feed stopped after 5 failures",
                    severity="error",
                    markup=False,
                )
                return
            await asyncio.sleep(min(2 ** failures, 30))

    def _event_kind_alias(self, event: dict[str, Any]) -> str | None:
        involved = event.get("involvedObject") or {}
        api_version = str(involved.get("apiVersion") or "")
        kind = str(involved.get("kind") or "")
        group = api_version.rpartition("/")[0]
        for alias, meta in self.aliases.items():
            if self._canonical_kind(alias) != alias or meta.synthetic:
                continue
            if meta.kind == kind and meta.group == group:
                return alias
        return None

# src/korvid/ui/app.py -- add at the end of _audit_write(), immediately after
# the existing audit append call inside _audit_write, immediately after it completes
if self._session_timeline is not None:
    result = self._session_timeline.append_write(
        epoch=self._ctx_epoch,
        action=action,
        kind_alias=meta.plural,
        display_kind=meta.kind,
        namespace=namespace,
        name=name,
        uid=None,
        outcome=outcome,
    )
    if not result.accepted and result.diagnostic is not None:
        self.notify(f"Timeline skipped write entry: {result.diagnostic}", severity="warning", markup=False)

# src/korvid/ui/app.py -- add in _switch_context_locked() after
# _ctx_switch_guards_pass(name) returns true and before await self._probe_context(name)
if self._session_timeline is not None:
    self._record_timeline_result(
        "context switch",
        self._session_timeline.append_context_switch(
            epoch=self._ctx_epoch,
            phase="started",
            from_context=old,
            to_context=name,
        ),
    )

# src/korvid/ui/app.py -- add in the probe failure path, before the existing failure notify
if self._session_timeline is not None:
    self._record_timeline_result(
        "context switch",
        self._session_timeline.append_context_switch(
            epoch=self._ctx_epoch,
            phase="failed",
            from_context=old,
            to_context=name,
            note=self._describe_ctx_error(exc),
        ),
    )

# src/korvid/ui/app.py -- add in the first exception path of _retarget_context(),
# before the existing mid-swap failure notify
if self._session_timeline is not None:
    self._record_timeline_result(
        "context switch",
        self._session_timeline.append_context_switch(
            epoch=self._ctx_epoch,
            phase="failed",
            from_context=old,
            to_context=name,
            note=self._describe_ctx_error(exc),
        ),
    )

# src/korvid/ui/app.py -- in _switch_context_locked(), after _retarget_context
# returns and only when applied == name. _apply_context_switch is also called
# while restoring the old context after a failed target switch, so recording
# completion there would falsely report the failed target as completed.
if applied == name and self._session_timeline is not None:
    self._record_timeline_result(
        "context switch",
        self._session_timeline.append_context_switch(
            epoch=self._ctx_epoch,
            phase="completed",
            from_context=old,
            to_context=name,
            note="all cluster state was reset",
        ),
    )
if self._session_timeline is not None and self._watch_warning_events is not None:
    self.run_worker(
        self._run_timeline_warning_watch(),
        exclusive=False,
        group=_TIMELINE_EVENT_GROUP,
        exit_on_error=False,
    )

# src/korvid/ui/app.py -- add at the end of _teardown_for_context_switch()
for worker in self.workers.cancel_group(self, _TIMELINE_EVENT_GROUP):
    with contextlib.suppress(WorkerError):
        await worker.wait()

# src/korvid/ui/app.py -- add to the top of on_worker_state_changed(), before
# the existing relationship-worker branch
if event.worker.node is not self:
    return
if event.worker.group == _TIMELINE_EVENT_GROUP and event.state is WorkerState.ERROR:
    error = event.worker.error
    detail = f"{type(error).__name__}: {error}" if error is not None else "unknown error"
    self.notify(
        f"Warning-event timeline feed failed - {detail[:200]}",
        severity="error",
        timeout=10,
        markup=False,
    )
    return
```

```python
# src/korvid/__main__.py
session_timeline = SessionTimeline(config.timeline_max_entries, config.timeline_max_bytes)
app = KorvidApp(
    session_timeline=session_timeline,
    watch_warning_events=kube.watch_warning_events,
)
```

```python
# tests/ui/test_app.py
def make_app(
    pods: list[PodSummary],
    namespaces: list[str] | None = None,
    *,
    extra_data: dict[str, list[Summary]] | None = None,
    aliases: dict[str, ResourceMeta] | None = None,
    audit: AuditLog | None = None,
    provider_hint: str | None = None,
    config: KorvidConfig | None = None,
    open_pod_exec: Any | None = None,
    get_manifest: Any | None = None,
    metrics: Any | None = None,
    session_timeline: SessionTimeline | None = None,
    watch_warning_events: Any | None = None,
) -> KorvidApp:
    return KorvidApp(
        session_timeline=session_timeline,
        watch_warning_events=watch_warning_events,
    )
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py tests/test_main_wiring.py -q
uv run ruff check --fix src/korvid/ui/app.py src/korvid/__main__.py tests/ui/test_app.py tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py tests/test_main_wiring.py
uv run ruff format src/korvid/ui/app.py src/korvid/__main__.py tests/ui/test_app.py tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py tests/test_main_wiring.py
uv run mypy src/korvid/ui/app.py src/korvid/__main__.py
uv run tach check
```

Expected:
- `pytest`: PASS
- `ruff check --fix`: `All checks passed!`
- `ruff format`: unchanged or only touched-file formatting changes
- `mypy`: `Success: no issues found`
- `tach check`: PASS

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/app.py src/korvid/__main__.py tests/ui/test_app.py tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py tests/test_main_wiring.py
git commit -m "feat: wire session timeline producers" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

### Task 4: Timeline screen, navigation, keybinding, and docs

**Files:**
- Create: `src/korvid/ui/widgets/session_timeline_screen.py`
- Create: `tests/ui/test_session_timeline_screen.py`
- Create: `tests/ui/test_session_timeline_flow.py`
- Modify: `src/korvid/ui/app.py`
- Modify: `src/korvid/ui/widgets/help_screen.py`
- Modify: `tests/ui/test_help_screen.py`
- Modify: `tests/ui/test_keybindings.py`
- Modify: `docs/keybindings.md`
- Modify: `docs/tui.md`
- Modify: `README.md`

**Interfaces:**
- Consumes:
  - `SessionTimeline.snapshot(*, epoch: int | None, source: TimelineSource | None, resource: TimelineResourceRef | None) -> TimelineSnapshot`
  - `TimelineResourceRef.matches(other: TimelineResourceRef) -> bool`
  - `KorvidApp._jump_to_object(kind: str, namespace: str, name: str, *, epoch: int | None = None) -> None`
- Produces:
  - `TimelineGotoResult = tuple[str, str, str, str]` with `("goto", kind_alias, namespace, name)`.
  - `SessionTimelineScreen(timeline: SessionTimeline, *, current_epoch: int, resource_toggle: TimelineResourceRef | None)`.
  - `KorvidApp.action_timeline(self) -> None`.
  - `Binding("T", "timeline", "Timeline", id="timeline")`.
- Screen behavior:
  - Default filter: `epoch=current_epoch`, `source=all`, `resource=all`.
  - `e` toggles current epoch vs all epochs.
  - `s` cycles `all -> watch -> event -> context -> write -> all`.
  - `r` toggles `all resources` vs the selected table resource captured when the modal opened.
  - `Enter` dismisses with `TimelineGotoResult` only when the selected row has a navigable `kind_alias`; otherwise it is inert and updates the status line.
  - The header/banner must show `stats.entry_count`, `stats.encoded_bytes`, and non-zero `evicted` / `refused` counters so caps are never silent.

- [ ] **Step 1: Add the failing screen/help/keybinding/docs tests**

```python
# tests/ui/test_session_timeline_screen.py
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Static

from korvid.core.session_timeline import SessionTimeline, TimelineResourceRef
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen, TimelineGotoResult

class _ScreenHarness(App[None]):
    last_result: ClassVar[TimelineGotoResult | None] = None

    def __init__(self, screen: SessionTimelineScreen) -> None:
        super().__init__()
        self._screen = screen

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_mount(self) -> None:
        type(self).last_result = None
        self.push_screen(self._screen, lambda result: setattr(type(self), "last_result", result))


async def test_screen_defaults_to_current_epoch_and_cycles_filters() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_watch(epoch=0, kind_alias="pods", display_kind="Pod", namespace="default", name="old", uid="uid-old", verb="ADDED")
    timeline.append_watch(epoch=1, kind_alias="pods", display_kind="Pod", namespace="default", name="new", uid="uid-new", verb="ADDED")
    timeline.append_context_switch(epoch=1, phase="completed", from_context="ctx-a", to_context="ctx-b")
    screen = SessionTimelineScreen(timeline, current_epoch=1, resource_toggle=TimelineResourceRef("pods", "Pod", "default", "new", "uid-new"))
    async with _ScreenHarness(screen).run_test() as pilot:
        table = screen.query_one(DataTable)
        await until(pilot, lambda: table.row_count == 2, label="current epoch rows")
        await pilot.press("s")
        await until(pilot, lambda: table.row_count == 1, label="watch-only rows")
        await pilot.press("e")
        await until(pilot, lambda: table.row_count == 2, label="all-epoch watch rows")
        assert "evicted=0" in screen.query_one("#timeline-banner", Static).renderable.plain


async def test_enter_dismisses_goto_for_navigable_row() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    timeline.append_write(epoch=0, action="delete", kind_alias="deployments", display_kind="Deployment", namespace="default", name="api", uid=None, outcome="success")
    screen = SessionTimelineScreen(timeline, current_epoch=0, resource_toggle=None)
    async with _ScreenHarness(screen).run_test() as pilot:
        await until(pilot, lambda: screen.query_one(DataTable).row_count == 1, label="row visible")
        await pilot.press("enter")
        assert _ScreenHarness.last_result == ("goto", "deployments", "default", "api")
```

```python
# tests/ui/test_session_timeline_flow.py
from korvid.core.session_timeline import SessionTimeline
from korvid.ui.widgets.resource_table import ResourceTable
from korvid.ui.widgets.session_timeline_screen import SessionTimelineScreen

async def test_timeline_binding_opens_without_agent_and_enter_reuses_navigation() -> None:
    timeline = SessionTimeline(max_entries=16, max_bytes=8192)
    timeline.append_write(epoch=0, action="delete", kind_alias="deployments", display_kind="Deployment", namespace="default", name="api", uid=None, outcome="success")
    app = make_app([_pod("web")], extra_data={"deployments": [_deploy("api")]}, session_timeline=timeline)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pods visible")
        await pilot.press("T")
        await until(pilot, lambda: isinstance(app.screen, SessionTimelineScreen), label="timeline open")
        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "deployments", label="navigated to deployment view")
        table = app.query_one(ResourceTable)
        assert any("default/api" == str(row.key.value) for row in table.ordered_rows)
```

```python
# tests/ui/test_help_screen.py
def test_collect_help_groups_timeline_under_table() -> None:
    groups = dict(collect_help([Binding("T", "timeline", "Timeline")], []))
    assert ("T", "Timeline") in groups["Table"]
```

```python
# tests/ui/test_keybindings.py
async def test_timeline_binding_can_be_remapped() -> None:
    timeline = SessionTimeline(max_entries=8, max_bytes=4096)
    app = make_app([_pod("web")], config=_config({"timeline": "ctrl+g"}), session_timeline=timeline)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.query_one(ResourceTable).row_count == 1, label="pod loaded")
        await pilot.press("T")
        assert not isinstance(app.screen, SessionTimelineScreen)
        await pilot.press("ctrl+g")
        await until(pilot, lambda: isinstance(app.screen, SessionTimelineScreen), label="timeline opens on remap")
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_session_timeline_screen.py tests/ui/test_session_timeline_flow.py tests/ui/test_help_screen.py tests/ui/test_keybindings.py -q
```

Expected:
- `ModuleNotFoundError: No module named 'korvid.ui.widgets.session_timeline_screen'`
- and/or `AttributeError: 'KorvidApp' object has no action 'timeline'`
- and/or docs/keybinding assertions failing because `timeline` is missing from the action list

- [ ] **Step 3: Add the modal screen, binding, and docs**

```python
# src/korvid/ui/widgets/session_timeline_screen.py
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.session_timeline import (
    ContextSwitchPayload,
    SessionTimeline,
    TimelineEntry,
    TimelineResourceRef,
    TimelineSource,
    WarningEventPayload,
    WatchDeltaPayload,
    WriteAuditPayload,
)

TimelineGotoResult = tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class _RowTarget:
    kind_alias: str | None
    namespace: str
    name: str


class SessionTimelineScreen(ModalScreen[TimelineGotoResult | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
        Binding("e", "toggle_epoch", "Epoch", show=True),
        Binding("s", "cycle_source", "Source", show=True),
        Binding("r", "toggle_resource", "Resource", show=True),
    ]

    def __init__(self, timeline: SessionTimeline, *, current_epoch: int, resource_toggle: TimelineResourceRef | None) -> None:
        super().__init__()
        self._timeline = timeline
        self._current_epoch = current_epoch
        self._epoch_filter: int | None = current_epoch
        self._source_filter: TimelineSource | None = None
        self._resource_filter: TimelineResourceRef | None = None
        self._resource_toggle = resource_toggle
        self._targets: dict[str, _RowTarget] = {}

    def compose(self) -> ComposeResult:
        yield Footer()
        yield Static("Session timeline", id="timeline-title", markup=False)
        yield Static("", id="timeline-banner", markup=False)
        yield Static("", id="timeline-status", markup=False)
        yield DataTable[str | Text](id="timeline-table")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("SEQ", "TIME", "EPOCH", "SOURCE", "RESOURCE", "DETAIL")
        self._render()
        table.focus()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_toggle_epoch(self) -> None:
        self._epoch_filter = None if self._epoch_filter is not None else self._current_epoch
        self._render()

    def action_cycle_source(self) -> None:
        order = [None, TimelineSource.WATCH, TimelineSource.EVENT, TimelineSource.CONTEXT, TimelineSource.WRITE]
        self._source_filter = order[(order.index(self._source_filter) + 1) % len(order)]
        self._render()

    def action_toggle_resource(self) -> None:
        if self._resource_toggle is None:
            return
        self._resource_filter = None if self._resource_filter is not None else self._resource_toggle
        self._render()

    def _render(self) -> None:
        snap = self._timeline.snapshot(epoch=self._epoch_filter, source=self._source_filter, resource=self._resource_filter)
        table = self.query_one(DataTable)
        table.clear()
        self._targets = {}
        for entry in reversed(snap.entries):
            row_key = f"row-{entry.sequence}"
            resource_label = "-"
            if entry.resource is not None:
                resource_label = "/".join(part for part in (entry.resource.kind_alias or entry.resource.display_kind, entry.resource.namespace, entry.resource.name) if part)
                self._targets[row_key] = _RowTarget(entry.resource.kind_alias, entry.resource.namespace, entry.resource.name)
            table.add_row(
                str(entry.sequence),
                entry.occurred_at,
                str(entry.epoch),
                entry.source.value,
                Text(resource_label),
                Text(self._detail(entry)),
                key=row_key,
            )
        self.query_one("#timeline-banner", Static).update(
            f"stored={snap.stats.entry_count} entries · bytes={snap.stats.encoded_bytes} · evicted={snap.stats.evicted} · refused={snap.stats.refused}"
        )
        epoch_text = "current" if self._epoch_filter is not None else "all"
        source_text = self._source_filter.value if self._source_filter is not None else "all"
        resource_text = "selected" if self._resource_filter is not None else "all"
        self.query_one("#timeline-status", Static).update(
            f"Filters: epoch={epoch_text} · source={source_text} · resource={resource_text} · Enter: navigate"
        )

    def _detail(self, entry: TimelineEntry) -> str:
        payload = entry.payload
        if isinstance(payload, WatchDeltaPayload):
            return payload.verb
        if isinstance(payload, WarningEventPayload):
            return f"{payload.reason} ×{payload.count}: {payload.note}"
        if isinstance(payload, ContextSwitchPayload):
            return f"{payload.phase}: {payload.from_context or '(default)'} -> {payload.to_context or '(default)'} {payload.note}".strip()
        assert isinstance(payload, WriteAuditPayload)
        return f"{payload.action}: {payload.outcome}"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        target = self._targets.get(str(event.row_key.value))
        if target is None or target.kind_alias is None:
            self.query_one("#timeline-status", Static).update("Selected row has no navigable resource")
            return
        self.dismiss(("goto", target.kind_alias, target.namespace, target.name))
```

```python
# src/korvid/ui/app.py
# append to KorvidApp.BINDINGS
Binding("T", "timeline", "Timeline", id="timeline")

    def action_timeline(self) -> None:
        if self._session_timeline is None:
            self.notify("Timeline unavailable in this session", severity="warning")
            return
        resource_toggle = self._selected_timeline_resource()
        self.push_screen(
            SessionTimelineScreen(
                self._session_timeline,
                current_epoch=self._ctx_epoch,
                resource_toggle=resource_toggle,
            ),
            functools.partial(self._on_timeline_result, self._ctx_epoch),
        )

    def _selected_timeline_resource(self) -> TimelineResourceRef | None:
        namespace, name = self._selected_ns_name()
        if name is None:
            return None
        kind_alias = self._canonical_kind(self.current_kind)
        meta = self.aliases.get(kind_alias)
        if meta is None:
            return None
        uid = self._selected_uid(namespace or None, name)
        return TimelineResourceRef(kind_alias=kind_alias, display_kind=meta.kind, namespace=namespace or "", name=name, uid=uid)

    def _on_timeline_result(self, epoch: int, result: TimelineGotoResult | None) -> None:
        if result is None:
            return
        if self._ctx_switch_crossed(epoch):
            self.notify("timeline navigation cancelled - the kube context changed while the timeline was open", severity="warning")
            return
        _, kind_alias, namespace, name = result
        self.run_worker(self._jump_to_object(kind_alias, namespace, name, epoch=epoch), exclusive=False)
```

```python
# src/korvid/ui/widgets/help_screen.py
# add to _ACTION_GROUPS
"timeline": ("Table",),
```

```markdown
<!-- docs/keybindings.md -->
| `T` | table | Open the bounded session timeline — watch deltas, live Warning events, context switches, and write intent/outcomes; `e` toggles current/all epochs, `s` cycles source, `r` toggles the selected resource filter, `Enter` navigates a resource row |

Action names: `quit`, `help`, `open_command`, `open_filter`,
`toggle_all_namespaces`, `describe`, `relationships`, `shell`, `logs`, `logs_multi`,
`log_format`, `log_wrap`, `log_timestamps`, `log_save`, `log_previous`,
`log_search_next`, `log_search_prev`, `sort_by_age`, `sort_by_cpu`,
`sort_by_mem`, `sort_picker`, `toggle_topbar`, `toggle_agent`, `interrupt_agent`,
`timeline`, `delete_resource`, `rollout_restart`, `resize_pod`, `scale_resource`,
`edit_resource`, `hint_details`, `operator_install`, `cordon_node`,
`uncordon_node`, `drain_node`, `port_forward`, `transfer`, `helm_install`,
`helm_upgrade`, `helm_rollback`, `helm_history`.
```

```markdown
<!-- docs/tui.md -->
## Session timeline

Press `T` from any resource table to open the session timeline. The view is
session-local and in-memory only: it stores bounded watch deltas, live
Kubernetes Warning events, context switch attempts/results, and write
intent/outcomes after the audit record is durable. The default view shows only
entries from the current context epoch; press `e` to include previous epochs,
`s` to cycle sources, and `r` to pin/unpin the selected resource.

    timeline:
      max_entries: 500
      max_bytes: 262144
```

```markdown
<!-- README.md -->
- **[Browsing the cluster](https://github.com/hellices/korvid/blob/main/docs/tui.md)** — custom columns, live metrics, ops hints, the split workspace, the log viewer (multi-pod merge, JSON highlighting, search, save), explicit namespace scope with RBAC-aware denials, probe-first context switching, and a bounded session timeline (`T`) for watch deltas, Warning events, context switches, and write outcomes.
```

- [ ] **Step 4: Run the focused validation to verify GREEN**

Run:

```bash
uv run pytest -p no:tach tests/ui/test_session_timeline_screen.py tests/ui/test_session_timeline_flow.py tests/ui/test_help_screen.py tests/ui/test_keybindings.py tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py -q
uv run ruff check --fix src/korvid/ui/widgets/session_timeline_screen.py src/korvid/ui/app.py src/korvid/ui/widgets/help_screen.py tests/ui/test_session_timeline_screen.py tests/ui/test_session_timeline_flow.py tests/ui/test_help_screen.py tests/ui/test_keybindings.py tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py
uv run ruff format src/korvid/ui/widgets/session_timeline_screen.py src/korvid/ui/app.py src/korvid/ui/widgets/help_screen.py tests/ui/test_session_timeline_screen.py tests/ui/test_session_timeline_flow.py tests/ui/test_help_screen.py tests/ui/test_keybindings.py tests/ui/test_ctx_switch.py tests/ui/test_write_ops.py
uv run mypy src/korvid/ui/widgets/session_timeline_screen.py src/korvid/ui/app.py src/korvid/ui/widgets/help_screen.py
uv run tach check
```

Expected:
- `pytest`: PASS
- `ruff check --fix`: `All checks passed!`
- `ruff format`: unchanged or only formatting changes in touched files
- `mypy`: `Success: no issues found`
- `tach check`: PASS

- [ ] **Step 5: Commit**

```bash
git add src/korvid/ui/widgets/session_timeline_screen.py src/korvid/ui/app.py src/korvid/ui/widgets/help_screen.py tests/ui/test_session_timeline_screen.py tests/ui/test_session_timeline_flow.py tests/ui/test_help_screen.py tests/ui/test_keybindings.py docs/keybindings.md docs/tui.md README.md
git commit -m "feat: add session timeline view" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

## Self-review

- **Spec coverage:**
  - Bounds / eviction / oversized refusal / no unbounded payloads: Task 1.
  - Watch `ADDED`/`MODIFIED`/`DELETED` after store accept: Task 2 + Task 3.
  - Warning Event watch/project/redact before storage: Task 1 + Task 2 + Task 3.
  - Context switch started/completed/failed and current-epoch partitioning: Task 3 + Task 4.
  - Write intent/outcome only after durable audit append succeeds: Task 3.
  - Deterministic source/resource filters, Textual navigation, docs/keybindings: Task 4.
  - “Failures cannot kill watches or weaken audit” and “works without LLM”: Tasks 2-4.
- **Placeholder scan:** No `TODO`, `TBD`, “similar to Task N”, or unnamed interfaces remain.
- **Type consistency:** `TimelineResourceRef`, `TimelineSource`, `TimelineGotoResult`, and the new `KorvidApp` constructor kwargs are defined once and reused consistently across tasks.
