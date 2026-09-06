from __future__ import annotations

import asyncio
import contextlib
import http.server
import inspect
import json
import ssl
import stat
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from korvid.providers import models_dev
from korvid.providers.models_dev import (
    CACHE_TTL_SECONDS,
    MAX_RESPONSE_BYTES,
    MODELS_DEV_URL,
    REQUEST_TIMEOUT_SECONDS,
    ModelMetadataSource,
    ModelsDevSource,
    RefreshOutcome,
    default_cache_path,
)
from tests.providers.tls_ca import mint_ca_and_server_cert

httpx = pytest.importorskip("httpx")

if TYPE_CHECKING:
    # `httpx` above is a *value* to a type checker, so annotations written
    # as `httpx.Request` do not resolve. The real module, imported only
    # for typing, gives new code somewhere to point.
    import httpx as httpx_types

    #: What `httpx.MockTransport` accepts. Both spellings are used below:
    #: a plain function for the ordinary cases, and a coroutine function
    #: where the handler has to await something mid-request.
    _MockHandler = Callable[
        [httpx_types.Request], "httpx_types.Response | Awaitable[httpx_types.Response]"
    ]

_DOCUMENT = {
    "anthropic": {
        "id": "anthropic",
        "name": "Anthropic",
        "env": ["ANTHROPIC_API_KEY"],
        "models": {
            "claude-sonnet-4-5": {
                "id": "claude-sonnet-4-5",
                "name": "Claude Sonnet 4.5",
                "reasoning": True,
                "tool_call": True,
                "release_date": "2025-09-29",
                "limit": {"context": 200000, "output": 64000},
            }
        },
    }
}


def _source(tmp_path: Path, handler: _MockHandler) -> ModelsDevSource:
    def factory() -> httpx_types.AsyncClient:
        client: httpx_types.AsyncClient = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    return ModelsDevSource(cache_path=tmp_path / "models-dev.json", client_factory=factory)


def _ok(request: httpx_types.Request) -> httpx_types.Response:
    response: httpx_types.Response = httpx.Response(
        200,
        json=_DOCUMENT,
        headers={"content-type": "application/json", "etag": '"v1"'},
    )
    return response


def _age_cache(cache_path: Path, seconds: int) -> None:
    """Rewrite the stored `fetched_at` timestamp to be `seconds` in the past.

    Uses the envelope's own field — not os.utime — because the TTL check
    reads `fetched_at` from the JSON, not from the filesystem metadata.
    """
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    data["fetched_at"] = data["fetched_at"] - seconds
    cache_path.write_text(json.dumps(data), encoding="utf-8")


async def test_a_refresh_stores_metadata_and_hints(tmp_path: Path) -> None:
    source = _source(tmp_path, _ok)
    assert await source.refresh() is RefreshOutcome.UPDATED
    entry = source.metadata("anthropic/claude-sonnet-4-5")
    assert entry is not None
    assert entry.display_name == "Claude Sonnet 4.5"
    assert entry.context_window_tokens == 200000
    assert entry.supports_tools is True
    assert source.env_hints("anthropic") == ("ANTHROPIC_API_KEY",)


async def test_the_request_carries_no_korvid_state(tmp_path: Path) -> None:
    seen: list[httpx_types.Request] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request)
        return _ok(request)

    await _source(tmp_path, handler).refresh()
    request = seen[0]
    assert str(request.url) == MODELS_DEV_URL
    assert request.url.query == b""
    assert request.method == "GET"
    assert not request.content
    forbidden = {"authorization", "cookie", "x-api-key", "proxy-authorization"}
    assert not forbidden & {name.lower() for name in request.headers}


