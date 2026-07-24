"""Tests for read-only agent tools (ToolExecutor + READ_TOOLS schema)."""

from __future__ import annotations

from typing import Any

from korvid.agent.tools import MAX_RESULT_CHARS, READ_TOOLS, ToolExecutor
from korvid.k8s.discovery import PODS_META
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine


class FakeKube:
    def __init__(self) -> None:
        self.manifest: dict[str, Any] = {"kind": "Pod", "metadata": {"name": "a"}}

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        return self.manifest


def make_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META})


def test_read_tools_schema_names() -> None:
    names = [t["function"]["name"] for t in READ_TOOLS]
    assert names == ["list_resources", "get_resource", "get_logs", "get_events"]


def test_read_tools_all_have_type_function() -> None:
    for tool in READ_TOOLS:
        assert tool["type"] == "function"
        assert "function" in tool
        assert "name" in tool["function"]
        assert "parameters" in tool["function"]


async def test_get_resource_masks_secret_data() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {"name": "s", "managedFields": [{"x": 1}]},
        "data": {"password": "aGVsbG8="},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )
    assert "aGVsbG8=" not in out
    assert "***MASKED***" in out
    assert "managedFields" not in out


async def test_get_resource_masks_secret_string_data() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {"name": "s"},
        "stringData": {"token": "super-secret"},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )
    assert "super-secret" not in out
    assert "***MASKED***" in out


async def test_get_resource_strips_last_applied_annotation_on_secret() -> None:
    """Client-side apply stores the unmasked manifest in this annotation."""
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {
            "name": "s",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": (
                    '{"kind":"Secret","data":{"password":"aGVsbG8="}}'
                ),
                "other": "kept",
            },
        },
        "data": {"password": "aGVsbG8="},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )
    assert "aGVsbG8=" not in out
    assert "last-applied-configuration" not in out
    assert "kept" in out


async def test_get_resource_strips_managed_fields_for_non_secret() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Pod",
        "metadata": {"name": "p", "managedFields": [{"manager": "kubectl"}]},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "p", "namespace": "default"}
    )
    assert "managedFields" not in out


async def test_unknown_tool_and_kind_return_error_text() -> None:
    ex = make_executor(FakeKube())
    assert (await ex.execute("nope", {})).startswith("ERROR:")
    assert (await ex.execute("list_resources", {"kind": "wat"})).startswith("ERROR:")


async def test_result_is_capped() -> None:
    kube = FakeKube()
    kube.manifest = {"kind": "Pod", "metadata": {"name": "a"}, "blob": "x" * 20000}
    out = await make_executor(kube).execute("get_resource", {"kind": "pods", "name": "a"})
    assert len(out) <= MAX_RESULT_CHARS + 50
    assert "[truncated" in out


async def test_executor_never_raises() -> None:
    class Boom:
        async def get_object(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("kaput")

    out = await make_executor(Boom()).execute("get_resource", {"kind": "pods", "name": "a"})
    assert out.startswith("ERROR:")


class FakeLogKube(FakeKube):
    """FakeKube with a scripted stream_logs recording its call arguments."""

    def __init__(self) -> None:
        super().__init__()
        self.manifest = {
            "kind": "Pod",
            "metadata": {"name": "web"},
            "spec": {"containers": [{"name": "app"}, {"name": "sidecar"}]},
        }
        self.log_calls: list[dict[str, Any]] = []

    async def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> Any:
        self.log_calls.append(
            {
                "namespace": namespace,
                "pod": pod,
                "container": container,
                "follow": follow,
                "tail_lines": tail_lines,
            }
        )
        for text in ("line-1", "line-2"):
            yield LogLine(pod=pod, container=container, text=text)


async def test_get_logs_defaults_to_first_container() -> None:
    kube = FakeLogKube()
    out = await make_executor(kube).execute("get_logs", {"pod": "web", "namespace": "d"})
    assert out == "line-1\nline-2"
    assert kube.log_calls[0]["container"] == "app"
    assert kube.log_calls[0]["follow"] is False


async def test_get_logs_uses_explicit_container() -> None:
    kube = FakeLogKube()
    _ = await make_executor(kube).execute(
        "get_logs", {"pod": "web", "namespace": "d", "container": "sidecar"}
    )
    assert kube.log_calls[0]["container"] == "sidecar"


async def test_get_logs_clamps_tail_lines() -> None:
    kube = FakeLogKube()
    _ = await make_executor(kube).execute(
        "get_logs", {"pod": "web", "namespace": "d", "tail_lines": 99999}
    )
    assert kube.log_calls[0]["tail_lines"] == 500
    _ = await make_executor(kube).execute(
        "get_logs", {"pod": "web", "namespace": "d", "tail_lines": 0}
    )
    assert kube.log_calls[1]["tail_lines"] == 1


async def test_get_logs_stream_error_returns_error_text() -> None:
    class BoomLogs(FakeLogKube):
        async def stream_logs(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("no such pod")
            yield  # pragma: no cover - makes this an async generator

    out = await make_executor(BoomLogs()).execute("get_logs", {"pod": "web", "namespace": "d"})
    assert out.startswith("ERROR:")
    assert "no such pod" in out


async def test_kind_is_normalized_before_lookup() -> None:
    kube = FakeKube()
    out = await make_executor(kube).execute(
        "get_resource", {"kind": " Pod ", "name": "a", "namespace": "d"}
    )
    assert not out.startswith("ERROR:")
    assert "kind: Pod" in out


class FakeEventKube(FakeKube):
    def __init__(self) -> None:
        super().__init__()
        self.manifest = {"kind": "Pod", "metadata": {"name": "web", "uid": "abc-123"}}
        self.event_calls: list[dict[str, Any]] = []

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        self.event_calls.append({"namespace": namespace, "name": name, "kind": kind, "uid": uid})
        return [{"type": "Warning", "reason": "BackOff", "count": 3, "message": "restarting"}]


async def test_get_events_scopes_by_kind_and_uid() -> None:
    kube = FakeEventKube()
    out = await make_executor(kube).execute(
        "get_events", {"kind": "Pod", "namespace": "d", "name": "web"}
    )
    assert "BackOff" in out
    assert kube.event_calls == [{"namespace": "d", "name": "web", "kind": "Pod", "uid": "abc-123"}]


async def test_get_events_falls_back_when_object_gone() -> None:
    class GoneKube(FakeEventKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            raise ApiStatusError(404, "NotFound")

    kube = GoneKube()
    out = await make_executor(kube).execute(
        "get_events", {"kind": "pods", "namespace": "d", "name": "web"}
    )
    assert "BackOff" in out
    assert kube.event_calls[0]["uid"] is None
    assert kube.event_calls[0]["kind"] == "Pod"


async def test_get_events_non_404_lookup_failure_is_error() -> None:
    """Only 404 proves absence; other failures must surface, not weaken scoping."""

    class ForbiddenKube(FakeEventKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            raise ApiStatusError(403, "Forbidden")

    kube = ForbiddenKube()
    out = await make_executor(kube).execute(
        "get_events", {"kind": "pods", "namespace": "d", "name": "web"}
    )
    assert out.startswith("ERROR:")
    assert kube.event_calls == []
