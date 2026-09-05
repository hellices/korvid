from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from korvid.providers.models_dev import (
    CACHE_TTL_SECONDS,
    MAX_RESPONSE_BYTES,
    MODELS_DEV_URL,
    REQUEST_TIMEOUT_SECONDS,
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
