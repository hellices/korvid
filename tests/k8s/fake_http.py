"""A fake aiohttp response and API client for bounded-transport tests."""

from __future__ import annotations

from unittest.mock import AsyncMock


class Stream:
    """A response body that hands out at most 17 bytes a read."""

    def __init__(self, payload: bytes, failure: BaseException | None = None) -> None:
        self.payload = payload
        self.position = 0
        self.failure = failure

    async def read(self, size: int) -> bytes:
        if self.failure is not None:
            raise self.failure
        chunk = self.payload[self.position : self.position + min(size, 17)]
        self.position += len(chunk)
        return chunk

    def at_eof(self) -> bool:
        return self.position == len(self.payload)


class Response:
    """One HTTP response, which records whether it was closed."""

    def __init__(
        self, payload: bytes, status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.content = Stream(payload)
        self.headers = dict(headers or {})
        self.closed = False

    def close(self) -> None:
        self.closed = True


def fake_api(response: Response) -> AsyncMock:
    """An `ApiClient` stand-in whose session answers every request with `response`."""
    api = AsyncMock()
    api.configuration.host = "https://cluster.test"
    api.default_headers = {"User-Agent": "korvid-test", "Accept": "text/plain"}
    api.cookie = None
    api.rest_client.proxy = None
    api.rest_client.proxy_headers = None
    api.rest_client.server_hostname = None
    api.rest_client.pool_manager.request.return_value = response
    api.rest_client.pool_manager.__aenter__.return_value = api.rest_client.pool_manager
    api.rest_client.pool_manager.__aexit__.return_value = False
    return api


def sent(api: AsyncMock) -> dict[str, object]:
    """The keyword arguments of the request `api`'s session sent."""
    kwargs: dict[str, object] = api.rest_client.pool_manager.request.await_args.kwargs
    return kwargs
