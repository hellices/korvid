from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

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
    """Async context manager + iterator that yields a fixed list of watch events.

    If *raise_at* is set, an ApiException is raised instead of returning that
    event — used to test mid-stream error propagation.
    """

    def __init__(
        self,
        events: list[dict[str, Any]],
        captured: dict[str, Any],
        raise_at: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._events = events
        self._captured = captured
        self._idx = 0
        self._raise_at = raise_at
        self._raise_exc = raise_exc

    async def __aenter__(self) -> "_FakeWatchStream":
        return self

    async def __aexit__(self, *_: object) -> None:
        pass

    def __aiter__(self) -> "_FakeWatchStream":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if self._raise_at is not None and self._idx == self._raise_at:
            assert self._raise_exc is not None
            raise self._raise_exc
        if self._idx >= len(self._events):
            raise StopAsyncIteration
        ev = self._events[self._idx]
        self._idx += 1
        return ev


class _FakeWatch:
    """Drop-in for k8s_watch.Watch in unit tests."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        raise_at: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._events = events
        self._raise_at = raise_at
        self._raise_exc = raise_exc
        self.captured_func: Any = None
        self.captured_args: tuple[Any, ...] = ()
        self.captured_kwargs: dict[str, Any] = {}

    def stream(self, func: Any, *args: Any, **kwargs: Any) -> _FakeWatchStream:
        self.captured_func = func
        self.captured_args = args
        self.captured_kwargs = kwargs
        return _FakeWatchStream(self._events, self.captured_kwargs, self._raise_at, self._raise_exc)


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
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden") as exc_info,
    ):
        async for _ in client.watch_pods("default"):
            pass
    assert exc_info.value.status == 403


async def test_watch_pods_all_namespaces_uses_cluster_path() -> None:
    """watch_pods(None) LISTs /api/v1/pods without a /namespaces/ segment."""
    client = KubeClient()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "100"}, "items": []}
    request_json_mock = AsyncMock(return_value=list_resp)
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_pods(None):
            pass

    called_path: str = request_json_mock.call_args[0][0]
    assert "/namespaces/" not in called_path
    assert called_path == "/api/v1/pods"


async def test_list_namespaces_api_error_raises_api_status_error() -> None:
    """ApiException must not cross the k8s boundary from list_namespaces."""
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
    """namespace=None uses a cluster-scoped LIST path (no /namespaces/ segment)."""
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
    # The watch callable must also target the cluster-scoped (no /namespaces/) path.
    # With the raw-callable approach, captured_args is empty (path is closed over).
    assert "/namespaces/" not in str(fake_watch.captured_args)


async def test_watch_objects_cluster_scoped_kind_ignores_namespace() -> None:
    """A cluster-scoped kind (namespaced=False) never uses a /namespaces/ path."""
    client = KubeClient()
    meta = ResourceMeta("Node", "nodes", "", "v1", False)
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "42"}, "items": []}
    request_json_mock = AsyncMock(return_value=list_resp)
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass

    called_path: str = request_json_mock.call_args[0][0]
    assert called_path == "/api/v1/nodes"


# ---------------------------------------------------------------------------
# get_object
# ---------------------------------------------------------------------------


async def test_get_object_raises_api_status_error() -> None:
    """ApiException from the raw GET is wrapped as ApiStatusError."""
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
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_event.side_effect = ApiException(status=403, reason="Forbidden")

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        await client.list_events_for("default", "my-pod")


# ---------------------------------------------------------------------------
# watch_objects — core-group regression (group == "")
# ---------------------------------------------------------------------------


def _service_meta() -> ResourceMeta:
    return ResourceMeta("Service", "services", "", "v1", True)


async def test_watch_objects_core_group_does_not_use_empty_group_in_watch() -> None:
    """Regression: watch for core resources (group=="") must NOT call CustomObjectsApi.

    CustomObjectsApi.list_*_custom_object with group="" produces the URL
    /apis//v1/... which returns 404.  The watch callable must be built from
    the raw api_base path ("/api/v1/...") rather than via CustomObjectsApi.
    """
    client = KubeClient()
    meta = _service_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "42"}, "items": []}
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass

    # With the broken CustomObjectsApi approach, the first positional arg passed
    # to Watch.stream is the empty group string ""; asserting it is absent proves
    # we no longer route core resources through /apis//v1/...
    assert "" not in fake_watch.captured_args, (
        "Watch received empty group as positional arg — would produce /apis//v1/... (404)"
    )


async def test_watch_objects_core_group_watch_list_path_is_api_v1() -> None:
    """The watch callable for a core-group resource must close over the /api/v1 path.

    This verifies that meta.api_base ("/api/v1") is used, not "/apis//v1".
    """
    client = KubeClient()
    meta = _service_meta()  # api_base == "/api/v1"
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "1"}, "items": []}
    fake_watch = _FakeWatch([])
    mock_api = MagicMock()
    watch_call_paths: list[str] = []

    async def _fake_watch_call(*args: Any, **kwargs: Any) -> Any:
        watch_call_paths.append(args[0] if args else "")
        return MagicMock()

    mock_api.call_api = _fake_watch_call

    with (
        patch.object(client, "_api", mock_api),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass

        # Invoke the captured callable while the patch is still active.
        await fake_watch.captured_func(watch=True, _preload_content=False, resource_version="1")

    assert len(watch_call_paths) == 1
    assert "/apis//" not in watch_call_paths[0], (
        f"watch path must not contain /apis//: got {watch_call_paths[0]!r}"
    )
    assert watch_call_paths[0].startswith("/api/v1"), (
        f"core-group watch path must start with /api/v1: got {watch_call_paths[0]!r}"
    )


# ---------------------------------------------------------------------------
# watch_objects — mid-stream error propagation
# ---------------------------------------------------------------------------


async def test_watch_objects_mid_stream_api_exception_raises_api_status_error() -> None:
    """An ApiException raised inside the watch stream must surface as ApiStatusError."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "10"}, "items": []}
    watch_events = [{"type": "ADDED", "raw_object": _generic("dep-x")}]
    mid_stream_exc = ApiException(status=410, reason="Gone")
    # Raise on index 1 (after the first valid event)
    fake_watch = _FakeWatch(watch_events, raise_at=1, raise_exc=mid_stream_exc)

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
        pytest.raises(ApiStatusError, match="API 410: Gone"),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass


# ---------------------------------------------------------------------------
# Path encoding & fieldSelector validation (review hardening)
# ---------------------------------------------------------------------------


async def test_watch_objects_encodes_namespace_in_list_path() -> None:
    """A namespace with path metacharacters must be percent-encoded, not interpolated raw."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "1"}, "items": []}
    request_json_mock = AsyncMock(return_value=list_resp)
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        async for _ in client.watch_objects(meta, "bad/ns?watch=true"):
            pass

    called_path: str = request_json_mock.call_args[0][0]
    assert "bad/ns" not in called_path
    assert "bad%2Fns%3Fwatch%3Dtrue" in called_path


async def test_get_object_encodes_namespace_and_name() -> None:
    """Namespace and name segments are percent-encoded in the GET path."""
    client = KubeClient()
    meta = _deploy_meta()
    request_json_mock = AsyncMock(return_value={"kind": "Deployment"})

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
    ):
        await client.get_object(meta, "team/a", "dep#1")

    called_path: str = request_json_mock.call_args[0][0]
    assert "team%2Fa" in called_path
    assert "dep%231" in called_path
    assert "team/a" not in called_path


async def test_list_events_for_rejects_invalid_name() -> None:
    """A name failing DNS-1123 validation short-circuits to [] (no fieldSelector injection)."""
    client = KubeClient()
    fake_v1 = AsyncMock()

    with patch.object(client, "_core_v1", fake_v1):
        events = await client.list_events_for("default", "pod,involvedObject.kind=Secret")

    assert events == []
    fake_v1.list_namespaced_event.assert_not_awaited()


# ---------------------------------------------------------------------------
# resolve_context_name — pins kubectl subprocesses to the session's context
# ---------------------------------------------------------------------------


def test_resolve_context_name_prefers_explicit() -> None:
    from korvid.k8s.client import resolve_context_name

    assert resolve_context_name("explicit-ctx") == "explicit-ctx"


def test_resolve_context_name_reads_current_context(tmp_path: Path) -> None:
    from korvid.k8s.client import resolve_context_name

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(
        """
apiVersion: v1
kind: Config
current-context: ctx-a
clusters:
- name: cluster-a
  cluster: {server: "https://a.example"}
contexts:
- name: ctx-a
  context: {cluster: cluster-a, user: user-a}
users:
- name: user-a
  user: {}
"""
    )
    assert resolve_context_name(None, config_file=str(kubeconfig)) == "ctx-a"


def test_resolve_context_name_unresolvable_returns_none(tmp_path: Path) -> None:
    from korvid.k8s.client import resolve_context_name

    assert resolve_context_name(None, config_file=str(tmp_path / "missing")) is None
