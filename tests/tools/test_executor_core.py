"""Tests for read-only agent tools (ToolExecutor + READ_TOOLS schema)."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.tools.executor import (
    MAX_RESULT_CHARS,
    READ_TOOLS,
    UI_TOOLS,
    ToolExecutor,
)
from korvid.tools.registry import TOOLS_BY_NAME, ToolDef
from korvid.tools.structured import ERROR_PREFIX
from tests.tools.executor_fakes import (
    FakeBridge,
    FakeEventKube,
    FakeKube,
    FakeLogKube,
    make_executor,
    make_ui_executor,
)


def test_read_tools_schema_names() -> None:
    names = [t["function"]["name"] for t in READ_TOOLS]
    assert names == [
        "list_resources",
        "get_resource",
        "get_logs",
        "get_events",
        "list_operators",
        "helm_list_releases",
        "diagnose_pod",
        "diagnose_workload",
        "diagnose_service",
        "diagnose_pvc",
    ]


def test_read_tools_all_have_type_function() -> None:
    for tool in READ_TOOLS:
        assert tool["type"] == "function"
        assert "function" in tool
        assert "name" in tool["function"]
        assert "parameters" in tool["function"]


class _ExplodingKube:
    """Fails the test if any cluster call is made."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"cluster reached via {name} — slash guard must reject first")


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_resource", {"kind": "pods", "name": "default/web-1", "namespace": "app"}),
        ("get_events", {"kind": "pods", "name": "default/web-1", "namespace": "app"}),
        ("get_logs", {"pod": "default/web-1", "namespace": "app"}),
        ("diagnose_pod", {"pod": "default/web-1", "namespace": "app"}),
        (
            "diagnose_workload",
            {"kind": "deployments", "name": "default/web", "namespace": "app"},
        ),
    ],
)
async def test_slash_in_name_is_rejected_with_guidance_before_any_api_call(
    tool: str, args: dict[str, Any]
) -> None:
    """Small models paste 'namespace/name' composites (from row keys or
    prose) as the name and burn an iteration on a 404. Kubernetes names
    can never contain '/', so reject locally with wording that teaches
    the model to split the two fields."""
    out = await make_executor(_ExplodingKube()).execute(tool, args)
    assert out.startswith("ERROR:")
    assert "never contain '/'" in out
    assert "separately" in out


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_resource", {"kind": "pods", "name": "web-1", "namespace": "default/web-1"}),
        ("get_events", {"kind": "pods", "name": "web-1", "namespace": "default/web-1"}),
        ("get_logs", {"pod": "web-1", "namespace": "default/web-1"}),
        ("diagnose_pod", {"pod": "web-1", "namespace": "default/web-1"}),
        (
            "diagnose_workload",
            {"kind": "deployments", "name": "web", "namespace": "default/web"},
        ),
    ],
)
async def test_slash_in_namespace_is_rejected_symmetrically(
    tool: str, args: dict[str, Any]
) -> None:
    """The inverse paste also happens: the composite lands in the
    namespace field. Namespace names can never contain '/' either -
    same local rejection, same teaching, no API round-trip."""
    out = await make_executor(_ExplodingKube()).execute(tool, args)
    assert out.startswith("ERROR:")
    assert "never contain '/'" in out


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


async def test_structured_result_is_bounded_and_stays_parseable() -> None:
    """The ingest cap is enforced on the *document*: a byte cut would
    leave a fragment that is no longer YAML (issue #189)."""
    kube = FakeKube()
    kube.manifest = {"kind": "Pod", "metadata": {"name": "a"}, "blob": "x" * 20000}
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "d"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    loaded = yaml.safe_load(out)
    assert loaded["kind"] == "Pod"
    assert loaded["metadata"]["name"] == "a"
    assert loaded["blob"].endswith("…")


