from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client.exceptions import ApiException

from korvid.k8s import client as client_mod
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import ReplicaSetSummary
from korvid.k8s.telemetry import ReadTelemetryEvent


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


async def test_list_namespaces_emits_list_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    fake_v1 = AsyncMock()
    fake_v1.list_namespace.return_value = {
        "items": [{"metadata": {"name": "default"}}, {"metadata": {"name": "kube-system"}}]
    }

    with patch.object(client, "_core_v1", fake_v1):
        namespaces = await client.list_namespaces()

    assert namespaces == ["default", "kube-system"]
    assert [event.operation for event in seen] == ["list"]
    assert seen[0].path == "/api/v1/namespaces"
    assert seen[0].object_count == 2
    assert seen[0].decoded_bytes > 0
    assert seen[0].status is None


async def test_list_pods_parses_summaries() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {"items": [_pod("a"), _pod("b")]}
    with patch.object(client, "_core_v1", fake_v1):
        pods = await client.list_pods("default")
    assert [p.name for p in pods] == ["a", "b"]
    fake_v1.list_namespaced_pod.assert_awaited_once_with("default", _preload_content=False)


async def test_list_pods_emits_list_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {"items": [_pod("a"), _pod("b")]}

    with patch.object(client, "_core_v1", fake_v1):
        pods = await client.list_pods("default")

    assert [pod.name for pod in pods] == ["a", "b"]
    assert [event.operation for event in seen] == ["list"]
    assert seen[0].path == "/api/v1/namespaces/default/pods"
    assert seen[0].object_count == 2
    assert seen[0].decoded_bytes > 0
    assert seen[0].status is None


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


