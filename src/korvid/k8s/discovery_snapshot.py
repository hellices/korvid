"""The raw API discovery of one session (#409, slice S2).

`fetch_discovery` reads `/api`, `/apis` and every group version they
advertise, and keeps each request's outcome: what it served, or the error it
raised. An entry that cannot be read marks its document incomplete, because it
might have named anything; it never makes a group, version or resource absent.
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
    false when an entry could not be read.
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
    is false when a resource entry could not be read.
    """

    group: str
    version: str
    resources: tuple[ServedResource, ...] = ()
    metas: tuple[ResourceMeta, ...] = ()
    complete: bool = True
    error: DiscoveryFailure | None = None

    @property
    def path(self) -> str:
        """The group version's discovery path."""
        return _group_version_path(self.group, self.version)


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
        document = await _request(request_json, path, timeout)
        resources, complete = _served_resources(document)
        metas = _parse_resource_list(document, group=group, version=version)
    except (ApiStatusError, KubeClientError) as error:
        if group:
            logger.warning("API discovery skipped %s: %s", path, error)
        return GroupVersionOutcome(group, version, error=error)
    return GroupVersionOutcome(group, version, resources, tuple(metas), complete)


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
    complete = True
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


def _served_resources(document: Mapping[str, Any]) -> tuple[tuple[ServedResource, ...], bool]:
    entries = document.get("resources", [])
    if not isinstance(entries, list):
        raise KubeMalformedError("Kubernetes API discovery returned an invalid resource list")
    served = tuple(
        resource for entry in entries if (resource := _served_resource(entry)) is not None
    )
    return served, len(served) == len(entries)


def _served_resource(entry: Any) -> ServedResource | None:
    if not isinstance(entry, Mapping):
        return None
    name, verbs = entry.get("name"), entry.get("verbs", [])
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


def _parse_resource_list(
    data: Mapping[str, Any], *, group: str, version: str
) -> list[ResourceMeta]:
    out = []
    resources = data.get("resources", [])
    if not isinstance(resources, list):
        raise KubeMalformedError("Kubernetes API discovery returned an invalid resource list")
    for resource in resources:
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
