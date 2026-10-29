"""Bounded LIST pages and GETs for diagnostic evidence (#409, slice S3).

`korvid.core.evidence_reads` decides what to read and what an outcome means.
This module only reads, through `korvid.k8s.bounded_transport`. A metadata
read asks the server for `PartialObjectMetadata`. A server that refuses
answers 406, and nothing falls back to reading the object.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from kubernetes_asyncio.client import ApiClient

from korvid.k8s.bounded_transport import (
    ERROR_BODY_LIMIT,
    JSON,
    api_path,
    decode_list_page,
    read_bounded_json,
)
from korvid.k8s.discovery_snapshot import RawDiscovery

METADATA_LIST = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
METADATA_OBJECT = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"


@dataclass(frozen=True, slots=True)
class EvidenceTarget:
    """The collection an evidence read addresses, and whether it reads only metadata.

    `namespace` is `None` for a cluster-wide read.
    """

    group: str
    version: str
    resource: str
    namespace: str | None = None
    metadata_only: bool = False


@dataclass(frozen=True, slots=True)
class EvidencePage:
    """One LIST page that passed its byte and object budgets."""

    items: tuple[dict[str, Any], ...]
    continuation: str = ""


class EvidenceReader(ABC):
    """What evidence reads depend on: a LIST page and a GET, and nothing that writes.

    This is a typed dependency, not a runtime boundary. Code typed against it
    calls only these two, though an implementation such as `KubeClient` also
    has write methods.
    """

    @abstractmethod
    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        """One page of at most `limit` items, read within `max_bytes`."""

    @abstractmethod
    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        """The object `name`, read within `max_bytes`."""


class EvidenceClient(EvidenceReader):
    """What a diagnostic needs of its client: evidence reads, discovery and the session.

    `KubeClient` is the implementation. A diagnostic resolves its sources from
    `discovery_snapshot()` and records `session_generation` in its run token,
    so a result read under other credentials is dropped.
    """

    @property
    @abstractmethod
    def session_generation(self) -> int:
        """An opaque counter that changes on every connect and context switch."""

    @abstractmethod
    async def discovery_snapshot(self) -> RawDiscovery:
        """This session's raw discovery."""


async def read_evidence_page(
    api: ApiClient | None,
    target: EvidenceTarget,
    *,
    limit: int,
    continuation: str,
    max_bytes: int,
) -> EvidencePage:
    """LIST one page of `target`, keeping up to `ERROR_BODY_LIMIT` of an error body."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("a page size is a positive integer")
    query = [("limit", str(limit))]
    if continuation:
        query.append(("continue", continuation))
    document = await read_bounded_json(
        api,
        api_path(target.group, target.version, target.resource, target.namespace),
        query,
        max_bytes=max_bytes,
        accept=METADATA_LIST if target.metadata_only else JSON,
        error_body_limit=ERROR_BODY_LIMIT,
    )
    items, token = decode_list_page(document, limit)
    return EvidencePage(items, token)


async def read_evidence_object(
    api: ApiClient | None, target: EvidenceTarget, name: str, *, max_bytes: int
) -> dict[str, Any]:
    """GET the object `name` of `target`, keeping up to `ERROR_BODY_LIMIT` of an error body."""
    if not name:
        raise ValueError("an object read names its object")
    return await read_bounded_json(
        api,
        api_path(target.group, target.version, target.resource, target.namespace, name),
        [],
        max_bytes=max_bytes,
        accept=METADATA_OBJECT if target.metadata_only else JSON,
        error_body_limit=ERROR_BODY_LIMIT,
    )
