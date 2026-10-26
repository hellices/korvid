# Discovery Snapshot and API Version Resolution (#409 S2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve every diagnostic read to an API version that discovery established, never to one inferred from a server version string. To do that, korvid keeps one discovery snapshot per authentication session (#491, slice S2 of #409).

**Architecture:**
- **`k8s`:** a new `discovery_snapshot` module reads `/api`, `/apis` and every advertised group version, and keeps each outcome, whether a success or the error raised. `KubeClient` gains:
  - a session generation, bumped on connect and on every context switch;
  - a five-minute cached snapshot;
  - a `discover_resources()` that is projected from a fresh snapshot.
- **`core`:** a new `api_versions` module turns the raw snapshot into observations and resolves logical sources candidate by candidate. A negative answer needs complete discovery.

**Tech Stack:** Python 3.11+ standard library (`asyncio`, `dataclasses`), pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-25-capability-aware-diagnostics-design.md`, section "API version resolution". Where this plan departs from it, the section below says why.
- **Layers:**
  - `k8s` imports no other korvid layer;
  - `core` may import `k8s`;
  - nothing new imports Textual or an optional extra.
- **`client.py` size:** `scripts/check_source_size.py` caps `src/korvid/k8s/client.py` at 1,944 lines, and it has 1,944 today. This slice moves discovery out of it, so it ends smaller. Do not raise the cap.
- **Behavior to keep:** `discover_resources()` keeps its signature and its results. A failed core `v1` or `/apis` still raises, and a failed named group version is still skipped and logged.
- **Session generation:** opaque, bumped on `connect()` and `switch_context()`. `switch_context(None)` is the kubeconfig reload path. korvid replaces credentials only by reconnecting: `:ctx` and the recovery path both call `switch_context()`.
- **Snapshot age:** a snapshot expires after 300 seconds.
- **Code rules:**
  - no `assert` in `src/`;
  - every `pytest.raises` takes `match=`;
  - maximum complexity 10;
  - Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **Discovery moves to `k8s/discovery_snapshot.py`.** `client.py` keeps thin methods. Its line cap leaves no room otherwise, and the parsing belongs with the snapshot. The DNS-1123 name pattern moves to `k8s/discovery.py` so that both modules share it.
- **`discover_resources()` always reads afresh, as it always has, and keeps the result as the session's snapshot.** Diagnostics call `discovery_snapshot()`, which reuses that snapshot for five minutes.
- **Discovery now also requests `/api`, which the design's `core_root` needs.** The two roots and core `v1` are requested together, so a stalled server costs one deadline rather than three. Core `v1` is read whatever `/api` says.
- **A root with any unreadable entry blocks every negative answer under it.** This applies to an unreadable group entry and to an unreadable version entry. korvid cannot always tell which group such an entry named, so it reports `UNAVAILABLE`/`DISCOVERY_FAILED` rather than "unsupported". An unusable `preferredVersion` only loses its place at the front, because the `versions` list says which versions exist.
- **A document that leaves out a list it must carry is incomplete, not empty.** This covers `/apis` without `groups`, a group version without `resources`, and a resource entry without `verbs`. The API server always sends these fields, so only a non-conforming server or proxy omits them. The projection is unchanged: it reads the first two as empty and skips the third, as `discover_resources()` always has.
- **A candidate carries no parser yet.** Nothing reads objects until S3 (#487), which attaches parsing where the reader returns them.
- **Falling through after a read is S3's job.** The design falls through to the next candidate when a read is `DENIED` or `UNSUPPORTED`. `Resolution` exposes the resolved sources in order and the failure the baseline precedence picks, and S3's reader does the falling through.
- **A blocked candidate keeps its discovery basis:** `DISCOVERY_FORBIDDEN`, `UNAUTHENTICATED`, `DISCOVERY_FAILED` or `THROTTLED`. A partly unreadable document blocks as `UNAVAILABLE`/`DISCOVERY_FAILED`.
- **`FAILURE_PRECEDENCE` becomes public in `core/capability.py`.** Resolution reuses the baseline's precedence.
- **The design's `groups` field is `DiscoverySnapshot.versions`, keyed by `(group, version)`.** Each entry is one group version, and a group's versions keep discovery's order, preferred first.
- **An exec plugin's in-place token refresh does not bump the generation.** `load_refreshable_kube_config` renews an expiring exec credential through `refresh_api_key_hook` without reconnecting. That is the token refresh the design's decision 4 leaves undetected, not a credential replacement.
- **Nothing calls `invalidate_discovery()` yet.** S3 (#487) calls it when a read reports `DISCOVERY_STALE`, and the first consumer's refresh action calls it for an explicit refresh.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/k8s/discovery.py` | modify | `DNS1123_SUBDOMAIN`, shared by discovery and the event field selector |
| `src/korvid/k8s/discovery_snapshot.py` | create | the raw snapshot: fetching, parsing, completeness, expiry, projection |
| `src/korvid/k8s/client.py` | modify | session generation, the cached snapshot, a thin `discover_resources()` |
| `src/korvid/core/capability.py` | modify | `FAILURE_PRECEDENCE` made public |
| `src/korvid/core/api_versions.py` | create | the observation snapshot, logical sources, `resolve` |
| `tests/k8s/test_discovery_snapshot.py` | create | the raw snapshot |
| `tests/k8s/test_discovery_session.py` | create | `KubeClient` sessions and snapshot caching |
| `tests/k8s/test_discovery.py`, `tests/k8s/test_discovery_versions.py`, `tests/k8s/test_error_categories.py` | modify | request mocks answer `/api`; request counts include it |
| `tests/core/test_api_versions.py` | create | observations and resolution |
| `tests/test_optional_extras.py` | modify | the new modules import without extras |

---

### Task 1: The raw discovery snapshot

**Files:**
- Modify: `src/korvid/k8s/discovery.py`
- Create: `src/korvid/k8s/discovery_snapshot.py`
- Test: `tests/k8s/test_discovery_snapshot.py`

**Interfaces:**
- Produces in `korvid.k8s.discovery`: `DNS1123_SUBDOMAIN: re.Pattern[str]`.
- Produces in `korvid.k8s.discovery_snapshot`:
  - `DISCOVERY_TTL_SECONDS = 300.0` and `CORE_VERSION = "v1"`;
  - `DiscoveryFailure = ApiStatusError | KubeClientError`;
  - `monotonic() -> float`;
  - `RootOutcome(path, advertised=(), complete=True, error=None)`, with `versions(group)`;
  - `ServedResource(name, verbs)`;
  - `GroupVersionOutcome(group, version, resources=(), metas=(), complete=True, error=None)`;
  - `RawDiscovery(generation, fetched_at, started, core_root, group_root, group_versions)`, with `expired()`;
  - `async fetch_discovery(request_json, *, generation, timeout, concurrency) -> RawDiscovery`;
  - `project_resources(snapshot) -> list[ResourceMeta]`.

- [ ] **Step 1: Write the failing test**

Create `tests/k8s/test_discovery_snapshot.py`:

```python
"""The raw discovery snapshot of one session (#409, slice S2)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.k8s import discovery_snapshot
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
    fetch_discovery,
    project_resources,
)
from korvid.k8s.errors import ApiStatusError, KubeMalformedError, KubeTimeoutError

APPS_GROUP = {
    "name": "apps",
    "preferredVersion": {"groupVersion": "apps/v1", "version": "v1"},
    "versions": [{"groupVersion": "apps/v1", "version": "v1"}],
}
DOCUMENTS: dict[str, dict[str, Any]] = {
    "/api": {"versions": ["v1"]},
    "/api/v1": {
        "resources": [
            {"name": "pods", "kind": "Pod", "namespaced": True, "verbs": ["get", "list", "watch"]},
            {"name": "pods/log", "kind": "Pod", "namespaced": True, "verbs": ["get"]},
            {"name": "bindings", "kind": "Binding", "namespaced": True, "verbs": ["create"]},
        ]
    },
    "/apis": {"groups": [APPS_GROUP]},
    "/apis/apps/v1": {
        "resources": [
            {"name": "deployments", "kind": "Deployment", "namespaced": True, "verbs": ["list"]}
        ]
    },
}


def _answering(
    documents: dict[str, dict[str, Any]], failures: dict[str, Exception] | None = None
) -> Callable[[str], Awaitable[dict[str, Any]]]:
    async def request(path: str) -> dict[str, Any]:
        if failures is not None and path in failures:
            raise failures[path]
        return documents[path]

    return request


async def _fetch(
    documents: dict[str, dict[str, Any]] = DOCUMENTS, failures: dict[str, Exception] | None = None
) -> RawDiscovery:
    request = _answering(documents, failures)
    return await fetch_discovery(request, generation=3, timeout=1.0, concurrency=4)


async def test_both_roots_and_every_advertised_version_are_kept() -> None:
    raw = await _fetch()

    assert raw.generation == 3
    assert raw.core_root == RootOutcome("/api", (("", "v1"),))
    assert raw.group_root == RootOutcome("/apis", (("apps", "v1"),))
    assert [(outcome.group, outcome.version) for outcome in raw.group_versions] == [
        ("", "v1"),
        ("apps", "v1"),
    ]


async def test_every_served_resource_keeps_its_verbs() -> None:
    core = (await _fetch()).group_versions[0]

    assert core.resources == (
        ServedResource("pods", frozenset({"get", "list", "watch"})),
        ServedResource("pods/log", frozenset({"get"})),
        ServedResource("bindings", frozenset({"create"})),
    )
    assert core.complete
    assert [meta.plural for meta in core.metas] == ["pods"]


@pytest.mark.parametrize("failed", ["/api", "/apis"])
async def test_a_failed_root_is_kept_without_affecting_the_other(failed: str) -> None:
    error = ApiStatusError(403, "Forbidden")
    raw = await _fetch(failures={failed: error})
    roots = {"/api": raw.core_root, "/apis": raw.group_root}

    assert roots[failed] == RootOutcome(failed, error=error)
    assert all(root.error is None for path, root in roots.items() if path != failed)


async def test_core_v1_is_read_even_when_api_fails() -> None:
    raw = await _fetch(failures={"/api": ApiStatusError(403, "Forbidden")})

    assert raw.group_versions[0].error is None
    assert [meta.plural for meta in project_resources(raw)] == ["pods", "deployments"]


async def test_a_failed_group_version_is_kept_and_skipped() -> None:
    error = ApiStatusError(503, "Service Unavailable")
    raw = await _fetch(failures={"/apis/apps/v1": error})

    assert raw.group_versions[1] == GroupVersionOutcome("apps", "v1", error=error)
    assert [meta.plural for meta in project_resources(raw)] == ["pods"]


@pytest.mark.parametrize("path", ["/api/v1", "/apis"])
async def test_a_failed_core_version_or_group_root_still_fails_projection(path: str) -> None:
    raw = await _fetch(failures={path: ApiStatusError(503, "Service Unavailable")})

    with pytest.raises(ApiStatusError, match="503"):
        project_resources(raw)


async def test_a_stalled_request_is_kept_as_a_timeout() -> None:
    async def request(path: str) -> dict[str, Any]:
        if path == "/apis/apps/v1":
            await asyncio.Future[None]()
        return DOCUMENTS[path]

    raw = await fetch_discovery(request, generation=1, timeout=0.01, concurrency=4)

    assert isinstance(raw.group_versions[1].error, KubeTimeoutError)


async def test_the_roots_and_core_v1_are_requested_together() -> None:
    roots = {"/api", "/api/v1", "/apis"}
    started: set[str] = set()
    all_started = asyncio.Event()

    async def request(path: str) -> dict[str, Any]:
        started.add(path)
        if roots <= started:
            all_started.set()
        if path in roots:
            await all_started.wait()
        return DOCUMENTS[path]

    fetch = fetch_discovery(request, generation=1, timeout=5.0, concurrency=4)
    raw = await asyncio.wait_for(fetch, timeout=2)

    assert raw.group_root.error is None


@pytest.mark.parametrize(
    "groups",
    [
        pytest.param([None, APPS_GROUP], id="not-a-group"),
        pytest.param([{"name": "../bad", "versions": []}, APPS_GROUP], id="invalid-name"),
        pytest.param(
            [{**APPS_GROUP, "versions": [*APPS_GROUP["versions"], {"version": 42}]}],
            id="version-entry",
        ),
        pytest.param([{**APPS_GROUP, "versions": None}], id="no-version-list"),
    ],
)
async def test_an_unreadable_group_root_entry_makes_the_root_incomplete(groups: list[Any]) -> None:
    raw = await _fetch({**DOCUMENTS, "/apis": {"groups": groups}})

    assert not raw.group_root.complete
    assert raw.group_root.versions("apps") == ("v1",)


async def test_an_unusable_preferred_version_only_loses_its_place() -> None:
    group = {**APPS_GROUP, "preferredVersion": "broken"}
    raw = await _fetch({**DOCUMENTS, "/apis": {"groups": [group]}})

    assert raw.group_root == RootOutcome("/apis", (("apps", "v1"),))


@pytest.mark.parametrize("versions", [["v1", 7], ["v1", "../v2"]])
async def test_an_unreadable_core_version_makes_the_core_root_incomplete(
    versions: list[Any],
) -> None:
    raw = await _fetch({**DOCUMENTS, "/api": {"versions": versions}})

    assert raw.core_root == RootOutcome("/api", (("", "v1"),), complete=False)


async def test_a_core_root_without_a_version_list_is_malformed() -> None:
    raw = await _fetch({**DOCUMENTS, "/api": {"groups": []}})

    assert isinstance(raw.core_root.error, KubeMalformedError)
    assert [meta.plural for meta in project_resources(raw)] == ["pods", "deployments"]


async def test_a_group_root_without_a_group_list_is_incomplete() -> None:
    raw = await _fetch({**DOCUMENTS, "/apis": {}})

    assert raw.group_root == RootOutcome("/apis", complete=False)
    assert [meta.plural for meta in project_resources(raw)] == ["pods"]


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(None, id="not-a-resource"),
        pytest.param({"kind": "Pod", "namespaced": True, "verbs": ["list"]}, id="no-name"),
        pytest.param({"name": "pods", "namespaced": True, "verbs": ["list"]}, id="no-kind"),
        pytest.param({"name": "pods", "kind": "Pod", "verbs": ["list"]}, id="no-scope"),
        pytest.param({"name": "pods", "kind": "Pod", "namespaced": True}, id="no-verbs"),
        pytest.param(
            {"name": "pods", "kind": "Pod", "namespaced": True, "verbs": "list"}, id="verbs-text"
        ),
        pytest.param(
            {"name": "pods", "kind": "Pod", "namespaced": True, "verbs": ["list", 7]},
            id="verb-not-text",
        ),
    ],
)
async def test_an_unreadable_resource_entry_makes_its_version_incomplete(entry: Any) -> None:
    resources = [entry, *DOCUMENTS["/apis/apps/v1"]["resources"]]
    raw = await _fetch({**DOCUMENTS, "/apis/apps/v1": {"resources": resources}})

    assert not raw.group_versions[1].complete
    assert "deployments" in [meta.plural for meta in raw.group_versions[1].metas]


@pytest.mark.parametrize(
    ("path", "index", "projected"),
    [
        pytest.param("/api/v1", 0, ["deployments"], id="core"),
        pytest.param("/apis/apps/v1", 1, ["pods"], id="named"),
    ],
)
async def test_a_version_without_a_resource_list_is_incomplete(
    path: str, index: int, projected: list[str]
) -> None:
    raw = await _fetch({**DOCUMENTS, path: {}})
    outcome = raw.group_versions[index]

    assert (outcome.resources, outcome.complete, outcome.error) == ((), False, None)
    assert [meta.plural for meta in project_resources(raw)] == projected


async def test_a_snapshot_expires_after_five_minutes(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(discovery_snapshot, "monotonic", lambda: clock[0])
    raw = await _fetch()

    clock[0] += discovery_snapshot.DISCOVERY_TTL_SECONDS
    assert not raw.expired()
    clock[0] += 1
    assert raw.expired()


def test_a_snapshot_starts_with_core_v1() -> None:
    now = datetime(2026, 10, 26, tzinfo=UTC)

    with pytest.raises(ValueError, match="starts with core v1"):
        RawDiscovery(1, now, 0.0, RootOutcome("/api"), RootOutcome("/apis"), ())
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_discovery_snapshot.py -q`
Expected: a collection error, `ImportError: cannot import name 'discovery_snapshot' from 'korvid.k8s'`.

- [ ] **Step 3: Share the DNS-1123 pattern**

In `src/korvid/k8s/discovery.py`, add `import re` to the imports, and after `_T = TypeVar("_T")` add:

```python
#: A DNS-1123 subdomain, as API group and object names must be.
DNS1123_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
```

- [ ] **Step 4: Write the snapshot module**

Create `src/korvid/k8s/discovery_snapshot.py`:

```python
"""The raw API discovery of one session (#409, slice S2).

`fetch_discovery` reads `/api`, `/apis` and every group version they
advertise, and keeps each request's outcome: what it served, or the error it
raised. An entry that cannot be read marks its document incomplete, because it
might have named anything; it never makes a group, version or resource absent.
So does a document that leaves out the list it must carry.
The snapshot knows nothing of evidence states: `korvid.core.api_versions`
turns it into observations. `project_resources` is what
`KubeClient.discover_resources()` returns.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from korvid.k8s.discovery import DNS1123_SUBDOMAIN, ResourceMeta
from korvid.k8s.errors import ApiStatusError, KubeClientError, KubeMalformedError, KubeTimeoutError

logger = logging.getLogger(__name__)

#: A snapshot older than this many seconds is read again.
DISCOVERY_TTL_SECONDS = 300.0
#: The core group version korvid always reads, whatever `/api` advertises.
CORE_VERSION = "v1"

_API_VERSION_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")

#: How a discovery request fails: an HTTP status or a client failure.
DiscoveryFailure = ApiStatusError | KubeClientError
RequestJson = Callable[[str], Awaitable[dict[str, Any]]]
_Advertised = tuple[tuple[str, str], ...]


def monotonic() -> float:
    """The clock a snapshot's age is measured on."""
    return time.monotonic()


@dataclass(frozen=True, slots=True)
class RootOutcome:
    """What `/api` or `/apis` advertised, or the error reading it raised.

    `advertised` lists `(group, version)` pairs in advertisement order, each
    group's preferred version first; the core group is `""`. `complete` is
    false when an entry could not be read, or `/apis` had no group list.
    """

    path: str
    advertised: _Advertised = ()
    complete: bool = True
    error: DiscoveryFailure | None = None

    def versions(self, group: str) -> tuple[str, ...]:
        """The versions advertised for `group`, preferred first."""
        return tuple(version for name, version in self.advertised if name == group)


@dataclass(frozen=True, slots=True)
class ServedResource:
    """One resource a group version serves, with its verbs.

    A subresource is named `<plural>/<subresource>`.
    """

    name: str
    verbs: frozenset[str]


@dataclass(frozen=True, slots=True)
class GroupVersionOutcome:
    """What one group version served, or the error reading it raised.

    `metas` are the kinds `discover_resources()` reports from it. `complete`
    is false when a resource entry could not be read, or the document had no
    resource list.
    """

    group: str
    version: str
    resources: tuple[ServedResource, ...] = ()
    metas: tuple[ResourceMeta, ...] = ()
    complete: bool = True
    error: DiscoveryFailure | None = None


@dataclass(frozen=True, slots=True)
class RawDiscovery:
    """One session's discovery: both roots and every group version read.

    `group_versions` starts with core `v1`, then any other core versions, then
    the named groups' versions in advertisement order. `started` is when the
    read began, on `monotonic()`.
    """

    generation: int
    fetched_at: datetime
    started: float
    core_root: RootOutcome
    group_root: RootOutcome
    group_versions: tuple[GroupVersionOutcome, ...]

    def __post_init__(self) -> None:
        first = self.group_versions[0] if self.group_versions else None
        if first is None or (first.group, first.version) != ("", CORE_VERSION):
            raise ValueError("a discovery snapshot starts with core v1")

    def expired(self) -> bool:
        """Whether the snapshot is older than `DISCOVERY_TTL_SECONDS`."""
        return monotonic() - self.started > DISCOVERY_TTL_SECONDS


async def fetch_discovery(
    request_json: RequestJson, *, generation: int, timeout: float, concurrency: int
) -> RawDiscovery:
    """Read both roots and every group version they advertise.

    The roots and core `v1` are requested together, so a stalled server costs
    one deadline rather than three. Every failure is kept, never raised.
    """
    fetched_at, started = datetime.now(UTC), monotonic()
    core_root, core, group_root = await asyncio.gather(
        _read_root(request_json, "/api", timeout, _core_versions),
        _read_group_version(request_json, "", CORE_VERSION, timeout),
        _read_root(request_json, "/apis", timeout, _named_group_versions),
    )
    advertised = (*core_root.advertised, *group_root.advertised)
    pending = [pair for pair in advertised if pair != ("", CORE_VERSION)]
    others = await _read_group_versions(request_json, pending, timeout, concurrency)
    return RawDiscovery(generation, fetched_at, started, core_root, group_root, (core, *others))


def project_resources(snapshot: RawDiscovery) -> list[ResourceMeta]:
    """The list-able kinds discovery found, each from its most preferred version.

    As `discover_resources()` always has, a failed core `v1` or `/apis` raises
    its error, and a failed named group version is skipped.
    """
    core = snapshot.group_versions[0]
    if core.error is not None:
        raise core.error
    if snapshot.group_root.error is not None:
        raise snapshot.group_root.error
    unique: dict[tuple[str, str, bool], ResourceMeta] = {}
    for outcome in snapshot.group_versions:
        for meta in outcome.metas:
            unique.setdefault(meta.identity, meta)
    return list(unique.values())


async def _request(request_json: RequestJson, path: str, timeout: float) -> dict[str, Any]:
    try:
        async with asyncio.timeout(timeout):
            return await request_json(path)
    except TimeoutError:
        raise KubeTimeoutError(
            "Kubernetes API discovery timed out; check cluster connectivity and retry"
        ) from None


async def _read_root(
    request_json: RequestJson,
    path: str,
    timeout: float,
    parse: Callable[[Mapping[str, Any]], tuple[_Advertised, bool]],
) -> RootOutcome:
    try:
        advertised, complete = parse(await _request(request_json, path, timeout))
    except (ApiStatusError, KubeClientError) as error:
        return RootOutcome(path, error=error)
    return RootOutcome(path, advertised, complete)


async def _read_group_version(
    request_json: RequestJson, group: str, version: str, timeout: float
) -> GroupVersionOutcome:
    path = _group_version_path(group, version)
    try:
        entries, listed = _resource_entries(await _request(request_json, path, timeout))
    except (ApiStatusError, KubeClientError) as error:
        if group:
            logger.warning("API discovery skipped %s: %s", path, error)
        return GroupVersionOutcome(group, version, error=error)
    resources, readable = _served_resources(entries)
    metas = _parse_resource_list(entries, group=group, version=version)
    return GroupVersionOutcome(group, version, resources, tuple(metas), listed and readable)


async def _read_group_versions(
    request_json: RequestJson, pairs: Sequence[tuple[str, str]], timeout: float, concurrency: int
) -> list[GroupVersionOutcome]:
    results: list[GroupVersionOutcome | None] = [None] * len(pairs)
    jobs = iter(enumerate(pairs))

    async def worker() -> None:
        for index, (group, version) in jobs:
            results[index] = await _read_group_version(request_json, group, version, timeout)

    await asyncio.gather(*(worker() for _ in range(min(concurrency, len(pairs)))))
    return [result for result in results if result is not None]


def _group_version_path(group: str, version: str) -> str:
    return f"/apis/{group}/{version}" if group else f"/api/{version}"


def _core_versions(document: Mapping[str, Any]) -> tuple[_Advertised, bool]:
    versions = document.get("versions")
    if not isinstance(versions, list):
        raise KubeMalformedError("Kubernetes API discovery returned an invalid core version list")
    usable = [
        version
        for version in versions
        if isinstance(version, str) and _API_VERSION_NAME.fullmatch(version)
    ]
    advertised = tuple(dict.fromkeys(("", version) for version in usable))
    return advertised, len(usable) == len(versions)


def _named_group_versions(document: Mapping[str, Any]) -> tuple[_Advertised, bool]:
    groups = document.get("groups", [])
    if not isinstance(groups, list):
        raise KubeMalformedError("Kubernetes API discovery returned an invalid group list")
    pairs: list[tuple[str, str]] = []
    # Without its group list, `/apis` reads as empty, as discovery always has,
    # but it cannot show that a group is absent.
    complete = "groups" in document
    for group in groups:
        versions, readable = _group_versions(group)
        pairs.extend(versions)
        complete = complete and readable
    return tuple(dict.fromkeys(pairs)), complete


def _group_versions(group: Any) -> tuple[list[tuple[str, str]], bool]:
    """One group entry's versions, preferred first, and whether all were readable.

    An unusable preferred version only loses its place at the front: the
    `versions` list is what says which versions exist.
    """
    if not isinstance(group, Mapping):
        return [], False
    name = group.get("name")
    if not isinstance(name, str) or not DNS1123_SUBDOMAIN.fullmatch(name):
        return [], False
    listed = group.get("versions")
    entries = listed if isinstance(listed, list) else []
    versions = [
        version for entry in entries if (version := _discovery_version(name, entry)) is not None
    ]
    preferred = _discovery_version(name, group.get("preferredVersion"))
    ordered = ([preferred] if preferred is not None else []) + versions
    readable = isinstance(listed, list) and len(versions) == len(entries)
    return [(name, version) for version in ordered], readable


def _discovery_version(name: str, candidate: Any) -> str | None:
    if not isinstance(candidate, Mapping):
        return None
    version = candidate.get("version")
    if not isinstance(version, str) or not _API_VERSION_NAME.fullmatch(version):
        return None
    expected = f"{name}/{version}"
    if candidate.get("groupVersion", expected) != expected:
        return None
    return version


def _resource_entries(document: Mapping[str, Any]) -> tuple[list[Any], bool]:
    """A group version's resource entries, and whether the document listed them.

    A document without its resource list reads as empty, as discovery always
    has, but it cannot show that a resource is absent.
    """
    entries = document.get("resources", [])
    if not isinstance(entries, list):
        raise KubeMalformedError("Kubernetes API discovery returned an invalid resource list")
    return entries, "resources" in document


def _served_resources(entries: list[Any]) -> tuple[tuple[ServedResource, ...], bool]:
    served = tuple(
        resource for entry in entries if (resource := _served_resource(entry)) is not None
    )
    return served, len(served) == len(entries)


def _served_resource(entry: Any) -> ServedResource | None:
    if not isinstance(entry, Mapping):
        return None
    name, verbs = entry.get("name"), entry.get("verbs")
    if not isinstance(name, str) or not name or not isinstance(entry.get("kind"), str):
        return None
    if entry.get("namespaced") is None or not isinstance(verbs, list):
        return None
    if not all(isinstance(verb, str) for verb in verbs):
        return None
    return ServedResource(name, frozenset(verbs))


def _resource_short_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(alias for alias in value if isinstance(alias, str) and alias)


def _parse_resource_list(entries: list[Any], *, group: str, version: str) -> list[ResourceMeta]:
    out = []
    for resource in entries:
        if not isinstance(resource, Mapping):
            continue
        name = resource.get("name")
        kind = resource.get("kind")
        namespaced = resource.get("namespaced")
        verbs = resource.get("verbs", [])
        if not isinstance(name, str) or not isinstance(kind, str) or namespaced is None:
            continue  # malformed entry must not kill discovery
        if "/" in name or not isinstance(verbs, list) or "list" not in verbs:
            continue
        out.append(
            ResourceMeta(
                kind,
                name,
                group,
                version,
                bool(namespaced),
                _resource_short_names(resource.get("shortNames")),
                # list-only aggregated APIs (OLM's packageserver) stay
                # discoverable; the watch source polls them (issue #141).
                watchable="watch" in verbs,
            )
        )
    return out
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_discovery_snapshot.py -q`
Expected: all pass.