async def test_pod_watch_emits_list_open_and_event_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {
        "metadata": {"resourceVersion": "100"},
        "items": [_pod("listed")],
    }
    fake_watch = _FakeWatch([{"type": "MODIFIED", "raw_object": _pod("watched")}])

    with (
        patch.object(client, "_core_v1", fake_v1),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        collected = [
            (event_type, pod.name) async for event_type, pod in client.watch_pods("default")
        ]

    assert collected == [("ADDED", "listed"), ("MODIFIED", "watched")]
    assert [event.operation for event in seen] == ["list", "watch_open", "watch_event"]
    assert {event.path for event in seen} == {"/api/v1/namespaces/default/pods"}
    assert seen[0].object_count == 1
    assert seen[0].decoded_bytes > 0
    assert seen[1].decoded_bytes == 0
    assert seen[2].object_count == 1
    assert seen[2].decoded_bytes > 0


async def test_no_telemetry_preserves_existing_watch_behavior() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {
        "metadata": {"resourceVersion": "100"},
        "items": [_pod("listed")],
    }
    fake_watch = _FakeWatch([])

    with (
        patch.object(client, "_core_v1", fake_v1),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
        patch(
            "korvid.k8s.client.json.dumps", side_effect=AssertionError("unexpected serialization")
        ),
    ):
        collected = [
            (event_type, pod.name) async for event_type, pod in client.watch_pods("default")
        ]

    assert collected == [("ADDED", "listed")]


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


async def test_watch_pods_list_error_emits_error_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.side_effect = ApiException(status=403, reason="Forbidden")

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        async for _ in client.watch_pods("default"):
            pass

    assert [event.operation for event in seen] == ["error"]
    assert seen[0].path == "/api/v1/namespaces/default/pods"
    assert seen[0].status == 403
    assert seen[0].decoded_bytes == 0
    assert seen[0].object_count == 0


async def test_watch_pods_watch_error_emits_error_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {
        "metadata": {"resourceVersion": "100"},
        "items": [],
    }
    fake_watch = _FakeWatch([], raise_at=0, raise_exc=ApiException(status=410, reason="Gone"))

    with (
        patch.object(client, "_core_v1", fake_v1),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
        pytest.raises(ApiStatusError, match="API 410: Gone"),
    ):
        async for _ in client.watch_pods("default"):
            pass

    assert [event.operation for event in seen] == ["list", "watch_open", "error"]
    assert seen[-1].path == "/api/v1/namespaces/default/pods"
    assert seen[-1].status == 410


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


async def test_list_namespaces_error_emits_error_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    fake_v1 = AsyncMock()
    fake_v1.list_namespace.side_effect = ApiException(status=403, reason="Forbidden")

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        await client.list_namespaces()

    assert [event.operation for event in seen] == ["error"]
    assert seen[0].path == "/api/v1/namespaces"
    assert seen[0].status == 403
    assert seen[0].decoded_bytes == 0
    assert seen[0].object_count == 0


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


async def test_watch_objects_emits_list_open_and_event_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    meta = _deploy_meta()
    list_resp = {
        "metadata": {"resourceVersion": "200"},
        "items": [_generic("dep-a")],
    }
    watch_events = [{"type": "MODIFIED", "raw_object": _generic("dep-a")}]
    fake_watch = _FakeWatch(watch_events)

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        collected = [
            (ev, summary.name) async for ev, summary in client.watch_objects(meta, "default")
        ]

    assert collected == [("ADDED", "dep-a"), ("MODIFIED", "dep-a")]
    assert [event.operation for event in seen] == ["list", "watch_open", "watch_event"]
    assert {event.path for event in seen} == {"/apis/apps/v1/namespaces/default/deployments"}
    assert seen[0].object_count == 1
    assert seen[0].decoded_bytes > 0
    assert seen[1].decoded_bytes == 0
    assert seen[2].object_count == 1
    assert seen[2].decoded_bytes > 0


async def test_watch_objects_initial_snapshot_reuses_computed_summaries() -> None:
    client = KubeClient()
    meta = _deploy_meta()
    list_resp = {
        "metadata": {"resourceVersion": "200"},
        "items": [_generic("dep-a")],
    }
    fake_watch = _FakeWatch([])
    original_summary = client._object_summary

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch.object(client, "_object_summary", side_effect=original_summary) as summary_mock,
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        collected = [
            (ev, summary.name) async for ev, summary in client.watch_objects(meta, "default")
        ]

    assert collected == [("ADDED", "dep-a")]
    assert summary_mock.call_count == 1


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
# watch_objects — list-only kinds poll instead of watching (issue #141)
# ---------------------------------------------------------------------------


def _pkg_meta() -> ResourceMeta:
    return ResourceMeta(
        "PackageManifest",
        "packagemanifests",
        "packages.operators.coreos.com",
        "v1",
        True,
        watchable=False,
    )


async def _take(gen: Any, n: int) -> list[tuple[str, str]]:
    """First *n* (event, name) pairs from an endless watch generator."""
    out: list[tuple[str, str]] = []
    async for ev, s in gen:
        out.append((ev, s.name))
        if len(out) >= n:
            break
    return out


async def test_unwatchable_kind_polls_lists_and_diffs_instead_of_watching() -> None:
    """A kind discovered without the watch verb (OLM packageserver) must be
    kept fresh by periodic re-LIST diffing: upserts for present rows, a
    DELETED for vanished ones - and the Watch API is never touched."""
    client = KubeClient()
    meta = _pkg_meta()
    snapshots = [
        {"metadata": {}, "items": [_generic("etcd"), _generic("kafka")]},
        {"metadata": {}, "items": [_generic("etcd"), _generic("postgres")]},
    ]
    request_json_mock = AsyncMock(side_effect=snapshots)
    watch_factory = MagicMock()

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
        patch.object(client_mod, "LIST_POLL_INTERVAL", 0.0),
        patch("korvid.k8s.client.k8s_watch.Watch", watch_factory),
    ):
        events = await _take(client.watch_objects(meta, "olm"), 5)

    assert events[:2] == [("ADDED", "etcd"), ("ADDED", "kafka")]
    # Second LIST round: upserts for present rows, DELETED for the vanished one.
    assert ("ADDED", "postgres") in events[2:]
    assert ("DELETED", "kafka") in events[2:]
    watch_factory.assert_not_called()


async def test_watch_405_falls_back_to_list_polling() -> None:
    """A server that advertises watch but rejects it with 405 (aggregated
    API drift) degrades to polling instead of dying in the retry loop.
    The raw-watch adapter surfaces the refusal as ApiStatusError (via
    _raise_for_status), so that exact type must be caught."""
    client = KubeClient()
    meta = _deploy_meta()
    snapshots = [
        {"metadata": {"resourceVersion": "1"}, "items": [_generic("dep-a")]},
        {"metadata": {}, "items": [_generic("dep-a"), _generic("dep-b")]},
    ]
    request_json_mock = AsyncMock(side_effect=snapshots)
    fake_watch = _FakeWatch([], raise_at=0, raise_exc=ApiStatusError(405, "Method Not Allowed"))

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
        patch.object(client_mod, "LIST_POLL_INTERVAL", 0.0),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        events = await _take(client.watch_objects(meta, "default"), 3)

    assert events[0] == ("ADDED", "dep-a")
    assert ("ADDED", "dep-b") in events[1:]


