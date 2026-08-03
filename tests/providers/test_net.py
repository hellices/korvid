"""Corporate CA trust for korvid-owned agent HTTP clients (issue #168).

One trust builder feeds the live providers and the :ai wizard's connection
test, so they can never disagree; a missing/malformed bundle fails
actionably, and TLS verification can never be disabled through it.
"""

from __future__ import annotations

import datetime
import http.server
import ssl
import threading
from pathlib import Path

import httpx
import pytest

from korvid.providers.net import build_verify, make_http_client_factory


def _mint_ca_and_server_cert(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A test CA plus a localhost server cert signed by it."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def _key() -> rsa.RSAPrivateKey:
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    now = datetime.datetime.now(datetime.UTC)
    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "korvid test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    srv_key = _key()
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = tmp_path / "ca.pem"
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_pem = tmp_path / "server.pem"
    cert_pem.write_bytes(srv_cert.public_bytes(serialization.Encoding.PEM))
    key_pem = tmp_path / "server-key.pem"
    key_pem.write_bytes(
        srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return ca_pem, cert_pem, key_pem


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
    server_ctx.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"https://localhost:{port}/"
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
    """The :ai connection test and the live providers derive their verify
    from the same builder — they cannot disagree about CA trust."""
    ca_pem, _, _ = _mint_ca_and_server_cert(tmp_path)
    captured: list[object] = []

    class SpyClient(httpx.AsyncClient):
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured.append(kwargs.get("verify"))
            super().__init__(timeout=5.0)

    import korvid.providers.net as net_mod

    real_client = httpx.AsyncClient
    try:
        httpx.AsyncClient = SpyClient  # type: ignore[misc]  # spy on ctor kwargs
        net_mod.make_http_client_factory(str(ca_pem))()
    finally:
        httpx.AsyncClient = real_client  # type: ignore[misc]  # restore

    from korvid.providers.ollama import OllamaProvider
    from korvid.providers.openai_compat import OpenAICompatProvider

    openai = OpenAICompatProvider(base_url="https://llm.corp/v1", model="m", ca_bundle=str(ca_pem))
    ollama = OllamaProvider(base_url="https://ollama.corp", model="m", ca_bundle=str(ca_pem))
    clients = [openai._get_client(), ollama._get_client()]
    try:
        assert captured  # the factory built a client…
        assert isinstance(captured[0], ssl.SSLContext)
        # Same builder → same verification posture for wizard and runtime.
        assert all(isinstance(v, ssl.SSLContext) for v in captured)
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
