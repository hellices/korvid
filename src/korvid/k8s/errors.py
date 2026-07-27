"""korvid exceptions wrapping kubernetes_asyncio API errors (k8s-layer only).

Callers in core/ import ApiStatusError instead of ApiException so the
third-party kubernetes_asyncio exception type never leaks past the k8s layer.
"""

from __future__ import annotations


class ApiStatusError(Exception):
    """Raised by the k8s layer when an API request returns an HTTP error status."""

    def __init__(self, status: int, reason: str, body: str = "") -> None:
        super().__init__(f"API {status}: {reason}")
        self.status = status
        self.reason = reason
        #: Raw response body (a Kubernetes ``Status`` JSON when available):
        #: callers that must tell apart same-status responses - e.g. an
        #: eviction's PDB denial vs API Priority and Fairness throttling,
        #: both 429 - inspect it.
        self.body = body