async def test_watch_405_api_exception_also_falls_back_to_polling() -> None:
    """The kubernetes client's own ApiException(405) takes the same
    fallback (both exception types cross the watch stream)."""
    client = KubeClient()
    meta = _deploy_meta()
    snapshots = [
        {"metadata": {"resourceVersion": "1"}, "items": [_generic("dep-a")]},
        {"metadata": {}, "items": [_generic("dep-a"), _generic("dep-b")]},
    ]
    request_json_mock = AsyncMock(side_effect=snapshots)
    fake_watch = _FakeWatch(
        [], raise_at=0, raise_exc=ApiException(status=405, reason="Method Not Allowed")
    )

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
        patch.object(client_mod, "LIST_POLL_INTERVAL", 0.0),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
    ):
        events = await _take(client.watch_objects(meta, "default"), 3)

    assert events[0] == ("ADDED", "dep-a")
    assert ("ADDED", "dep-b") in events[1:]


async def test_watch_non_405_api_status_error_still_raises() -> None:
    """A non-405 ApiStatusError from the raw adapter propagates with its
    status, reason and body intact - the body disambiguates same-status
    responses (PDB denial vs APF throttling)."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "9"}, "items": []}
    fake_watch = _FakeWatch(
        [], raise_at=0, raise_exc=ApiStatusError(410, "Gone", '{"kind":"Status"}')
    )

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
        pytest.raises(ApiStatusError, match="Gone") as excinfo,
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass
    assert excinfo.value.body == '{"kind":"Status"}'


async def test_watch_objects_list_error_emits_error_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    meta = _deploy_meta()

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(
            client,
            "_request_json",
            AsyncMock(side_effect=ApiStatusError(401, "Unauthorized")),
        ),
        pytest.raises(ApiStatusError, match="API 401: Unauthorized"),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass

    assert [event.operation for event in seen] == ["error"]
    assert seen[0].path == "/apis/apps/v1/namespaces/default/deployments"
    assert seen[0].status == 401
    assert seen[0].decoded_bytes == 0
    assert seen[0].object_count == 0


async def test_watch_non_405_api_exception_still_raises() -> None:
    """Only the deterministic 405 falls back to polling: other watch errors
    keep propagating so the WatchManager's retry/report loop stays in charge."""
    client = KubeClient()
    meta = _deploy_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "9"}, "items": []}
    fake_watch = _FakeWatch([], raise_at=0, raise_exc=ApiException(status=500, reason="boom"))

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
        pytest.raises(ApiStatusError, match="boom"),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass


async def test_watch_objects_watch_error_emits_error_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    meta = _deploy_meta()
    list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "9"}, "items": []}
    fake_watch = _FakeWatch([], raise_at=0, raise_exc=ApiException(status=500, reason="boom"))

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
        pytest.raises(ApiStatusError, match="boom"),
    ):
        async for _ in client.watch_objects(meta, "default"):
            pass

    assert [event.operation for event in seen] == ["list", "watch_open", "error"]
    assert seen[-1].path == "/apis/apps/v1/namespaces/default/deployments"
    assert seen[-1].status == 500


# ---------------------------------------------------------------------------
# get_object
# ---------------------------------------------------------------------------


async def test_get_object_emits_get_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    meta = _deploy_meta()
    request_json_mock = AsyncMock(return_value=_generic("my-dep"))

    with patch.object(client, "_request_json", request_json_mock):
        obj = await client.get_object(meta, "default", "my-dep")

    assert obj["metadata"]["name"] == "my-dep"
    assert [event.operation for event in seen] == ["get"]
    assert seen[0].path == "/apis/apps/v1/namespaces/default/deployments/my-dep"
    assert seen[0].object_count == 1
    assert seen[0].decoded_bytes > 0
    assert seen[0].status is None


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
        return _raw_response(200, "OK", b"")

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


