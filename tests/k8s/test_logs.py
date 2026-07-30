"""TDD tests for k8s log streaming (Task 7)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from korvid.k8s.client import KubeClient
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine


class _FakeContent:
    """Async-iterable that yields bytes chunks; optionally raises ApiException mid-stream."""

    def __init__(
        self,
        chunks: list[bytes],
        raise_at: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._raise_at = raise_at
        self._raise_exc = raise_exc
        self._idx = 0

    def __aiter__(self) -> _FakeContent:
        return self

    async def __anext__(self) -> bytes:
        if self._raise_at is not None and self._idx == self._raise_at:
            assert self._raise_exc is not None
            raise self._raise_exc
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._idx]
        self._idx += 1
        return chunk


class _FakeResp:
    """Fake aiohttp response with async-iterable `.content` and close tracking."""

    def __init__(
        self,
        chunks: list[bytes],
        raise_at: int | None = None,
        raise_exc: Exception | None = None,
        status: int = 200,
    ) -> None:
        self.content = _FakeContent(chunks, raise_at, raise_exc)
        self.closed = False
        self.status = status
        self.reason = "OK" if 200 <= status <= 299 else "Error"

    async def read(self) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Happy-path: LogLine fields, newline stripping, unicode
# ---------------------------------------------------------------------------


async def test_stream_logs_yields_correct_log_lines() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp(
        [b"line one\n", b"r\xc3\xa9sum\xc3\xa9\n"]
    )
    with patch.object(client, "_core_v1", fake_v1):
        lines = [line async for line in client.stream_logs("ns", "mypod", "myctx")]

    assert lines[0] == LogLine(pod="mypod", container="myctx", text="line one")
    assert lines[1] == LogLine(pod="mypod", container="myctx", text="résumé")


async def test_stream_logs_strips_crlf() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([b"hello\r\n", b"world\n"])
    with patch.object(client, "_core_v1", fake_v1):
        lines = [line async for line in client.stream_logs("ns", "pod", "c")]

    assert lines[0].text == "hello"
    assert lines[1].text == "world"


async def test_stream_logs_invalid_utf8_replaced() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([b"bad \xff byte\n"])
    with patch.object(client, "_core_v1", fake_v1):
        lines = [line async for line in client.stream_logs("ns", "pod", "c")]

    assert "\ufffd" in lines[0].text  # replacement char; no crash


async def test_stream_logs_empty_line_still_yielded() -> None:
    """A line containing only a newline is a real blank log line."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([b"before\n", b"\n", b"after\n"])
    with patch.object(client, "_core_v1", fake_v1):
        lines = [line async for line in client.stream_logs("ns", "pod", "c")]

    assert len(lines) == 3
    assert lines[1].text == ""


# ---------------------------------------------------------------------------
# Kwargs: previous=True forces follow=False; tail_lines forwarded
# ---------------------------------------------------------------------------


async def test_stream_logs_previous_true_forces_follow_false() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([])
    with patch.object(client, "_core_v1", fake_v1):
        async for _ in client.stream_logs("ns", "mypod", "myctx", previous=True, tail_lines=50):
            pass

    fake_v1.read_namespaced_pod_log.assert_awaited_once_with(
        name="mypod",
        namespace="ns",
        container="myctx",
        follow=False,
        previous=True,
        tail_lines=50,
        timestamps=True,
        _preload_content=False,
    )


async def test_stream_logs_default_kwargs_forwarded() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([])
    with patch.object(client, "_core_v1", fake_v1):
        async for _ in client.stream_logs("ns", "mypod", "myctx"):
            pass

    fake_v1.read_namespaced_pod_log.assert_awaited_once_with(
        name="mypod",
        namespace="ns",
        container="myctx",
        follow=True,
        previous=False,
        tail_lines=200,
        timestamps=True,
        _preload_content=False,
    )


