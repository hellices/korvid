"""TLS trust for korvid-owned agent HTTP clients (issue #168).

One builder feeds the live providers and the `:ai` wizard's connection
test, so they can never disagree about CA trust. There is deliberately no
insecure mode: a configured bundle either loads or the caller fails with
an actionable error naming the path.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any

import httpx


def build_verify(ca_bundle: str | None) -> ssl.SSLContext | bool:
    """The httpx `verify` value for a configured `network.ca_bundle`.

    Returns True (httpx default trust: system store plus the standard
    `SSL_CERT_FILE`/proxy environment behavior) when no bundle is
    configured, or a verifying `SSLContext` loaded from the bundle.

    Raises:
        ValueError: when the bundle is missing, unreadable, or malformed —
            the message names the configured path; there is no silent
            fallback to default trust.
    """
    if ca_bundle is None:
        return True
    try:
        return ssl.create_default_context(cafile=ca_bundle)
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"network.ca_bundle {ca_bundle!r} could not be loaded: {exc}") from exc


class _CANamedClient(httpx.AsyncClient):
    """Client that names the configured bundle on verification failure.

    An `SSLContext` does not retain its source path, so a raw
    `CERTIFICATE_VERIFY_FAILED` at request time would leave the user
    guessing which trust was in effect (issue #168). Trust is passed via
    `verify=` (never a custom transport) so httpx's environment proxy
    discovery (`HTTP(S)_PROXY`/`NO_PROXY`) keeps working.
    """

    def __init__(self, ca_bundle: str, *, timeout: httpx.Timeout | float) -> None:
        self._ca_bundle_path = ca_bundle
        super().__init__(timeout=timeout, verify=build_verify(ca_bundle))

    async def send(self, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        try:
            return await super().send(request, **kwargs)
        except httpx.ConnectError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise httpx.ConnectError(
                    f"TLS verification failed against network.ca_bundle"
                    f" '{self._ca_bundle_path}': {exc}",
                    request=request,
                ) from exc
            raise


def make_client(ca_bundle: str | None, timeout: httpx.Timeout | float) -> httpx.AsyncClient:
    """An agent HTTP client honoring `network.ca_bundle`.

    With a bundle configured, request-time verification failures are
    re-raised naming the configured path; without one, httpx default trust
    (which consults `SSL_CERT_FILE`) applies untouched. Environment proxy
    discovery (`HTTP(S)_PROXY`/`NO_PROXY`) is preserved in both modes; a
    configured bundle *replaces* the default CA source by design.
    """
    if ca_bundle is None:
        return httpx.AsyncClient(timeout=timeout)
    return _CANamedClient(ca_bundle, timeout=timeout)


def make_http_client_factory(ca_bundle: str | None) -> Callable[[], httpx.AsyncClient]:
    """Client factory for the `:ai` setup wizard's connection test.

    Built from the same `build_verify` the live providers use, so the
    wizard test and the runtime share one trust configuration.
    """

    def factory() -> httpx.AsyncClient:
        return make_client(ca_bundle, timeout=15.0)

    return factory
