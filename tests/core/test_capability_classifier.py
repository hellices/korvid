"""The read-outcome classifier (#409): one table for every diagnostic."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from korvid.core.capability import (
    ApiSource,
    Basis,
    DiscoverySource,
    EvidenceState,
    Observation,
    Projection,
    SourceScope,
    Verb,
)
from korvid.core.capability_classifier import (
    STATUS_BODY_LIMIT,
    ReadKind,
    ReadRequest,
    StatusDetails,
    classify,
    parse_status,
)
from korvid.k8s.errors import (
    ApiStatusError,
    KubeClientError,
    KubeMalformedError,
    KubeTimeoutError,
    KubeTooLargeError,
    KubeTransportError,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
TEAM = SourceScope.namespace("team-a")
CONFIG = ApiSource(
    "", "configmaps", TEAM, "v1", Verb.GET, projection=Projection.METADATA, name="app"
)
DEPLOYMENTS = ApiSource("apps", "deployments", TEAM, "v1", Verb.LIST)
QUOTAS = ApiSource("", "resourcequotas", TEAM, "v1", Verb.LIST, projection=Projection.METADATA)

GET = ReadRequest(ReadKind.GET, CONFIG)
LIST = ReadRequest(ReadKind.LIST, DEPLOYMENTS)
METADATA_LIST = ReadRequest(ReadKind.LIST, QUOTAS)
CONTINUATION = ReadRequest(ReadKind.LIST, DEPLOYMENTS, continuation=True)
METADATA_CONTINUATION = ReadRequest(ReadKind.LIST, QUOTAS, continuation=True)
DISCOVERY = ReadRequest(ReadKind.DISCOVERY, DiscoverySource.group_version("apps", "v1"))
READS = [GET, LIST, METADATA_LIST, CONTINUATION, METADATA_CONTINUATION, DISCOVERY]


def _status(code: int, reason: str = "", **details: str) -> ApiStatusError:
    body = {
        "kind": "Status",
        "apiVersion": "v1",
        "status": "Failure",
        "reason": reason,
        "details": details,
        "code": code,
    }
    return ApiStatusError(code, reason, json.dumps(body))


NOT_FOUND = _status(404, "NotFound", name="app", kind="configmaps")
FAILURES = [
    _status(401, "Unauthorized"),
    _status(403, "Forbidden"),
    NOT_FOUND,
    _status(404, "NotFound"),
    ApiStatusError(404, "Not Found", "404 page not found"),
    _status(405, "MethodNotAllowed"),
    _status(406, "NotAcceptable"),
    _status(410, "Expired"),
    _status(429, "TooManyRequests"),
    _status(503, "ServiceUnavailable"),
    _status(400, "BadRequest"),
    KubeTimeoutError("timed out"),
    KubeTransportError("connection failed"),
    KubeMalformedError("malformed"),
    KubeTooLargeError("over budget"),
    KubeClientError("not connected"),
]

AVAILABLE = EvidenceState.AVAILABLE
PARTIAL = EvidenceState.PARTIAL
UNSUPPORTED = EvidenceState.UNSUPPORTED
DENIED = EvidenceState.DENIED
UNAVAILABLE = EvidenceState.UNAVAILABLE


@pytest.mark.parametrize(
    ("failure", "read", "state", "basis"),
    [
        pytest.param(_status(401), LIST, DENIED, Basis.UNAUTHENTICATED, id="401-list"),
        pytest.param(_status(401), GET, DENIED, Basis.UNAUTHENTICATED, id="401-get"),
        pytest.param(_status(401), DISCOVERY, DENIED, Basis.UNAUTHENTICATED, id="401-discovery"),
        pytest.param(_status(403), LIST, DENIED, Basis.FORBIDDEN, id="403-list"),
        pytest.param(_status(403), GET, DENIED, Basis.FORBIDDEN, id="403-get"),
        pytest.param(
            _status(403), DISCOVERY, DENIED, Basis.DISCOVERY_FORBIDDEN, id="403-discovery"
        ),
        pytest.param(NOT_FOUND, GET, AVAILABLE, Basis.OBJECT_ABSENT, id="404-names-the-object"),
        pytest.param(
            _status(404, "NotFound", name="other", kind="configmaps"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-names-another-object",
        ),
        pytest.param(
            _status(404, "NotFound", name="app", kind="secrets"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-names-another-resource",
        ),
        pytest.param(
            _status(404, "NotFound", name="app", group="apps", kind="configmaps"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-names-another-group",
        ),
        pytest.param(
            _status(404, "NotFound"), GET, UNAVAILABLE, Basis.DISCOVERY_STALE, id="404-no-details"
        ),
        pytest.param(
            ApiStatusError(404, "Not Found", "404 page not found"),
            GET,
            UNAVAILABLE,
            Basis.DISCOVERY_STALE,
            id="404-no-status-body",
        ),
        pytest.param(NOT_FOUND, LIST, UNAVAILABLE, Basis.DISCOVERY_STALE, id="404-list"),
        pytest.param(NOT_FOUND, DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="404-discovery"),
        pytest.param(_status(405), LIST, UNAVAILABLE, Basis.DISCOVERY_STALE, id="405-list"),
        pytest.param(_status(405), GET, UNAVAILABLE, Basis.DISCOVERY_STALE, id="405-get"),
        pytest.param(
            _status(405), DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="405-discovery"
        ),
        pytest.param(
            _status(406),
            METADATA_LIST,
            UNSUPPORTED,
            Basis.PROJECTION_UNSUPPORTED,
            id="406-metadata",
        ),
        pytest.param(
            _status(406), GET, UNSUPPORTED, Basis.PROJECTION_UNSUPPORTED, id="406-metadata-get"
        ),
        pytest.param(_status(406), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="406-objects"),
        pytest.param(
            _status(406), DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="406-discovery"
        ),
        pytest.param(
            _status(410, "Expired"),
            CONTINUATION,
            PARTIAL,
            Basis.CONTINUATION_EXPIRED,
            id="410-expired-continuation",
        ),
        pytest.param(
            _status(410, "Gone"), CONTINUATION, PARTIAL, Basis.UNEXPECTED_STATUS, id="410-gone"
        ),
        pytest.param(
            _status(410, "Expired"), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="410-first-page"
        ),
        pytest.param(_status(429), LIST, UNAVAILABLE, Basis.THROTTLED, id="429-list"),
        pytest.param(_status(429), GET, UNAVAILABLE, Basis.THROTTLED, id="429-get"),
        pytest.param(_status(429), DISCOVERY, UNAVAILABLE, Basis.THROTTLED, id="429-discovery"),
        pytest.param(_status(500), LIST, UNAVAILABLE, Basis.SERVER_ERROR, id="500-list"),
        pytest.param(_status(503), GET, UNAVAILABLE, Basis.SERVER_ERROR, id="503-get"),
        pytest.param(
            _status(503), DISCOVERY, UNAVAILABLE, Basis.DISCOVERY_FAILED, id="503-discovery"
        ),
        pytest.param(_status(400), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="400-list"),
        pytest.param(_status(409), GET, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="409-get"),
        pytest.param(
            ApiStatusError(0, ""), LIST, UNAVAILABLE, Basis.UNEXPECTED_STATUS, id="no-status"
        ),
        pytest.param(KubeTimeoutError("t"), LIST, UNAVAILABLE, Basis.TRANSPORT, id="timeout"),
        pytest.param(KubeTransportError("t"), GET, UNAVAILABLE, Basis.TRANSPORT, id="transport"),
        pytest.param(KubeClientError("t"), LIST, UNAVAILABLE, Basis.TRANSPORT, id="uncategorized"),
        pytest.param(KubeMalformedError("t"), LIST, UNAVAILABLE, Basis.MALFORMED, id="malformed"),
        pytest.param(KubeTooLargeError("t"), GET, UNAVAILABLE, Basis.TOO_LARGE, id="too-large"),
        pytest.param(
            KubeTimeoutError("t"),
            DISCOVERY,
            UNAVAILABLE,
            Basis.DISCOVERY_FAILED,
            id="timeout-discovery",
        ),
        pytest.param(
            KubeMalformedError("t"),
            DISCOVERY,
            UNAVAILABLE,
            Basis.DISCOVERY_FAILED,
            id="malformed-discovery",
        ),
        pytest.param(
            _status(401), CONTINUATION, PARTIAL, Basis.UNAUTHENTICATED, id="401-continued"
        ),
        pytest.param(_status(403), CONTINUATION, PARTIAL, Basis.FORBIDDEN, id="403-continued"),
        pytest.param(
            _status(404), CONTINUATION, PARTIAL, Basis.DISCOVERY_STALE, id="404-continued"
        ),
        pytest.param(
            _status(405), CONTINUATION, PARTIAL, Basis.DISCOVERY_STALE, id="405-continued"
        ),
        pytest.param(
            _status(406),
            METADATA_CONTINUATION,
            PARTIAL,
            Basis.PROJECTION_UNSUPPORTED,
            id="406-metadata-continued",
        ),
        pytest.param(
            KubeTooLargeError("t"), CONTINUATION, PARTIAL, Basis.TOO_LARGE, id="large-continued"
        ),
    ],
)
def test_the_classification_table(
    failure: BaseException, read: ReadRequest, state: EvidenceState, basis: Basis
) -> None:
    result = classify(failure, read)

    assert (result.state, result.basis) == (state, basis)


@pytest.mark.parametrize("read", READS)
@pytest.mark.parametrize("failure", FAILURES)
def test_every_classification_can_be_recorded(failure: BaseException, read: ReadRequest) -> None:
    result = classify(failure, read)
    observation = Observation(read.source, result.state, result.basis, NOW, detail=result.detail)

    assert (observation.state, observation.basis) == (result.state, result.basis)


@pytest.mark.parametrize("read", [CONTINUATION, METADATA_CONTINUATION])
@pytest.mark.parametrize("failure", FAILURES)
def test_every_failure_on_a_continuation_keeps_its_pages(
    failure: BaseException, read: ReadRequest
) -> None:
    assert classify(failure, read).state is EvidenceState.PARTIAL


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (_status(503, "token=secret"), "HTTP 503"),
        (KubeTimeoutError("token=secret"), "request timed out"),
        (KubeTransportError("token=secret"), "connection failed"),
        (KubeMalformedError("token=secret"), "malformed response"),
        (KubeTooLargeError("token=secret"), "response over budget"),
        (KubeClientError("token=secret"), "request failed"),
    ],
)
def test_details_come_from_fixed_templates(failure: BaseException, detail: str) -> None:
    assert classify(failure, LIST).detail == detail


def test_cancellation_is_re_raised_and_never_classified() -> None:
    with pytest.raises(asyncio.CancelledError, match="stop reading"):
        classify(asyncio.CancelledError("stop reading"), LIST)


def test_a_failure_outside_the_kubernetes_layer_propagates() -> None:
    with pytest.raises(RuntimeError, match="a bug"):
        classify(RuntimeError("a bug"), LIST)


@pytest.mark.parametrize(
    ("kind", "source", "continuation", "message"),
    [
        (ReadKind.DISCOVERY, DEPLOYMENTS, False, "discovery source"),
        (ReadKind.LIST, DiscoverySource.group_root(), False, "discovery source"),
        (ReadKind.GET, CONFIG, True, "only a LIST continues"),
        (ReadKind.GET, ApiSource("", "configmaps", TEAM), False, "names its object"),
    ],
)
def test_a_read_request_is_well_formed(
    kind: ReadKind, source: ApiSource | DiscoverySource, continuation: bool, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ReadRequest(kind, source, continuation)


def test_a_status_body_yields_its_reason_and_details() -> None:
    assert parse_status(NOT_FOUND.body) == StatusDetails("NotFound", "app", "", "configmaps")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty"),
        pytest.param("404 page not found", id="text"),
        pytest.param("[]", id="list"),
        pytest.param('{"kind": "Pod", "reason": "NotFound"}', id="another-kind"),
        pytest.param("[" * 5000, id="deeply-nested"),
    ],
)
def test_a_body_that_is_not_a_status_reads_as_none(body: str) -> None:
    assert parse_status(body) is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"kind": "Status", "reason": 3}', id="reason"),
        pytest.param('{"kind": "Status", "details": "app"}', id="details"),
        pytest.param('{"kind": "Status", "details": {"name": ["app"]}}', id="name"),
        pytest.param('{"kind": "Status", "details": {"group": 123}}', id="group"),
        pytest.param('{"kind": "Status", "details": {"kind": {}}}', id="kind"),
    ],
)
def test_a_status_with_a_wrongly_typed_field_is_unreadable(body: str) -> None:
    assert parse_status(body) is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('{"kind": "Status"}', id="omitted"),
        pytest.param('{"kind": "Status", "reason": null, "details": null}', id="null"),
        pytest.param('{"kind": "Status", "details": {"group": null}}', id="null-group"),
    ],
)
def test_an_omitted_or_null_status_field_reads_as_empty(body: str) -> None:
    assert parse_status(body) == StatusDetails("", "", "", "")


def test_a_not_found_with_an_invalid_group_never_proves_absence() -> None:
    body = {
        "kind": "Status",
        "reason": "NotFound",
        "details": {"name": "app", "group": 123, "kind": "configmaps"},
    }
    failure = ApiStatusError(404, "Not Found", json.dumps(body))

    assert classify(failure, GET).basis is Basis.DISCOVERY_STALE


def test_a_status_body_over_the_budget_is_not_parsed() -> None:
    body = NOT_FOUND.body[:-1] + " " * STATUS_BODY_LIMIT + "}"

    assert json.loads(body)["reason"] == "NotFound"
    assert parse_status(body) is None
    assert classify(ApiStatusError(404, "Not Found", body), GET).basis is Basis.DISCOVERY_STALE


def test_the_body_budget_counts_utf8_bytes() -> None:
    message = "é" * (STATUS_BODY_LIMIT // 2)
    body = json.dumps(
        {"kind": "Status", "reason": "NotFound", "message": message}, ensure_ascii=False
    )

    assert len(body) <= STATUS_BODY_LIMIT < len(body.encode())
    assert parse_status(body) is None
