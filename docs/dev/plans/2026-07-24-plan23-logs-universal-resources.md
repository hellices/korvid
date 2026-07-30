# korvid Plan 2+3: Log Viewer & Universal Resources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the two remaining UX pillars of Phase 1: (A) universal resource browsing — any kind incl. CRDs via API discovery, per-namespace or all-namespaces scope, describe (`d`), shell-in (`s`) — and (B) a first-class log viewer — merged multi-pod streams, JSON formatted↔raw toggle (`f`), previous logs (`p`), search n/N, 2-pane split, reconnect indicator.

**Architecture:** Extends the existing layered design (`ui→{core,agent,k8s}`, `core→k8s`). New k8s modules: `discovery.py` (ResourceMeta + API discovery), `logs.py` (log streaming). New core module: `logbuffer.py`. `ResourceStore`/`WatchManager` gain an explicit *scope* key (`namespace` or `"*"` = all namespaces) instead of bucketing by object namespace. The pods view keeps its rich `PodSummary` pipeline; every other kind flows through a generic `GenericSummary` pipeline with universal columns (NAME, [NAMESPACE], AGE).

**Tech Stack:** unchanged — Python ≥3.11, Textual ≥8, kubernetes_asyncio, PyYAML, rich; uv + Ruff + mypy --strict + pytest/Pilot + tach.

## Global Constraints

- All constraints from `docs/dev/specs/2026-07-24-korvid-engineering-standards.md` and the phase-1 plan apply (mypy --strict, ruff, tach layers, `pytest.raises(match=)`, commit per green task, never `--no-verify`)
- Third-party `ApiException` NEVER crosses the k8s layer — wrap as `ApiStatusError` (`src/korvid/k8s/errors.py`)
- Textual imports **only** in `ui/`
- Key bindings: `d`=describe, `s`=shell-in, `l`=logs, `0`=all namespaces, `D`=debug (reserved, later plan)
- ALL-namespaces sentinel is the string `"*"` — exported as `ALL_NAMESPACES` from `korvid.core.store`
- Run `make check` before every commit; deptry runs in CI against declared deps only (direct imports must be declared in pyproject)

---

### Task 1: API discovery — ResourceMeta + discover_resources

**Files:**
- Create: `src/korvid/k8s/discovery.py`
- Modify: `src/korvid/k8s/client.py` (add `discover_resources`)
- Test: `tests/k8s/test_discovery.py`

**Interfaces:**
- Produces: `ResourceMeta(kind, plural, group, version, namespaced, shortnames)` frozen dataclass with `.api_base` property (`"/api/v1"` for core, `"/apis/{group}/{version}"` otherwise); `PODS_META` constant; `build_alias_map(metas: list[ResourceMeta]) -> dict[str, ResourceMeta]` (lowercase plural/kind/shortnames → meta); `KubeClient.discover_resources() -> list[ResourceMeta]` returning every list+watch-able resource from `/api/v1` and aggregated `/apis` discovery (CRDs included automatically — they appear in discovery like any group).

- [ ] **Step 1: Write failing tests**

```python
# tests/k8s/test_discovery.py
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map


def test_api_base_core_and_group() -> None:
    assert PODS_META.api_base == "/api/v1"
    deploy = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
    assert deploy.api_base == "/apis/apps/v1"


def test_alias_map_covers_plural_kind_and_shortnames() -> None:
    deploy = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
    aliases = build_alias_map([deploy])
    assert aliases["deployments"] is deploy
    assert aliases["deployment"] is deploy
    assert aliases["deploy"] is deploy


def test_alias_map_first_meta_wins_on_conflict() -> None:
    a = ResourceMeta("Foo", "foos", "a.io", "v1", True, ("f",))
    b = ResourceMeta("Bar", "bars", "b.io", "v1", True, ("f",))
    aliases = build_alias_map([a, b])
    assert aliases["f"] is a  # deterministic: earlier discovery order wins
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/k8s/test_discovery.py -v` → ImportError

- [ ] **Step 3: Implement `discovery.py`**

```python
"""Resource metadata + API discovery (any kind incl. CRDs, spec §5)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceMeta:
    kind: str  # "Deployment"
    plural: str  # "deployments"
    group: str  # "" for core
    version: str  # "v1"
    namespaced: bool
    shortnames: tuple[str, ...] = ()

    @property
    def api_base(self) -> str:
        return f"/apis/{self.group}/{self.version}" if self.group else "/api/v1"


PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))


def build_alias_map(metas: list[ResourceMeta]) -> dict[str, ResourceMeta]:
    """lowercase plural / kind / shortnames -> meta; first meta wins on conflict."""
    aliases: dict[str, ResourceMeta] = {}
    for meta in metas:
        for alias in (meta.plural, meta.kind.lower(), *meta.shortnames):
            aliases.setdefault(alias.lower(), meta)
    return aliases
```

