from typing import Any
from unittest.mock import AsyncMock, patch

from korvid.k8s.client import KubeClient
from korvid.k8s.errors import ApiStatusError


def _pod(name: str, ns: str = "default") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {},
        "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
    }


class _FakeWatchStream:
    """Async context manager + iterator that yields a fixed list of watch events."""

    def __init__(self, events: list[dict[str, Any]], captured: dict[str, Any]) -> None:
        self._events = events
        self._captured = captured
        self._idx = 0

    async def __aenter__(self) -> "_FakeWatchStream":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def __aiter__(self) -> "_FakeWatchStream":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._idx >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._idx]
        self._idx += 1
        return ev


class _FakeWatch:
    """Drop-in for k8s_watch.Watch in unit tests."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events
        self.captured_kwargs: dict[str, Any] = {}

    def stream(self, func: Any, *args: Any, **kwargs: Any) -> _FakeWatchStream:
        self.captured_kwargs = kwargs
        return _FakeWatchStream(self._events, self.captured_kwargs)


async def test_list_pods_parses_summaries() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {"items": [_pod("a"), _pod("b")]}
    with patch.object(client, "_core_v1", fake_v1):
        pods = await client.list_pods("default")
    assert [p.name for p in pods] == ["a", "b"]
    fake_v1.list_namespaced_pod.assert_awaited_once_with("default", _preload_content=False)


async def test_watch_pods_yields_list_items_first() -> None:
    """Pre-existing pods from the initial LIST appear as ADDED before watch events."""
    client = KubeClient()
    list_resp = {
        "metadata": {"resourceVersion": "100"},
        "items": [_pod("alpha"), _pod("beta")],
    }
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = list_resp

    watch_events = [{"type": "MODIFIED", "raw_object": _pod("alpha")}]
    fake_watch = _FakeWatch(watch_events)

    with (
        patch.object(client, "_core_v1", fake_v1),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        collected = [(ev, p.name) async for ev, p in client.watch_pods("default")]

    assert collected[0] == ("ADDED", "alpha")
    assert collected[1] == ("ADDED", "beta")
    assert collected[2] == ("MODIFIED", "alpha")


async def test_watch_pods_passes_resource_version_to_watch() -> None:
    """resource_version captured from the LIST is forwarded to Watch.stream."""
    client = KubeClient()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "999"}, "items": []}
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = list_resp

    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_core_v1", fake_v1),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_pods("default"):
            pass  # drain

    assert fake_watch.captured_kwargs.get("resource_version") == "999"


async def test_watch_pods_list_api_error_raises_api_status_error() -> None:
    """ApiException from the initial LIST is wrapped as ApiStatusError."""
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

    import pytest

    with patch.object(client, "_core_v1", fake_v1), pytest.raises(ApiStatusError) as exc_info:
        async for _ in client.watch_pods("default"):
            pass
    assert exc_info.value.status == 403
