"""Tests for read-only agent tools (ToolExecutor + READ_TOOLS schema)."""

from __future__ import annotations

from typing import Any

from korvid.agent.tools import MAX_RESULT_CHARS, READ_TOOLS, UI_TOOLS, ToolExecutor, UIBridge
from korvid.core.secrets import MASK_PLACEHOLDER
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
    assert names == [
        "list_resources",
        "get_resource",
        "get_logs",
        "get_events",
        "list_operators",
        "diagnose_pod",
    ]


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
    assert MASK_PLACEHOLDER in out
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
    assert MASK_PLACEHOLDER in out


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


async def test_synthetic_kinds_are_not_api_resources() -> None:
    """Synthetic view kinds (helm browser, issue #28) live in the alias map
    for navigation, but they have no API endpoint - the read tools must
    reject them instead of requesting /api/v1/helmreleases."""
    from korvid.k8s.helm import HELM_RELEASES_META

    class ExplodingKube:
        async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
            raise AssertionError("synthetic kind must not reach the API client")

        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            raise AssertionError("synthetic kind must not reach the API client")

    ex = ToolExecutor(ExplodingKube(), {"helmreleases": HELM_RELEASES_META})  # type: ignore[arg-type]
    for tool, args in (
        ("list_resources", {"kind": "helmreleases"}),
        ("get_resource", {"kind": "helmreleases", "name": "web", "namespace": "d"}),
        ("get_events", {"kind": "helmreleases", "name": "web", "namespace": "d"}),
    ):
        out = await ex.execute(tool, args)
        assert out.startswith("ERROR:")
        assert "not an API resource" in out


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


def test_set_filter_schema_documents_filter_grammar() -> None:
    tool = next(t for t in UI_TOOLS if t["function"]["name"] == "set_filter")
    desc = tool["function"]["description"]
    for token in ("substring", "~", "regex", "!", "-l", "-s"):
        assert token in desc


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


async def test_resize_pod_rejects_container_name_collision_after_normalization() -> None:
    """'app' and ' app ' collapsing to one key must not silently drop one
    requested set of changes (last-write-wins) - reject the collision."""
    bridge = FakeBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
    result = await executor.execute(
        "resize_pod",
        {
            "name": "web-1",
            "namespace": "default",
            "resources": {
                "app": {"requests": {"cpu": "200m"}},
                " app ": {"requests": {"memory": "1Gi"}},
            },
        },
    )
    assert result.startswith("ERROR:")
    assert bridge.calls == []


async def test_resize_pod_rejects_invalid_container_name_grammar() -> None:
    """Container names are RFC 1123 DNS labels: lowercase alphanumerics and
    hyphens, alphanumeric endpoints, at most 63 characters. Anything else
    produces a patch the apiserver must reject - fail before the approval
    dialog opens."""
    for container in ("app sidecar", "App", "-app", "app-", "a" * 64):
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


def _olm_aliases() -> dict[str, Any]:
    from korvid.k8s.discovery import ResourceMeta
    from korvid.k8s.olm import OPERATORS_GROUP, PACKAGES_GROUP

    return {
        "pods": PODS_META,
        "packagemanifests": ResourceMeta(
            "PackageManifest", "packagemanifests", PACKAGES_GROUP, "v1", True
        ),
        "subscriptions": ResourceMeta(
            "Subscription", "subscriptions", OPERATORS_GROUP, "v1alpha1", True
        ),
    }


async def test_list_operators_reports_catalog_and_installed() -> None:
    from korvid.k8s.models import OLMSubscriptionSummary, PackageManifestSummary

    class OLMKube:
        async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
            if meta.plural == "packagemanifests":
                return [
                    PackageManifestSummary(
                        name="cert-manager",
                        namespace="olm",
                        kind="PackageManifest",
                        created="2026-07-26T10:00:00Z",
                        uid="p1",
                        catalog="operatorhubio-catalog",
                        default_channel="stable",
                        channels=("candidate", "stable"),
                    )
                ]
            return [
                OLMSubscriptionSummary(
                    name="argocd-operator",
                    namespace="operators",
                    kind="Subscription",
                    created="2026-07-26T10:00:00Z",
                    uid="s1",
                    channel="alpha",
                    source="operatorhubio-catalog",
                    installed_csv="argocd-operator.v0.8.0",
                    state="AtLatestKnown",
                )
            ]

    ex = ToolExecutor(OLMKube(), _olm_aliases())  # type: ignore[arg-type]
    out = await ex.execute("list_operators", {})
    assert "cert-manager" in out
    assert "channels=candidate,stable" in out
    assert "default=stable" in out
    assert "argocd-operator" in out
    assert "argocd-operator.v0.8.0" in out
    assert "AtLatestKnown" in out