async def test_a_response_over_the_ceiling_is_refused(tmp_path: Path) -> None:
    oversized = b"[" + b"0," * (MAX_RESPONSE_BYTES // 2) + b"0]"

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        response: httpx_types.Response = httpx.Response(
            200, content=oversized, headers={"content-type": "application/json"}
        )
        return response

    source = _source(tmp_path, handler)
    assert await source.refresh() is RefreshOutcome.UNAVAILABLE
    assert source.metadata("anthropic/claude-sonnet-4-5") is None


async def test_a_non_json_content_type_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx_types.Request) -> httpx_types.Response:
        response: httpx_types.Response = httpx.Response(
            200, text="<html>hi</html>", headers={"content-type": "text/html"}
        )
        return response

    assert await _source(tmp_path, handler).refresh() is RefreshOutcome.UNAVAILABLE


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"anthropic": "not-an-object"},
        {"anthropic": {"models": "not-an-object"}},
        {"anthropic": {"models": {"m": {"limit": {"context": "lots"}}}}},
        {"anthropic": {"models": {"m": {"tool_call": "yes"}}}},
    ],
)
async def test_a_malformed_document_never_reaches_the_catalog(
    tmp_path: Path, payload: object
) -> None:
    def handler(request: httpx_types.Request) -> httpx_types.Response:
        response: httpx_types.Response = httpx.Response(
            200, json=payload, headers={"content-type": "application/json"}
        )
        return response

    source = _source(tmp_path, handler)
    outcome = await source.refresh()
    assert source.metadata("anthropic/m") is None or outcome is RefreshOutcome.UPDATED
    assert source.env_hints("anthropic") == ()


async def test_a_failed_refresh_keeps_the_previous_cache(tmp_path: Path) -> None:
    source = _source(tmp_path, _ok)
    await source.refresh()
    _age_cache(tmp_path / "models-dev.json", CACHE_TTL_SECONDS + 60)

    def boom(request: httpx_types.Request) -> httpx_types.Response:
        raise httpx.ConnectError("offline", request=request)

    stale = _source(tmp_path, boom)
    assert await stale.refresh() is RefreshOutcome.UNAVAILABLE
    assert stale.metadata("anthropic/claude-sonnet-4-5") is not None


