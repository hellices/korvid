"""TLS trust for korvid-owned agent HTTP clients (issue #168).

One builder feeds the live providers and the `:ai` wizard's connection
test, so they can never disagree about CA trust. There is deliberately no
insecure mode: a configured bundle either loads or the caller fails with
an actionable error naming the path.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable

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


def make_http_client_factory(ca_bundle: str | None) -> Callable[[], httpx.AsyncClient]:
    """Client factory for the `:ai` setup wizard's connection test.

    Built from the same `build_verify` the live providers use, so the
    wizard test and the runtime share one trust configuration.
    """

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=15.0, verify=build_verify(ca_bundle))

    return factory
