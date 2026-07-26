"""Tests for read-only agent tools (ToolExecutor + READ_TOOLS schema)."""

from __future__ import annotations

from typing import Any

from korvid.agent.tools import MAX_RESULT_CHARS, READ_TOOLS, UI_TOOLS, ToolExecutor, UIBridge
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
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "d"}
    )
    assert len(out) <= MAX_RESULT_CHARS + 50
    assert "[truncated" in out


async def test_executor_never_raises() -> None:
    class Boom:
        async def get_object(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("kaput")

    out = await make_executor(Boom()).execute(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "d"}
    )
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


async def test_error_results_are_capped() -> None:
    """A long exception reason must pass through the same ingest cap."""

    class LoudKube(FakeKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            raise RuntimeError("x" * (MAX_RESULT_CHARS * 2))

    out = await make_executor(LoudKube()).execute(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "d"}
    )
    assert out.startswith("ERROR:")
    assert len(out) <= MAX_RESULT_CHARS + 50  # cap + truncation suffix


async def test_get_resource_requires_namespace_for_namespaced_kind() -> None:
    out = await make_executor(FakeKube()).execute("get_resource", {"kind": "pods", "name": "a"})
    assert out.startswith("ERROR:")
    assert "namespace" in out


# --- Slice 3: UI-control tools (spec §4.1 UI Bus / §6 UI control row) ---


