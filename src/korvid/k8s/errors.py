"""korvid exceptions normalizing Kubernetes client failures (k8s-layer only).

Callers import these types instead of third-party transport/API exceptions so
the kubernetes_asyncio implementation never leaks past the k8s layer.
"""

from __future__ import annotations

from typing import ClassVar, Literal

#: What went wrong with a request that produced no usable HTTP answer, so that
#: a caller can classify the failure without parsing its message.
KubeErrorCategory = Literal["timeout", "transport", "malformed", "too_large"]


class KubeClientError(Exception):
    """Raised when a Kubernetes request fails without an HTTP API status.

    Raise the subclass that names what went wrong, so that `category` says it.
    The base class leaves `category` as `None`: the request produced no
    usable answer for a reason no subclass describes, such as a client that
    was never connected.
    """

    category: ClassVar[KubeErrorCategory | None] = None


class KubeTimeoutError(KubeClientError):
    """A Kubernetes request did not finish before its deadline."""

    category: ClassVar[KubeErrorCategory | None] = "timeout"


class KubeTransportError(KubeClientError):
    """A Kubernetes request failed below HTTP: the connection, TLS or socket."""

    category: ClassVar[KubeErrorCategory | None] = "transport"


class KubeMalformedError(KubeClientError):
    """A Kubernetes response could not be decoded, or had the wrong shape."""

    category: ClassVar[KubeErrorCategory | None] = "malformed"


class KubeTooLargeError(KubeClientError):
    """A Kubernetes response exceeded its byte or object budget."""

    category: ClassVar[KubeErrorCategory | None] = "too_large"


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
