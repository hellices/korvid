from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import logging
import ssl
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from korvid.agent.model_profiles import ModelEntrySource
from korvid.providers import endpoint_discovery
from korvid.providers.endpoint_discovery import EndpointDiscovery
from tests.providers.tls_ca import mint_ca_and_server_cert

pytestmark = pytest.mark.anyio

httpx = pytest.importorskip("httpx")

if TYPE_CHECKING:
    # `httpx` above is a *value* to a type checker, so annotations written
    # as `httpx.Request` do not resolve. The real module, imported only
    # for typing, gives new code somewhere to point.
    import httpx as httpx_types


def _response(status_code: int = 200, **kwargs: Any) -> httpx_types.Response:
    """A typed `httpx.Response` — `importorskip` hands mypy an `Any` module."""
    response: httpx_types.Response = httpx.Response(status_code, **kwargs)
    return response


def _factory(
    handler: Callable[[Any], Any],
    *,
    follow_redirects: bool = False,
) -> Callable[[], httpx_types.AsyncClient]:
    """A client over a mock transport.

    The handler is typed loosely because `MockTransport` takes a sync or
    an async one, and the tests here use both.
    """

    def make() -> httpx_types.AsyncClient:
        client: httpx_types.AsyncClient = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=follow_redirects,
        )
        return client

    return make


def _routes(
    mapping: dict[str, httpx_types.Response],
) -> Callable[[httpx_types.Request], httpx_types.Response]:
    """Return a handler that dispatches by URL path."""
    captured: list[httpx_types.Request] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        captured.append(request)
        path = request.url.path
        if path in mapping:
            return mapping[path]
        return _response(404)

    handler.captured = captured  # type: ignore[attr-defined]
    return handler