# ---------------------------------------------------------------------------
# Error handling: ApiException → ApiStatusError
# ---------------------------------------------------------------------------


async def test_stream_logs_api_exception_at_call_raises_api_status_error() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.side_effect = ApiException(status=403, reason="Forbidden")
    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403"),
    ):
        async for _ in client.stream_logs("ns", "mypod", "myctx"):
            pass


async def test_stream_logs_api_exception_mid_stream_raises_api_status_error() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    mid_exc = ApiException(status=500, reason="Internal Server Error")
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp(
        [b"first\n"], raise_at=1, raise_exc=mid_exc
    )
    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 500"),
    ):
        async for _ in client.stream_logs("ns", "mypod", "myctx"):
            pass


# ---------------------------------------------------------------------------
# Resource cleanup: HTTP response is closed in every exit path
# ---------------------------------------------------------------------------


async def test_stream_logs_closes_response_on_completion() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    resp = _FakeResp([b"one\n"])
    fake_v1.read_namespaced_pod_log.return_value = resp
    with patch.object(client, "_core_v1", fake_v1):
        async for _ in client.stream_logs("ns", "pod", "c"):
            pass
    assert resp.closed


async def test_stream_logs_closes_response_on_early_generator_close() -> None:
    """Cancelling consumption (pane closed) must still release the connection."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    resp = _FakeResp([b"one\n", b"two\n"])
    fake_v1.read_namespaced_pod_log.return_value = resp
    with patch.object(client, "_core_v1", fake_v1):
        gen = aiter(client.stream_logs("ns", "pod", "c"))
        await anext(gen)
        assert isinstance(gen, AsyncGenerator)
        await gen.aclose()
    assert resp.closed


async def test_stream_logs_closes_response_on_mid_stream_error() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    resp = _FakeResp([b"one\n"], raise_at=1, raise_exc=ApiException(status=500, reason="ISE"))
    fake_v1.read_namespaced_pod_log.return_value = resp
    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 500"),
    ):
        async for _ in client.stream_logs("ns", "pod", "c"):
            pass
    assert resp.closed


async def test_stream_logs_empty_container_omits_kwarg() -> None:
    """An empty container name is omitted so single-container pods use the API default."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([])
    with patch.object(client, "_core_v1", fake_v1):
        async for _ in client.stream_logs("ns", "mypod", ""):
            pass

    fake_v1.read_namespaced_pod_log.assert_awaited_once_with(
        name="mypod",
        namespace="ns",
        follow=True,
        previous=False,
        tail_lines=200,
        timestamps=True,
        _preload_content=False,
    )


# ---------------------------------------------------------------------------
# Timestamp prefix parsing (timestamps=true responses)
# ---------------------------------------------------------------------------


async def test_stream_logs_parses_and_strips_timestamp_prefix() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp(
        [b"2024-01-02T03:04:05.123456789Z hello world\n"]
    )
    with patch.object(client, "_core_v1", fake_v1):
        lines = [line async for line in client.stream_logs("ns", "pod", "c")]

    assert lines[0].text == "hello world"
    assert lines[0].timestamp == datetime(2024, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


async def test_stream_logs_unparsable_prefix_keeps_full_text() -> None:
    """Lines without a valid RFC3339 prefix are yielded untouched (timestamp=None)."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([b"plain text line\n"])
    with patch.object(client, "_core_v1", fake_v1):
        lines = [line async for line in client.stream_logs("ns", "pod", "c")]

    assert lines[0].text == "plain text line"
    assert lines[0].timestamp is None


async def test_stream_logs_timestamped_blank_line() -> None:
    """A blank log line still carries its timestamp; text becomes empty."""
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = _FakeResp([b"2024-01-02T03:04:05Z \n"])
    with patch.object(client, "_core_v1", fake_v1):
        lines = [line async for line in client.stream_logs("ns", "pod", "c")]

    assert lines[0].text == ""
    assert lines[0].timestamp is not None
