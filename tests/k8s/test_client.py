from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
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
        self.captured_func: Any = None
        self.captured_args: tuple[Any, ...] = ()
        self.captured_kwargs: dict[str, Any] = {}

    def stream(self, func: Any, *args: Any, **kwargs: Any) -> _FakeWatchStream:
        self.captured_func = func
        self.captured_args = args
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

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden") as exc_info,
    ):
        async for _ in client.watch_pods("default"):
            pass
    assert exc_info.value.status == 403


async def test_list_namespaces_api_error_raises_api_status_error() -> None:
    """ApiException must not cross the k8s boundary from list_namespaces."""
    import pytest
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespace.side_effect = ApiException(status=403, reason="Forbidden")

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        await client.list_namespaces()


async def test_list_pods_api_error_raises_api_status_error() -> None:
    """ApiException must not cross the k8s boundary from list_pods."""
    import pytest
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.side_effect = ApiException(status=401, reason="Unauthorized")

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 401: Unauthorized"),
    ):
        await client.list_pods("default")


# ---------------------------------------------------------------------------
# watch_objects
# ---------------------------------------------------------------------------


def _generic(name: str, ns: str = "default") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns, "creationTimestamp": "2024-01-01T00:00:00Z"}
    }


def _deploy_meta() -> ResourceMeta:
    return ResourceMeta("Deployment", "deployments", "apps", "v1", True)


async def test_watch_objects_yields_list_items_first() -> None:
    """Pre-existing items from the initial LIST appear as ADDED before watch events."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp = {
        "metadata": {"resourceVersion": "200"},
        "items": [_generic("dep-a"), _generic("dep-b")],
    }
    watch_events = [{"type": "MODIFIED", "raw_object": _generic("dep-a")}]
    fake_watch = _FakeWatch(watch_events)

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        collected = [(ev, s.name) async for ev, s in client.watch_objects(meta, "default")]

    assert collected[0] == ("ADDED", "dep-a")
    assert collected[1] == ("ADDED", "dep-b")
    assert collected[2] == ("MODIFIED", "dep-a")


async def test_watch_objects_passes_resource_version_to_watch() -> None:
    """resourceVersion from the LIST snapshot is forwarded to Watch.stream."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "500"}, "items": []}
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass

    assert fake_watch.captured_kwargs.get("resource_version") == "500"


async def test_watch_objects_all_namespaces_uses_cluster_path() -> None:
    """namespace=None uses a cluster-scoped LIST path and cluster watch function."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "300"}, "items": []}
    request_json_mock = AsyncMock(return_value=list_resp)
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_objects(meta, None):
            pass

    called_path: str = request_json_mock.call_args[0][0]
    assert "/namespaces/" not in called_path
    assert "cluster" in fake_watch.captured_func.__name__


# ---------------------------------------------------------------------------
# get_object
# ---------------------------------------------------------------------------


async def test_get_object_raises_api_status_error() -> None:
    """ApiException from the raw GET is wrapped as ApiStatusError."""
    import pytest
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    meta = _deploy_meta()
    mock_api = MagicMock()
    mock_api.call_api = AsyncMock(side_effect=ApiException(status=404, reason="Not Found"))

    with (
        patch.object(client, "_api", mock_api),
        pytest.raises(ApiStatusError, match="API 404: Not Found"),
    ):
        await client.get_object(meta, "default", "my-dep")


# ---------------------------------------------------------------------------
# list_events_for
# ---------------------------------------------------------------------------


async def test_list_events_for_raises_api_status_error() -> None:
    """ApiException from CoreV1 is wrapped as ApiStatusError."""
    import pytest
    from kubernetes_asyncio.client.exceptions import ApiException

    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        await client.list_events_for("default", "my-pod")