async def test_a_slow_drip_cannot_outlast_the_whole_request_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Many sub-timeout chunks must not add up past the total deadline.

    `REQUEST_TIMEOUT_SECONDS` is documented as a *whole-request* budget,
    but an HTTP client spends it per operation: a server that answers
    every individual read comfortably inside the limit can hold the
    connection — and the operator's refresh — open indefinitely. The
    deadline has to wrap the entire fetch/read/parse, not each socket
    call.

    Deterministic by construction, not by wall clock: `asyncio.sleep`
    never returns early, so delivering every chunk provably cannot fit in
    the budget. Nothing here asserts on elapsed time.
    """
    budget = 0.05
    per_chunk = 0.02
    total_chunks = 200

    # The premise of the test: each individual read is well inside the
    # budget, so no per-operation timeout would ever fire.
    assert per_chunk < budget
    assert per_chunk * total_chunks > budget

    monkeypatch.setattr(models_dev, "REQUEST_TIMEOUT_SECONDS", budget)

    delivered: list[int] = []

    async def drip() -> AsyncIterator[bytes]:
        yield b'{"anthropic": {"models": {'
        for i in range(total_chunks):
            await asyncio.sleep(per_chunk)
            delivered.append(i)
            yield b'"m%d": {},' % i
        yield b'"last": {}}}}'

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        response: httpx_types.Response = httpx.Response(
            200,
            content=drip(),
            headers={"content-type": "application/json"},
        )
        return response

    # Seed a cache and age it past the TTL, so the refresh actually goes
    # out and there is stale data whose survival can be checked.
    await _source(tmp_path, _ok).refresh()
    cache_path = tmp_path / "models-dev.json"
    _age_cache(cache_path, CACHE_TTL_SECONDS + 60)
    before = cache_path.read_bytes()

    source = _source(tmp_path, handler)
    assert await source.refresh() is RefreshOutcome.UNAVAILABLE

    # Cut short: the read never got through the drip.
    assert len(delivered) < total_chunks
    # Stale data survives a timeout exactly as it survives a refused
    # connection — the failure is silent and total.
    assert source.metadata("anthropic/claude-sonnet-4-5") is not None
    assert cache_path.read_bytes() == before


async def test_a_fresh_cache_makes_no_request(tmp_path: Path) -> None:
    """The TTL governs every caller that did not explicitly ask to revalidate.

    Both spellings of "not explicit" are pinned: the default, and
    `force=False` written out. A refresh korvid decided to make on its own
    stays a cache read.
    """
    calls = 0

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        nonlocal calls
        calls += 1
        return _ok(request)

    source = _source(tmp_path, handler)
    await source.refresh()
    assert await _source(tmp_path, handler).refresh() is RefreshOutcome.CACHED
    assert await _source(tmp_path, handler).refresh(force=False) is RefreshOutcome.CACHED
    assert calls == 1


async def test_a_stale_cache_revalidates_with_the_stored_etag(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request.headers.get("if-none-match"))
        response: httpx_types.Response = httpx.Response(304, headers={"etag": '"v1"'})
        return response

    source = _source(tmp_path, _ok)
    await source.refresh()
    _age_cache(tmp_path / "models-dev.json", CACHE_TTL_SECONDS + 60)
    assert await _source(tmp_path, handler).refresh() is RefreshOutcome.NOT_MODIFIED
    assert seen == ['"v1"']


async def test_the_cache_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "models-dev.json"
    await _source(tmp_path, _ok).refresh()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_default_cache_path_follows_the_platform_convention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert default_cache_path() == tmp_path / "korvid" / "models-dev.json"


def test_the_bounds_are_actually_bounds() -> None:
    assert REQUEST_TIMEOUT_SECONDS <= 10.0
    assert MAX_RESPONSE_BYTES <= 16 * 1024 * 1024
    assert MODELS_DEV_URL.startswith("https://")


@pytest.mark.parametrize(
    ("system", "expected_parts"),
    [
        ("Darwin", ("Library", "Caches", "korvid", "models-dev.json")),
        ("Linux", (".cache", "korvid", "models-dev.json")),
    ],
)
def test_the_cache_lands_where_each_platform_keeps_caches(
    monkeypatch: pytest.MonkeyPatch, system: str, expected_parts: tuple[str, ...]
) -> None:
    """No `XDG_CACHE_HOME`, so the platform convention decides.

    macOS is the one that is easy to get wrong: `~/.cache` exists there
    too, and writing to it would put the cache somewhere no macOS tool
    (or documentation) looks.
    """
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr("korvid.providers.models_dev.platform.system", lambda: system)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/operator")))

    assert default_cache_path() == Path("/home/operator").joinpath(*expected_parts)


def test_the_windows_cache_follows_localappdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr("korvid.providers.models_dev.platform.system", lambda: "Windows")
    monkeypatch.setenv("LOCALAPPDATA", str(Path("C:/Users/op/AppData/Local")))

    assert default_cache_path() == Path("C:/Users/op/AppData/Local/korvid/models-dev.json")


def test_the_cache_path_never_depends_on_the_config_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cache is disposable; config is not. Deleting the cache must never
    be able to take a profile with it."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    path = default_cache_path()

    assert (tmp_path / "config") not in path.parents
    assert path.name == "models-dev.json"


async def test_a_source_answers_refresh_through_the_metadata_contract(tmp_path: Path) -> None:
    """`refresh` is on `ModelMetadataSource`, not only on this class.

    The catalog holds sources by the ABC, so the setup UI's refresh action
    reaches one through it — a source that declared `refresh` only on the
    concrete type would make the action's reachability depend on which
    implementation happened to be injected.
    """
    assert issubclass(ModelsDevSource, ModelMetadataSource)
    assert getattr(ModelMetadataSource.refresh, "__isabstractmethod__", False)

    source = _source(tmp_path, _ok)
    assert isinstance(await source.refresh(), RefreshOutcome)


# ---------------------------------------------------------------------------
# The explicit refresh: `force` is the only thing that bypasses the TTL
# ---------------------------------------------------------------------------


def test_the_metadata_contract_declares_the_force_flag() -> None:
    """The bypass belongs on the ABC, not on this one class.

    The catalog holds its source by `ModelMetadataSource`, so a `force`
    that existed only on `ModelsDevSource` would make an operator's
    explicit refresh work or silently degrade to a cache read depending on
    which implementation happened to be injected. Keyword-only with a
    `False` default: every existing caller keeps the freshness window, and
    a caller that wants a revalidation has to say so at the call site.
    """
    params = inspect.signature(ModelMetadataSource.refresh).parameters
    assert params["force"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["force"].default is False


async def test_an_explicit_refresh_revalidates_a_cache_inside_its_ttl(tmp_path: Path) -> None:
    """Ctrl-R means "go and look", not "read the file you already have".

    The TTL exists so korvid does not hammer models.dev on its own
    initiative. An operator who presses the refresh key has overridden
    that judgement: answering `CACHED` for up to a day makes the only
    control they have over this layer do nothing at all.
    """
    seen: list[str | None] = []

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        seen.append(request.headers.get("if-none-match"))
        response: httpx_types.Response = httpx.Response(304, headers={"etag": '"v1"'})
        return response

    await _source(tmp_path, _ok).refresh()  # a cache well inside its TTL
    source = _source(tmp_path, handler)

    assert await source.refresh(force=True) is RefreshOutcome.NOT_MODIFIED
    # Exactly one request, and a conditional one: forcing past the TTL must
    # not also throw away the ETag and pull 4 MiB that has not changed.
    assert seen == ['"v1"']


async def test_an_explicit_refresh_of_a_fresh_cache_takes_new_metadata(tmp_path: Path) -> None:
    """A forced revalidation that finds something new applies it."""
    changed = {
        "anthropic": {
            "env": ["ANTHROPIC_API_KEY"],
            "models": {"claude-sonnet-4-5": {"name": "Claude Sonnet 4.5 (new)"}},
        }
    }

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        response: httpx_types.Response = httpx.Response(
            200,
            json=changed,
            headers={"content-type": "application/json", "etag": '"v2"'},
        )
        return response

    await _source(tmp_path, _ok).refresh()
    source = _source(tmp_path, handler)

    assert await source.refresh(force=True) is RefreshOutcome.UPDATED

    entry = source.metadata("anthropic/claude-sonnet-4-5")
    assert entry is not None
    assert entry.display_name == "Claude Sonnet 4.5 (new)"
    envelope = json.loads((tmp_path / "models-dev.json").read_text(encoding="utf-8"))
    assert envelope["etag"] == '"v2"'


async def test_two_explicit_refreshes_at_once_make_one_request(tmp_path: Path) -> None:
    """Concurrent forced refreshes coalesce into a single conditional GET.

    Once `force` bypasses the TTL, the TTL is no longer the thing that
    keeps korvid from repeating itself. Two screens — or one held key that
    outran a screen's own guard — must still be one request out; the
    caller that arrives second reports what the first one got.
    """
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx_types.Request) -> httpx_types.Response:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        response: httpx_types.Response = _ok(request)
        return response

    source = _source(tmp_path, handler)
    first = asyncio.create_task(source.refresh(force=True))
    await started.wait()
    second = asyncio.create_task(source.refresh(force=True))
    await asyncio.sleep(0)  # let the joiner reach the guard before the reply
    release.set()

    outcomes = list(await asyncio.gather(first, second))

    assert outcomes == [RefreshOutcome.UPDATED, RefreshOutcome.UPDATED]
    assert calls == 1


async def test_a_forced_refresh_never_joins_a_cache_read(tmp_path: Path) -> None:
    """A cache read is not a revalidation, so it cannot answer one.

    Coalescing must not become a second TTL: a `CACHED` answer means the
    other caller never went to the network, and reporting it to a forced
    caller would re-introduce exactly the short-circuit `force` exists to
    bypass.
    """
    calls = 0

    def handler(request: httpx_types.Request) -> httpx_types.Response:
        nonlocal calls
        calls += 1
        response: httpx_types.Response = httpx.Response(304, headers={"etag": '"v1"'})
        return response

    await _source(tmp_path, _ok).refresh()
    source = _source(tmp_path, handler)

    assert await source.refresh() is RefreshOutcome.CACHED
    assert calls == 0
    assert await source.refresh(force=True) is RefreshOutcome.NOT_MODIFIED
    assert calls == 1


# ---------------------------------------------------------------------------
# `network.ca_bundle` reaches this client too (issue #168)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _tls_document_server(
    cert_pem: Path, key_pem: Path, seen: list[tuple[str, dict[str, str]]]
) -> Iterator[str]:
    """A local HTTPS server that serves `_DOCUMENT` and records the request."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # http.server API name
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
            body = json.dumps(_DOCUMENT).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.send_header("etag", '"tls-v1"')
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
        yield f"https://127.0.0.1:{server.server_address[1]}/api.json"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