async def test_list_objects_emits_list_telemetry() -> None:
    seen: list[ReadTelemetryEvent] = []
    client = KubeClient(read_telemetry=seen.append)
    meta = _deploy_meta()
    request_json_mock = AsyncMock(return_value={"items": [_generic("dep-a"), _generic("dep-b")]})

    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(client, "_request_json", request_json_mock),
    ):
        summaries = await client.list_objects(meta, "default")

    assert [summary.name for summary in summaries] == ["dep-a", "dep-b"]
    assert [event.operation for event in seen] == ["list"]
    assert seen[0].path == "/apis/apps/v1/namespaces/default/deployments"
    assert seen[0].object_count == 2
    assert seen[0].decoded_bytes > 0
    assert seen[0].status is None


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
    resp.status = 200
    resp.reason = "OK"
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
    resp.status = 200
    resp.reason = "OK"
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


async def test_secrets_never_evaluate_custom_columns() -> None:
    """Defense in depth: even a directly-constructed client must not run
    custom extraction against raw Secret manifests (masking bypass)."""
    from korvid.k8s.columns import CustomColumn

    client = KubeClient(
        custom_columns={"secrets": (CustomColumn("TOKEN", "jsonpath", ".data.token"),)}
    )
    secret = {
        "metadata": {"name": "s1", "namespace": "default"},
        "data": {"token": "aHVudGVyMg=="},
    }
    meta = ResourceMeta(kind="Secret", plural="secrets", group="", version="v1", namespaced=True)
    with (
        patch.object(client, "_api", MagicMock()),
        patch.object(
            client,
            "_request_json",
            AsyncMock(return_value={"metadata": {"resourceVersion": "1"}, "items": [secret]}),
        ),
    ):
        summaries = await client.list_objects(meta, "default")
    assert summaries[0].custom == ()


# ---------------------------------------------------------------------------
# list_context_names — :ctx picker source (issue #36)
# ---------------------------------------------------------------------------

_TWO_CTX_KUBECONFIG = """
apiVersion: v1
kind: Config
current-context: ctx-b
clusters:
- name: cluster-a
  cluster: {server: "https://a.example"}
- name: cluster-b
  cluster: {server: "https://b.example"}
contexts:
- name: ctx-a
  context: {cluster: cluster-a, user: user-a}
- name: ctx-b
  context: {cluster: cluster-b, user: user-b}
users:
- name: user-a
  user: {}
- name: user-b
  user: {}
"""


def test_list_context_names_returns_names_and_active(tmp_path: Path) -> None:
    from korvid.k8s.client import list_context_names

    kubeconfig = tmp_path / "config"
    kubeconfig.write_text(_TWO_CTX_KUBECONFIG)
    names, active = list_context_names(config_file=str(kubeconfig))
    assert names == ["ctx-a", "ctx-b"]
    assert active == "ctx-b"


def test_list_context_names_unreadable_returns_empty(tmp_path: Path) -> None:
    from korvid.k8s.client import list_context_names

    names, active = list_context_names(config_file=str(tmp_path / "missing"))
    assert names == []
    assert active is None


# ---------------------------------------------------------------------------
# probe_context / switch_context — runtime :ctx switching (issue #36)
# ---------------------------------------------------------------------------


