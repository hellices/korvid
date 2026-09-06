from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path

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

httpx = pytest.importorskip("httpx")

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


def _source(
    tmp_path: Path,
    handler,  # type: ignore[type-arg]  # httpx's sync/async transport handler is untyped
) -> ModelsDevSource:
    def factory():  # type: ignore[return]  # pytest-loaded httpx cannot expose its generic type
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return ModelsDevSource(cache_path=tmp_path / "models-dev.json", client_factory=factory)


def _ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json=_DOCUMENT,
        headers={"content-type": "application/json", "etag": '"v1"'},
    )


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
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=oversized, headers={"content-type": "application/json"})

    source = _source(tmp_path, handler)
    assert await source.refresh() is RefreshOutcome.UNAVAILABLE
    assert source.metadata("anthropic/claude-sonnet-4-5") is None


async def test_a_non_json_content_type_is_refused(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hi</html>", headers={"content-type": "text/html"})

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
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, headers={"content-type": "application/json"})

    source = _source(tmp_path, handler)
    outcome = await source.refresh()
    assert source.metadata("anthropic/m") is None or outcome is RefreshOutcome.UPDATED
    assert source.env_hints("anthropic") == ()


async def test_a_failed_refresh_keeps_the_previous_cache(tmp_path: Path) -> None:
    source = _source(tmp_path, _ok)
    await source.refresh()
    _age_cache(tmp_path / "models-dev.json", CACHE_TTL_SECONDS + 60)

    def boom(request: httpx.Request) -> httpx.Response:
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

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=drip(),
            headers={"content-type": "application/json"},
        )

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
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _ok(request)

    source = _source(tmp_path, handler)
    await source.refresh()
    assert await _source(tmp_path, handler).refresh() is RefreshOutcome.CACHED
    assert calls == 1


async def test_a_stale_cache_revalidates_with_the_stored_etag(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("if-none-match"))
        return httpx.Response(304, headers={"etag": '"v1"'})

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