async def test_list_operators_without_olm_explains() -> None:
    ex = make_executor(FakeKube())
    out = await ex.execute("list_operators", {})
    assert "OLM" in out
    # Deliberately avoids asserting the raw API-group string: CodeQL flags
    # domain-like substring checks as URL-sanitization smells.
    assert "neither" in out
    assert "API groups were discovered" in out


async def test_list_operators_installed_first_and_catalog_capped_sorted() -> None:
    """Installed state leads (a huge catalog must not push it past the
    result cap), the catalog is sorted, and overflow is summarized."""
    from korvid.agent.tools import _MAX_CATALOG_PACKAGES
    from korvid.k8s.models import OLMSubscriptionSummary, PackageManifestSummary

    class BigCatalogKube:
        async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
            if meta.plural == "packagemanifests":
                return [
                    PackageManifestSummary(
                        name=f"pkg-{i:04d}",
                        namespace="olm",
                        kind="PackageManifest",
                        created="2026-07-26T10:00:00Z",
                        uid=f"p{i}",
                        catalog="operatorhubio-catalog",
                        default_channel="stable",
                        channels=("stable",),
                    )
                    # Reversed input proves the listing is sorted.
                    for i in reversed(range(_MAX_CATALOG_PACKAGES + 5))
                ]
            return [
                OLMSubscriptionSummary(
                    name="argocd-operator",
                    namespace="operators",
                    kind="Subscription",
                    created="2026-07-26T10:00:00Z",
                    uid="s1",
                    channel="alpha",
                    source="operatorhubio-catalog",
                    installed_csv="argocd-operator.v0.8.0",
                    state="AtLatestKnown",
                )
            ]

    ex = ToolExecutor(BigCatalogKube(), _olm_aliases())  # type: ignore[arg-type]
    out = await ex.execute("list_operators", {})
    assert out.index("INSTALLED") < out.index("AVAILABLE")
    assert "pkg-0000" in out  # sorted: lowest names shown
    assert f"pkg-{_MAX_CATALOG_PACKAGES:04d}" not in out  # beyond the cap
    assert "...and 5 more catalog packages" in out


async def test_list_operators_reports_installed_when_package_server_missing() -> None:
    """subscriptions discovered but the package server absent: installed
    operators are still reported, with an unavailable note for the catalog."""
    from korvid.k8s.discovery import ResourceMeta
    from korvid.k8s.models import OLMSubscriptionSummary

    class SubsOnlyKube:
        async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
            return [
                OLMSubscriptionSummary(
                    name="argocd-operator",
                    namespace="operators",
                    kind="Subscription",
                    created="2026-07-26T10:00:00Z",
                    uid="s1",
                    channel="alpha",
                    source="operatorhubio-catalog",
                    installed_csv="argocd-operator.v0.8.0",
                    state="AtLatestKnown",
                )
            ]

    aliases = {
        "pods": ResourceMeta("Pod", "pods", "", "v1", True),
        "subscriptions": ResourceMeta(
            "Subscription", "subscriptions", "operators.coreos.com", "v1alpha1", True
        ),
    }
    ex = ToolExecutor(SubsOnlyKube(), aliases)  # type: ignore[arg-type]
    out = await ex.execute("list_operators", {})
    assert "argocd-operator" in out
    assert "AVAILABLE (operator catalog): unavailable" in out


# --- diagnose_pod (issue #70) ------------------------------------------------


