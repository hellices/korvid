"""Bounded, read-only snapshot inputs for the attention surface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from kubernetes_asyncio.client import ApiClient

from korvid.k8s.bounded_transport import api_path, decode_list_page, read_bounded_json


@dataclass(frozen=True, slots=True)
class PulseSource:
    """One explicitly registered namespaced LIST input."""

    key: str
    group: str
    version: str
    plural: str
    field_selector: str = ""


@dataclass(frozen=True, slots=True)
class PulsePage:
    """A fully decoded page that passed byte and object bounds."""

    items: tuple[dict[str, Any], ...]
    continuation: str = ""


class PulseReader(ABC):
    """Read a bounded page without extending the general resource reader."""

    @abstractmethod
    async def read_pulse_page(
        self,
        source: PulseSource,
        namespace: str | None,
        continuation: str,
        limit: int,
        max_bytes: int,
    ) -> PulsePage:
        """Return a page or a normalized failure, closing every response."""


async def read_pulse_page(
    api: ApiClient | None,
    source: PulseSource,
    namespace: str | None,
    continuation: str,
    limit: int,
    max_bytes: int,
) -> PulsePage:
    """Stream at most the decoded-byte budget before parsing a LIST response.

    An error body is never read: Pulse classifies a failure by its status.
    """
    for budget in (limit, max_bytes):
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise ValueError("Pulse budgets must be a positive integer")
    query = [("limit", str(limit))]
    if continuation:
        query.append(("continue", continuation))
    if source.field_selector:
        query.append(("fieldSelector", source.field_selector))
    path = api_path(source.group, source.version, source.plural, namespace)
    document = await read_bounded_json(api, path, query, max_bytes=max_bytes)
    items, token = decode_list_page(document, limit)
    return PulsePage(items, token)
