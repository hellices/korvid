"""Bounded, read-only evidence reads (#409, slice S3)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core import evidence_reads, pulse_collector
from korvid.core.capability import (
    DEFAULT_POLICY,
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    RunToken,
    SourceScope,
    Verb,
    conclusive,
)
from korvid.core.capability_classifier import STATUS_BODY_LIMIT
from korvid.core.evidence_reads import (
    DEFAULT_MAX_BYTES,
    LAST_APPLIED_ANNOTATION,
    LIST_PAGE_SIZE,
    read_list,
    read_object,
)
from korvid.k8s.bounded_transport import ERROR_BODY_LIMIT
from korvid.k8s.errors import ApiStatusError, KubeMalformedError, KubeTooLargeError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
PODS = ApiSource("", "pods", TEAM, version="v1", verb=Verb.LIST)
POD = ApiSource("", "pods", TEAM, version="v1", verb=Verb.GET, name="web-0")
METADATA_PODS = dataclasses.replace(PODS, projection=Projection.METADATA)
METADATA_POD = dataclasses.replace(POD, projection=Projection.METADATA)
POD_TARGET = EvidenceTarget("", "v1", "pods", "team-a")
#: Stands for a request the server never answers.
STALL = object()

PAYLOAD_METADATA = {
    "name": "settings",
    "namespace": "team-a",
    "uid": "uid-settings",
    "resourceVersion": "42",
    "creationTimestamp": "2026-10-26T08:00:00Z",
    "deletionTimestamp": "2026-10-26T08:30:00Z",
    "labels": {"app": "web"},
    "ownerReferences": [{"kind": "Deployment", "name": "web"}],
    "annotations": {
        LAST_APPLIED_ANNOTATION: '{"data": {"password": "hunter2"}}',
        "note": "hunter2",
    },
    "managedFields": [{"manager": "kubectl", "fieldsV1": {"f:data": {"f:password": {}}}}],
    "finalizers": ["example.io/keep"],
    "generateName": "settings-",
}
KEPT_PAYLOAD_METADATA = {
    key: PAYLOAD_METADATA[key]
    for key in (
        "name",
        "namespace",
        "uid",
        "resourceVersion",
        "creationTimestamp",
        "deletionTimestamp",
        "labels",
        "ownerReferences",
    )
}
PAYLOAD_ITEM = {"metadata": PAYLOAD_METADATA, "data": {"password": "hunter2"}, "type": "Opaque"}


def _clock() -> datetime:
    return NOW


def _pod(name: str, uid: str = "") -> dict[str, Any]:
    return {"metadata": {"name": name, "uid": uid or f"uid-{name}"}, "spec": {"nodeName": "n1"}}


def _status(
    code: int, reason: str, *, name: str = "", kind: str = "", retry_after: float | None = None
) -> ApiStatusError:
    details = {"name": name, "group": "", "kind": kind}
    body = json.dumps({"kind": "Status", "reason": reason, "details": details})
    return ApiStatusError(code, reason, body, retry_after_seconds=retry_after)


def _state(observation: Observation) -> tuple[EvidenceState, Basis]:
    return observation.state, observation.basis


def _names(items: tuple[dict[str, Any], ...]) -> list[str]:
    return [item["metadata"]["name"] for item in items]


@dataclasses.dataclass(frozen=True)
class Late:
    """An answer that arrives `seconds` later, past any deadline the tests set."""

    result: object
    seconds: float = 0.1


class FakeReader(EvidenceReader):
    """Answers each request with the next scripted outcome, and records the requests."""

    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes)
        self.lists: list[tuple[EvidenceTarget, int, str, int]] = []
        self.gets: list[tuple[EvidenceTarget, str, int]] = []

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append((target, limit, continuation, max_bytes))
        page = await self._next()
        assert isinstance(page, EvidencePage)
        return page

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name, max_bytes))
        document = await self._next()
        assert isinstance(document, dict)
        return document

    async def _next(self) -> object:
        outcome = self.outcomes.pop(0)
        if outcome is STALL:
            await asyncio.Event().wait()
        if isinstance(outcome, Late):
            # Blocks without yielding, so no timeout can interrupt it.
            time.sleep(outcome.seconds)
            outcome = outcome.result
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def test_the_default_budgets_are_pulses() -> None:
    assert (
        evidence_reads.LIST_PAGE_SIZE,
        evidence_reads.DEFAULT_MAX_PAGES,
        evidence_reads.DEFAULT_DEADLINE,
        evidence_reads.DEFAULT_MAX_BYTES,
    ) == (
        pulse_collector.PAGE_SIZE,
        pulse_collector.MAX_PAGES,
        pulse_collector.SOURCE_DEADLINE,
        pulse_collector.PAGE_BYTES,
    )


def test_the_classifier_reads_no_more_of_a_status_than_a_reader_keeps() -> None:
    assert STATUS_BODY_LIMIT == ERROR_BODY_LIMIT


async def test_a_list_that_ends_is_available() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), EvidencePage((_pod("b"),)))

    result = await read_list(reader, PODS, clock=_clock)

    assert _names(result.items) == ["a", "b"]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert (result.observation.items, result.observation.observed_at) == (2, NOW)
    assert result.observation.source == PODS
    assert reader.lists == [
        (POD_TARGET, LIST_PAGE_SIZE, "", DEFAULT_MAX_BYTES),
        (POD_TARGET, LIST_PAGE_SIZE, "next", DEFAULT_MAX_BYTES),
    ]


async def test_an_empty_collection_is_available() -> None:
    result = await read_list(FakeReader(EvidencePage(())), PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert result.items == ()


async def test_a_cluster_wide_read_names_no_namespace() -> None:
    nodes = ApiSource("", "nodes", SourceScope.cluster(), version="v1", verb=Verb.LIST)
    reader = FakeReader(EvidencePage(()))

    await read_list(reader, nodes, page_size=10, max_bytes=4096)

    assert reader.lists == [(EvidenceTarget("", "v1", "nodes"), 10, "", 4096)]


@pytest.mark.parametrize(
    "items", [pytest.param((_pod("a"),), id="with-items"), pytest.param((), id="empty-pages")]
)
async def test_the_page_cap_with_a_token_pending_is_truncated(
    items: tuple[dict[str, Any], ...],
) -> None:
    reader = FakeReader(EvidencePage(items, "p2"), EvidencePage(items, "p3"))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
    assert result.observation.items == 2 * len(items)
    assert len(reader.lists) == 2


async def test_more_pages_are_read_up_to_a_raised_cap() -> None:
    pages = [EvidencePage((_pod(str(index)),), f"p{index + 1}") for index in range(9)]
    reader = FakeReader(*pages, EvidencePage((_pod("9"),)))

    result = await read_list(reader, PODS, max_pages=10, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert len(result.items) == 10


@pytest.mark.parametrize(
    ("failure", "basis"),
    [
        pytest.param(_status(405, "MethodNotAllowed"), Basis.DISCOVERY_STALE, id="405"),
        pytest.param(_status(410, "Expired"), Basis.CONTINUATION_EXPIRED, id="expired"),
        pytest.param(KubeTooLargeError("byte cap"), Basis.TOO_LARGE, id="too-large"),
        pytest.param(ApiStatusError(503, "Service Unavailable"), Basis.SERVER_ERROR, id="503"),
        pytest.param(ApiStatusError(403, "Forbidden"), Basis.FORBIDDEN, id="403"),
    ],
)
@pytest.mark.parametrize(
    "items", [pytest.param((_pod("a"),), id="with-items"), pytest.param((), id="empty-page")]
)
async def test_a_failure_after_an_accepted_page_keeps_it(
    failure: Exception, basis: Basis, items: tuple[dict[str, Any], ...]
) -> None:
    reader = FakeReader(EvidencePage(items, "next"), failure)

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, basis)
    assert len(result.items) == len(items)


async def test_a_refused_metadata_continuation_keeps_the_pages() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), ApiStatusError(406, "Not Acceptable"))

    result = await read_list(reader, METADATA_PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.PROJECTION_UNSUPPORTED)
    assert _names(result.items) == ["a"]


@pytest.mark.parametrize(
    ("failure", "state", "basis"),
    [
        pytest.param(
            ApiStatusError(401, "Unauthorized"),
            EvidenceState.DENIED,
            Basis.UNAUTHENTICATED,
            id="401",
        ),
        pytest.param(
            ApiStatusError(403, "Forbidden"), EvidenceState.DENIED, Basis.FORBIDDEN, id="403"
        ),
        pytest.param(
            _status(404, "NotFound"), EvidenceState.UNAVAILABLE, Basis.DISCOVERY_STALE, id="404"
        ),
        pytest.param(
            KubeTooLargeError("byte cap"),
            EvidenceState.UNAVAILABLE,
            Basis.TOO_LARGE,
            id="too-large",
        ),
        pytest.param(
            KubeMalformedError("bad"), EvidenceState.UNAVAILABLE, Basis.MALFORMED, id="malformed"
        ),
    ],
)
async def test_a_failure_before_any_page_reads_nothing(
    failure: Exception, state: EvidenceState, basis: Basis
) -> None:
    result = await read_list(FakeReader(failure), PODS, clock=_clock)

    assert _state(result.observation) == (state, basis)
    assert (result.items, result.observation.items) == ((), 0)


async def test_a_failure_detail_never_carries_the_response_body() -> None:
    failure = ApiStatusError(403, "Forbidden", '{"message": "token secret-token denied"}')

    result = await read_list(FakeReader(failure), PODS, clock=_clock)

    assert result.observation.detail == "HTTP 403"


async def test_a_list_whose_metadata_projection_is_refused_is_unsupported() -> None:
    reader = FakeReader(ApiStatusError(406, "Not Acceptable"))

    result = await read_list(reader, METADATA_PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNSUPPORTED, Basis.PROJECTION_UNSUPPORTED)
    assert [request[0] for request in reader.lists] == [
        dataclasses.replace(POD_TARGET, metadata_only=True)
    ]


async def test_an_object_whose_metadata_projection_is_refused_is_unsupported() -> None:
    reader = FakeReader(ApiStatusError(406, "Not Acceptable"))

    result = await read_object(reader, METADATA_POD, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNSUPPORTED, Basis.PROJECTION_UNSUPPORTED)
    assert [request[0] for request in reader.gets] == [
        dataclasses.replace(POD_TARGET, metadata_only=True)
    ]


async def test_a_429_is_retried_once_within_the_deadline() -> None:
    throttled = _status(429, "TooManyRequests", retry_after=0.0)
    reader = FakeReader(throttled, EvidencePage((_pod("a"),)))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert len(reader.lists) == 2


async def test_a_second_429_is_throttled() -> None:
    reader = FakeReader(
        _status(429, "TooManyRequests", retry_after=0.0),
        _status(429, "TooManyRequests", retry_after=0.0),
    )

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.THROTTLED)
    assert len(reader.lists) == 2


async def test_a_429_whose_wait_outlasts_the_deadline_is_not_retried() -> None:
    reader = FakeReader(_status(429, "TooManyRequests", retry_after=60.0))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.THROTTLED)
    assert len(reader.lists) == 1


async def test_a_read_retries_only_one_429() -> None:
    reader = FakeReader(
        EvidencePage((_pod("a"),), "p2"),
        _status(429, "TooManyRequests", retry_after=0.0),
        EvidencePage((_pod("b"),), "p3"),
        _status(429, "TooManyRequests", retry_after=0.0),
    )

    result = await read_list(reader, PODS, max_pages=3, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.THROTTLED)
    assert _names(result.items) == ["a", "b"]
    assert len(reader.lists) == 4


@pytest.mark.parametrize(("default", "requests"), [(0.0, 2), (60.0, 1)])
async def test_a_429_without_retry_after_waits_the_default(
    monkeypatch: pytest.MonkeyPatch, default: float, requests: int
) -> None:
    monkeypatch.setattr(evidence_reads, "DEFAULT_RETRY_AFTER", default)
    reader = FakeReader(_status(429, "TooManyRequests"), EvidencePage(()))

    await read_list(reader, PODS, clock=_clock)

    assert len(reader.lists) == requests


async def test_the_deadline_before_any_page_reads_nothing() -> None:
    result = await read_list(FakeReader(STALL), PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)
    assert result.items == ()


async def test_the_deadline_after_a_page_keeps_it() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), STALL)

    result = await read_list(reader, PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.DEADLINE)
    assert _names(result.items) == ["a"]


async def test_a_page_that_arrives_after_the_deadline_is_not_accepted() -> None:
    reader = FakeReader(Late(EvidencePage((_pod("a"),), "next")), EvidencePage((_pod("b"),)))

    result = await read_list(reader, PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)
    assert result.items == ()
    assert len(reader.lists) == 1


async def test_a_later_page_that_arrives_after_the_deadline_is_not_accepted() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "next"), Late(EvidencePage((_pod("b"),))))

    result = await read_list(reader, PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.DEADLINE)
    assert _names(result.items) == ["a"]


async def test_an_object_that_arrives_after_the_deadline_is_not_accepted() -> None:
    result = await read_object(FakeReader(Late(_pod("web-0"))), POD, deadline=0.05, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_an_absence_reported_after_the_deadline_proves_nothing() -> None:
    late_absence = Late(_status(404, "NotFound", name="web-0", kind="pods"))

    result = await read_object(FakeReader(late_absence), POD, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_a_failure_reported_after_the_deadline_is_the_deadline() -> None:
    late_refusal = Late(ApiStatusError(403, "Forbidden"))

    result = await read_list(FakeReader(late_refusal), PODS, deadline=0.05, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_cancelling_a_read_cancels_it() -> None:
    reader = FakeReader(STALL)
    task = asyncio.create_task(read_list(reader, PODS))
    await asyncio.sleep(0)
    assert len(reader.lists) == 1

    task.cancel()

    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await task


async def test_a_cancelled_request_is_never_classified() -> None:
    with pytest.raises(asyncio.CancelledError, match=r"^$"):
        await read_list(FakeReader(asyncio.CancelledError()), PODS)


async def test_a_timeout_that_is_not_the_deadline_is_never_classified() -> None:
    with pytest.raises(TimeoutError, match="reader timed out"):
        await read_list(FakeReader(TimeoutError("reader timed out")), PODS)


async def test_a_repeated_continue_token_is_malformed() -> None:
    reader = FakeReader(EvidencePage((_pod("a"),), "same"), EvidencePage((_pod("b"),), "same"))

    result = await read_list(reader, PODS, max_pages=3, clock=_clock)

    assert _state(result.observation) == (EvidenceState.PARTIAL, Basis.MALFORMED)
    assert _names(result.items) == ["a"]


async def test_a_page_with_an_item_without_metadata_is_never_accepted() -> None:
    reader = FakeReader(EvidencePage((_pod("a"), {"spec": {}})))

    result = await read_list(reader, PODS, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.MALFORMED)
    assert result.items == ()


async def test_a_metadata_read_returns_only_metadata() -> None:
    result = await read_list(FakeReader(EvidencePage((_pod("a"),))), METADATA_PODS, clock=_clock)

    assert result.items == ({"metadata": {"name": "a", "uid": "uid-a"}},)


@pytest.mark.parametrize("resource", ["configmaps", "secrets"])
async def test_no_configuration_payload_reaches_a_diagnostic_through_a_list(resource: str) -> None:
    source = ApiSource(
        "", resource, TEAM, version="v1", verb=Verb.LIST, projection=Projection.METADATA
    )

    result = await read_list(FakeReader(EvidencePage((PAYLOAD_ITEM,))), source, clock=_clock)

    assert result.items == ({"metadata": KEPT_PAYLOAD_METADATA},)
    assert "hunter2" not in json.dumps(result.items)


@pytest.mark.parametrize("resource", ["configmaps", "secrets"])
async def test_no_configuration_payload_reaches_a_diagnostic_through_a_get(resource: str) -> None:
    source = ApiSource(
        "",
        resource,
        TEAM,
        version="v1",
        verb=Verb.GET,
        name="settings",
        projection=Projection.METADATA,
    )

    result = await read_object(FakeReader(PAYLOAD_ITEM), source, clock=_clock)

    assert result.item == {"metadata": KEPT_PAYLOAD_METADATA}
    assert "hunter2" not in json.dumps(result.item)


async def test_no_metadata_keeps_the_last_applied_configuration_or_managed_fields() -> None:
    metadata = {
        **_pod("web-0")["metadata"],
        "annotations": {LAST_APPLIED_ANNOTATION: "{}", "team": "a"},
        "managedFields": [{"manager": "kubectl"}],
    }
    reader = FakeReader({"metadata": metadata, "spec": {"nodeName": "n1"}})

    result = await read_object(reader, POD, clock=_clock)

    assert result.item == {
        "metadata": {"name": "web-0", "uid": "uid-web-0", "annotations": {"team": "a"}},
        "spec": {"nodeName": "n1"},
    }


TEMPLATE_METADATA = {
    "labels": {"app": "web"},
    "annotations": {LAST_APPLIED_ANNOTATION: '{"password": "hunter2"}', "team": "a"},
    "managedFields": [{"manager": "kubectl"}],
}
DEPLOYMENT = {
    "metadata": {"name": "web", "uid": "uid-web"},
    "spec": {"template": {"metadata": TEMPLATE_METADATA, "spec": {"containers": []}}},
}
SCRUBBED_TEMPLATE = {"labels": {"app": "web"}, "annotations": {"team": "a"}}


async def test_no_embedded_metadata_keeps_the_last_applied_configuration_through_a_list() -> None:
    source = ApiSource("apps", "deployments", TEAM, version="v1", verb=Verb.LIST)

    result = await read_list(FakeReader(EvidencePage((DEPLOYMENT,))), source, clock=_clock)

    (item,) = result.items
    assert item["spec"]["template"]["metadata"] == SCRUBBED_TEMPLATE
    assert "hunter2" not in json.dumps(result.items)


async def test_no_embedded_metadata_keeps_the_last_applied_configuration_through_a_get() -> None:
    source = ApiSource("apps", "deployments", TEAM, version="v1", verb=Verb.GET, name="web")

    result = await read_object(FakeReader(DEPLOYMENT), source, clock=_clock)

    assert result.item is not None
    assert result.item["spec"]["template"]["metadata"] == SCRUBBED_TEMPLATE
    assert result.item["spec"]["template"]["spec"] == {"containers": []}


async def test_an_object_too_deep_to_scrub_is_malformed() -> None:
    nested: dict[str, Any] = {}
    for _ in range(5000):
        nested = {"nested": nested}

    result = await read_list(FakeReader(EvidencePage(({**_pod("a"), "spec": nested},))), PODS)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.MALFORMED)


@pytest.mark.parametrize(
    "annotations",
    [
        pytest.param({LAST_APPLIED_ANNOTATION: "{}"}, id="emptied"),
        pytest.param("not a map", id="unreadable"),
    ],
)
async def test_annotations_with_nothing_left_to_keep_are_dropped(annotations: object) -> None:
    item = {"metadata": {**_pod("a")["metadata"], "annotations": annotations}}

    result = await read_list(FakeReader(EvidencePage((item,))), PODS, clock=_clock)

    assert result.items == ({"metadata": {"name": "a", "uid": "uid-a"}},)


async def test_an_unresolved_source_is_never_read() -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match="resolved for list"):
        await read_list(reader, ApiSource("", "pods", TEAM))
    with pytest.raises(ValueError, match="resolved for get"):
        await read_object(reader, ApiSource("", "pods", TEAM, name="web-0"))

    assert (reader.lists, reader.gets) == ([], [])


async def test_each_read_refuses_a_source_resolved_for_the_other_verb() -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match="resolved for list"):
        await read_list(reader, POD)
    with pytest.raises(ValueError, match="resolved for get"):
        await read_object(reader, PODS)

    assert (reader.lists, reader.gets) == ([], [])


@pytest.mark.parametrize("resource", ["configmaps", "secrets"])
async def test_a_configuration_payload_is_never_read_as_an_object(resource: str) -> None:
    reader = FakeReader()
    listing = ApiSource("", resource, TEAM, version="v1", verb=Verb.LIST)
    getting = ApiSource("", resource, TEAM, version="v1", verb=Verb.GET, name="settings")

    with pytest.raises(ValueError, match="metadata only"):
        await read_list(reader, listing)
    with pytest.raises(ValueError, match="metadata only"):
        await read_object(reader, getting)

    assert (reader.lists, reader.gets) == ([], [])


async def test_a_subresource_is_not_an_evidence_read() -> None:
    source = dataclasses.replace(POD, subresource="status")

    with pytest.raises(ValueError, match="subresources"):
        await read_object(FakeReader(), source)


@pytest.mark.parametrize("scope", [SourceScope.node("node-1"), SourceScope.provider("prometheus")])
async def test_an_api_read_is_cluster_or_namespace_scoped(scope: SourceScope) -> None:
    source = ApiSource("", "pods", scope, version="v1", verb=Verb.LIST)

    with pytest.raises(ValueError, match="cluster- or namespace-scoped"):
        await read_list(FakeReader(), source)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"page_size": 0}, "page_size"),
        ({"page_size": 101}, "page_size"),
        ({"page_size": True}, "page_size"),
        ({"max_pages": 0}, "max_pages"),
        ({"max_pages": 11}, "max_pages"),
        ({"deadline": 0}, "deadline"),
        ({"deadline": 15.5}, "deadline"),
        ({"deadline": float("nan")}, "deadline"),
        ({"max_bytes": 0}, "max_bytes"),
        ({"max_bytes": 2 * 1024 * 1024 + 1}, "max_bytes"),
    ],
)
async def test_a_list_budget_out_of_range_is_refused(options: dict[str, Any], message: str) -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match=message):
        await read_list(reader, PODS, **options)

    assert reader.lists == []


@pytest.mark.parametrize(
    ("options", "message"), [({"deadline": 16}, "deadline"), ({"max_bytes": 0}, "max_bytes")]
)
async def test_an_object_budget_out_of_range_is_refused(
    options: dict[str, Any], message: str
) -> None:
    reader = FakeReader()

    with pytest.raises(ValueError, match=message):
        await read_object(reader, POD, **options)

    assert reader.gets == []


async def test_an_object_read_records_the_uid_it_saw() -> None:
    source = dataclasses.replace(POD, expected_uid="uid-web-0")
    reader = FakeReader(_pod("web-0"))

    result = await read_object(reader, source, clock=_clock)
    observation = result.observation

    assert result.item == _pod("web-0")
    assert _state(observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert (observation.items, observation.observed_uid, observation.recreated) == (
        1,
        "uid-web-0",
        False,
    )
    assert conclusive(observation, DEFAULT_POLICY, NOW)
    assert reader.gets == [(POD_TARGET, "web-0", DEFAULT_MAX_BYTES)]


async def test_a_recreated_object_is_never_conclusive() -> None:
    source = dataclasses.replace(POD, expected_uid="uid-old")

    result = await read_object(FakeReader(_pod("web-0", "uid-new")), source, clock=_clock)

    assert result.observation.observed_uid == "uid-new"
    assert result.observation.recreated
    assert not conclusive(result.observation, DEFAULT_POLICY, NOW)


async def test_an_absent_object_is_available_and_absent() -> None:
    reader = FakeReader(_status(404, "NotFound", name="web-0", kind="pods"))

    result = await read_object(reader, POD, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBJECT_ABSENT)
    assert result.observation.items == 0


async def test_a_404_that_names_another_object_suggests_stale_discovery() -> None:
    reader = FakeReader(_status(404, "NotFound", name="other", kind="pods"))

    result = await read_object(reader, POD, clock=_clock)

    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DISCOVERY_STALE)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"spec": {}}, id="no-metadata"),
        pytest.param({"metadata": {"name": "web-0"}}, id="no-uid"),
        pytest.param({"metadata": {"name": "web-0", "uid": ""}}, id="empty-uid"),
        pytest.param({"metadata": {"name": "web-1", "uid": "uid-web-1"}}, id="another-object"),
    ],
)
async def test_an_object_that_is_not_the_one_asked_for_is_malformed(
    document: dict[str, Any],
) -> None:
    result = await read_object(FakeReader(document), POD, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.MALFORMED)


async def test_an_object_read_stops_at_its_deadline() -> None:
    result = await read_object(FakeReader(STALL), POD, deadline=0.05, clock=_clock)

    assert result.item is None
    assert _state(result.observation) == (EvidenceState.UNAVAILABLE, Basis.DEADLINE)


async def test_an_object_read_retries_one_429() -> None:
    reader = FakeReader(_status(429, "TooManyRequests", retry_after=0.0), _pod("web-0"))

    result = await read_object(reader, POD, clock=_clock)

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert len(reader.gets) == 2


async def test_both_reads_record_the_run_token() -> None:
    token = RunToken(1, 2, 3)

    listed = await read_list(FakeReader(EvidencePage(())), PODS, token=token, clock=_clock)
    got = await read_object(FakeReader(_pod("web-0")), POD, token=token, clock=_clock)

    assert listed.observation.token == got.observation.token == token