class _FakeProbeApi:
    """Stands in for ApiClient in probe_context tests."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestProbeContext:
    async def test_success_uses_private_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        load_calls: list[dict[str, Any]] = []

        async def fake_load(**kwargs: Any) -> None:
            load_calls.append(kwargs)

        probe_apis: list[_FakeProbeApi] = []

        def fake_api(*args: Any, **kwargs: Any) -> _FakeProbeApi:
            api = _FakeProbeApi()
            probe_apis.append(api)
            return api

        reviews: list[Any] = []

        class FakeAuthzApi:
            def __init__(self, api: Any) -> None:
                self._api = api

            async def create_self_subject_access_review(self, body: Any) -> Any:
                reviews.append(body)
                return body

        monkeypatch.setattr(k8s_config, "load_kube_config", fake_load)
        monkeypatch.setattr(k8s_client, "ApiClient", fake_api)
        monkeypatch.setattr(k8s_client, "AuthorizationV1Api", FakeAuthzApi)

        kube = KubeClient()
        await kube.probe_context("ctx-b")

        # Probe loads into a private Configuration and never persists —
        # the live (global) connection must stay untouched on failure.
        assert load_calls[0]["context"] == "ctx-b"
        assert load_calls[0]["client_configuration"] is not None
        assert load_calls[0]["persist_config"] is False
        assert probe_apis[0].closed is True

    async def test_auth_failure_raises_and_closes_probe_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:

        async def fake_load(**kwargs: Any) -> None:
            return None

        probe_apis: list[_FakeProbeApi] = []

        def fake_api(*args: Any, **kwargs: Any) -> _FakeProbeApi:
            api = _FakeProbeApi()
            probe_apis.append(api)
            return api

        class FakeAuthzApi:
            def __init__(self, api: Any) -> None:
                pass

            async def create_self_subject_access_review(self, body: Any) -> Any:
                raise k8s_client.exceptions.ApiException(status=401, reason="Unauthorized")

        monkeypatch.setattr(k8s_config, "load_kube_config", fake_load)
        monkeypatch.setattr(k8s_client, "ApiClient", fake_api)
        monkeypatch.setattr(k8s_client, "AuthorizationV1Api", FakeAuthzApi)

        kube = KubeClient()
        with pytest.raises(k8s_client.exceptions.ApiException, match="Unauthorized"):
            await kube.probe_context("ctx-b")
        assert probe_apis[0].closed is True

    async def test_timeout_covers_credential_loading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The probe deadline bounds kubeconfig/credential loading too — an
        exec credential plugin that stalls must not hang the switch forever."""
        import asyncio

        async def stalling_load(**kwargs: Any) -> None:
            await asyncio.sleep(30)

        monkeypatch.setattr(k8s_config, "load_kube_config", stalling_load)
        monkeypatch.setattr(client_mod, "_PROBE_TIMEOUT", 0.05)

        kube = KubeClient()
        with pytest.raises(TimeoutError):
            await kube.probe_context("ctx-slow-creds")

    async def test_unknown_context_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kubernetes_asyncio.config import ConfigException

        async def fake_load(**kwargs: Any) -> None:
            raise ConfigException("context ctx-nope not found")

        monkeypatch.setattr(k8s_config, "load_kube_config", fake_load)

        kube = KubeClient()
        with pytest.raises(ConfigException, match="not found"):
            await kube.probe_context("ctx-nope")