class FakeBridge(UIBridge):
    """Records UI-control calls; returns canned confirmations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        self.calls.append(("navigate", {"view": view, "namespace": namespace}))
        return f"switched to {view}"

    async def agent_set_filter(self, pattern: str) -> str:
        self.calls.append(("set_filter", {"pattern": pattern}))
        return f"filter set to {pattern!r}"

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        self.calls.append(
            ("open_logs", {"pod": pod, "namespace": namespace, "container": container})
        )
        return f"log pane opened for {namespace}/{pod}"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        self.calls.append(("open_describe", {"kind": kind, "name": name, "namespace": namespace}))
        return f"describe opened for {kind}/{name}"

    async def agent_drill_down(self, name: str) -> str:
        self.calls.append(("drill_down", {"name": name}))
        return f"drilled into {name}"

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        self.calls.append(
            (
                "request_write",
                {
                    "action": action,
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "replicas": replicas,
                    "resources": resources,
                },
            )
        )
        return f"approved and executed: {action} {kind}/{name}"


def make_ui_executor(bridge: Any) -> ToolExecutor:
    kube: Any = FakeKube()
    return ToolExecutor(kube, {"pods": PODS_META}, ui=bridge)


def test_ui_tools_schema_names() -> None:
    names = [t["function"]["name"] for t in UI_TOOLS]
    assert names == ["navigate", "set_filter", "open_logs", "open_describe", "drill_down"]


def test_ui_tools_all_have_type_function() -> None:
    for tool in UI_TOOLS:
        assert tool["type"] == "function"
        assert "parameters" in tool["function"]


async def test_navigate_dispatches_to_bridge() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute(
        "navigate", {"view": "deployments", "namespace": "prod"}
    )
    assert out == "switched to deployments"
    assert bridge.calls == [("navigate", {"view": "deployments", "namespace": "prod"})]


async def test_set_filter_dispatches_to_bridge() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute("set_filter", {"pattern": "web"})
    assert out == "filter set to 'web'"


async def test_open_logs_dispatches_to_bridge() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute("open_logs", {"pod": "web-1", "namespace": "d"})
    assert out == "log pane opened for d/web-1"
    assert bridge.calls[0][1]["container"] is None


async def test_open_describe_dispatches_to_bridge() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute(
        "open_describe", {"kind": "pods", "name": "web-1", "namespace": "d"}
    )
    assert out == "describe opened for pods/web-1"


async def test_drill_down_dispatches_to_bridge() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute("drill_down", {"name": "web"})
    assert out == "drilled into web"
    assert bridge.calls == [("drill_down", {"name": "web"})]


async def test_ui_tool_without_bridge_is_error() -> None:
    kube: Any = FakeKube()
    executor = ToolExecutor(kube, {"pods": PODS_META})
    out = await executor.execute("navigate", {"view": "pods"})
    assert out.startswith("ERROR:")
    assert "UI control unavailable" in out


async def test_ui_tool_bridge_result_is_capped() -> None:
    class LoudBridge(FakeBridge):
        async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
            return "x" * (MAX_RESULT_CHARS * 2)

    out = await make_ui_executor(LoudBridge()).execute("navigate", {"view": "pods"})
    assert len(out) <= MAX_RESULT_CHARS + 50


async def test_ui_tool_bridge_exception_is_error_result() -> None:
    class BoomBridge(FakeBridge):
        async def agent_set_filter(self, pattern: str) -> str:
            raise RuntimeError("widget gone")

    out = await make_ui_executor(BoomBridge()).execute("set_filter", {"pattern": "x"})
    assert out.startswith("ERROR:")
    assert "widget gone" in out


# -- In-place pod resize tool (issue #27) ------------------------------------


def test_resize_tools_schema() -> None:
    from korvid.agent.tools import RESIZE_TOOLS

    assert [t["function"]["name"] for t in RESIZE_TOOLS] == ["resize_pod"]
    fn = RESIZE_TOOLS[0]["function"]
    assert RESIZE_TOOLS[0]["type"] == "function"
    assert set(fn["parameters"]["required"]) == {"name", "namespace", "resources"}


async def test_resize_pod_dispatches_to_bridge() -> None:
    bridge = FakeBridge()
    resources = {"app": {"requests": {"cpu": "200m"}}}
    out = await make_ui_executor(bridge).execute(
        "resize_pod",
        {"name": "web-1", "namespace": "default", "resources": resources},
    )
    assert "approved and executed" in out
    assert bridge.calls == [
        (
            "request_write",
            {
                "action": "resize",
                "kind": "pods",
                "name": "web-1",
                "namespace": "default",
                "replicas": None,
                "resources": resources,
            },
        )
    ]


async def test_resize_pod_rejects_non_dict_resources() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute(
        "resize_pod", {"name": "web-1", "namespace": "default", "resources": "250m"}
    )
    assert out.startswith("ERROR:")
    assert bridge.calls == []


async def test_resize_pod_rejects_malformed_container_entries() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute(
        "resize_pod",
        {"name": "web-1", "namespace": "default", "resources": {"app": {"requests": "250m"}}},
    )
    assert out.startswith("ERROR:")
    assert bridge.calls == []


async def test_resize_pod_rejects_unknown_section() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute(
        "resize_pod",
        {
            "name": "web-1",
            "namespace": "default",
            "resources": {"app": {"claims": {"cpu": "250m"}}},
        },
    )
    assert out.startswith("ERROR:")
    assert bridge.calls == []


async def test_resize_pod_rejects_invalid_quantity_amounts() -> None:
    """A malformed amount must fail before the user is shown an approval
    dialog for a request guaranteed to be rejected by the apiserver."""
    bridge = FakeBridge()
    for bad in ("lots", "", "-100m", "0"):
        out = await make_ui_executor(bridge).execute(
            "resize_pod",
            {
                "name": "web-1",
                "namespace": "default",
                "resources": {"app": {"requests": {"cpu": bad}}},
            },
        )
        assert out.startswith("ERROR:"), bad
    assert bridge.calls == []


async def test_resize_pod_accepts_full_quantity_grammar() -> None:
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute(
        "resize_pod",
        {
            "name": "web-1",
            "namespace": "default",
            "resources": {"app": {"requests": {"cpu": "100u", "memory": ".5Gi"}}},
        },
    )
    assert "approved and executed" in out


async def test_resize_pod_rejects_blank_container_names() -> None:
    """An empty or whitespace-only container key passes an isinstance check
    but produces a patch Kubernetes is guaranteed to reject; it must fail
    before any approval dialog opens."""
    for container in ("", "   "):
        bridge = FakeBridge()
        executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
        result = await executor.execute(
            "resize_pod",
            {
                "name": "web-1",
                "namespace": "default",
                "resources": {container: {"requests": {"cpu": "1"}}},
            },
        )
        assert result.startswith("ERROR:")
        assert bridge.calls == []


async def test_resize_pod_normalizes_padded_amounts() -> None:
    """parse_quantity strips whitespace for validation, but Kubernetes'
    quantity parser does not: a padded amount like ' 200m ' must be
    normalized before it crosses the UI bridge, or the approved resize
    fails server-side."""
    bridge = FakeBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
    result = await executor.execute(
        "resize_pod",
        {
            "name": "web-1",
            "namespace": "default",
            "resources": {"app": {"requests": {"cpu": " 200m "}}},
        },
    )
    assert not result.startswith("ERROR:")
    _, kwargs = bridge.calls[-1]
    assert kwargs["resources"] == {"app": {"requests": {"cpu": "200m"}}}


async def test_resize_pod_normalizes_padded_container_names() -> None:
    """Kubernetes container names cannot contain spaces: a padded key like
    ' app ' must be normalized before it crosses the UI bridge, not turned
    into an approval dialog for a patch guaranteed to fail."""
    bridge = FakeBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
    result = await executor.execute(
        "resize_pod",
        {
            "name": "web-1",
            "namespace": "default",
            "resources": {" app ": {"requests": {"cpu": "200m"}}},
        },
    )
    assert not result.startswith("ERROR:")
    _, kwargs = bridge.calls[-1]
    assert kwargs["resources"] == {"app": {"requests": {"cpu": "200m"}}}
