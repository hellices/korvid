from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client.exceptions import ApiException

from korvid.k8s import client as client_mod
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import ReplicaSetSummary


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


async def test_watch_objects_replicaset_yields_rich_summary() -> None:
    """ReplicaSet kinds get ReplicaSetSummary (revision/desired/ready) via summary_for."""
    client = KubeClient()
    meta = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True, ("rs",))
    item: dict[str, Any] = {
        "metadata": {
            "name": "web-6d9f88",
            "namespace": "default",
            "uid": "rs-1",
            "annotations": {"deployment.kubernetes.io/revision": "2"},
        },
        "spec": {"replicas": 3},
        "status": {"replicas": 3, "readyReplicas": 3},
    }
    list_resp = {"metadata": {"resourceVersion": "1"}, "items": [item]}
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        collected = [s async for _, s in client.watch_objects(meta, "default")]

    assert isinstance(collected[0], ReplicaSetSummary)
    assert collected[0].revision == "2"
    assert collected[0].ready == "3/3"


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


# ---------------------------------------------------------------------------
# list_objects
# ---------------------------------------------------------------------------


async def test_list_objects_returns_generic_summaries() -> None:
    """list_objects builds the namespaced LIST path and returns GenericSummary items."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp = {
        "items": [_generic("dep-a"), _generic("dep-b")],
    }
    request_json_mock = AsyncMock(return_value=list_resp)

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
    ):
        summaries = await client.list_objects(meta, "default")

    assert [s.name for s in summaries] == ["dep-a", "dep-b"]
    called_path: str = request_json_mock.call_args[0][0]
    assert "/namespaces/default/deployments" in called_path


async def test_list_objects_all_namespaces_uses_cluster_path() -> None:
    """namespace=None produces a cluster-scoped path without /namespaces/."""
    client = KubeClient()
    meta = _deploy_meta()
    request_json_mock = AsyncMock(return_value={"items": []})

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
    ):
        summaries = await client.list_objects(meta, None)

    assert summaries == []
    called_path: str = request_json_mock.call_args[0][0]
    assert "/namespaces/" not in called_path
    assert called_path.endswith("/deployments")


async def test_list_objects_raises_api_status_error() -> None:
    """ApiException from the underlying GET is wrapped as ApiStatusError."""
    client = KubeClient()
    meta = _deploy_meta()
    mock_api = MagicMock()
    mock_api.call_api = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))

    with (
        patch.object(client, "_api", mock_api),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        await client.list_objects(meta, "default")


# Write operations (issue #16) -------------------------------------------------


def _write_api() -> MagicMock:
    """ApiClient mock whose call_api returns an empty JSON body."""
    api = MagicMock()
    resp = MagicMock()
    resp.read = AsyncMock(return_value=b"{}")
    api.call_api = AsyncMock(return_value=resp)
    return api


async def test_delete_object_issues_delete_on_object_path() -> None:
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.delete_object(_deploy_meta(), "default", "web")
    args = api.call_api.call_args[0]
    assert args[0] == "/apis/apps/v1/namespaces/default/deployments/web"
    assert args[1] == "DELETE"


async def test_delete_object_encodes_segments() -> None:
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.delete_object(_deploy_meta(), "team/a", "dep#1")
    path = api.call_api.call_args[0][0]
    assert "team%2Fa" in path
    assert "dep%231" in path
    assert "team/a" not in path


async def test_scale_object_patches_scale_subresource() -> None:
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.scale_object(_deploy_meta(), "default", "web", 5)
    args, kwargs = api.call_api.call_args
    assert args[0] == "/apis/apps/v1/namespaces/default/deployments/web/scale"
    assert args[1] == "PATCH"
    assert kwargs["body"] == {"spec": {"replicas": 5}}
    assert kwargs["header_params"]["Content-Type"] == "application/merge-patch+json"


async def test_rollout_restart_patches_restartedAt_annotation() -> None:
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.rollout_restart(_deploy_meta(), "default", "web")
    args, kwargs = api.call_api.call_args
    assert args[0] == "/apis/apps/v1/namespaces/default/deployments/web"
    assert args[1] == "PATCH"
    annotations = kwargs["body"]["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in annotations
    assert kwargs["header_params"]["Content-Type"] == "application/strategic-merge-patch+json"


async def test_write_api_error_raises_api_status_error() -> None:
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))
    with (
        patch.object(client, "_api", api),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        await client.delete_object(_deploy_meta(), "default", "web")


async def test_write_without_connect_raises() -> None:
    client = KubeClient()
    with pytest.raises(RuntimeError, match="connect"):
        await client.delete_object(_deploy_meta(), "default", "web")


async def test_write_consumes_response_body() -> None:
    """Review round 1: with _preload_content=False the caller owns the
    response; reading it releases the pooled connection."""
    client = KubeClient()
    api = _write_api()
    resp = api.call_api.return_value
    with patch.object(client, "_api", api):
        await client.delete_object(_deploy_meta(), "default", "web")
    resp.read.assert_awaited()


def _ssar_api(allowed: bool) -> MagicMock:
    api = MagicMock()
    resp = MagicMock()
    payload = b'{"status": {"allowed": true}}' if allowed else b'{"status": {"allowed": false}}'
    resp.read = AsyncMock(return_value=payload)
    api.call_api = AsyncMock(return_value=resp)
    return api


async def test_can_i_allowed() -> None:
    client = KubeClient()
    api = _ssar_api(allowed=True)
    with patch.object(client, "_api", api):
        assert await client.can_i("delete", "pods", "", "default") is True
    args, kwargs = api.call_api.call_args
    assert args[0] == "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"
    assert args[1] == "POST"
    attrs = kwargs["body"]["spec"]["resourceAttributes"]
    assert attrs == {"verb": "delete", "resource": "pods", "namespace": "default"}


async def test_can_i_denied() -> None:
    client = KubeClient()
    with patch.object(client, "_api", _ssar_api(allowed=False)):
        assert await client.can_i("patch", "deployments", "scale", "default") is False


async def test_can_i_fails_open_on_error() -> None:
    """SSAR itself may be forbidden or flaky; the write stays approval-gated
    and audited, so infrastructure errors must not block it."""
    client = KubeClient()
    api = MagicMock()
    api.call_api = AsyncMock(side_effect=ApiException(status=403, reason="Forbidden"))
    with patch.object(client, "_api", api):
        assert await client.can_i("delete", "pods", "", "default") is True


async def test_can_i_includes_group_name_and_subresource() -> None:
    """Review round 2: without the API group every apps/* check was evaluated
    against the core group and wrongly denied."""
    client = KubeClient()
    api = _ssar_api(allowed=True)
    with patch.object(client, "_api", api):
        assert await client.can_i("patch", "deployments", "scale", "prod", "apps", "web") is True
    attrs = api.call_api.call_args.kwargs["body"]["spec"]["resourceAttributes"]
    assert attrs == {
        "verb": "patch",
        "resource": "deployments",
        "group": "apps",
        "name": "web",
        "subresource": "scale",
        "namespace": "prod",
    }


async def test_delete_object_sends_uid_precondition() -> None:
    """A uid pins the delete to the approved object incarnation: the API
    server answers 409 if the object was recreated under the same name."""
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.delete_object(_deploy_meta(), "default", "web", uid="abc-123")
    args, kwargs = api.call_api.call_args
    assert args[1] == "DELETE"
    assert kwargs["body"] == {
        "propagationPolicy": "Background",
        "preconditions": {"uid": "abc-123"},
    }
    assert kwargs["header_params"]["Content-Type"] == "application/json"


async def test_delete_object_without_uid_omits_preconditions() -> None:
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.delete_object(_deploy_meta(), "default", "web")
    kwargs = api.call_api.call_args[1]
    assert kwargs["body"] == {"propagationPolicy": "Background"}


async def test_scale_object_sends_uid_precondition() -> None:
    """metadata.uid in a merge patch is an apiserver precondition (409 on mismatch)."""
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.scale_object(_deploy_meta(), "default", "web", 5, uid="abc-123")
    kwargs = api.call_api.call_args[1]
    assert kwargs["body"] == {"spec": {"replicas": 5}, "metadata": {"uid": "abc-123"}}


async def test_rollout_restart_sends_uid_precondition() -> None:
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.rollout_restart(_deploy_meta(), "default", "web", uid="abc-123")
    body = api.call_api.call_args[1]["body"]
    assert body["metadata"] == {"uid": "abc-123"}
    assert (
        "kubectl.kubernetes.io/restartedAt" in body["spec"]["template"]["metadata"]["annotations"]
    )


def test_path_segment_rejects_traversal_segments() -> None:
    """quote() leaves '.' intact, so empty and dot segments must be rejected
    before they can survive as literal traversal segments in an object URL."""
    from korvid.k8s.client import _path_segment

    for bad in ("", ".", ".."):
        with pytest.raises(ValueError, match="invalid URL path segment"):
            _path_segment(bad)
    assert _path_segment("web-1") == "web-1"
    assert _path_segment("a/b") == "a%2Fb"


async def test_delete_object_rejects_dot_name() -> None:
    """A write addressed at name '..' must fail before any request is built."""
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        with pytest.raises(ValueError, match="invalid URL path segment"):
            await client.delete_object(_deploy_meta(), "default", "..")
        with pytest.raises(ValueError, match="invalid URL path segment"):
            await client.delete_object(_deploy_meta(), "..", "web")
    api.call_api.assert_not_called()


async def test_replace_object_puts_manifest_on_object_path() -> None:
    client = KubeClient()
    api = _write_api()
    manifest = {"apiVersion": "apps/v1", "kind": "Deployment", "spec": {"replicas": 2}}
    with patch.object(client, "_api", api):
        await client.replace_object(_deploy_meta(), "default", "web", manifest)
    args, kwargs = api.call_api.call_args
    assert args[0] == "/apis/apps/v1/namespaces/default/deployments/web"
    assert args[1] == "PUT"
    assert kwargs["body"] == manifest
    assert kwargs["header_params"]["Content-Type"] == "application/json"


async def test_replace_object_pins_uid_precondition() -> None:
    """The uid is injected into metadata: the apiserver answers 409 when the
    live object is a different incarnation than the one that was approved."""
    client = KubeClient()
    api = _write_api()
    manifest = {"metadata": {"name": "web", "resourceVersion": "41"}, "spec": {}}
    with patch.object(client, "_api", api):
        await client.replace_object(_deploy_meta(), "default", "web", manifest, uid="abc-123")
    body = api.call_api.call_args.kwargs["body"]
    assert body["metadata"]["uid"] == "abc-123"
    assert body["metadata"]["resourceVersion"] == "41"
    # The caller's manifest is not mutated.
    assert "uid" not in manifest["metadata"]


async def test_create_object_posts_manifest_on_collection_path() -> None:
    """OLM install (issue #29) creates a Subscription: POST on the
    namespaced collection, manifest as the body."""
    client = KubeClient()
    api = _write_api()
    sub_meta = ResourceMeta(
        "Subscription", "subscriptions", "operators.coreos.com", "v1alpha1", True
    )
    manifest = {
        "apiVersion": "operators.coreos.com/v1alpha1",
        "kind": "Subscription",
        "metadata": {"name": "cert-manager", "namespace": "operators"},
        "spec": {"channel": "stable"},
    }
    with patch.object(client, "_api", api):
        await client.create_object(sub_meta, "operators", manifest)
    args, kwargs = api.call_api.call_args
    assert args[0] == "/apis/operators.coreos.com/v1alpha1/namespaces/operators/subscriptions"
    assert args[1] == "POST"
    assert kwargs["body"] == manifest
    assert kwargs["header_params"]["Content-Type"] == "application/json"


async def test_create_object_rejects_bad_namespace_segment() -> None:
    client = KubeClient()
    api = _write_api()
    sub_meta = ResourceMeta(
        "Subscription", "subscriptions", "operators.coreos.com", "v1alpha1", True
    )
    with (
        patch.object(client, "_api", api),
        pytest.raises(ValueError, match="invalid URL path segment"),
    ):
        await client.create_object(sub_meta, "..", {"metadata": {}})
    api.call_api.assert_not_called()


# ---------------------------------------------------------------------------
# list_pod_metrics
# ---------------------------------------------------------------------------


async def test_list_pod_metrics_namespaced_path() -> None:
    client = KubeClient()
    list_resp = {
        "items": [
            {
                "metadata": {"name": "web-1", "namespace": "default"},
                "containers": [{"usage": {"cpu": "100m", "memory": "128Mi"}}],
            }
        ]
    }
    request_json_mock = AsyncMock(return_value=list_resp)

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
    ):
        metrics = await client.list_pod_metrics("default")

    assert [(m.namespace, m.name) for m in metrics] == [("default", "web-1")]
    assert metrics[0].cpu_cores == pytest.approx(0.1)
    assert metrics[0].memory_bytes == 128 * 2**20
    called_path: str = request_json_mock.call_args[0][0]
    assert called_path == "/apis/metrics.k8s.io/v1beta1/namespaces/default/pods"


async def test_list_pod_metrics_cluster_path() -> None:
    client = KubeClient()
    request_json_mock = AsyncMock(return_value={"items": []})

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
    ):
        metrics = await client.list_pod_metrics(None)

    assert metrics == []
    called_path: str = request_json_mock.call_args[0][0]
    assert called_path == "/apis/metrics.k8s.io/v1beta1/pods"


async def test_list_pod_metrics_propagates_api_status_error() -> None:
    """404 (metrics-server absent) must surface as ApiStatusError so the
    poller can degrade gracefully."""
    client = KubeClient()
    mock_api = MagicMock()
    mock_api.call_api = AsyncMock(side_effect=ApiException(status=404, reason="Not Found"))

    with (
        patch.object(client, "_api", mock_api),
        pytest.raises(ApiStatusError, match="API 404: Not Found"),
    ):
        await client.list_pod_metrics("default")


async def test_delete_object_sends_explicit_background_propagation() -> None:
    """Omitting propagationPolicy lets finalizers/resource defaults pick the
    policy; the approval dialog promises background cascade, so the request
    states it explicitly - with and without a uid precondition."""
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.delete_object(_deploy_meta(), "default", "web")
    kwargs = api.call_api.call_args[1]
    assert kwargs["body"] == {"propagationPolicy": "Background"}
    assert kwargs["header_params"]["Content-Type"] == "application/json"


async def test_rollout_restart_with_stamp_pins_provided_stamp() -> None:
    """KubeClient overrides the timestamp-aware hook for exact replay: the
    stamp shown in the preview is the stamp the write sends."""
    client = KubeClient()
    api = _write_api()
    with patch.object(client, "_api", api):
        await client.rollout_restart_with_stamp(
            _deploy_meta(), "default", "web", uid="u-1", restarted_at="2026-07-26T00:00:00+00:00"
        )
    kwargs = api.call_api.call_args[1]
    annotations = kwargs["body"]["spec"]["template"]["metadata"]["annotations"]
    assert annotations["kubectl.kubernetes.io/restartedAt"] == "2026-07-26T00:00:00+00:00"
    assert kwargs["body"]["metadata"] == {"uid": "u-1"}


async def test_create_object_requires_namespace_for_namespaced_kind() -> None:
    """Kubernetes forbids cluster-wide POST on a namespaced collection:
    reject the bad input locally instead of sending a guaranteed-invalid
    request."""
    client = KubeClient()
    api = _write_api()
    sub_meta = ResourceMeta(
        "Subscription", "subscriptions", "operators.coreos.com", "v1alpha1", True
    )
    with (
        patch.object(client, "_api", api),
        pytest.raises(ValueError, match="requires a namespace"),
    ):
        await client.create_object(sub_meta, None, {"metadata": {}})
    api.call_api.assert_not_called()


async def test_create_object_posts_on_cluster_scoped_collection_path() -> None:
    """create_object is a generic API: a cluster-scoped kind POSTs on the
    bare collection path (no namespaces segment)."""
    client = KubeClient()
    api = _write_api()
    og_meta = ResourceMeta("ClusterThing", "clusterthings", "example.com", "v1", False)
    manifest = {
        "apiVersion": "example.com/v1",
        "kind": "ClusterThing",
        "metadata": {"name": "global"},
    }
    with patch.object(client, "_api", api):
        await client.create_object(og_meta, None, manifest)
    args, kwargs = api.call_api.call_args
    assert args[0] == "/apis/example.com/v1/clusterthings"
    assert args[1] == "POST"
    assert kwargs["body"] == manifest


class TestOpenPodExec:
    """open_pod_exec builds the exec websocket session for file transfer (issue #47)."""

    def test_requires_connect(self) -> None:
        kube = client_mod.KubeClient()
        with pytest.raises(RuntimeError, match="connect"):
            kube.open_pod_exec("ns", "pod", None, ["tar"], stdin=False)

    async def test_opens_ws_with_exec_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sentinel_ws = object()
        closed: list[bool] = []
        captured: dict[str, object] = {}

        class FakeWsApi:
            async def close(self) -> None:
                closed.append(True)

        class FakeWsCtx:
            async def __aenter__(self) -> object:
                return sentinel_ws

            async def __aexit__(self, *exc: object) -> None:
                return None

        class FakeCoreWs:
            def __init__(self, api: object) -> None:
                captured["api"] = api

            async def connect_get_namespaced_pod_exec(
                self, name: str, namespace: str, **kwargs: object
            ) -> FakeWsCtx:
                captured["name"] = name
                captured["namespace"] = namespace
                captured.update(kwargs)
                return FakeWsCtx()

        monkeypatch.setattr(client_mod, "WsApiClient", FakeWsApi)
        monkeypatch.setattr(k8s_client, "CoreV1Api", FakeCoreWs)
        kube = client_mod.KubeClient()
        kube._core_v1 = object()  # type: ignore[assignment]  # connected marker

        async with kube.open_pod_exec(
            "prod", "api-0", "app", ["tar", "cf", "-"], stdin=False
        ) as ws:
            assert ws is sentinel_ws
        assert captured["name"] == "api-0"
        assert captured["namespace"] == "prod"
        assert captured["command"] == ["tar", "cf", "-"]
        assert captured["container"] == "app"
        assert captured["stdin"] is False
        assert captured["stdout"] is True
        assert captured["stderr"] is True
        assert captured["tty"] is False
        assert captured["_preload_content"] is False
        assert closed == [True]

    async def test_omits_container_when_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        class FakeWsApi:
            async def close(self) -> None:
                return None

        class FakeWsCtx:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, *exc: object) -> None:
                return None

        class FakeCoreWs:
            def __init__(self, api: object) -> None:
                pass

            async def connect_get_namespaced_pod_exec(
                self, name: str, namespace: str, **kwargs: object
            ) -> FakeWsCtx:
                captured.update(kwargs)
                return FakeWsCtx()

        monkeypatch.setattr(client_mod, "WsApiClient", FakeWsApi)
        monkeypatch.setattr(k8s_client, "CoreV1Api", FakeCoreWs)
        kube = client_mod.KubeClient()
        kube._core_v1 = object()  # type: ignore[assignment]  # connected marker

        async with kube.open_pod_exec("ns", "p", None, ["tar"], stdin=True):
            pass
        assert "container" not in captured
        assert captured["stdin"] is True

    async def test_ws_api_closed_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        closed: list[bool] = []

        class FakeWsApi:
            async def close(self) -> None:
                closed.append(True)

        class FakeCoreWs:
            def __init__(self, api: object) -> None:
                pass

            async def connect_get_namespaced_pod_exec(self, *a: object, **k: object) -> object:
                raise OSError("boom")

        monkeypatch.setattr(client_mod, "WsApiClient", FakeWsApi)
        monkeypatch.setattr(k8s_client, "CoreV1Api", FakeCoreWs)
        kube = client_mod.KubeClient()
        kube._core_v1 = object()  # type: ignore[assignment]  # connected marker

        with pytest.raises(OSError, match="boom"):
            async with kube.open_pod_exec("ns", "p", None, ["tar"], stdin=False):
                pass
        assert closed == [True]


# ---------------------------------------------------------------------------
# resolve_context_namespace — kubeconfig fallback for RBAC-limited users (#49)
# ---------------------------------------------------------------------------

_KUBECONFIG_WITH_NAMESPACES = """
apiVersion: v1
kind: Config
current-context: ctx-a
clusters:
- name: cluster-a
  cluster: {server: "https://a.example"}
contexts:
- name: ctx-a
  context: {cluster: cluster-a, user: user-a, namespace: team-a}
- name: ctx-b
  context: {cluster: cluster-a, user: user-a, namespace: team-b}
- name: ctx-bare
  context: {cluster: cluster-a, user: user-a}
users:
- name: user-a
  user: {}
"""


def test_resolve_context_namespace_reads_active_context(tmp_path: Path) -> None:
    from korvid.k8s.client import resolve_context_namespace

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(_KUBECONFIG_WITH_NAMESPACES)
    assert resolve_context_namespace(None, config_file=str(kubeconfig)) == "team-a"


def test_resolve_context_namespace_honors_named_context(tmp_path: Path) -> None:
    from korvid.k8s.client import resolve_context_namespace

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(_KUBECONFIG_WITH_NAMESPACES)
    assert resolve_context_namespace("ctx-b", config_file=str(kubeconfig)) == "team-b"


def test_resolve_context_namespace_without_namespace_returns_none(tmp_path: Path) -> None:
    from korvid.k8s.client import resolve_context_namespace

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(_KUBECONFIG_WITH_NAMESPACES)
    assert resolve_context_namespace("ctx-bare", config_file=str(kubeconfig)) is None


def test_resolve_context_namespace_unresolvable_returns_none(tmp_path: Path) -> None:
    from korvid.k8s.client import resolve_context_namespace

    assert resolve_context_namespace(None, config_file=str(tmp_path / "missing")) is None


# ---------------------------------------------------------------------------
# custom columns (issue #45)
# ---------------------------------------------------------------------------


def _labeled_generic(name: str, team: str) -> dict[str, Any]:
    manifest = _generic(name)
    manifest["metadata"]["labels"] = {"team": team}
    return manifest


async def test_list_objects_fills_custom_column_values() -> None:
    from korvid.k8s.columns import CustomColumn

    client = KubeClient(custom_columns={"deployments": (CustomColumn("TEAM", "label", "team"),)})
    list_resp = {"items": [_labeled_generic("dep-a", "payments")]}
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
    ):
        summaries = await client.list_objects(_deploy_meta(), "default")
    assert summaries[0].custom == ("payments",)


async def test_watch_objects_fills_custom_column_values() -> None:
    from korvid.k8s.columns import CustomColumn

    client = KubeClient(custom_columns={"deployments": (CustomColumn("TEAM", "label", "team"),)})
    list_resp = {
        "metadata": {"resourceVersion": "7"},
        "items": [_labeled_generic("dep-a", "payments")],
    }
    watch_events = [{"type": "MODIFIED", "raw_object": _labeled_generic("dep-a", "billing")}]
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch(watch_events)),
    ):
        seen = [
            summary.custom async for _, summary in client.watch_objects(_deploy_meta(), "default")
        ]
    assert seen == [("payments",), ("billing",)]


async def test_watch_pods_fills_custom_column_values() -> None:
    from korvid.k8s.columns import CustomColumn

    client = KubeClient(custom_columns={"pods": (CustomColumn("TEAM", "label", "team"),)})
    pod = _pod("api-1")
    pod["metadata"]["labels"] = {"team": "payments"}
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {
        "metadata": {"resourceVersion": "5"},
        "items": [pod],
    }
    with (
        patch.object(client, "_core_v1", fake_v1),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch([])),
    ):
        seen = [summary.custom async for _, summary in client.watch_pods("default")]
    assert seen == [("payments",)]


async def test_kinds_without_configured_columns_keep_empty_custom() -> None:
    from korvid.k8s.columns import CustomColumn

    client = KubeClient(custom_columns={"services": (CustomColumn("TEAM", "label", "team"),)})
    list_resp = {"items": [_labeled_generic("dep-a", "payments")]}
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
    ):
        summaries = await client.list_objects(_deploy_meta(), "default")
    assert summaries[0].custom == ()
