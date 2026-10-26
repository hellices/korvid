"""Classify failed Kubernetes reads into the evidence vocabulary (#409).

One table decides what a 403, a 404, an expired continuation or a dropped
connection means for every diagnostic, so that none decides it alone. The
classifier reads the HTTP status, the category the k8s layer gave a client
failure, and at most `STATUS_BODY_LIMIT` bytes of a Kubernetes `Status`
body. It never parses error messages, and never classifies cancellation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from korvid.core.capability import ApiSource, Basis, DiscoverySource, EvidenceState, Projection
from korvid.k8s.bounded_transport import ERROR_BODY_LIMIT
from korvid.k8s.errors import ApiStatusError, KubeClientError, KubeErrorCategory

#: Readers keep at most this many bytes of an error body, and the classifier
#: parses no more.
STATUS_BODY_LIMIT = ERROR_BODY_LIMIT


class ReadKind(StrEnum):
    """The kind of request that failed."""

    LIST = "list"
    GET = "get"
    DISCOVERY = "discovery"


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """What a failed request asked for: the context its exception does not carry.

    `continuation` is true when the request followed an accepted page, which is
    what makes its failure `PARTIAL`.
    """

    kind: ReadKind
    source: ApiSource | DiscoverySource
    continuation: bool = False

    def __post_init__(self) -> None:
        if (self.kind is ReadKind.DISCOVERY) != isinstance(self.source, DiscoverySource):
            raise ValueError("a discovery read names a discovery source, and no other read does")
        if self.continuation and self.kind is not ReadKind.LIST:
            raise ValueError("only a LIST continues")
        if (
            self.kind is ReadKind.GET
            and isinstance(self.source, ApiSource)
            and not self.source.name
        ):
            raise ValueError("a GET names its object")


@dataclass(frozen=True, slots=True)
class StatusDetails:
    """The parts of a Kubernetes `Status` body the classifier reads."""

    reason: str
    name: str
    group: str
    kind: str


@dataclass(frozen=True, slots=True)
class Classification:
    """The state and basis a failure maps to, with fixed detail text."""

    state: EvidenceState
    basis: Basis
    detail: str


_CATEGORY_BASIS: Mapping[KubeErrorCategory | None, Basis] = MappingProxyType(
    {
        None: Basis.TRANSPORT,
        "timeout": Basis.TRANSPORT,
        "transport": Basis.TRANSPORT,
        "malformed": Basis.MALFORMED,
        "too_large": Basis.TOO_LARGE,
    }
)
_CATEGORY_DETAIL: Mapping[KubeErrorCategory | None, str] = MappingProxyType(
    {
        None: "request failed",
        "timeout": "request timed out",
        "transport": "connection failed",
        "malformed": "malformed response",
        "too_large": "response over budget",
    }
)
_BASIS_STATE: Mapping[Basis, EvidenceState] = MappingProxyType(
    {
        Basis.UNAUTHENTICATED: EvidenceState.DENIED,
        Basis.FORBIDDEN: EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN: EvidenceState.DENIED,
        Basis.OBJECT_ABSENT: EvidenceState.AVAILABLE,
        Basis.PROJECTION_UNSUPPORTED: EvidenceState.UNSUPPORTED,
    }
)


def parse_status(body: str) -> StatusDetails | None:
    """Read `reason` and `details` from a Kubernetes `Status` body.

    Returns `None` for a body over `STATUS_BODY_LIMIT` UTF-8 bytes, one that
    is not a `Status` object, or one with a wrongly typed field: a group of
    `123` must not read as the core group. An omitted or null field reads as
    `""`.
    """
    document = _status_document(body)
    if document is None:
        return None
    details = document.get("details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        return None
    reason = _text(document.get("reason"))
    name, group, kind = (_text(details.get(key)) for key in ("name", "group", "kind"))
    if reason is None or name is None or group is None or kind is None:
        return None
    return StatusDetails(reason, name, group, kind)


def _status_document(body: str) -> dict[str, object] | None:
    if len(body) > STATUS_BODY_LIMIT or len(body.encode("utf-8", "replace")) > STATUS_BODY_LIMIT:
        return None
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "Status":
        return None
    return document


def classify(failure: BaseException, request: ReadRequest) -> Classification:
    """Map a failed read to a state, a basis and a fixed detail.

    Only Kubernetes failures are classified: anything else, cancellation
    included, is re-raised unchanged. A failure on a continuation keeps the
    pages already read, so it is `PARTIAL` with the same basis.
    """
    if isinstance(failure, ApiStatusError):
        basis = _status_basis(failure, request)
        detail = f"HTTP {failure.status}"
    elif isinstance(failure, KubeClientError):
        is_discovery = request.kind is ReadKind.DISCOVERY
        basis = Basis.DISCOVERY_FAILED if is_discovery else _CATEGORY_BASIS[failure.category]
        detail = _CATEGORY_DETAIL[failure.category]
    else:
        raise failure
    if request.continuation:
        return Classification(EvidenceState.PARTIAL, basis, detail)
    return Classification(_BASIS_STATE.get(basis, EvidenceState.UNAVAILABLE), basis, detail)


def _status_basis(failure: ApiStatusError, request: ReadRequest) -> Basis:
    if failure.status == 401:
        return Basis.UNAUTHENTICATED
    if failure.status == 429:
        return Basis.THROTTLED
    if isinstance(request.source, DiscoverySource):
        return Basis.DISCOVERY_FORBIDDEN if failure.status == 403 else Basis.DISCOVERY_FAILED
    if failure.status == 403:
        return Basis.FORBIDDEN
    return _read_status_basis(failure, request, request.source)


def _read_status_basis(failure: ApiStatusError, request: ReadRequest, source: ApiSource) -> Basis:
    status = failure.status
    if status == 404:
        named = _names_object(failure, request, source)
        return Basis.OBJECT_ABSENT if named else Basis.DISCOVERY_STALE
    if status == 405:
        return Basis.DISCOVERY_STALE
    if status == 406 and source.projection is Projection.METADATA:
        return Basis.PROJECTION_UNSUPPORTED
    if status == 410 and request.continuation and _reason(failure) == "Expired":
        return Basis.CONTINUATION_EXPIRED
    if 500 <= status <= 599:
        return Basis.SERVER_ERROR
    return Basis.UNEXPECTED_STATUS


def _names_object(failure: ApiStatusError, request: ReadRequest, source: ApiSource) -> bool:
    if request.kind is not ReadKind.GET:
        return False
    status = parse_status(failure.body)
    return (
        status is not None
        and status.reason == "NotFound"
        and (status.name, status.group, status.kind) == (source.name, source.group, source.resource)
    )


def _reason(failure: ApiStatusError) -> str:
    status = parse_status(failure.body)
    return "" if status is None else status.reason


def _text(value: object) -> str | None:
    if value is None:
        return ""
    return value if isinstance(value, str) else None
