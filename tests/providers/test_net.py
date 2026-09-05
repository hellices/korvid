"""Corporate CA trust for korvid-owned agent HTTP clients (issue #168).

One trust builder feeds the live providers and the :ai wizard's connection
test, so they can never disagree; a missing/malformed bundle fails
actionably, and TLS verification can never be disabled through it.
"""

from __future__ import annotations

import http.server
import os
import ssl
import threading
from pathlib import Path

import httpx
import pytest

from korvid.providers.net import build_verify, make_http_client_factory
from tests.providers.tls_ca import mint_ca_and_server_cert

#: Kept under the old private name so the tests below read unchanged; the
#: minting itself is shared with the LiteLLM TLS seam test.
_mint_ca_and_server_cert = mint_ca_and_server_cert


def test_no_bundle_keeps_default_trust() -> None:
    # True = httpx default verification: system store plus the standard
    # SSL_CERT_FILE / proxy environment behavior stays intact.
    assert build_verify(None) is True


def test_bundle_builds_a_verifying_ssl_context(tmp_path: Path) -> None:
    ca_pem, _, _ = _mint_ca_and_server_cert(tmp_path)
    ctx = build_verify(str(ca_pem))
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED  # verification can never be off
    assert any("korvid test CA" in str(cert.get("subject", "")) for cert in ctx.get_ca_certs())


def test_missing_bundle_fails_actionably(tmp_path: Path) -> None:
    missing = tmp_path / "nope.pem"
    with pytest.raises(ValueError, match=r"nope\.pem"):
        build_verify(str(missing))


def test_malformed_bundle_fails_actionably(tmp_path: Path) -> None:
    bad = tmp_path / "garbage.pem"
    bad.write_text("this is not a certificate")
    with pytest.raises(ValueError, match=r"garbage\.pem"):
        build_verify(str(bad))


async def test_private_ca_endpoint_needs_the_bundle(tmp_path: Path) -> None:
    """End to end: an HTTPS endpoint signed by a corporate CA fails with
    default trust and succeeds with network.ca_bundle — without touching
    the system trust store."""
    ca_pem, cert_pem, key_pem = _mint_ca_and_server_cert(tmp_path)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server API name
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # no legacy TLS
    server_ctx.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://127.0.0.1:{port}/"
    try:
        async with httpx.AsyncClient(timeout=5.0) as default_client:
            with pytest.raises(httpx.ConnectError):
                await default_client.get(url)
        async with make_http_client_factory(str(ca_pem))() as trusted:
            response = await trusted.get(url)
        assert response.status_code == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def test_factory_and_providers_share_one_trust_builder(tmp_path: Path) -> None:
    """The :ai connection test and the live providers build their clients
    through the same CA-aware client — they cannot disagree about trust,
    and all three name the configured bundle on failure."""
    from korvid.providers.net import _CANamedClient
    from korvid.providers.ollama import OllamaProvider
    from korvid.providers.openai_compat import OpenAICompatProvider

    ca_pem, _, _ = _mint_ca_and_server_cert(tmp_path)
    factory_client = make_http_client_factory(str(ca_pem))()
    openai = OpenAICompatProvider(base_url="https://llm.corp/v1", model="m", ca_bundle=str(ca_pem))
    ollama = OllamaProvider(base_url="https://ollama.corp", model="m", ca_bundle=str(ca_pem))
    clients = [factory_client, openai._get_client(), ollama._get_client()]
    try:
        assert len(clients) == 3  # wizard test + both live providers
        # Same builder → same verification posture for wizard and runtime.
        assert all(isinstance(c, _CANamedClient) for c in clients)
        assert all(
            c._ca_bundle_path == str(ca_pem) for c in clients if isinstance(c, _CANamedClient)
        )
    finally:
        for client in clients:
            await client.aclose()


async def test_injected_clients_keep_precedence(tmp_path: Path) -> None:
    """Constructor-injected clients (the test seam) are never replaced by
    the CA configuration."""
    ca_pem, _, _ = _mint_ca_and_server_cert(tmp_path)
    from korvid.providers.openai_compat import OpenAICompatProvider

    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    provider = OpenAICompatProvider(
        base_url="https://x/v1", model="m", client=injected, ca_bundle=str(ca_pem)
    )
    assert provider._get_client() is injected
    await injected.aclose()


async def test_verification_failure_names_the_configured_bundle(tmp_path: Path) -> None:
    """A valid-but-wrong bundle must fail naming the configured path — an
    SSLContext forgets where it came from, so the transport carries it
    (issue #168: actionable error when TLS verification fails)."""
    _ca_pem, cert_pem, key_pem = _mint_ca_and_server_cert(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    wrong_ca, _, _ = _mint_ca_and_server_cert(other_dir)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server API name
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # no legacy TLS
    server_ctx.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with make_http_client_factory(str(wrong_ca))() as client:
            with pytest.raises(httpx.ConnectError, match=r"network\.ca_bundle") as excinfo:
                await client.get(f"https://127.0.0.1:{port}/")
        assert str(wrong_ca) in str(excinfo.value)  # the path the user set
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def test_ca_bundle_keeps_environment_proxy_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuring network.ca_bundle must not turn off standard proxy
    environment behavior (HTTP(S)_PROXY / NO_PROXY) — a custom transport
    would (review on #181)."""
    ca_pem, _, _ = _mint_ca_and_server_cert(tmp_path)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp.example:3128")
    async with make_http_client_factory(str(ca_pem))() as client:
        # httpx materializes env proxies as mounted transports; a client
        # built with transport= skips that discovery entirely.
        assert client._mounts  # the proxy mount exists alongside the CA trust


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="POSIX permission semantics (root reads anything)",
)
def test_unreadable_bundle_fails_actionably(tmp_path: Path) -> None:
    secret = tmp_path / "secret.pem"
    secret.write_text("PEM")
    secret.chmod(0)
    try:
        with pytest.raises(ValueError, match=r"secret\.pem"):
            build_verify(str(secret))
    finally:
        secret.chmod(0o600)  # let pytest clean the tmp dir
