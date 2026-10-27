"""Bounded, read-only evidence reads for capability-aware diagnostics (#409, slice S3).

`read_list` and `read_object` read one `ApiSource` that discovery resolved,
and return what they read with one `Observation` of it. Each read is bounded
in pages, bytes and time. It lets cancellation through, and retries nothing
but one 429 whose wait fits its deadline. `korvid.core.capability_classifier`
classifies every failure.

What a diagnostic receives is not what crossed the wire:

- a ConfigMap or Secret is never read as an object, so its data never is;
- its metadata keeps only `PAYLOAD_METADATA_FIELDS`;
- no metadata keeps `managedFields` or the `LAST_APPLIED_ANNOTATION`, which
  embeds the whole object: neither an object's own, nor any it embeds, such
  as a pod template's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from korvid.core.api_versions import Resolution
from korvid.core.capability import (
    FAILURE_PRECEDENCE,
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    RunToken,
    ScopeKind,
    Verb,
)
from korvid.core.capability_classifier import Classification, ReadKind, ReadRequest, classify
from korvid.k8s.errors import ApiStatusError, KubeClientError, KubeMalformedError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

_T = TypeVar("_T")

#: The default budgets are Pulse's: pages of 100 objects, two pages, five
#: seconds and 256 KiB a response. A diagnostic may raise them to the maxima.
LIST_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 2
MAX_PAGES = 10
DEFAULT_DEADLINE = 5.0
MAX_DEADLINE = 15.0
DEFAULT_MAX_BYTES = 256 * 1024
MAX_BYTES = 2 * 1024 * 1024
#: How long to wait before retrying a 429 that sent no usable `Retry-After`.
DEFAULT_RETRY_AFTER = 1.0

#: Resources whose objects carry configuration payloads. They are read as
#: metadata only, and only `PAYLOAD_METADATA_FIELDS` of that is kept: any
#: annotation can carry configuration data.
PAYLOAD_RESOURCES = frozenset({"configmaps", "secrets"})
PAYLOAD_METADATA_FIELDS = (
    "name",
    "namespace",
    "uid",
    "resourceVersion",
    "creationTimestamp",
    "deletionTimestamp",
    "labels",
    "ownerReferences",
)
#: Embeds the whole object as it was last applied, data included.
LAST_APPLIED_ANNOTATION = "kubectl.kubernetes.io/last-applied-configuration"
_DEADLINE = "deadline reached"
_PAST_DEADLINE = Classification(EvidenceState.UNAVAILABLE, Basis.DEADLINE, _DEADLINE)
#: How a read waits out a 429 before retrying it.
_pause = asyncio.sleep


@dataclass(frozen=True, slots=True)
class ListEvidence:
    """The objects a LIST read, and what the read established."""

    items: tuple[dict[str, Any], ...]
    observation: Observation


@dataclass(frozen=True, slots=True)
class ObjectEvidence:
    """The object a GET read, if it read one, and what the read established."""

    item: dict[str, Any] | None
    observation: Observation


def _now() -> datetime:
    return datetime.now(UTC)


async def read_list(
    reader: EvidenceReader,
    source: ApiSource,
    *,
    page_size: int = LIST_PAGE_SIZE,
    max_pages: int = DEFAULT_MAX_PAGES,
    deadline: float = DEFAULT_DEADLINE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> ListEvidence:
    """LIST `source` a page at a time, within its budgets.

    The read is `AVAILABLE` only when the last page carried no continue token:
    a page with no items and a token pending proves nothing. After an accepted
    page, it keeps what it read and is `PARTIAL`, `TRUNCATED` at the page cap,
    `DEADLINE` at the deadline, and with a failure's own basis otherwise.

    Raises:
        ValueError: if `source` was not resolved for `list`, is a ConfigMap or
            Secret read as objects, or a budget is out of range.
    """
    target = _target(source, Verb.LIST)
    _check_count("page_size", page_size, LIST_PAGE_SIZE)
    _check_count("max_pages", max_pages, MAX_PAGES)
    _check_budgets(deadline, max_bytes)
    attempts = _Attempts(_deadline_at(deadline))
    run = _ListRun(reader, source, target, page_size, max_pages, max_bytes, attempts)
    try:
        await run.read()
    except _DeadlineReachedError:
        outcome = run.at_deadline()
    except (ApiStatusError, KubeClientError) as failure:
        request = ReadRequest(ReadKind.LIST, source, continuation=run.pages > 0)
        outcome = classify(failure, request)
        # A failure processed or classified past the deadline proves nothing either.
        if _is_late(attempts.deadline_at):
            outcome = run.at_deadline()
    else:
        outcome = run.finished()
    items = tuple(run.items)
    return ListEvidence(items, _observe(source, outcome, clock(), len(items), token))


async def read_object(
    reader: EvidenceReader,
    source: ApiSource,
    *,
    deadline: float = DEFAULT_DEADLINE,
    max_bytes: int = DEFAULT_MAX_BYTES,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> ObjectEvidence:
    """GET the object `source` names, and record the UID it has.

    A `NotFound` that names the object is `AVAILABLE`/`OBJECT_ABSENT`. An
    object whose UID differs from the one `source` expects is still returned,
    and its observation is `recreated`.

    Raises:
        ValueError: if `source` was not resolved for `get`, is a ConfigMap or
            Secret read as an object, or a budget is out of range.
    """
    target = _target(source, Verb.GET)
    _check_budgets(deadline, max_bytes)
    attempts = _Attempts(_deadline_at(deadline))
    try:
        document = await _call(
            attempts,
            lambda: reader.get_evidence_object(target, source.name, max_bytes=max_bytes),
        )
        item, uid = _named_object(source, document)
        attempts.check()
    except _DeadlineReachedError:
        outcome = _PAST_DEADLINE
    except (ApiStatusError, KubeClientError) as failure:
        outcome = classify(failure, ReadRequest(ReadKind.GET, source))
        if _is_late(attempts.deadline_at):
            outcome = _PAST_DEADLINE
    else:
        observed = Classification(EvidenceState.AVAILABLE, Basis.OBSERVED, "")
        return ObjectEvidence(item, _observe(source, observed, clock(), 1, token, uid))
    return ObjectEvidence(None, _observe(source, outcome, clock(), 0, token))


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


class _DeadlineReachedError(Exception):
    """The read's own deadline passed before it finished."""