async def test_openai_shaped_response_becomes_model_entry() -> None:
    """An OpenAI-compat `{"data": [{"id": "m"}]}` becomes one `ModelEntry`."""
    handler = _routes(
        {
            "/v1/models": _response(
                200,
                json={"data": [{"id": "m"}]},
                headers={"content-type": "application/json"},
            ),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="openai"
    )
    assert len(entries) == 1
    assert entries[0].reference == "openai/m"
    assert entries[0].source is ModelEntrySource.ENDPOINT


async def test_ollama_shaped_response_keeps_colon_in_tag() -> None:
    """An Ollama `{"models": [{"name": "qwen3:8b"}]}` becomes `{prefix}/qwen3:8b`.

    The colon must survive — it is the Ollama tag separator and is valid
    in the model portion of a reference.
    """
    handler = _routes(
        {
            "/v1/models": _response(404),
            "/api/tags": _response(
                200,
                json={"models": [{"name": "qwen3:8b"}]},
                headers={"content-type": "application/json"},
            ),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:11434", api_key=None, prefix="ollama"
    )
    assert len(entries) == 1
    assert entries[0].reference == "ollama/qwen3:8b"
    assert entries[0].source is ModelEntrySource.ENDPOINT


async def test_404_on_v1_models_falls_through_to_api_tags() -> None:
    handler = _routes(
        {
            "/v1/models": _response(404),
            "/api/tags": _response(
                200,
                json={"models": [{"name": "llama3"}]},
                headers={"content-type": "application/json"},
            ),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="ollama"
    )
    assert len(entries) == 1
    assert entries[0].reference == "ollama/llama3"


async def test_both_endpoints_failing_returns_empty() -> None:
    handler = _routes(
        {
            "/v1/models": _response(500),
            "/api/tags": _response(500),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="custom"
    )
    assert entries == ()


async def test_connection_error_returns_empty_and_does_not_raise() -> None:
    """Network failure is silent — type the name yourself is a better outcome."""

    def boom(request: httpx_types.Request) -> httpx_types.Response:
        raise httpx.ConnectError("refused", request=request)

    discovery = EndpointDiscovery(client_factory=_factory(boom))
    entries = await discovery.list_models(
        base_url="http://localhost:9999", api_key=None, prefix="custom"
    )
    assert entries == ()


async def test_oversized_body_returns_empty() -> None:
    """A body exceeding the 2 MiB ceiling is discarded silently."""
    oversized = b"x" * (2 * 1024 * 1024 + 1)

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        return _response(200, content=oversized, headers={"content-type": "application/json"})

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="openai"
    )
    assert entries == ()


async def test_more_than_500_entries_are_truncated() -> None:
    def handler(request: httpx_types.Request) -> httpx_types.Response:
        return _response(
            200,
            json={"data": [{"id": f"model-{i}"} for i in range(600)]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="openai"
    )
    assert len(entries) == 500


# ---------------------------------------------------------------------------
# The credential: real, bearer-shaped, and going nowhere else
# ---------------------------------------------------------------------------

_SECRET = "sk-live-3f9c2b7d"


async def test_the_configured_credential_is_what_actually_goes_out() -> None:
    """The header carries the operator's key, not a redaction of it.

    Sending the literal mask meant an authenticated endpoint answered 401
    to every discovery attempt, so the feature could only ever work
    against endpoints that needed no key at all.
    """
    seen: list[httpx_types.Request] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request)
        return _response(
            200,
            json={"data": [{"id": "gpt-4o"}]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=_SECRET, prefix="openai"
    )
    assert entries
    assert entries[0].reference == "openai/gpt-4o"
    assert seen
    assert seen[0].headers.get("authorization") == f"Bearer {_SECRET}"


async def test_authorization_header_absent_when_no_key() -> None:
    """A keyless endpoint (an Ollama on localhost) is sent no header at all."""
    seen: list[httpx_types.Request] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request)
        return _response(
            200,
            json={"data": [{"id": "gpt-4o"}]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    await discovery.list_models(base_url="http://host:8080", api_key=None, prefix="openai")
    assert seen
    assert "authorization" not in seen[0].headers


async def test_an_empty_key_is_the_same_as_no_key() -> None:
    """`""` is "the environment variable was unset", not a credential."""
    seen: list[httpx_types.Request] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request)
        return _response(
            200,
            json={"data": [{"id": "m"}]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    await discovery.list_models(base_url="http://host:8080", api_key="", prefix="openai")
    assert seen
    assert "authorization" not in seen[0].headers


async def test_both_attempts_stay_on_the_operators_own_origin() -> None:
    """Every request — the fallback included — goes to the configured host.

    The credential is sent because the operator named this endpoint. That
    consent does not travel: no attempt may be built against any other
    scheme, host or port.
    """
    seen: list[httpx_types.Request] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request)
        return _response(404)

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    assert (
        await discovery.list_models(
            base_url="http://host.internal:8080", api_key=_SECRET, prefix="openai"
        )
        == ()
    )

    assert [str(request.url) for request in seen] == [
        "http://host.internal:8080/v1/models",
        "http://host.internal:8080/api/tags",
    ]
    for request in seen:
        assert (request.url.scheme, request.url.host, request.url.port) == (
            "http",
            "host.internal",
            8080,
        )


async def test_a_redirect_never_carries_the_credential_elsewhere() -> None:
    """A 302 is a failed attempt, not an instruction.

    An endpoint that answers `Location: https://evil.example/...` must not
    be able to collect the operator's key — and the fallback attempt is
    held to the same rule, so the second request cannot be redirected
    either. The client is even built with `follow_redirects=True` here:
    the refusal belongs to the request, not to a client default that a
    different factory could quietly reverse.
    """
    seen: list[httpx_types.Request] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request)
        return _response(302, headers={"location": "https://evil.example/v1/models"})

    discovery = EndpointDiscovery(client_factory=_factory(handler, follow_redirects=True))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=_SECRET, prefix="openai"
    )

    assert entries == ()
    assert [request.url.host for request in seen] == ["host", "host"]
    for request in seen:
        assert request.headers.get("authorization") == f"Bearer {_SECRET}"


async def test_a_meaningless_base_url_builds_nothing_and_sends_nothing() -> None:
    """No scheme, no host, no client, no request — and so no key on the wire.

    The credential is spent on the endpoint the operator named. When what
    they typed names no endpoint at all, discovery stops before it has an
    HTTP client to spend it with, rather than handing an unusable URL to
    httpx and reading the outcome out of an exception.
    """
    built = 0
    attempted: list[str] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        attempted.append(str(request.url))
        return _response(404)

    def factory() -> httpx_types.AsyncClient:
        nonlocal built
        built += 1
        return _factory(handler)()

    discovery = EndpointDiscovery(client_factory=factory)
    for base_url in ("", "   ", "/v1", "host:8080", "ftp://host/models"):
        assert (
            await discovery.list_models(base_url=base_url, api_key=_SECRET, prefix="openai") == ()
        )

    assert built == 0
    assert attempted == []


async def test_the_credential_never_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure is diagnosable without becoming a credential leak.

    Modelled on the real leak shape: a transport whose error text quotes
    the request headers back. Debug logging formats the failure itself
    rather than handing the exception to `exc_info`, precisely so the key
    can be scrubbed out of whatever the failure happens to say.
    """

    def echoing_boom(request: httpx_types.Request) -> httpx_types.Response:
        raise httpx.ConnectError(
            f"proxy rejected request: authorization={request.headers.get('authorization')}",
            request=request,
        )

    discovery = EndpointDiscovery(client_factory=_factory(echoing_boom))
    with caplog.at_level(logging.DEBUG, logger="korvid.providers.endpoint_discovery"):
        entries = await discovery.list_models(
            base_url="http://host:8080", api_key=_SECRET, prefix="openai"
        )

    assert entries == ()
    # The failure is reported...
    assert caplog.records
    # ...and the key is in no part of the report.
    assert _SECRET not in caplog.text
    assert not any(_SECRET in str(record.args) for record in caplog.records)
    assert "***" in caplog.text


async def test_the_credential_is_not_kept_after_the_call() -> None:
    """Discovery borrows the key for one call; it never holds one."""
    handler = _routes(
        {
            "/v1/models": _response(
                200,
                json={"data": [{"id": "m"}]},
                headers={"content-type": "application/json"},
            ),
        }
    )
    discovery = EndpointDiscovery(client_factory=_factory(handler))
    assert await discovery.list_models(
        base_url="http://host:8080", api_key=_SECRET, prefix="openai"
    )

    assert _SECRET not in repr(vars(discovery))
    assert _SECRET not in repr(discovery)


# ---------------------------------------------------------------------------
# One deadline over the whole operation, not one per socket call
# ---------------------------------------------------------------------------


async def test_a_slow_drip_cannot_outlast_the_whole_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Many sub-budget chunks must not add up past the total deadline.

    An HTTP client's `timeout=` is spent per operation, so an endpoint
    that answers every individual read comfortably inside the limit can
    hold the setup screen open for as long as it likes. The documented
    bound is a whole-operation bound, so it is enforced as one.

    Deterministic by construction: `asyncio.sleep` never returns early, so
    delivering every chunk provably cannot fit in the budget. Nothing here
    asserts on elapsed time.
    """
    budget = 0.05
    per_chunk = 0.02
    total_chunks = 200

    # The premise: each individual read is well inside the budget, so no
    # per-operation timeout would ever fire.
    assert per_chunk < budget
    assert per_chunk * total_chunks > budget

    monkeypatch.setattr(endpoint_discovery, "_TIMEOUT_SECONDS", budget)

    delivered: list[int] = []

    async def drip() -> AsyncIterator[bytes]:
        yield b'{"data": ['
        for i in range(total_chunks):
            await asyncio.sleep(per_chunk)
            delivered.append(i)
            yield b'{"id": "m%d"},' % i
        yield b'{"id": "last"}]}'

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        return _response(
            200,
            content=drip(),
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:8080", api_key=None, prefix="openai"
    )

    assert entries == ()
    # Cut short: the read never got through the drip.
    assert len(delivered) < total_chunks


async def test_the_budget_covers_both_attempts_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two attempts share one deadline; they do not each get their own.

    Each attempt here answers inside the budget, so a per-request timeout
    would let the pair run on to a successful listing — twice the bound
    the docstring promises. The whole operation is what is bounded, so the
    second attempt is cut off and the answer is `()`.
    """
    budget = 0.5
    per_attempt = 0.3

    assert per_attempt < budget  # each attempt is individually inside the bound
    assert per_attempt * 2 > budget  # the pair provably is not

    monkeypatch.setattr(endpoint_discovery, "_TIMEOUT_SECONDS", budget)

    attempts: list[str] = []

    async def handler(request: httpx_types.Request) -> httpx_types.Response:
        attempts.append(request.url.path)
        await asyncio.sleep(per_attempt)
        if request.url.path == "/v1/models":
            return _response(404)
        return _response(
            200,
            json={"models": [{"name": "llama3"}]},
            headers={"content-type": "application/json"},
        )

    discovery = EndpointDiscovery(client_factory=_factory(handler))
    entries = await discovery.list_models(
        base_url="http://host:11434", api_key=None, prefix="ollama"
    )

    assert entries == ()
    assert attempts[0] == "/v1/models"


# ---------------------------------------------------------------------------
# `network.ca_bundle` reaches this client too (issue #168)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _tls_models_server(
    cert_pem: Path, key_pem: Path, seen: list[tuple[str, dict[str, str]]]
) -> Iterator[str]:
    """A local HTTPS endpoint that lists one model and records the request."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server API name
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            body = json.dumps({"data": [{"id": "private-model"}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.minimum_version = ssl.TLSVersion.TLSv1_2  # no legacy TLS
    server_ctx.load_cert_chain(certfile=str(cert_pem), keyfile=str(key_pem))
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def test_the_default_client_is_built_through_korvids_trust(tmp_path: Path) -> None:
    """Discovery is a korvid-owned HTTPS client like any other.

    Built through `net.make_client`, so a configured bundle produces the
    same verifying context — and the same "TLS verification failed against
    network.ca_bundle '<path>'" message — as the live providers, the
    wizard's probe and the models.dev refresh. Nothing here can express
    "do not verify".
    """
    from korvid.providers.net import _CANamedClient

    ca_pem, _, _ = mint_ca_and_server_cert(tmp_path)

    configured = endpoint_discovery._default_client_factory(str(ca_pem))
    default = endpoint_discovery._default_client_factory(None)
    try:
        assert isinstance(configured, _CANamedClient)
        assert configured._ca_bundle_path == str(ca_pem)
        # No bundle: httpx default trust, untouched. Not a downgrade.
        assert isinstance(default, httpx.AsyncClient)
        assert not isinstance(default, _CANamedClient)
    finally:
        await configured.aclose()
        await default.aclose()


def test_constructing_discovery_builds_no_client_and_reads_no_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundle is held, not opened, until someone asks for a listing.

    Building a client during wiring would read the CA bundle off disk on
    every start, and turn a misconfigured `network.ca_bundle` into a
    failure to launch the TUI at all.
    """
    from korvid.providers import net

    def _refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("no HTTPS client may be built before a listing is requested")

    monkeypatch.setattr(net, "make_client", _refuse)

    assert EndpointDiscovery(ca_bundle="/etc/pki/does-not-exist.pem") is not None


async def test_an_unreadable_bundle_is_a_silent_empty_listing() -> None:
    """Trust is never downgraded to get an answer.

    A bundle that cannot be loaded raises out of `net.build_verify`; the
    listing reports the same `()` as a refused connection rather than
    retrying without verification.
    """
    discovery = EndpointDiscovery(ca_bundle="/etc/pki/does-not-exist.pem")
    entries = await discovery.list_models(
        base_url="https://llm.corp.example", api_key=_SECRET, prefix="openai"
    )
    assert entries == ()


async def test_a_private_ca_endpoint_needs_the_configured_bundle(tmp_path: Path) -> None:
    """End to end over real TLS: the bundle decides, and only the bundle.

    A deployment behind a TLS-inspecting proxy is the case this exists
    for — with `network.ca_bundle` unwired, discovery answered `()`
    forever and the operator had to type every model name by hand. The
    untrusted half is also the no-downgrade proof: a client that skipped
    verification would list models there.
    """
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    seen: list[tuple[str, dict[str, str]]] = []

    with _tls_models_server(cert_pem, key_pem, seen) as base_url:
        untrusted = EndpointDiscovery()
        assert await untrusted.list_models(base_url=base_url, api_key=None, prefix="openai") == ()
        assert seen == []

        trusted = EndpointDiscovery(ca_bundle=str(ca_pem))
        entries = await trusted.list_models(base_url=base_url, api_key=_SECRET, prefix="openai")

    assert [entry.reference for entry in entries] == ["openai/private-model"]


async def test_the_tls_request_carries_the_key_and_no_trust_configuration(
    tmp_path: Path,
) -> None:
    """What the server actually received, through the production client.

    The credential arrives bearer-shaped at the endpoint the operator
    named — and the configured bundle path, which is transport
    configuration and never payload, appears nowhere in the request.
    """
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    seen: list[tuple[str, dict[str, str]]] = []

    with _tls_models_server(cert_pem, key_pem, seen) as base_url:
        discovery = EndpointDiscovery(ca_bundle=str(ca_pem))
        assert await discovery.list_models(base_url=base_url, api_key=_SECRET, prefix="openai")

    assert len(seen) == 1
    path, headers = seen[0]
    assert path == "/v1/models"  # no query string, ever
    assert headers["authorization"] == f"Bearer {_SECRET}"
    assert not any(str(ca_pem) in value for value in headers.values())
    assert str(ca_pem) not in path
