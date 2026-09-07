"""korvid exceptions normalizing Kubernetes client failures (k8s-layer only).

Callers import these types instead of third-party transport/API exceptions so
the kubernetes_asyncio implementation never leaks past the k8s layer.
"""

from __future__ import annotations


class KubeClientError(Exception):
    """Raised when a Kubernetes request fails without an HTTP API status."""


class ApiStatusError(Exception):
    """Raised by the k8s layer when an API request returns an HTTP error status."""

    def __init__(
        self,
        status: int,
        reason: str,
        body: str = "",
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(f"API {status}: {reason}")
        self.status = status
        self.reason = reason
        #: Raw response body (a Kubernetes ``Status`` JSON when available):
        #: callers that must tell apart same-status responses - e.g. an
        #: eviction's PDB denial vs API Priority and Fairness throttling,
        #: both 429 - inspect it.
        self.body = body
        self.retry_after_seconds: float | None = retry_after_seconds