Run: `uv run --frozen ruff check src/korvid/k8s tests/k8s/test_discovery_snapshot.py && uv run --frozen mypy src/korvid/k8s/discovery_snapshot.py src/korvid/k8s/discovery.py tests/k8s/test_discovery_snapshot.py`
Expected: no findings.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/k8s/discovery.py src/korvid/k8s/discovery_snapshot.py tests/k8s/test_discovery_snapshot.py
git commit -m "k8s: keep every discovery outcome in a raw snapshot"
```

### Task 2: Sessions and the cached snapshot in `KubeClient`

**Files:**
- Modify: `src/korvid/k8s/client.py`
- Modify: `tests/k8s/test_discovery.py`, `tests/k8s/test_discovery_versions.py`, `tests/k8s/test_error_categories.py`
- Test: `tests/k8s/test_discovery_session.py`

**Interfaces:**
- Consumes: from Task 1, `RawDiscovery`, `fetch_discovery`, `project_resources` and `DNS1123_SUBDOMAIN`.
- Produces on `KubeClient`:
  - `session_generation: int` (property);
  - `async discovery_snapshot() -> RawDiscovery`;
  - `invalidate_discovery() -> None`;
  - `discover_resources()` with an unchanged signature and results.

- [ ] **Step 1: Write the failing session tests**

Create `tests/k8s/test_discovery_session.py`:

```python
"""KubeClient keeps one discovery snapshot per session generation (#409, slice S2)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from kubernetes_asyncio import client as k8s_client

from korvid.k8s import client as client_module
from korvid.k8s import discovery_snapshot
from korvid.k8s.client import KubeClient

DOCUMENTS: dict[str, dict[str, Any]] = {
    "/api": {"versions": ["v1"]},
    "/api/v1": {
        "resources": [{"name": "pods", "kind": "Pod", "namespaced": True, "verbs": ["list"]}]
    },
    "/apis": {"groups": []},
}


async def _answer(path: str) -> dict[str, Any]:
    return DOCUMENTS[path]


class _Api:
    def __init__(self, configuration: Any) -> None:
        self.configuration = configuration

    async def close(self) -> None:
        return None


@pytest.fixture
def offline_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "load_refreshable_kube_config", AsyncMock())
    monkeypatch.setattr(k8s_client, "ApiClient", _Api)
    monkeypatch.setattr(k8s_client, "CoreV1Api", lambda api: api)
    monkeypatch.setattr(k8s_client.Configuration, "set_default", lambda configuration: None)


@pytest.mark.usefixtures("offline_kubeconfig")
async def test_connect_and_every_context_switch_start_a_new_session() -> None:
    client = KubeClient()
    assert client.session_generation == 0

    await client.connect("ctx-a")
    assert client.session_generation == 1
    await client.switch_context("ctx-b")
    assert client.session_generation == 2
    await client.switch_context(None)
    assert client.session_generation == 3


async def test_a_snapshot_is_reused_within_its_session_and_age() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)) as fetch:
        first = await client.discovery_snapshot()
        second = await client.discovery_snapshot()

    assert second is first
    assert fetch.await_count == 3


async def test_concurrent_snapshot_requests_share_one_read() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)) as fetch:
        first, second = await asyncio.gather(
            client.discovery_snapshot(), client.discovery_snapshot()
        )

    assert second is first
    assert fetch.await_count == 3


async def test_an_expired_snapshot_is_read_again(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(discovery_snapshot, "monotonic", lambda: clock[0])
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)):
        first = await client.discovery_snapshot()
        clock[0] += discovery_snapshot.DISCOVERY_TTL_SECONDS + 1
        second = await client.discovery_snapshot()

    assert second is not first


async def test_invalidated_discovery_is_read_again() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)):
        first = await client.discovery_snapshot()
        client.invalidate_discovery()
        second = await client.discovery_snapshot()

    assert second is not first


@pytest.mark.usefixtures("offline_kubeconfig")
async def test_a_snapshot_from_an_earlier_session_is_never_reused() -> None:
    client = KubeClient()
    await client.connect("ctx-a")
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)):
        first = await client.discovery_snapshot()
        await client.switch_context("ctx-b")
        second = await client.discovery_snapshot()

    assert (first.generation, second.generation) == (1, 2)


async def test_a_snapshot_read_across_a_session_change_is_not_kept() -> None:
    client = KubeClient()

    async def answer(path: str) -> dict[str, Any]:
        if path == "/api":
            client._new_session()
        return DOCUMENTS[path]

    with patch.object(client, "_request_json", AsyncMock(side_effect=answer)) as fetch:
        stale = await client.discovery_snapshot()
        fetch.side_effect = _answer
        fresh = await client.discovery_snapshot()

    assert (stale.generation, fresh.generation) == (0, 1)


async def test_discover_resources_reads_afresh_and_keeps_the_snapshot() -> None:
    client = KubeClient()
    with patch.object(client, "_request_json", AsyncMock(side_effect=_answer)) as fetch:
        await client.discovery_snapshot()
        metas = await client.discover_resources()
        snapshot = await client.discovery_snapshot()

    assert [meta.plural for meta in metas] == ["pods"]
    assert fetch.await_count == 6
    assert snapshot.generation == client.session_generation
```

- [ ] **Step 2: Update the existing discovery tests to the new request pattern**

These tests answer discovery requests themselves. Each one now also answers `/api`, and its request count includes it:

- `tests/k8s/test_discovery_versions.py`:
  - add `"/api": {"versions": ["v1"]},` to every `responses` dict;
  - in `test_discovery_bounds_concurrent_version_requests`, answer `/api` with `{"versions": ["v1"]}` before the counted requests;
  - raise both `fetch.await_count == 4` assertions to `5`, and `transport.call_api.await_count == 4` to `5`, with `"/api": json.dumps({"versions": ["v1"]}).encode()` in that test's responses;
  - in `test_bootstrap_discovery_requests_have_a_deadline`, replace the two `fetch.await_args` assertions with `assert (stalled_path,) in [call.args for call in fetch.await_args_list]`. The roots are now requested together, so the stalled one need not be the last.
- `tests/k8s/test_discovery.py`: add `"/api": {"versions": ["v1"]}` to the `responses` dicts of `test_discover_resources_filters_subresources_and_non_watchable` and `test_discover_resources_keeps_list_only_kinds_as_unwatchable`. In `test_discover_resources_skips_group_without_name`, answer `/api` with `{"versions": ["v1"]}` before the unexpected-request assertion.
- `tests/k8s/test_error_categories.py`: in `test_an_invalid_discovery_document_is_malformed`, add `"/api": {"versions": ["v1"]}` to both documents, and `"/apis": {"groups": []}` to the resource-list case.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_discovery_session.py tests/k8s/test_discovery.py tests/k8s/test_discovery_versions.py tests/k8s/test_error_categories.py -q`
Expected:
- the session tests fail, with `AttributeError: 'KubeClient' object has no attribute 'session_generation'` (or `discovery_snapshot`);
- the raised request counts fail (`assert 4 == 5`), because `/api` is not requested yet.

- [ ] **Step 4: Read discovery through the snapshot**

In `src/korvid/k8s/client.py`:

1. Import `DNS1123_SUBDOMAIN` with `PODS_META, ResourceMeta` from `korvid.k8s.discovery`, and add `from korvid.k8s.discovery_snapshot import RawDiscovery, fetch_discovery, project_resources`.
2. Delete `_DNS1123_NAME` and `_API_VERSION_NAME`, and use `DNS1123_SUBDOMAIN.match(name)` in the events field-selector guard.
3. Delete `_request_discovery_json` and the module functions `_discovery_version`, `_group_versions`, `_resource_short_names` and `_parse_resource_list`. Task 1 moved them. Keep `_DISCOVERY_CONCURRENCY` and `_DISCOVERY_TIMEOUT_SECONDS`, because tests patch them on this module.
4. At the end of `KubeClient.__init__`, add:

```python
        #: Changes on every connect and context switch: anything read under an
        #: older generation describes another session (#409).
        self._session_generation = 0
        self._discovery: RawDiscovery | None = None
        self._discovery_lock = asyncio.Lock()
```

5. In `connect()` and `switch_context()`, replace the comment and the two lines that reset `_pod_resize_supported` and `_provider_info` with `self._new_session()`. Then add after `switch_context`:

```python
    def _new_session(self) -> None:
        """Start a session generation: the connection may now reach another
        cluster or principal, so nothing read for the last one carries over."""
        self._session_generation += 1
        self._pod_resize_supported = None
        self._provider_info = None
        self._discovery = None

    @property
    def session_generation(self) -> int:
        """An opaque counter that changes on every connect and context switch."""
        return self._session_generation
```

6. Replace `discover_resources` with:

```python
    async def discover_resources(self) -> list[ResourceMeta]:
        """Return every LIST-able resource from /api/v1 and /apis.

        Kinds without a watch verb (aggregated APIs like OLM's
        packageserver) are included with ``watchable=False`` — the watch
        source keeps them fresh by polling (issue #141).

        Every served version is fetched by a bounded worker pool. Preferred
        versions win per resource identity, regardless of response order.
        Discovery is always read afresh and kept as the session's snapshot.
        """
        snapshot = self._discovery = await self._fetch_discovery()
        return project_resources(snapshot)

    async def discovery_snapshot(self) -> RawDiscovery:
        """This session's raw discovery, read again once it is five minutes old."""
        async with self._discovery_lock:
            snapshot = self._current_discovery()
            if snapshot is None:
                snapshot = self._discovery = await self._fetch_discovery()
            return snapshot

    def invalidate_discovery(self) -> None:
        """Read discovery again next time: an explicit refresh, or a read that
        found discovery stale."""
        self._discovery = None

    async def _fetch_discovery(self) -> RawDiscovery:
        return await fetch_discovery(
            self._request_json,
            generation=self._session_generation,
            timeout=_DISCOVERY_TIMEOUT_SECONDS,
            concurrency=_DISCOVERY_CONCURRENCY,
        )

    def _current_discovery(self) -> RawDiscovery | None:
        # A snapshot read across a session change describes the old session.
        snapshot = self._discovery
        if snapshot is None or snapshot.generation != self._session_generation:
            return None
        return None if snapshot.expired() else snapshot
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/k8s -q`
Expected: all pass.

Run: `uv run --frozen python scripts/check_source_size.py && uv run --frozen mypy src/korvid/k8s tests/k8s/test_discovery_session.py`
Expected: exit 0 with `client.py` under 1,944 lines, and no mypy findings.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/k8s/client.py tests/k8s
git commit -m "k8s: keep one discovery snapshot per session generation"
```

### Task 3: Observe discovery and resolve logical sources

**Files:**
- Modify: `src/korvid/core/capability.py` (`_FAILURE_PRECEDENCE` becomes `FAILURE_PRECEDENCE`)
- Create: `src/korvid/core/api_versions.py`
- Modify: `tests/test_optional_extras.py`
- Test: `tests/core/test_api_versions.py`

**Interfaces:**
- Consumes:
  - from Task 1, `RawDiscovery`, `RootOutcome`, `GroupVersionOutcome`, `ServedResource` and `DiscoveryFailure`;
  - from S1, the vocabulary, `classify`, `ReadKind` and `ReadRequest`.
- Produces, all in `korvid.core.api_versions`:
  - `VersionEvidence(observation, served)`;
  - `DiscoverySnapshot(generation, observed_at, core_root, group_root, versions)`, with `root(group)` and `versions_of(group)`;
  - `observe_discovery(raw) -> DiscoverySnapshot`;
  - `Candidate(group, resource, versions)`, with `.name`;
  - `LogicalSource(key, candidates, policy=DEFAULT_POLICY)`, with `declaration(*, required, role)`;
  - `EVENTS`;
  - `CandidateOutcome(candidate, source, observation=None)`;
  - `Resolution(logical_source, outcomes)`, with `.sources` and `.failure`;
  - `resolve(logical_source, verb, snapshot, *, scope, projection=Projection.OBJECT, name="", expected_uid=None) -> Resolution`.

- [ ] **Step 1: Write the failing test**

Create `tests/core/test_api_versions.py`:

```python
"""API version resolution from discovery, never from server versions (#409, S2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.api_versions import (
    EVENTS,
    Candidate,
    CandidateOutcome,
    DiscoverySnapshot,
    LogicalSource,
    Resolution,
    observe_discovery,
    resolve,
)
from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    EvidenceState,
    Projection,
    Role,
    SourceScope,
    Verb,
)
from korvid.k8s.discovery_snapshot import (
    DiscoveryFailure,
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
    fetch_discovery,
)
from korvid.k8s.errors import ApiStatusError, KubeMalformedError, KubeTimeoutError

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
FORBIDDEN = ApiStatusError(403, "Forbidden")
SERVER_ERROR = ApiStatusError(503, "Service Unavailable")

WIDGETS = LogicalSource("widgets", (Candidate("example.io", "widgets", ("v1", "v1beta1")),))
GADGETS = LogicalSource("gadgets", (Candidate("example.io", "gadgets", ("v1",)),))
CONFIGMAPS = LogicalSource("configmaps", (Candidate("", "configmaps", ("v1",)),))
SECRETS = LogicalSource("secrets", (Candidate("", "secrets", ("v1",)),))


def _served(name: str, *verbs: str) -> ServedResource:
    return ServedResource(name, frozenset(verbs or ("get", "list", "watch")))


def _version(
    group: str,
    version: str,
    *resources: ServedResource,
    complete: bool = True,
    error: DiscoveryFailure | None = None,
) -> GroupVersionOutcome:
    return GroupVersionOutcome(group, version, resources, complete=complete, error=error)


CORE = _version("", "v1", _served("pods"), _served("configmaps"), _served("events"))


def _snapshot(
    *named: GroupVersionOutcome,
    core: GroupVersionOutcome = CORE,
    core_root: RootOutcome | None = None,
    group_root: RootOutcome | None = None,
) -> DiscoverySnapshot:
    advertised = tuple(dict.fromkeys((outcome.group, outcome.version) for outcome in named))
    raw = RawDiscovery(
        7,
        NOW,
        0.0,
        core_root or RootOutcome("/api", (("", "v1"),)),
        group_root or RootOutcome("/apis", advertised),
        (core, *named),
    )
    return observe_discovery(raw)


def _resolve(
    source: LogicalSource, snapshot: DiscoverySnapshot, verb: Verb = Verb.LIST, **target: Any
) -> Resolution:
    return resolve(source, verb, snapshot, scope=TEAM, **target)


def _failure(resolution: Resolution) -> tuple[EvidenceState, Basis]:
    (outcome,) = resolution.outcomes
    assert outcome.observation is not None
    return outcome.observation.state, outcome.observation.basis


def _versions(resolution: Resolution) -> list[tuple[str, str | None]]:
    return [(source.group, source.version) for source in resolution.sources]


def test_every_endpoint_read_is_observed_at_its_own_source() -> None:
    snapshot = _snapshot(_version("apps", "v1", _served("deployments")))
    versions = snapshot.versions

    assert snapshot.generation == 7
    assert snapshot.core_root.source == DiscoverySource.core_root()
    assert snapshot.group_root.source == DiscoverySource.group_root()
    assert {key: evidence.observation.source for key, evidence in versions.items()} == {
        ("", "v1"): DiscoverySource.group_version("", "v1"),
        ("apps", "v1"): DiscoverySource.group_version("apps", "v1"),
    }
    observations = [
        snapshot.core_root,
        snapshot.group_root,
        *(evidence.observation for evidence in versions.values()),
    ]
    assert {(item.state, item.basis, item.observed_at) for item in observations} == {
        (EvidenceState.AVAILABLE, Basis.OBSERVED, NOW)
    }


def test_each_document_counts_the_entries_it_returned() -> None:
    snapshot = _snapshot(
        _version("apps", "v1", _served("deployments"), _served("replicasets")),
        _version("batch", "v1", _served("jobs")),
    )
    versions = snapshot.versions

    assert (snapshot.core_root.items, snapshot.group_root.items) == (1, 2)
    assert {key: evidence.observation.items for key, evidence in versions.items()} == {
        ("", "v1"): 3,
        ("apps", "v1"): 2,
        ("batch", "v1"): 1,
    }


@pytest.mark.parametrize(
    ("error", "state", "basis"),
    [
        (ApiStatusError(401, "Unauthorized"), EvidenceState.DENIED, Basis.UNAUTHENTICATED),
        (FORBIDDEN, EvidenceState.DENIED, Basis.DISCOVERY_FORBIDDEN),
        (ApiStatusError(429, "Too Many Requests"), EvidenceState.UNAVAILABLE, Basis.THROTTLED),
        (SERVER_ERROR, EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
        (KubeTimeoutError("timed out"), EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
        (KubeMalformedError("malformed"), EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
    ],
)
def test_a_failed_discovery_request_is_classified(
    error: DiscoveryFailure, state: EvidenceState, basis: Basis
) -> None:
    snapshot = _snapshot(
        _version("apps", "v1", error=error), core_root=RootOutcome("/api", error=error)
    )
    apps = snapshot.versions[("apps", "v1")]

    for observation in (snapshot.core_root, apps.observation):
        assert (observation.state, observation.basis) == (state, basis)
    assert apps.served == {}


def test_a_document_with_an_unreadable_entry_is_partial() -> None:
    snapshot = _snapshot(
        _version("apps", "v1", _served("deployments"), complete=False),
        group_root=RootOutcome("/apis", (("apps", "v1"),), complete=False),
    )

    for observation in (snapshot.group_root, snapshot.versions[("apps", "v1")].observation):
        assert (observation.state, observation.basis, observation.items) == (
            EvidenceState.PARTIAL,
            Basis.MALFORMED,
            1,
        )


def test_the_most_preferred_version_is_chosen() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", _served("widgets")),
        _version("example.io", "v1beta1", _served("widgets")),
    )

    assert _resolve(WIDGETS, snapshot).sources == (
        ApiSource("example.io", "widgets", TEAM, "v1", Verb.LIST),
    )


def test_a_non_preferred_version_is_chosen_when_only_it_serves_the_resource() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", _served("gadgets")),
        _version("example.io", "v1beta1", _served("widgets")),
    )

    assert _versions(_resolve(WIDGETS, snapshot)) == [("example.io", "v1beta1")]


def test_a_version_korvid_cannot_parse_is_never_chosen() -> None:
    snapshot = _snapshot(
        _version("example.io", "v2", _served("widgets")),
        _version("example.io", "v1", _served("widgets")),
    )

    assert _versions(_resolve(WIDGETS, snapshot)) == [("example.io", "v1")]


@pytest.mark.parametrize(
    ("served", "basis"),
    [
        pytest.param(
            _version("example.io", "v1", _served("widgets", "get")),
            Basis.OPERATION_UNSUPPORTED,
            id="verb-missing",
        ),
        pytest.param(
            _version("example.io", "v2", _served("widgets")),
            Basis.NO_KNOWN_VERSION,
            id="unknown-version-only",
        ),
        pytest.param(
            _version("example.io", "v1", _served("gadgets")), Basis.NOT_SERVED, id="not-served"
        ),
    ],
)
def test_complete_discovery_proves_a_negative_answer(
    served: GroupVersionOutcome, basis: Basis
) -> None:
    assert _failure(_resolve(WIDGETS, _snapshot(served))) == (EvidenceState.UNSUPPORTED, basis)


def test_an_unadvertised_group_is_not_served() -> None:
    assert _failure(_resolve(WIDGETS, _snapshot())) == (
        EvidenceState.UNSUPPORTED,
        Basis.NOT_SERVED,
    )


@pytest.mark.parametrize(
    ("source", "snapshot", "detail"),
    [
        pytest.param(
            WIDGETS,
            _snapshot(_version("example.io", "v1", _served("widgets", "get"))),
            "example.io/widgets does not support list",
            id="verb-missing",
        ),
        pytest.param(
            WIDGETS,
            _snapshot(_version("example.io", "v2", _served("widgets"))),
            "example.io/widgets is served only in versions korvid cannot read",
            id="unknown-version-only",
        ),
        pytest.param(
            CONFIGMAPS,
            _snapshot(core=_version("", "v1", _served("pods"))),
            "configmaps is not served",
            id="core-not-served",
        ),
        pytest.param(
            WIDGETS,
            _snapshot(_version("example.io", "v1", error=SERVER_ERROR)),
            "discovery of /apis/example.io/v1 is incomplete",
            id="version-failed",
        ),
        pytest.param(
            CONFIGMAPS,
            _snapshot(
                core=_version("", "v1", _served("pods")),
                core_root=RootOutcome("/api", error=FORBIDDEN),
            ),
            "discovery of /api is incomplete",
            id="core-root-failed",
        ),
    ],
)
def test_each_failure_says_what_stopped_the_candidate(
    source: LogicalSource, snapshot: DiscoverySnapshot, detail: str
) -> None:
    (outcome,) = _resolve(source, snapshot).outcomes

    assert outcome.observation is not None
    assert outcome.observation.detail == detail


@pytest.mark.parametrize(
    ("verbs", "verb", "resolves"),
    [
        (("get",), Verb.GET, True),
        (("get",), Verb.LIST, False),
        (("list",), Verb.LIST, True),
        (("list",), Verb.GET, False),
    ],
)
def test_a_resource_resolves_only_for_the_verbs_it_advertises(
    verbs: tuple[str, ...], verb: Verb, resolves: bool
) -> None:
    snapshot = _snapshot(_version("example.io", "v1", _served("widgets", *verbs)))
    name = "app" if verb is Verb.GET else ""

    assert bool(_resolve(WIDGETS, snapshot, verb, name=name).sources) is resolves


def test_a_known_version_resolves_beside_a_failed_sibling() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", error=SERVER_ERROR),
        _version("example.io", "v1beta1", _served("widgets")),
    )

    assert _versions(_resolve(WIDGETS, snapshot)) == [("example.io", "v1beta1")]


@pytest.mark.parametrize(
    "sibling",
    [
        pytest.param(_version("example.io", "v1beta1"), id="not-served"),
        pytest.param(
            _version("example.io", "v1beta1", _served("widgets", "get")), id="verb-missing"
        ),
        pytest.param(_version("example.io", "v2", _served("widgets")), id="unknown-version-only"),
    ],
)
@pytest.mark.parametrize(
    ("error", "state", "basis"),
    [
        (FORBIDDEN, EvidenceState.DENIED, Basis.DISCOVERY_FORBIDDEN),
        (ApiStatusError(401, "Unauthorized"), EvidenceState.DENIED, Basis.UNAUTHENTICATED),
        (SERVER_ERROR, EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED),
        (ApiStatusError(429, "Too Many Requests"), EvidenceState.UNAVAILABLE, Basis.THROTTLED),
    ],
)
def test_a_failed_version_of_the_group_blocks_every_negative_answer(
    sibling: GroupVersionOutcome, error: DiscoveryFailure, state: EvidenceState, basis: Basis
) -> None:
    snapshot = _snapshot(_version("example.io", "v1", error=error), sibling)

    assert _failure(_resolve(WIDGETS, snapshot)) == (state, basis)


def test_a_denied_part_of_discovery_outranks_an_unavailable_one() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", error=SERVER_ERROR),
        _version("example.io", "v1beta1", error=FORBIDDEN),
    )

    assert _failure(_resolve(WIDGETS, snapshot)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )


def test_an_unreadable_entry_blocks_negative_answers_but_not_positive_ones() -> None:
    snapshot = _snapshot(_version("example.io", "v1", _served("widgets"), complete=False))

    assert _resolve(WIDGETS, snapshot).sources
    assert _failure(_resolve(GADGETS, snapshot)) == (
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
    )


def test_an_unreadable_root_entry_blocks_every_named_negative_answer() -> None:
    snapshot = _snapshot(
        _version("example.io", "v1", _served("widgets")),
        group_root=RootOutcome("/apis", (("example.io", "v1"),), complete=False),
    )

    assert _resolve(WIDGETS, snapshot).sources
    assert _failure(_resolve(GADGETS, snapshot)) == (
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
    )


EXAMPLE_GROUP = {
    "name": "example.io",
    "preferredVersion": {"groupVersion": "example.io/v1", "version": "v1"},
    "versions": [{"groupVersion": "example.io/v1", "version": "v1"}],
}
WIDGET_ENTRY = {"name": "widgets", "kind": "Widget", "namespaced": True}
#: Discovery a server answered in full: widgets are served, but cannot be listed.
SERVED_DOCUMENTS: dict[str, dict[str, Any]] = {
    "/api": {"versions": ["v1"]},
    "/api/v1": {"resources": []},
    "/apis": {"groups": [EXAMPLE_GROUP]},
    "/apis/example.io/v1": {"resources": [{**WIDGET_ENTRY, "verbs": ["get"]}]},
}


async def _resolve_documents(changes: dict[str, dict[str, Any]]) -> tuple[EvidenceState, Basis]:
    """Read discovery from `SERVED_DOCUMENTS` with `changes`, then resolve widgets."""
    documents = {**SERVED_DOCUMENTS, **changes}

    async def request(path: str) -> dict[str, Any]:
        return documents[path]

    raw = await fetch_discovery(request, generation=1, timeout=1.0, concurrency=2)
    return _failure(_resolve(WIDGETS, observe_discovery(raw)))


async def test_discovery_read_in_full_can_answer_unsupported() -> None:
    assert await _resolve_documents({}) == (
        EvidenceState.UNSUPPORTED,
        Basis.OPERATION_UNSUPPORTED,
    )


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"/apis": {}}, id="no-group-list"),
        pytest.param(
            {"/apis": {"groups": [{**EXAMPLE_GROUP, "versions": None}]}}, id="no-version-list"
        ),
        pytest.param({"/apis/example.io/v1": {}}, id="no-resource-list"),
        pytest.param({"/apis/example.io/v1": {"resources": None}}, id="null-resource-list"),
        pytest.param({"/apis/example.io/v1": {"resources": [WIDGET_ENTRY]}}, id="no-verbs"),
        pytest.param(
            {"/apis/example.io/v1": {"resources": [{**WIDGET_ENTRY, "verbs": None}]}},
            id="null-verbs",
        ),
    ],
)
async def test_discovery_not_read_in_full_never_answers_unsupported(
    changes: dict[str, dict[str, Any]],
) -> None:
    assert await _resolve_documents(changes) == (
        EvidenceState.UNAVAILABLE,
        Basis.DISCOVERY_FAILED,
    )


@pytest.mark.parametrize(
    ("error", "state"),
    [(FORBIDDEN, EvidenceState.DENIED), (SERVER_ERROR, EvidenceState.UNAVAILABLE)],
)
def test_a_failed_core_root_blocks_only_negative_core_answers(
    error: ApiStatusError, state: EvidenceState
) -> None:
    snapshot = _snapshot(core_root=RootOutcome("/api", error=error))

    assert _failure(_resolve(SECRETS, snapshot))[0] is state
    assert _resolve(CONFIGMAPS, snapshot).sources
    assert _failure(_resolve(WIDGETS, snapshot)) == (
        EvidenceState.UNSUPPORTED,
        Basis.NOT_SERVED,
    )


def test_a_failed_group_root_blocks_only_named_groups() -> None:
    snapshot = _snapshot(group_root=RootOutcome("/apis", error=FORBIDDEN))

    assert _failure(_resolve(WIDGETS, snapshot)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )
    assert _failure(_resolve(SECRETS, snapshot)) == (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED)
    assert _resolve(CONFIGMAPS, snapshot).sources


def test_a_refresh_that_drops_the_verb_is_operation_unsupported() -> None:
    before = _snapshot(_version("example.io", "v1", _served("widgets", "get", "list")))
    after = _snapshot(_version("example.io", "v1", _served("widgets", "get")))

    assert _resolve(WIDGETS, before).sources
    assert _failure(_resolve(WIDGETS, after)) == (
        EvidenceState.UNSUPPORTED,
        Basis.OPERATION_UNSUPPORTED,
    )


def test_events_resolve_through_both_groups_in_order() -> None:
    resolution = _resolve(EVENTS, _snapshot(_version("events.k8s.io", "v1", _served("events"))))

    assert _versions(resolution) == [("events.k8s.io", "v1"), ("", "v1")]
    assert resolution.failure is None


def test_events_fall_back_to_core_when_the_events_group_is_absent() -> None:
    resolution = _resolve(EVENTS, _snapshot())
    first = resolution.outcomes[0].observation

    assert _versions(resolution) == [("", "v1")]
    assert resolution.failure is None
    assert first is not None
    assert first.basis is Basis.NOT_SERVED


def test_events_fall_back_to_core_when_the_events_group_is_denied() -> None:
    resolution = _resolve(EVENTS, _snapshot(_version("events.k8s.io", "v1", error=FORBIDDEN)))
    first = resolution.outcomes[0].observation

    assert _versions(resolution) == [("", "v1")]
    assert resolution.failure is None
    assert first is not None
    assert first.state is EvidenceState.DENIED


def test_with_neither_group_serving_events_the_strongest_failure_is_reported() -> None:
    snapshot = _snapshot(
        _version("events.k8s.io", "v1", error=FORBIDDEN), core=_version("", "v1", _served("pods"))
    )
    resolution = _resolve(EVENTS, snapshot)

    assert resolution.sources == ()
    assert resolution.failure is not None
    assert (resolution.failure.state, resolution.failure.basis) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )
    assert [
        outcome.observation.basis for outcome in resolution.outcomes if outcome.observation
    ] == [
        Basis.DISCOVERY_FORBIDDEN,
        Basis.NOT_SERVED,
    ]


def test_a_resolved_source_carries_the_read_target() -> None:
    resolution = resolve(
        CONFIGMAPS,
        Verb.GET,
        _snapshot(),
        scope=TEAM,
        projection=Projection.METADATA,
        name="app",
        expected_uid="uid-1",
    )

    assert resolution.sources == (
        ApiSource(
            "",
            "configmaps",
            TEAM,
            "v1",
            Verb.GET,
            projection=Projection.METADATA,
            name="app",
            expected_uid="uid-1",
        ),
    )


def test_an_unresolved_candidate_is_observed_at_its_unresolved_source() -> None:
    (outcome,) = _resolve(SECRETS, _snapshot()).outcomes

    assert outcome.source == ApiSource("", "secrets", TEAM)
    assert outcome.observation is not None
    assert (outcome.observation.source, outcome.observation.observed_at) == (outcome.source, NOW)


def test_events_can_confirm_but_never_rule_out() -> None:
    assert EVENTS.declaration(required=True, role=Role.CONFIRMS).policy.retention_limited
    with pytest.raises(ValueError, match="never rule one out"):
        EVENTS.declaration(required=True, role=Role.RULES_OUT)


@pytest.mark.parametrize(
    ("resource", "versions"), [("", ("v1",)), ("widgets", ()), ("widgets", ("v1", ""))]
)
def test_a_candidate_names_its_resource_and_versions(
    resource: str, versions: tuple[str, ...]
) -> None:
    with pytest.raises(ValueError, match="candidate names its resource"):
        Candidate("example.io", resource, versions)


@pytest.mark.parametrize(
    ("key", "candidates"), [("", (Candidate("", "pods", ("v1",)),)), ("pods", ())]
)
def test_a_logical_source_has_a_key_and_candidates(
    key: str, candidates: tuple[Candidate, ...]
) -> None:
    with pytest.raises(ValueError, match="key and at least one candidate"):
        LogicalSource(key, candidates)


def test_a_candidate_outcome_is_resolved_exactly_when_nothing_stopped_it() -> None:
    with pytest.raises(ValueError, match="resolved exactly when"):
        CandidateOutcome(Candidate("", "pods", ("v1",)), ApiSource("", "pods", TEAM))
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --frozen pytest -p no:tach tests/core/test_api_versions.py -q`
Expected: a collection error, `ModuleNotFoundError: No module named 'korvid.core.api_versions'`.

- [ ] **Step 3: Make the failure precedence public**

In `src/korvid/core/capability.py`, rename `_FAILURE_PRECEDENCE` to `FAILURE_PRECEDENCE` and reword its comment, since resolution now uses it too:

```python
#: How failures are summarized: as the first of these states that one of them
#: is in, so a failure is never summarized as a missing capability. The
#: baseline summarizes its required sources this way, and API version
#: resolution its candidates.
FAILURE_PRECEDENCE = (
    EvidenceState.DENIED,
    EvidenceState.UNAVAILABLE,
    EvidenceState.UNKNOWN,
    EvidenceState.UNSUPPORTED,
)
```

and use the new name in `EvidenceSet.baseline`:

```python
        return Baseline(next(state for state in FAILURE_PRECEDENCE if state in states), blocking)
```

- [ ] **Step 4: Write the resolution module**

Create `src/korvid/core/api_versions.py`:

```python
"""Choose which API version to read from discovery, never from server versions.

Slice S2 of #409. `observe_discovery` turns the k8s layer's raw discovery into
an observation of every endpoint it read. `resolve` takes a logical source's
candidates in order and, for each, finds the most preferred discovered version
korvid can parse that serves the resource with the needed verb, or says why
there is none. A negative answer needs complete discovery: a failed or partly
unreadable root or group version blocks it, so that a failure is never
reported as a missing capability.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from korvid.core.capability import (
    DEFAULT_POLICY,
    FAILURE_PRECEDENCE,
    ApiSource,
    Basis,
    Declaration,
    DiscoverySource,
    EvidenceState,
    FreshnessPolicy,
    Observation,
    Projection,
    Role,
    SourceScope,
    Verb,
)
from korvid.core.capability_classifier import ReadKind, ReadRequest, classify
from korvid.k8s.discovery_snapshot import (
    DiscoveryFailure,
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
)

#: When several parts of a group's discovery are incomplete, a blocked
#: candidate reports the first of these states that one of them is in.
_BLOCKING_ORDER = (EvidenceState.DENIED, EvidenceState.UNAVAILABLE, EvidenceState.PARTIAL)


@dataclass(frozen=True, slots=True)
class VersionEvidence:
    """What discovery established about one group version.

    Attributes:
        observation: The observation of its discovery endpoint.
        served: Each resource it serves, with that resource's verbs; empty
            unless the endpoint was read.
    """

    observation: Observation
    served: Mapping[str, frozenset[str]]


@dataclass(frozen=True, slots=True, eq=False)
class DiscoverySnapshot:
    """Observations of one session's discovery, for resolution to read.

    Attributes:
        generation: The session generation the raw snapshot belongs to.
        observed_at: When discovery was read, in UTC.
        core_root: The observation of `/api`.
        group_root: The observation of `/apis`.
        versions: Each group version read, keyed by `(group, version)`, each
            group's preferred version first.
    """

    generation: int
    observed_at: datetime
    core_root: Observation
    group_root: Observation
    versions: Mapping[tuple[str, str], VersionEvidence]

    def root(self, group: str) -> Observation:
        """The root that advertises `group`: `/api` for the core group, else `/apis`."""
        return self.group_root if group else self.core_root

    def versions_of(self, group: str) -> tuple[str, ...]:
        """The versions read for `group`, preferred first."""
        return tuple(version for name, version in self.versions if name == group)


def observe_discovery(raw: RawDiscovery) -> DiscoverySnapshot:
    """Observe every endpoint `raw` read: both roots and each group version."""
    at = raw.fetched_at
    versions = {
        (outcome.group, outcome.version): _version_evidence(outcome, at)
        for outcome in raw.group_versions
    }
    return DiscoverySnapshot(
        raw.generation,
        at,
        _root_observation(raw.core_root, DiscoverySource.core_root(), at),
        _root_observation(raw.group_root, DiscoverySource.group_root(), at),
        MappingProxyType(versions),
    )


def _root_observation(outcome: RootOutcome, source: DiscoverySource, at: datetime) -> Observation:
    if outcome.error is not None:
        return _failed(outcome.error, source, at)
    return _read(source, at, len(outcome.advertised), complete=outcome.complete)


def _version_evidence(outcome: GroupVersionOutcome, at: datetime) -> VersionEvidence:
    source = DiscoverySource.group_version(outcome.group, outcome.version)
    if outcome.error is not None:
        return VersionEvidence(_failed(outcome.error, source, at), MappingProxyType({}))
    served = {resource.name: resource.verbs for resource in outcome.resources}
    observation = _read(source, at, len(outcome.resources), complete=outcome.complete)
    return VersionEvidence(observation, MappingProxyType(served))


def _read(source: DiscoverySource, at: datetime, items: int, *, complete: bool) -> Observation:
    if complete:
        return Observation(source, EvidenceState.AVAILABLE, Basis.OBSERVED, at, items=items)
    detail = f"{source.path} had an entry korvid could not read"
    return Observation(
        source, EvidenceState.PARTIAL, Basis.MALFORMED, at, items=items, detail=detail
    )


def _failed(error: DiscoveryFailure, source: DiscoverySource, at: datetime) -> Observation:
    result = classify(error, ReadRequest(ReadKind.DISCOVERY, source))
    return Observation(source, result.state, result.basis, at, detail=result.detail)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One API resource that can supply a logical source.

    Attributes:
        group: The API group; `""` for the core group.
        resource: The resource's plural name.
        versions: The versions korvid can parse; no other version is read.
    """

    group: str
    resource: str
    versions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.resource or not self.versions or "" in self.versions:
            raise ValueError("a candidate names its resource and the versions korvid can parse")

    @property
    def name(self) -> str:
        """The resource's qualified name, such as `events.k8s.io/events`."""
        return f"{self.group}/{self.resource}" if self.group else self.resource


@dataclass(frozen=True, slots=True)
class LogicalSource:
    """One kind of evidence, read from the first of its candidates that serves it."""

    key: str
    candidates: tuple[Candidate, ...]
    policy: FreshnessPolicy = DEFAULT_POLICY

    def __post_init__(self) -> None:
        if not self.key or not self.candidates:
            raise ValueError("a logical source has a key and at least one candidate")

    def declaration(self, *, required: bool, role: Role) -> Declaration:
        """How a diagnostic declares this source, with its freshness policy."""
        return Declaration(self.key, required, role, self.policy)


#: Events, from `events.k8s.io/v1` first and then core `v1`; RBAC treats the
#: two groups as separate resources. Old events expire, so the source can
#: confirm a recorded event but never show that none happened.
EVENTS = LogicalSource(
    "events",
    (Candidate("events.k8s.io", "events", ("v1",)), Candidate("", "events", ("v1",))),
    FreshnessPolicy(retention_limited=True),
)


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    """How one candidate resolved: a source to read, or why there is none.

    `source` is resolved exactly when there is no `observation`; otherwise it
    is the unresolved source the observation is about.
    """

    candidate: Candidate
    source: ApiSource
    observation: Observation | None = None

    def __post_init__(self) -> None:
        if self.source.resolved != (self.observation is None):
            raise ValueError("a candidate is resolved exactly when nothing stopped it")


@dataclass(frozen=True, slots=True)
class Resolution:
    """Every candidate's outcome, in the logical source's order."""

    logical_source: LogicalSource
    outcomes: tuple[CandidateOutcome, ...]

    @property
    def sources(self) -> tuple[ApiSource, ...]:
        """The resolved sources, in the order to read them."""
        return tuple(outcome.source for outcome in self.outcomes if outcome.observation is None)

    @property
    def failure(self) -> Observation | None:
        """When no candidate resolved, the outcome the baseline precedence picks.

        Every candidate's own outcome stays in `outcomes`.
        """
        failures = [o.observation for o in self.outcomes if o.observation is not None]
        if len(failures) < len(self.outcomes):
            return None
        return min(failures, key=lambda failure: FAILURE_PRECEDENCE.index(failure.state))


def resolve(
    logical_source: LogicalSource,
    verb: Verb,
    snapshot: DiscoverySnapshot,
    *,
    scope: SourceScope,
    projection: Projection = Projection.OBJECT,
    name: str = "",
    expected_uid: str | None = None,
) -> Resolution:
    """Resolve each of `logical_source`'s candidates for a `verb` read at `scope`.

    For each candidate, in order, the first of these applies:

    1. The most preferred version korvid can parse that discovery read and
       that serves the resource with `verb` is the resolved source. A broken
       sibling version does not block it.
    2. If the candidate's root, or any version of its group, was not fully
       read, the candidate is `DENIED` or `UNAVAILABLE`: a negative answer
       would need what korvid could not read.
    3. If a version korvid can parse serves it without `verb`, it is
       `UNSUPPORTED`/`OPERATION_UNSUPPORTED`.
    4. If only versions korvid cannot parse serve it, it is
       `UNSUPPORTED`/`NO_KNOWN_VERSION`.
    5. Otherwise it is `UNSUPPORTED`/`NOT_SERVED`.
    """
    outcomes = []
    for candidate in logical_source.candidates:
        unresolved = ApiSource(
            candidate.group,
            candidate.resource,
            scope,
            projection=projection,
            name=name,
            expected_uid=expected_uid,
        )
        outcomes.append(_resolve(candidate, verb, snapshot, unresolved))
    return Resolution(logical_source, tuple(outcomes))


def _resolve(
    candidate: Candidate, verb: Verb, snapshot: DiscoverySnapshot, unresolved: ApiSource
) -> CandidateOutcome:
    version = _known_version(candidate, verb, snapshot)
    if version is not None:
        resolved = dataclasses.replace(unresolved, version=version, verb=verb)
        return CandidateOutcome(candidate, resolved)
    blocking = _incomplete_part(candidate.group, snapshot)
    if blocking is not None:
        return CandidateOutcome(candidate, unresolved, _blocked(unresolved, blocking))
    unsupported = _unsupported(candidate, verb, snapshot, unresolved)
    return CandidateOutcome(candidate, unresolved, unsupported)


def _known_version(candidate: Candidate, verb: Verb, snapshot: DiscoverySnapshot) -> str | None:
    for version in snapshot.versions_of(candidate.group):
        served = snapshot.versions[(candidate.group, version)].served
        if version in candidate.versions and verb in served.get(candidate.resource, ()):
            return version
    return None


def _incomplete_part(group: str, snapshot: DiscoverySnapshot) -> Observation | None:
    parts = [snapshot.root(group)]
    parts += [
        snapshot.versions[(group, version)].observation for version in snapshot.versions_of(group)
    ]
    incomplete = [part for part in parts if part.state is not EvidenceState.AVAILABLE]
    if not incomplete:
        return None
    return min(incomplete, key=lambda part: _BLOCKING_ORDER.index(part.state))


def _blocked(unresolved: ApiSource, part: Observation) -> Observation:
    if part.state is EvidenceState.PARTIAL:
        state, basis = EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED
    else:
        state, basis = part.state, part.basis
    path = part.source.path if isinstance(part.source, DiscoverySource) else "discovery"
    detail = f"discovery of {path} is incomplete"
    return Observation(unresolved, state, basis, part.observed_at, detail=detail)


def _unsupported(
    candidate: Candidate, verb: Verb, snapshot: DiscoverySnapshot, unresolved: ApiSource
) -> Observation:
    serving = [
        version
        for version in snapshot.versions_of(candidate.group)
        if candidate.resource in snapshot.versions[(candidate.group, version)].served
    ]
    if any(version in candidate.versions for version in serving):
        basis, detail = Basis.OPERATION_UNSUPPORTED, f"{candidate.name} does not support {verb}"
    elif serving:
        basis = Basis.NO_KNOWN_VERSION
        detail = f"{candidate.name} is served only in versions korvid cannot read"
    else:
        basis, detail = Basis.NOT_SERVED, f"{candidate.name} is not served"
    at = snapshot.observed_at
    return Observation(unresolved, EvidenceState.UNSUPPORTED, basis, at, detail=detail)
```

- [ ] **Step 5: Cover the new modules in the extras guard**

In `tests/test_optional_extras.py`, add `"korvid.core.api_versions"` and `"korvid.k8s.discovery_snapshot"` to the parameters of `test_base_import_does_not_require_optional_extras`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run --frozen pytest -p no:tach tests/core/test_api_versions.py tests/core/test_capability_baseline.py "tests/test_optional_extras.py::test_base_import_does_not_require_optional_extras" -q`
Expected: all pass.

Run: `uv run --frozen tach check && uv run --frozen mypy src/korvid/core tests/core/test_api_versions.py`
Expected: `All modules validated!` and no mypy findings.

- [ ] **Step 7: Commit**

```bash
git add src/korvid/core tests/core/test_api_versions.py tests/test_optional_extras.py
git commit -m "core: resolve API versions from discovery, candidate by candidate"
```

### Task 4: Run the full gate

- [ ] **Step 1: Run every check that `make check` runs**

```bash
uv run --frozen python scripts/check_source_size.py
uv run --frozen ruff check src/ tests/
uv run --frozen ruff format --check src/ tests/
uv run --frozen mypy
uv run --frozen tach check
uv run --frozen pytest -x -q
```

Expected: every command exits 0, and `client.py` is under its cap. Plain `mypy` checks `tests/` as well as `src/`, which is how `make typecheck` runs it.

- [ ] **Step 2: Check #491's acceptance criteria against tests**

| Criterion | Test |
| --- | --- |
| Preferred and non-preferred versions | `test_the_most_preferred_version_is_chosen`, `test_a_non_preferred_version_is_chosen_when_only_it_serves_the_resource` |
| Unknown versions only, or a missing verb | `test_complete_discovery_proves_a_negative_answer`, `test_a_resource_resolves_only_for_the_verbs_it_advertises` |
| Not served | `test_complete_discovery_proves_a_negative_answer`, `test_an_unadvertised_group_is_not_served` |
| A failed sibling version | `test_a_known_version_resolves_beside_a_failed_sibling`, `test_a_failed_version_of_the_group_blocks_every_negative_answer` |
| `/api` and `/apis` fail independently | `test_a_failed_core_root_blocks_only_negative_core_answers`, `test_a_failed_group_root_blocks_only_named_groups`, `test_a_failed_root_is_kept_without_affecting_the_other` |
| Incomplete discovery never yields `NOT_SERVED`, `NO_KNOWN_VERSION` or `OPERATION_UNSUPPORTED` | `test_a_failed_version_of_the_group_blocks_every_negative_answer`, `test_an_unreadable_entry_blocks_negative_answers_but_not_positive_ones`, `test_an_unreadable_root_entry_blocks_every_named_negative_answer` |
| Events fall through to core `v1` | the four `test_events_…` tests |
| Nothing in `k8s` imports `core` | `tach check` |
| Session generation and snapshot expiry | `tests/k8s/test_discovery_session.py` |