def _diagnose_aliases() -> dict[str, Any]:
    from korvid.k8s.discovery import ResourceMeta

    return {
        "pods": PODS_META,
        "pod": PODS_META,
        "replicasets": ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True),
        "deployments": ResourceMeta("Deployment", "deployments", "apps", "v1", True),
        "nodes": ResourceMeta("Node", "nodes", "", "v1", False),
        "persistentvolumeclaims": ResourceMeta(
            "PersistentVolumeClaim", "persistentvolumeclaims", "", "v1", True
        ),
    }


class FakeDiagnoseKube:
    """Scripted cluster for the compound tool: pod, owners, node, PVC, events, logs."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {
            ("pods", "api-1"): {
                "kind": "Pod",
                "metadata": {
                    "name": "api-1",
                    "namespace": "default",
                    "uid": "pod-uid",
                    "creationTimestamp": "2026-07-27T06:00:00Z",
                    "ownerReferences": [
                        {"kind": "ReplicaSet", "name": "api-6f", "controller": True}
                    ],
                },
                "spec": {
                    "nodeName": "node-a",
                    "containers": [{"name": "app"}],
                    "volumes": [
                        {"name": "data", "persistentVolumeClaim": {"claimName": "data-claim"}}
                    ],
                },
                "status": {
                    "phase": "Running",
                    "conditions": [
                        {"type": "Ready", "status": "False", "reason": "ContainersNotReady"}
                    ],
                    "containerStatuses": [
                        {
                            "name": "app",
                            "ready": False,
                            "restartCount": 7,
                            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                            "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}},
                        }
                    ],
                },
            },
            ("replicasets", "api-6f"): {
                "kind": "ReplicaSet",
                "metadata": {
                    "name": "api-6f",
                    "ownerReferences": [{"kind": "Deployment", "name": "api", "controller": True}],
                },
            },
            ("nodes", "node-a"): {
                "metadata": {"name": "node-a"},
                "status": {
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "MemoryPressure", "status": "True"},
                    ]
                },
            },
            ("persistentvolumeclaims", "data-claim"): {
                "metadata": {"name": "data-claim"},
                "status": {"phase": "Bound"},
            },
        }
        self.events: list[dict[str, Any]] = [
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "restarting failed container",
                "count": 9,
                "lastTimestamp": "2026-07-27T06:20:00Z",
            }
        ]
        self.log_lines: list[str] = [
            *(f"serving request {i}" for i in range(40)),
            "ERROR: db connection refused",
            *(f"retrying {i}" for i in range(20)),
        ]
        self.log_calls: list[dict[str, Any]] = []

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        obj = self.objects.get((meta.plural, name))
        if obj is None:
            raise ApiStatusError(404, "NotFound")
        return obj

    async def list_events_for(
        self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
    ) -> list[dict[str, Any]]:
        return self.events

    async def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> Any:
        self.log_calls.append(
            {"pod": pod, "container": container, "previous": previous, "tail_lines": tail_lines}
        )
        for text in self.log_lines:
            yield LogLine(pod=pod, container=container, text=text)


def _diagnose_executor(kube: Any) -> ToolExecutor:
    return ToolExecutor(kube, _diagnose_aliases())


async def test_diagnose_pod_reports_all_sections_in_evidence_order() -> None:
    kube = FakeDiagnoseKube()
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert not out.startswith("ERROR:")
    # Identity and owner chain up front.
    assert "pod default/api-1" in out
    assert "phase=Running" in out
    assert "owner: Deployment api (via ReplicaSet api-6f)" in out
    # Related context.
    assert "node node-a" in out
    assert "MemoryPressure=True" in out
    assert "pvc data-claim: Bound" in out
    # Evidence.
    assert "CrashLoopBackOff" in out
    assert "restarts=7" in out
    assert "BackOff (9x" in out
    assert "ERROR: db connection refused" in out
    # Primacy/recency ordering: identity first, log evidence last.
    assert out.index("phase=Running") < out.index("BackOff (9x")
    assert out.index("BackOff (9x") < out.index("ERROR: db connection refused")


async def test_diagnose_pod_fetches_logs_only_for_troubled_containers() -> None:
    kube = FakeDiagnoseKube()
    _ = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [c["container"] for c in kube.log_calls] == ["app"]


async def test_diagnose_pod_healthy_pod_skips_log_fetches() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["containerStatuses"] = [
        {
            "name": "app",
            "ready": True,
            "restartCount": 0,
            "state": {"running": {"startedAt": "2026-07-27T06:01:00Z"}},
        }
    ]
    kube.objects[("pods", "api-1")]["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    kube.events = []
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert kube.log_calls == []
    assert "no troubled containers" in out


async def test_diagnose_pod_missing_pod_is_an_error() -> None:
    kube = FakeDiagnoseKube()
    del kube.objects[("pods", "api-1")]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert out.startswith("ERROR:")


async def test_diagnose_pod_sub_fetch_failures_do_not_kill_the_report() -> None:
    """Owner, node, PVC, events, and logs are all best-effort evidence."""

    class FlakyKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            if meta.plural != "pods":
                raise RuntimeError("api hiccup")
            return await super().get_object(meta, namespace, name)

        async def list_events_for(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            raise RuntimeError("events down")

    out = await _diagnose_executor(FlakyKube()).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert not out.startswith("ERROR:")
    assert "phase=Running" in out
    assert "unavailable" in out  # the failed sections say so instead of vanishing
    assert "ERROR: db connection refused" in out  # log evidence still present


async def test_diagnose_pod_report_stays_under_the_ingest_cap() -> None:
    kube = FakeDiagnoseKube()
    kube.log_lines = [f"noise {i} " + "x" * 80 for i in range(1000)]
    kube.log_lines[500] = "ERROR: the one that matters"
    kube.events = [
        {
            "type": "Warning",
            "reason": f"Reason{i}",
            "message": f"message {i} " + "y" * 60,
            "count": 1,
            "lastTimestamp": f"2026-07-27T06:{i % 60:02d}:00Z",
        }
        for i in range(100)
    ]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) < MAX_RESULT_CHARS
    assert "truncated" not in out  # under the cap by construction, not by chopping
    assert "ERROR: the one that matters" in out


async def test_diagnose_pod_owner_chain_stops_at_a_direct_workload() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["metadata"]["ownerReferences"] = [
        {"kind": "StatefulSet", "name": "db", "controller": True}
    ]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "owner: StatefulSet db" in out
    assert "via" not in out


async def test_diagnose_pod_without_owner_reports_standalone() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["metadata"].pop("ownerReferences")
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "owner: none (standalone pod)" in out


async def test_diagnose_pod_reads_previous_instance_logs_after_restarts() -> None:
    """The crash evidence of a restarted container lives in the previous
    instance; the current one is either freshly restarted or gone."""
    kube = FakeDiagnoseKube()  # container "app" has restartCount=7
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [(c["container"], c["previous"]) for c in kube.log_calls] == [("app", True)]
    assert "[app] (previous instance)" in out


async def test_diagnose_pod_falls_back_to_current_logs_when_previous_unavailable() -> None:
    class NoPreviousKube(FakeDiagnoseKube):
        async def stream_logs(self, *a: Any, previous: bool = False, **k: Any) -> Any:
            if previous:
                raise RuntimeError("previous terminated logs rotated away")
            async for line in super().stream_logs(*a, previous=previous, **k):
                yield line

    kube = NoPreviousKube()
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "ERROR: db connection refused" in out
    assert "(previous instance)" not in out


async def test_diagnose_pod_reads_current_logs_for_a_never_restarted_container() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["containerStatuses"][0]["restartCount"] = 0
    _ = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [(c["container"], c["previous"]) for c in kube.log_calls] == [("app", False)]


async def test_diagnose_pod_unbounded_messages_cannot_evict_the_log_evidence() -> None:
    """Event/condition messages are cluster-controlled and unbounded; the
    report must clamp them and reserve room so the final LOG EXCERPTS
    section survives instead of being prefix-truncated away."""
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["conditions"] = [
        {"type": "Ready", "status": "False", "reason": "Huge", "message": "c" * 5000}
    ]
    kube.events = [
        {
            "type": "Warning",
            "reason": f"Reason{i}",
            "message": f"m{i} " + "e" * 4000,
            "count": 1,
            "lastTimestamp": f"2026-07-27T06:{i % 60:02d}:00Z",
        }
        for i in range(10)
    ]
    kube.log_lines = ["boot ok", "ERROR: db connection refused", "final line " + "z" * 3000]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    assert "truncated" not in out  # budgeted by construction, not chopped by execute()
    assert "ERROR: db connection refused" in out  # the evidence survived


async def test_diagnose_pod_finds_an_error_marker_beyond_the_line_clamp() -> None:
    """The error marker must be searched in the raw line — clamping first
    would hide a marker buried past the clamp in a long (e.g. JSON) line."""
    kube = FakeDiagnoseKube()
    kube.log_lines = [f"noise {i}" for i in range(40)]
    kube.log_lines[19] = "context sentinel before the buried marker"
    kube.log_lines[20] = "padding " * 40 + "ERROR: buried past the clamp"
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "context sentinel before the buried marker" in out


async def test_diagnose_pod_budgets_each_container_block_keeping_headers() -> None:
    """Overflow is trimmed within each container's block — a huge excerpt
    for one container must not evict another container's header or logs."""
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["spec"]["containers"] = [{"name": f"c{i}"} for i in range(3)]
    pod["status"]["containerStatuses"] = [
        {
            "name": f"c{i}",
            "ready": False,
            "restartCount": 4,
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
        }
        for i in range(3)
    ]
    kube.log_lines = [f"line {j} " + "x" * 230 for j in range(60)]
    kube.log_lines[30] = "ERROR: shared failure"
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    for i in range(3):  # every block keeps its attribution header
        assert f"[c{i}] (previous instance)" in out
    assert out.count("…") >= 3  # each over-budget block elides visibly