class TestSwitchContext:
    async def test_swaps_connection_and_closes_old(self, monkeypatch: pytest.MonkeyPatch) -> None:

        load_calls: list[dict[str, Any]] = []

        async def fake_load(**kwargs: Any) -> None:
            load_calls.append(kwargs)

        apis: list[_FakeProbeApi] = []

        def fake_api(*args: Any, **kwargs: Any) -> _FakeProbeApi:
            api = _FakeProbeApi()
            apis.append(api)
            return api

        monkeypatch.setattr(k8s_config, "load_kube_config", fake_load)
        monkeypatch.setattr(k8s_client, "ApiClient", fake_api)
        monkeypatch.setattr(k8s_client, "CoreV1Api", lambda api: api)

        kube = KubeClient()
        await kube.connect("ctx-a")
        old_api = apis[0]
        # Seed per-connection caches to prove the switch resets them.
        kube._pod_resize_supported = True
        kube._provider_info = object()  # type: ignore[assignment]  # cache reset check only

        await kube.switch_context("ctx-b")

        assert old_api.closed is True
        assert apis[1].closed is False
        assert load_calls[-1]["context"] == "ctx-b"
        assert kube._pod_resize_supported is None
        assert kube._provider_info is None

    async def test_stalled_retarget_load_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The retarget load is bounded like the probe: an exec credential
        plugin that succeeded during the probe but stalls on this second
        invocation must surface as a timeout into the caller's recovery
        path, not hang the already-torn-down session (issue #36 review)."""
        import asyncio

        async def stalling_load(**kwargs: Any) -> None:
            await asyncio.sleep(30)

        monkeypatch.setattr(k8s_config, "load_kube_config", stalling_load)
        monkeypatch.setattr(client_mod, "_PROBE_TIMEOUT", 0.05)

        kube = KubeClient()
        with pytest.raises(TimeoutError):
            await kube.switch_context("ctx-slow-creds")


# ---------------------------------------------------------------------------
# non-2xx handling on the raw (_preload_content=False) path
#
# kubernetes_asyncio's rest.py only raises ApiException when it preloads the
# body; with _preload_content=False the caller gets the raw aiohttp response
# back regardless of status. The raw helpers must therefore check the HTTP
# status themselves — otherwise a refused write (409 uid mismatch, 429 PDB
# denial) silently "succeeds" and a 404 GET returns the Status JSON as if it
# were the object (caught live by the contract suite, issue #109).
# ---------------------------------------------------------------------------


def _raw_response(status: int, reason: str, body: bytes) -> MagicMock:
    """Fake aiohttp response as returned by call_api with _preload_content=False."""
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    resp.read = AsyncMock(return_value=body)
    return resp


async def test_delete_object_raises_on_non_2xx_raw_response() -> None:
    """A 409 (uid precondition failed) delivered as a raw response body must
    surface as ApiStatusError, not be swallowed as success."""
    client = KubeClient()
    body = b'{"kind":"Status","status":"Failure","reason":"Conflict","code":409}'
    mock_api = MagicMock()
    mock_api.call_api = AsyncMock(return_value=_raw_response(409, "Conflict", body))

    with (
        patch.object(client, "_api", mock_api),
        pytest.raises(ApiStatusError, match="API 409: Conflict"),
    ):
        await client.delete_object(_deploy_meta(), "default", "my-dep", uid="wrong-uid")


async def test_create_object_raises_on_non_2xx_raw_response() -> None:
    """A 409 AlreadyExists on POST must raise, proving create-exactly-once."""
    client = KubeClient()
    body = b'{"kind":"Status","status":"Failure","reason":"AlreadyExists","code":409}'
    mock_api = MagicMock()
    mock_api.call_api = AsyncMock(return_value=_raw_response(409, "Conflict", body))

    with (
        patch.object(client, "_api", mock_api),
        pytest.raises(ApiStatusError, match="API 409: Conflict") as excinfo,
    ):
        await client.create_object(_deploy_meta(), "default", {"metadata": {"name": "my-dep"}})
    assert "AlreadyExists" in excinfo.value.body


async def test_get_object_raises_on_non_2xx_raw_response() -> None:
    """A 404 GET must raise instead of returning the Status JSON as the object."""
    client = KubeClient()
    body = b'{"kind":"Status","status":"Failure","reason":"NotFound","code":404}'
    mock_api = MagicMock()
    mock_api.call_api = AsyncMock(return_value=_raw_response(404, "Not Found", body))

    with (
        patch.object(client, "_api", mock_api),
        pytest.raises(ApiStatusError, match="API 404: Not Found"),
    ):
        await client.get_object(_deploy_meta(), "default", "my-dep")


async def test_list_namespaces_raises_on_non_2xx_raw_response() -> None:
    """A 403 on the namespace LIST must raise, not degrade to an empty list."""
    client = KubeClient()
    body = b'{"kind":"Status","status":"Failure","reason":"Forbidden","code":403}'
    fake_v1 = AsyncMock()
    fake_v1.list_namespace.return_value = _raw_response(403, "Forbidden", body)

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 403: Forbidden"),
    ):
        await client.list_namespaces()


async def test_stream_logs_raises_on_non_2xx_raw_response() -> None:
    """A 400 on the log request must raise instead of streaming the error
    Status body as if it were log lines."""
    client = KubeClient()
    resp = _raw_response(400, "Bad Request", b"")
    resp.close = MagicMock()
    fake_v1 = AsyncMock()
    fake_v1.read_namespaced_pod_log.return_value = resp

    with (
        patch.object(client, "_core_v1", fake_v1),
        pytest.raises(ApiStatusError, match="API 400: Bad Request"),
    ):
        async for _ in client.stream_logs("default", "my-pod", ""):
            pass
    # The finally-close must also cover the error path (no leaked connection).
    resp.close.assert_called_once()


async def test_raw_watch_callable_raises_on_non_2xx_raw_response() -> None:
    """kubernetes_asyncio's Watch never inspects resp.status: a non-2xx watch
    response would be retried forever (empty body) or parsed as malformed
    events, so the watch callable must raise before handing it to Watch."""
    client = KubeClient()
    body = b'{"kind":"Status","reason":"Forbidden","message":"denied"}'
    mock_api = AsyncMock()
    mock_api.call_api = AsyncMock(return_value=_raw_response(403, "Forbidden", body))

    with patch.object(client, "_api", mock_api):
        watch_func = client._make_raw_watch_callable("/api/v1/pods")
        with pytest.raises(ApiStatusError, match="API 403: Forbidden"):
            await watch_func(watch=True, _preload_content=False)