@dataclass(slots=True)
class _Attempts:
    """When a read must finish, and whether it has spent its one 429 retry."""

    deadline_at: float
    retried: bool = False

    def retry_delay(self, failure: ApiStatusError) -> float | None:
        """How long to wait before retrying `failure`, or `None` not to retry it."""
        if failure.status != 429 or self.retried:
            return None
        seconds = failure.retry_after_seconds
        delay = DEFAULT_RETRY_AFTER if seconds is None else seconds
        if asyncio.get_running_loop().time() + delay >= self.deadline_at:
            return None
        return delay

    def check(self) -> None:
        """Refuse a result read or processed after the deadline.

        A timeout interrupts a request only where it waits, so a result can
        still be ready after the deadline.
        """
        _refuse_late(self.deadline_at)


@dataclass(slots=True)
class _ListRun:
    """One LIST in progress: the pages it accepted, and where it stopped."""

    reader: EvidenceReader
    source: ApiSource
    target: EvidenceTarget
    page_size: int
    max_pages: int
    max_bytes: int
    attempts: _Attempts
    items: list[dict[str, Any]] = field(default_factory=list)
    pages: int = 0
    continuation: str = ""

    async def read(self) -> None:
        """Accept pages until the last one, the page cap, or a failure."""
        seen: set[str] = set()
        while True:
            page = await _call(self.attempts, self._request)
            if page.continuation in seen:
                raise KubeMalformedError("Kubernetes API repeated a continue token")
            items = [_reduce_item(self.source, item) for item in page.items]
            self.attempts.check()
            self.items.extend(items)
            self.pages += 1
            self.continuation = page.continuation
            if not page.continuation or self.pages == self.max_pages:
                return
            seen.add(page.continuation)

    def finished(self) -> Classification:
        """The outcome of a read that stopped without a failure."""
        if self.continuation:
            detail = f"stopped after {self.pages} pages with more pending"
            return Classification(EvidenceState.PARTIAL, Basis.TRUNCATED, detail)
        return Classification(EvidenceState.AVAILABLE, Basis.OBSERVED, "")

    def at_deadline(self) -> Classification:
        """The outcome of a read its deadline stopped."""
        state = EvidenceState.PARTIAL if self.pages else EvidenceState.UNAVAILABLE
        return Classification(state, Basis.DEADLINE, _DEADLINE)

    def _request(self) -> Awaitable[EvidencePage]:
        return self.reader.list_evidence_page(
            self.target,
            limit=self.page_size,
            continuation=self.continuation,
            max_bytes=self.max_bytes,
        )


def _deadline_at(deadline: float) -> float:
    return asyncio.get_running_loop().time() + deadline


async def _call(attempts: _Attempts, request: Callable[[], Awaitable[_T]]) -> _T:
    """Make `request` before the deadline, retrying one 429 whose wait fits in it.

    No request is made once the deadline has passed, not even a retry whose
    wait ended later than asked.
    """
    while True:
        _refuse_late(attempts.deadline_at)
        try:
            return await _before_deadline(attempts.deadline_at, request())
        except ApiStatusError as failure:
            delay = attempts.retry_delay(failure)
            if delay is None:
                raise
            attempts.retried = True
            await _pause(delay)