async def test_diagnose_pod_marks_pvcs_beyond_the_fetch_cap() -> None:
    """Storage evidence must not present a capped fetch as the full set —
    claim six could be the Pending one."""
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["spec"]["volumes"] = [
        {"name": f"v{i}", "persistentVolumeClaim": {"claimName": f"claim-{i}"}} for i in range(7)
    ]
    for i in range(7):
        kube.objects[("persistentvolumeclaims", f"claim-{i}")] = {
            "metadata": {"name": f"claim-{i}"},
            "status": {"phase": "Bound"},
        }
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "pvc claim-4: Bound" in out
    assert "pvc claim-5" not in out  # not fetched — and not silently omitted either
    assert "(2 more claims not fetched: claim-5, claim-6)" in out


async def test_diagnose_pod_works_with_pod_only_aliases() -> None:
    """Before background API discovery lands, the alias table holds only
    pods — the built-in ReplicaSet/Node/PVC lookups must still work via
    fixed metadata for these stable APIs, not silently vanish."""
    kube: Any = FakeDiagnoseKube()
    executor = ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META})
    out = await executor.execute("diagnose_pod", {"pod": "api-1", "namespace": "default"})
    assert "owner: Deployment api (via ReplicaSet api-6f)" in out
    assert "MemoryPressure=True" in out
    assert "pvc data-claim: Bound" in out


async def test_diagnose_pod_labels_a_failed_parent_lookup() -> None:
    """An RBAC/API failure on the ReplicaSet hop must not masquerade as
    'this ReplicaSet has no controller'."""

    class NoRsKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            if meta.plural == "replicasets":
                raise RuntimeError("rbac denied")
            return await super().get_object(meta, namespace, name)

    out = await _diagnose_executor(NoRsKube()).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "owner: ReplicaSet api-6f" in out
    assert "parent lookup unavailable" in out


async def test_diagnose_pod_clamps_the_skipped_container_summary() -> None:
    """The 'also troubled' name list is cluster-controlled (many long
    container names) and must respect the line clamp like everything else."""
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    names = [f"sidecar-{i}-" + "n" * 50 for i in range(30)]
    pod["status"]["containerStatuses"] = [
        {
            "name": name,
            "ready": False,
            "restartCount": 0,
            "state": {"waiting": {"reason": "ImagePullBackOff"}},
        }
        for name in names
    ]
    kube.log_lines = ["pull failed"]
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    assert "truncated" not in out  # never falls back to prefix truncation
    assert all(len(line) <= 250 for line in out.splitlines())