- [ ] **Step 4: Add `discover_resources` to KubeClient with a test**

Test (append to `tests/k8s/test_discovery.py`) — mock `call_api`-level responses through a fake `_request_json`:

```python
from unittest.mock import AsyncMock, patch

from korvid.k8s.client import KubeClient

_CORE = {"resources": [
    {"name": "pods", "kind": "Pod", "namespaced": True, "shortNames": ["po"],
     "verbs": ["list", "watch", "get"]},
    {"name": "pods/log", "kind": "Pod", "namespaced": True, "verbs": ["get"]},
]}
_APIS = {"groups": [{"name": "apps", "preferredVersion": {"version": "v1"}}]}
_APPS = {"resources": [
    {"name": "deployments", "kind": "Deployment", "namespaced": True,
     "shortNames": ["deploy"], "verbs": ["list", "watch"]},
]}


async def test_discover_resources_filters_subresources_and_non_watchable() -> None:
    client = KubeClient()
    responses = {"/api/v1": _CORE, "/apis": _APIS, "/apis/apps/v1": _APPS}

    async def fake_request(path: str) -> dict:
        return responses[path]

    with patch.object(client, "_request_json", side_effect=fake_request):
        metas = await client.discover_resources()
    by_plural = {m.plural: m for m in metas}
    assert by_plural["pods"].shortnames == ("po",)
    assert by_plural["deployments"].group == "apps"
    assert "pods/log" not in by_plural  # subresources excluded
```

Implementation (in `client.py`): add a `_request_json(path)` helper using `self._api.call_api` raw GET (follow the existing `_preload_content=False` + json pattern; wrap `ApiException` as `ApiStatusError`), then:

```python
async def discover_resources(self) -> list[ResourceMeta]:
    if self._api is None:
        raise RuntimeError("connect() first")
    metas: list[ResourceMeta] = []
    core = await self._request_json("/api/v1")
    metas += _parse_resource_list(core, group="", version="v1")
    groups = await self._request_json("/apis")
    for g in groups.get("groups", []):
        version = (g.get("preferredVersion") or {}).get("version")
        if not version:
            continue
        try:
            rl = await self._request_json(f"/apis/{g['name']}/{version}")
        except ApiStatusError:
            continue  # a broken aggregated API must not kill discovery
        metas += _parse_resource_list(rl, group=g["name"], version=version)
    return metas


def _parse_resource_list(data: dict[str, Any], *, group: str, version: str) -> list[ResourceMeta]:
    out = []
    for r in data.get("resources", []):
        if "/" in r["name"] or "list" not in r.get("verbs", []) or "watch" not in r.get("verbs", []):
            continue
        out.append(ResourceMeta(r["kind"], r["name"], group, version, r["namespaced"],
                                tuple(r.get("shortNames") or ())))
    return out
```