async def _before_deadline(deadline_at: float, request: Awaitable[_T]) -> _T:
    timeout = asyncio.timeout_at(deadline_at)
    try:
        async with timeout:
            return await request
    except TimeoutError:
        if timeout.expired():
            raise _DeadlineReachedError from None
        raise
    except (ApiStatusError, KubeClientError):
        # A failure reported after the deadline proves no more than a late
        # success would: a late `NotFound` must not establish absence.
        _refuse_late(deadline_at)
        raise


def _is_late(deadline_at: float) -> bool:
    return asyncio.get_running_loop().time() >= deadline_at


def _refuse_late(deadline_at: float) -> None:
    if _is_late(deadline_at):
        raise _DeadlineReachedError


def _target(source: ApiSource, verb: Verb) -> EvidenceTarget:
    """Check that `source` may be read with `verb`, and address it."""
    if source.version is None or source.verb is not verb:
        raise ValueError(f"this read needs a source discovery resolved for {verb}")
    if source.subresource:
        raise ValueError("evidence reads do not read subresources")
    metadata_only = source.projection is Projection.METADATA
    if source.resource in PAYLOAD_RESOURCES and not metadata_only:
        raise ValueError(f"{source.resource} are read as metadata only, never as objects")
    namespace = _namespace(source)
    return EvidenceTarget(source.group, source.version, source.resource, namespace, metadata_only)


def _namespace(source: ApiSource) -> str | None:
    if source.scope.kind is ScopeKind.CLUSTER:
        return None
    if source.scope.kind is ScopeKind.NAMESPACE:
        return source.scope.name
    raise ValueError("an API read is cluster- or namespace-scoped")


def _check_count(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} is a whole number from 1 to {maximum}")


def _check_budgets(deadline: float, max_bytes: int) -> None:
    _check_count("max_bytes", max_bytes, MAX_BYTES)
    is_number = isinstance(deadline, int | float) and not isinstance(deadline, bool)
    if not is_number or not 0 < deadline <= MAX_DEADLINE:
        raise ValueError(f"deadline is more than 0 and at most {MAX_DEADLINE:g} seconds")


def _reduce_item(source: ApiSource, item: Mapping[str, Any]) -> dict[str, Any]:
    """What a diagnostic may receive of `item`: only reduced metadata, for a metadata read."""
    metadata = item.get("metadata")
    if not isinstance(metadata, Mapping):
        raise KubeMalformedError("Kubernetes API returned an object without metadata")
    scope = source.scope
    if scope.kind is ScopeKind.NAMESPACE and metadata.get("namespace") != scope.name:
        raise KubeMalformedError("Kubernetes API returned an object from another namespace")
    reduced = _reduce_metadata(source.resource, metadata)
    if source.projection is Projection.METADATA:
        return {"metadata": reduced}
    try:
        scrubbed = _scrub(item)
    except RecursionError:
        raise KubeMalformedError("Kubernetes API returned an object nested too deeply") from None
    return {**scrubbed, "metadata": reduced}


def _scrub(value: Any) -> Any:
    """`value` with every metadata it embeds reduced, as any resource's is."""
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if not isinstance(value, Mapping):
        return value
    scrubbed = {key: _scrub(item) for key, item in value.items()}
    metadata = scrubbed.get("metadata")
    if isinstance(metadata, Mapping):
        scrubbed["metadata"] = _reduce_metadata("", metadata)
    return scrubbed


def _reduce_metadata(resource: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    if resource in PAYLOAD_RESOURCES:
        return {key: metadata[key] for key in PAYLOAD_METADATA_FIELDS if key in metadata}
    reduced = {key: value for key, value in metadata.items() if key != "managedFields"}
    annotations = reduced.pop("annotations", None)
    if isinstance(annotations, Mapping):
        kept = {key: value for key, value in annotations.items() if key != LAST_APPLIED_ANNOTATION}
        if kept:
            reduced["annotations"] = kept
    return reduced


def _named_object(source: ApiSource, document: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """The reduced object and its UID, if it is the object `source` names."""
    item = _reduce_item(source, document)
    metadata = item["metadata"]
    uid = metadata.get("uid")
    if not isinstance(uid, str) or not uid:
        raise KubeMalformedError("Kubernetes API returned an object without a UID")
    if metadata.get("name") != source.name:
        raise KubeMalformedError("Kubernetes API returned another object")
    return item, uid


def _observe(
    source: ApiSource,
    outcome: Classification,
    at: datetime,
    items: int,
    token: RunToken | None,
    observed_uid: str | None = None,
) -> Observation:
    return Observation(
        source,
        outcome.state,
        outcome.basis,
        at,
        items=items,
        observed_uid=observed_uid,
        detail=outcome.detail,
        token=token,
    )