async def test_structured_result_elides_bulk_collections_but_keeps_identity() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Pod",
        "metadata": {
            "name": "a",
            "labels": {f"label-{index}": "x" * 200 for index in range(500)},
        },
        "spec": {"containers": [{"name": f"c{index}"} for index in range(400)]},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "d"}
    )
    assert len(out) <= MAX_RESULT_CHARS
    loaded = yaml.safe_load(out)
    assert loaded["kind"] == "Pod"
    assert loaded["metadata"]["name"] == "a"
    assert "elided" in out


async def test_executor_never_raises() -> None:
    class Boom:
        async def get_object(self, *a: Any, **k: Any) -> dict[str, Any]:
            raise RuntimeError("kaput")

    out = await make_executor(Boom()).execute(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "d"}
    )
    assert out.startswith("ERROR:")


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
    from korvid.tools.executor import RESIZE_TOOLS

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
    from korvid.k8s.models import OLMSubscriptionSummary, PackageManifestSummary
    from korvid.tools.executor import _MAX_CATALOG_PACKAGES

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


async def test_write_resize_path_routes_by_write_action_not_tool_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resize-specific behavior (implicit pods kind, resource validation)
    keys off the registry's `write_action`, not the literal tool name: a
    valid resize definition under another name must take the resize path."""
    fake = ToolDef(
        name="shrink_pod",
        schema={"type": "function", "function": {"name": "shrink_pod", "parameters": {}}},
        effect="cluster_write",
        dispatch="agent_request_write",
        surfaces=frozenset({"high_agent"}),
        result_format="untrusted_text",
        approval="user_confirmation",
        write_action="resize",
    )
    monkeypatch.setitem(TOOLS_BY_NAME, "shrink_pod", fake)
    bridge = FakeBridge()
    resources = {"app": {"requests": {"cpu": "100m"}}}
    out = await make_ui_executor(bridge).execute(
        "shrink_pod", {"name": "web-1", "namespace": "d", "resources": resources}
    )
    assert "must be a string" not in out
    assert bridge.calls[0][0] == "request_write"
    assert bridge.calls[0][1]["action"] == "resize"
    assert bridge.calls[0][1]["kind"] == "pods"


async def test_write_resize_path_still_validates_resources_under_any_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = ToolDef(
        name="shrink_pod",
        schema={"type": "function", "function": {"name": "shrink_pod", "parameters": {}}},
        effect="cluster_write",
        dispatch="agent_request_write",
        surfaces=frozenset({"high_agent"}),
        result_format="untrusted_text",
        approval="user_confirmation",
        write_action="resize",
    )
    monkeypatch.setitem(TOOLS_BY_NAME, "shrink_pod", fake)
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute(
        "shrink_pod", {"name": "web-1", "namespace": "d", "resources": {"app": {"bad": {}}}}
    )
    assert out.startswith("ERROR:")
    assert bridge.calls == []


# --- write proposal tools (issue #110) -----------------------------------


async def test_proposal_tools_are_rejected_off_the_mcp_surface() -> None:
    """The proposal tools are registered, so the shared executor 'knows'
    them — but only the MCP path enforces the per-run capability token
    before dispatch. A default-constructed executor (the built-in agent's
    wiring) must refuse them, or a hallucinated/prompt-injected call could
    queue proposals with empty transport metadata and no capability."""
    kube: Any = FakeKube()
    executor = ToolExecutor(kube, {"pods": PODS_META}, ui=FakeBridge())
    for name, args in (
        ("propose_write", {"action": "delete", "kind": "pods", "name": "web"}),
        ("get_write_proposal", {"proposal_id": "p1"}),
        ("cancel_write_proposal", {"proposal_id": "p1"}),
    ):
        result = await executor.execute(name, dict(args))
        assert result.startswith("ERROR:"), name
        assert "MCP" in result, name


async def test_propose_write_dispatches_to_the_proposal_entrypoint() -> None:
    bridge = FakeBridge()
    executor = make_ui_executor(bridge)
    result = await executor.execute(
        "propose_write",
        {
            "action": "scale",
            "kind": "deploy",
            "name": "web",
            "namespace": "default",
            "replicas": 3,
            "_session_id": "sess-1",
            "_client_name": "claude-code",
            "_client_version": "1.2",
        },
    )
    assert "proposal" in result
    assert bridge.calls == [
        (
            "submit_proposal",
            {
                "action": "scale",
                "kind": "deploy",
                "name": "web",
                "namespace": "default",
                "replicas": 3,
                "resources": None,
                "session_id": "sess-1",
                "client_name": "claude-code",
                "client_version": "1.2",
            },
        )
    ]


async def test_propose_write_rejects_an_unknown_action() -> None:
    executor = make_ui_executor(FakeBridge())
    result = await executor.execute(
        "propose_write", {"action": "drain", "kind": "nodes", "name": "n1"}
    )
    assert result.startswith("ERROR:")
    assert "action" in result


async def test_propose_write_scale_requires_replicas() -> None:
    executor = make_ui_executor(FakeBridge())
    result = await executor.execute(
        "propose_write", {"action": "scale", "kind": "deploy", "name": "web", "namespace": "d"}
    )
    assert result.startswith("ERROR:")
    assert "replicas" in result


async def test_propose_write_rejects_replicas_on_a_non_scale_action() -> None:
    """The immutable record must be exactly the operation the caller
    submitted: a stray `replicas` on a delete would be silently ignored by
    the operation builder while never appearing in the review dialog."""
    executor = make_ui_executor(FakeBridge())
    result = await executor.execute(
        "propose_write",
        {"action": "delete", "kind": "pods", "name": "web", "replicas": 3},
    )
    assert result.startswith("ERROR:")
    assert "only valid for a scale proposal" in result


async def test_propose_write_rejects_unknown_arguments() -> None:
    """A caller option the validator doesn't model (e.g. a delete
    propagation policy) must fail loudly: silently dropping it would queue
    a proposal that is not the operation the caller submitted — the same
    silent-mismatch already prevented for stray replicas/resources.
    Server-reserved keys (transport metadata, capability token) stay
    accepted."""
    executor = make_ui_executor(FakeBridge())
    result = await executor.execute(
        "propose_write",
        {"action": "delete", "kind": "pods", "name": "web", "propagation_policy": "Orphan"},
    )
    assert result.startswith("ERROR:")
    assert "propagation_policy" in result

    reserved = await executor.execute(
        "propose_write",
        {
            "action": "delete",
            "kind": "pods",
            "name": "web",
            "_session_id": "sess-1",
            "capability": "tok",
        },
    )
    assert not reserved.startswith("ERROR:")


async def test_get_write_proposal_requires_a_proposal_id() -> None:
    executor = make_ui_executor(FakeBridge())
    result = await executor.execute("get_write_proposal", {})
    assert result.startswith("ERROR:")
    assert "proposal_id" in result


async def test_cancel_write_proposal_forwards_the_transport_session() -> None:
    bridge = FakeBridge()
    executor = make_ui_executor(bridge)
    result = await executor.execute(
        "cancel_write_proposal", {"proposal_id": "p1", "_session_id": "sess-9"}
    )
    assert "cancelled" in result
    assert bridge.calls == [("cancel_proposal", {"proposal_id": "p1", "session_id": "sess-9"})]


async def test_proposal_tools_without_a_bridge_return_an_error() -> None:
    executor = make_ui_executor(None)
    result = await executor.execute(
        "propose_write", {"action": "delete", "kind": "pods", "name": "web"}
    )
    assert result.startswith("ERROR:")


async def test_an_ordinary_tool_error_is_still_an_error_string() -> None:
    """A cluster or argument failure stays model-visible and non-blocking."""
    outcome = await make_executor(_ExplodingKube()).execute_recorded(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )

    assert outcome.text.startswith(ERROR_PREFIX)


async def test_an_unknown_kind_is_still_an_error_string() -> None:
    outcome = await make_executor(FakeKube()).execute_recorded(
        "get_resource", {"kind": "widgets", "name": "s", "namespace": "d"}
    )

    assert outcome.text.startswith(ERROR_PREFIX)