async def test_the_default_client_is_built_through_korvids_trust(tmp_path: Path) -> None:
    """This client is a korvid-owned HTTPS client like any other.

    Built through `net.make_client`, so a configured bundle produces the
    same verifying context — and the same "TLS verification failed against
    network.ca_bundle '<path>'" message — as the live providers and the
    wizard's probe. Nothing here can express "do not verify".
    """
    from korvid.providers.net import _CANamedClient

    ca_pem, _, _ = mint_ca_and_server_cert(tmp_path)

    configured = cast("httpx_types.AsyncClient", models_dev._default_client_factory(str(ca_pem)))
    default = cast("httpx_types.AsyncClient", models_dev._default_client_factory(None))
    try:
        assert isinstance(configured, _CANamedClient)
        assert configured._ca_bundle_path == str(ca_pem)
        # No bundle: httpx default trust, untouched. Not a downgrade.
        assert isinstance(default, httpx.AsyncClient)
        assert not isinstance(default, _CANamedClient)
    finally:
        await configured.aclose()
        await default.aclose()


async def test_a_private_ca_endpoint_needs_the_configured_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end over real TLS: the bundle decides, and only the bundle.

    A deployment behind a TLS-inspecting proxy is the case this exists
    for — with `network.ca_bundle` unwired, the refresh key answers
    "unavailable" forever and no message says why. The untrusted half is
    also the no-downgrade proof: a client that skipped verification would
    succeed there.
    """
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    seen: list[tuple[str, dict[str, str]]] = []

    with _tls_document_server(cert_pem, key_pem, seen) as url:
        monkeypatch.setattr(models_dev, "MODELS_DEV_URL", url)

        untrusted = ModelsDevSource(cache_path=tmp_path / "untrusted.json")
        assert await untrusted.refresh(force=True) is RefreshOutcome.UNAVAILABLE
        assert untrusted.metadata("anthropic/claude-sonnet-4-5") is None

        trusted = ModelsDevSource(cache_path=tmp_path / "trusted.json", ca_bundle=str(ca_pem))
        assert await trusted.refresh(force=True) is RefreshOutcome.UPDATED
        assert trusted.metadata("anthropic/claude-sonnet-4-5") is not None


async def test_the_tls_request_carries_no_trust_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trust is transport configuration; it never becomes payload.

    Asserted on what the server actually received, through the production
    client rather than a mock transport: a bare conditional GET, no body,
    no credential header, and the configured bundle path nowhere in it.
    """
    ca_pem, cert_pem, key_pem = mint_ca_and_server_cert(tmp_path)
    seen: list[tuple[str, dict[str, str]]] = []

    with _tls_document_server(cert_pem, key_pem, seen) as url:
        monkeypatch.setattr(models_dev, "MODELS_DEV_URL", url)
        source = ModelsDevSource(cache_path=tmp_path / "cache.json", ca_bundle=str(ca_pem))
        assert await source.refresh(force=True) is RefreshOutcome.UPDATED

    assert len(seen) == 1
    path, headers = seen[0]
    assert path == "/api.json"  # no query string, ever
    forbidden = {"authorization", "cookie", "x-api-key", "proxy-authorization"}
    assert not forbidden & set(headers)
    assert int(headers.get("content-length", "0")) == 0
    assert not any(str(ca_pem) in value for value in headers.values())