`_request_json` implementation sketch (use `self._api.call_api(path, "GET", auth_settings=["BearerToken"], response_type=None, _preload_content=False)` then `json.loads(await resp.read())`; check kubernetes_asyncio's actual call_api signature during implementation — an alternative is `aiohttp` via `self._api.rest_client.pool_manager`; pick whichever passes against the fake in tests and a real cluster).

- [ ] **Step 5: `make check` green, commit** — `feat: API discovery — ResourceMeta and discover_resources`

---

### Task 2: Generic list/watch — GenericSummary + watch_objects

**Files:**
- Modify: `src/korvid/k8s/models.py` (add `GenericSummary`), `src/korvid/k8s/client.py` (add `watch_objects`, `get_object`, `list_events_for`)
- Test: `tests/k8s/test_models.py`, `tests/k8s/test_client.py`

**Interfaces:**
- Consumes: `ResourceMeta` from Task 1.
- Produces: `GenericSummary(name, namespace, kind, created)` frozen dataclass with `from_manifest(kind: str, manifest: dict) -> GenericSummary` (created = `metadata.creationTimestamp` ISO string or `""`) and `age() -> str` (compact style: `"5m"`, `"3h"`, `"2d"`; empty created → `"-"`); `KubeClient.watch_objects(meta: ResourceMeta, namespace: str | None) -> AsyncIterator[tuple[str, GenericSummary]]` (None = all namespaces; LIST-then-watch, same pattern as `watch_pods`); `KubeClient.get_object(meta, namespace, name) -> dict` (raw manifest); `KubeClient.list_events_for(namespace: str, name: str) -> list[dict]` (core v1 Events with `involvedObject.name==name`, via fieldSelector).
- Also: `PodSummary` gains `containers: tuple[str, ...] = ()` (from `spec.containers[].name`) — needed by the log viewer (Task 8).

- [ ] **Step 1: Failing tests** — `GenericSummary.from_manifest` + `age()` (freeze time with a fixed `now` parameter: `age(now: datetime | None = None)`), `PodSummary.from_manifest` picks up container names, `watch_objects` LIST+watch against the `_FakeWatch` harness already in `tests/k8s/test_client.py` (reuse it; all-namespaces path asserts the URL/method call has no namespace arg), `get_object`/`list_events_for` ApiException wrapping with `pytest.raises(ApiStatusError, match=...)`.
- [ ] **Step 2: Verify FAIL**
- [ ] **Step 3: Implement.** `watch_objects` uses raw paths: LIST `{meta.api_base}/namespaces/{ns}/{plural}` (or `{meta.api_base}/{plural}` when namespace is None) via `_request_json`, then `k8s_watch.Watch().stream` with `self._custom_list(meta, namespace)` — kubernetes_asyncio watch supports `CustomObjectsApi.list_cluster_custom_object` / `list_namespaced_custom_object` for group kinds; for core kinds use the typed CoreV1 list functions when they exist, else custom objects. Implementation freedom here, but the contract (LIST as ADDED events, then watch from the snapshot resourceVersion, ApiStatusError wrapping) must match `watch_pods` exactly.
- [ ] **Step 4: `make check` green, commit** — `feat: generic list/watch, get_object, events lookup`

---

### Task 3: Scope-aware store & watch — ALL_NAMESPACES

**Files:**
- Modify: `src/korvid/core/store.py`, `src/korvid/core/watch.py`, `src/korvid/ui/app.py`, `src/korvid/__main__.py`
- Test: `tests/core/test_store.py`, `tests/core/test_watch.py`, `tests/ui/test_app.py`

**Interfaces:**
- Produces: `ALL_NAMESPACES = "*"` exported from `korvid.core.store`. `ResourceStore` keys change from `(kind, obj.namespace)` to explicit scope: `apply_event(kind: str, scope: str, event_type: str, obj) -> None`, `get(kind, scope) -> list`, `clear(kind, scope) -> None`. Store values become a `Summary` Protocol (`name: str; namespace: str` attributes) so both `PodSummary` and `GenericSummary` fit. Sort in `get` by `(namespace, name)`.
- `WatchSource` changes to `Callable[[str, str], AsyncIterator[tuple[str, Summary]]]` — called as `source(kind, scope)`. `WatchManager.start/stop(kind, scope)` semantics unchanged otherwise (retry, purge-on-reconnect, purge-on-start all keyed by scope now).
- `__main__` wires a dispatching source: `kind == "pods"` → `watch_pods(namespace)` (scope `"*"` → needs `watch_pods` to accept `namespace=None` for all-ns; extend it); other kinds resolve meta via the alias map built at startup from `discover_resources()` and call `watch_objects(meta, None if scope == ALL_NAMESPACES else scope)`.
- `KorvidApp.__init__` gains `aliases: dict[str, ResourceMeta] | None = None` and tracks `self.current_kind: str = "pods"`, `self.current_scope: str` (init from config namespace).

- [ ] **Step 1: Update store/watch tests to the new signatures (mechanical), plus new behavior tests:** all-ns scope stores pods from different namespaces under one `("pods", "*")` key and `get` returns them sorted by `(namespace, name)`; `apply_event` DELETED in all-ns scope removes by `(namespace, name)` — the bucket key must be `f"{obj.namespace}/{obj.name}"` internally so same-name pods in two namespaces don't collide.
- [ ] **Step 2: Verify FAIL, implement, all existing tests migrated and green.**
- [ ] **Step 3: `make check` green, commit** — `refactor: scope-aware store and watch (ALL_NAMESPACES)`

---

### Task 4: Universal views — command grammar v2, dynamic columns, `0` key

**Files:**
- Modify: `src/korvid/ui/command.py`, `src/korvid/ui/messages.py`, `src/korvid/ui/widgets/resource_table.py`, `src/korvid/ui/app.py`, `src/korvid/ui/widgets/command_bar.py` (placeholder text)
- Test: `tests/ui/test_command.py`, `tests/ui/test_app.py`

**Interfaces:**
- `parse_command(text: str, known: Callable[[str], str | None]) -> ...` — `known(alias)` returns the canonical plural or None. Grammar: `:<alias>` → NavigateCommand(plural, namespace=None=keep current); `:<alias> all` → NavigateCommand(plural, namespace=ALL_NAMESPACES); `:<alias> <ns>` → NavigateCommand(plural, namespace=ns); `:ns` → picker; `:ns <name>` → NavigateCommand(current view … keep as "pods"? NO — NavigateCommand(None-view means keep)) — add `view: str | None` support: `NavigateCommand(view=None, namespace=X)` = switch namespace, keep kind. `:q|quit` unchanged. Unknown alias → UnknownCommand.
- App: `0` key binding → toggle scope to ALL_NAMESPACES for current kind (and back to config default on second press). `on_navigate_command` stops the old (kind, scope) watch, starts the new one.
- `ResourceTable.show(kind, rows, *, all_namespaces: bool, pattern: str)` — pods render the existing rich 8 columns (+NAMESPACE first when all_namespaces); other kinds render NAMESPACE?/NAME/AGE. Row keys are `f"{namespace}/{name}"` always (needed by describe/shell/logs in all-ns scope). Columns are rebuilt (`clear(columns=True)`) when kind or scope changes — track last shown (kind, all_namespaces) on the widget.
- StatusBar shows `ns: *` in all-ns scope.

- [ ] **Step 1: Failing tests** — grammar cases above; Pilot tests: `:deployments` renders generic columns; `:pods all` adds NAMESPACE column and shows pods from two namespaces (extend `make_app` with a generic fake source keyed by kind); `0` toggles; row keys are `ns/name`; filter still matches name only.
- [ ] **Step 2: Verify FAIL, implement.**
- [ ] **Step 3: `make check` green, commit** — `feat: universal resource views with all-namespaces scope`

---

### Task 5: Describe view (`d`)

**Files:**
- Create: `src/korvid/ui/widgets/describe_screen.py`
- Modify: `src/korvid/ui/app.py` (binding + handler + injected callables), `src/korvid/__main__.py` (wire `kube.get_object`/`kube.list_events_for`)
- Test: `tests/ui/test_app.py`

**Interfaces:**
- `KorvidApp.__init__` gains `get_manifest: Callable[[str, str | None, str], Awaitable[dict]] | None = None` (kind-alias, namespace, name → raw manifest; `__main__` adapter resolves alias→meta and calls `kube.get_object`) and `get_events: Callable[[str, str], Awaitable[list[dict]]] | None = None`.
- `DescribeScreen(ModalScreen[None])` — full-screen modal: title `{kind}/{ns}/{name}`, body = `yaml.safe_dump(manifest)` in a scrollable `Static` (managed fields stripped: drop `metadata.managedFields`), followed by an `EVENTS` section (`type reason age message` lines, `"<no events>"` when empty). Esc/q dismisses. `d` on the table opens it for the selected row; errors surface via `explain_api_error` for `ApiStatusError` (same pattern as the namespace picker handler).

- [ ] **Step 1: Failing Pilot tests** — press `d` with a selected pod → DescribeScreen mounted, body contains the pod name and `EVENTS`; managedFields absent; Esc dismisses; 403 from get_manifest → notification contains `RBAC`, screen not mounted.
- [ ] **Step 2: Verify FAIL, implement.**
- [ ] **Step 3: `make check` green, commit** — `feat: describe view (d) with events`

---

### Task 6: Shell-in (`s`)

**Files:**
- Create: `src/korvid/ui/shell.py` (pure helper building the exec argv — testable without a TTY)
- Modify: `src/korvid/ui/app.py` (binding + handler)
- Test: `tests/ui/test_shell.py`, `tests/ui/test_app.py`

**Interfaces:**
- `build_exec_argv(namespace: str, pod: str, container: str | None = None) -> list[str]` → `["kubectl", "exec", "-it", "-n", ns, pod, *(["-c", container] if container else []), "--", "sh", "-c", "command -v bash >/dev/null 2>&1 && exec bash || exec sh"]` (bash with sh fallback — distroless pods fail with a readable kubectl error).
- App handler `action_shell()`: only for pods kind; no selection → warning notify; `shutil.which("kubectl")` is None → error notify `"kubectl not found on PATH — shell-in requires kubectl"`; else `with self.suspend(): subprocess.call(argv)` and refresh on return. Binding: `("s", "shell", "Shell")`.

- [ ] **Step 1: Failing tests** — argv builder cases (with/without container); Pilot: `s` with kubectl missing (patch `shutil.which` → None) notifies error and does NOT suspend (patch `subprocess.call`, assert not called); `s` on non-pods kind → warning.
- [ ] **Step 2: Verify FAIL, implement.**
- [ ] **Step 3: `make check` green, commit** — `feat: shell-in (s) via kubectl exec with PTY suspend`

---

### Task 7: Log streaming (k8s layer)

**Files:**
- Create: `src/korvid/k8s/logs.py`
- Modify: `src/korvid/k8s/client.py` (add `stream_logs`)
- Test: `tests/k8s/test_logs.py`

**Interfaces:**
- `LogLine(pod: str, container: str, text: str)` frozen dataclass in `logs.py`.
- `KubeClient.stream_logs(namespace: str, pod: str, container: str, *, previous: bool = False, follow: bool = True, tail_lines: int = 200) -> AsyncIterator[LogLine]` — `read_namespaced_pod_log(..., follow=follow, previous=previous, tail_lines=tail_lines, _preload_content=False)`, iterate `resp.content` line by line (aiohttp StreamReader `async for raw in resp.content`), decode utf-8 with `errors="replace"`, strip trailing newline, yield `LogLine(pod, container, text)`. `ApiException` → `ApiStatusError` (both at call time and mid-stream). `previous=True` implies `follow=False` (terminated container logs can't follow).

- [ ] **Step 1: Failing tests** — fake response object with an async-iterable `.content` yielding bytes lines; assert LogLine fields, unicode-replace decoding, previous flag passed to the API call and follow forced False, ApiException(403) → `pytest.raises(ApiStatusError, match="API 403")`.
- [ ] **Step 2: Verify FAIL, implement.**
- [ ] **Step 3: `make check` green, commit** — `feat: pod log streaming with previous-container support`

---

### Task 8: LogBuffer (core) — ring buffer, overflow, search

**Files:**
- Create: `src/korvid/core/logbuffer.py`
- Test: `tests/core/test_logbuffer.py`

**Interfaces:**
- `LogBuffer(max_lines: int = 5000)`: `append(line: LogLine) -> None`; `lines() -> list[LogLine]`; `overflowed: bool` property (True once the ring has dropped anything — powers the §5 #1 explicit overflow banner); `search(pattern: str) -> list[int]` (case-insensitive substring over `text`, returns indices into `lines()`); `clear() -> None` (resets overflow too). Uses `collections.deque(maxlen=...)`.

- [ ] **Step 1: Failing tests** — append/lines order, ring drop sets `overflowed`, search returns indices, clear resets, empty pattern → `[]`.
- [ ] **Step 2: Verify FAIL, implement (small, pure).**
- [ ] **Step 3: `make check` green, commit** — `feat: log ring buffer with overflow flag and search`

---

### Task 9: LogPane + 2-pane split + merged multi-container streams

**Files:**
- Create: `src/korvid/ui/widgets/log_pane.py`
- Modify: `src/korvid/ui/app.py` (compose split, `l` binding, stream task management), `src/korvid/__main__.py` (wire `kube.stream_logs`)
- Test: `tests/ui/test_log_pane.py`, `tests/ui/test_app.py`

**Interfaces:**
- App gains `stream_logs: Callable[..., AsyncIterator[LogLine]] | None = None` injection (signature of `KubeClient.stream_logs`).
- `LogPane(Widget)` — hidden by default; contains a header `Static` (sources + state: `● streaming` / `⟳ reconnecting` / `▮ ended`) and a `RichLog`. Public API: `open(sources: list[tuple[str, str]]) -> None` (pod, container pairs; renders header), `feed(line: LogLine) -> None` (writes `[pod/container] text` — prefix omitted when only one source), `set_state(state: str) -> None`, `show_overflow_banner() -> None`, `close() -> None`.
- App `l` on a selected pod: collect `(pod, container)` for every container of the selected pod (`PodSummary.containers`; falls back to a single unnamed stream when empty), show LogPane in the lower half (Vertical split: table 60% / logs 40% via CSS), spawn one `asyncio.Task` per container that feeds pane + a shared `LogBuffer`; task exceptions → `set_state` + notify via `explain_api_error` when ApiStatusError. Esc inside pane (or `l` again) cancels tasks and closes. Switching view/namespace closes the pane.
- Merged multi-pod: when the table has an active filter matching >1 pod, `L` (shift-l) streams ALL visible pods' containers (cap 8 pods, notify when capped) — this is the §5 #1 merged multi-pod stream.

- [ ] **Step 1: Failing Pilot tests** — `l` opens pane with the pod's containers, lines render with prefixes (fake stream_logs yielding 2 containers), single-source lines have no prefix, Esc closes and cancels (assert tasks cancelled), `L` with a filter matching 2 pods streams both with pod prefixes, cap notification at >8.
- [ ] **Step 2: Verify FAIL, implement.**
- [ ] **Step 3: `make check` green, commit** — `feat: log pane with 2-pane split and merged streams`

---

### Task 10: JSON toggle (`f`), previous logs (`p`), search n/N in the pane

**Files:**
- Create: `src/korvid/ui/logformat.py` (pure: JSON detect + format)
- Modify: `src/korvid/ui/widgets/log_pane.py`, `src/korvid/ui/app.py`
- Test: `tests/ui/test_logformat.py`, `tests/ui/test_log_pane.py`

**Interfaces:**
- `format_log_line(text: str, *, formatted: bool) -> Text` — when `formatted` and the line parses as a JSON object: render `level` (colored: error=red, warn=yellow, info=green, else dim), `ts`/`time`/`timestamp` dim, `msg`/`message` bold, then remaining keys as `key=value` dim — all on one line. Non-JSON or `formatted=False` → plain `Text(text)`. Detection = `json.loads` succeeding AND result is a dict (auto-detect never forces a rendering — the toggle decides, spec §5 #1).
- LogPane: `f` toggles `self.formatted` (default True) and re-renders the buffer through `format_log_line`; header shows `[json]`/`[raw]`. `p` closes live streams and re-opens with `previous=True, follow=False` (banner `── previous container logs ──`). `/` opens a small inline search Input; Enter stores hits from `LogBuffer.search`, header shows `n/N: 3/17`, `n`/`N` scroll RichLog to next/prev hit line.
- App threads `previous` through its stream-spawning helper.

- [ ] **Step 1: Failing tests** — formatter: JSON line formatted (level color, msg bold), invalid JSON stays raw, JSON array stays raw, `formatted=False` raw; Pilot: `f` re-renders and flips header tag, `p` re-opens with `previous=True` (assert fake received flag), search flow sets hit counter and `n` advances.
- [ ] **Step 2: Verify FAIL, implement.**
- [ ] **Step 3: `make check` green, commit** — `feat: JSON formatted/raw toggle, previous logs, n/N search`

---

### Task 11: Integration hardening + docs + real-cluster smoke

**Files:**
- Modify: `src/korvid/ui/app.py` (reconnect indicator wiring: stream task retry loop with `set_state("reconnecting")` → resumed → `streaming`; overflow banner call when `LogBuffer.overflowed` flips), `README.md` (keybindings table: `: / 0 d s l L f p n N q Esc`), `src/korvid/ui/widgets/command_bar.py` + `filter_bar.py` placeholders mention new grammar
- Test: `tests/ui/test_app.py` (reconnect state transition with a flaky fake stream; overflow banner appears once)

**Steps:**
- [ ] **Step 1: Failing tests for reconnect indicator + overflow banner; implement.**
- [ ] **Step 2: Full gate:** `uv run pytest --cov --cov-fail-under=80` and `make check` green.
- [ ] **Step 3: Real-cluster smoke (manual, aks-shared-runners):** `uv run korvid` → `:pods all` shows NAMESPACE column; `:deployments` generic view; `d` describe kube-system pod; `l` logs stream; `f` toggle on a JSON-logging pod; `s` opens a shell into a pod with bash/sh; `0` toggles scope. Record results in the PR body.
- [ ] **Step 4: Commit** — `feat: reconnect indicator, overflow banner, docs`

---

## Out of Scope (subsequent plans)

- **Plan 4 — Agent runtime**: agentic loop, ToolRegistry, provider adapters, approval gate, audit log, agent panel
- **Plan 5 — Diagnostics**: kubectl debug (`D`), event intelligence, ownership tree
- Port-forward, Secret decode, metrics usage columns (Phase 2)

## Verification at the End

```bash
uv run pytest --cov --cov-fail-under=80   # full suite green
make check                                 # ruff + mypy + tach green
uv run korvid                              # smoke per Task 11 Step 3
```
