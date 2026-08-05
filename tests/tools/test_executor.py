"""Tests for read-only agent tools (ToolExecutor + READ_TOOLS schema)."""

from __future__ import annotations

import copy
from typing import Any
from unittest import mock

import pytest
import yaml

import korvid.tools.executor as executor_module
from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.k8s.discovery import PODS_META
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.logs import LogLine
from korvid.k8s.models import summary_for
from korvid.tools.executor import (
    MAX_RESULT_CHARS,
    READ_TOOLS,
    UI_TOOLS,
    RecordedExecution,
    ToolExecutor,
    ToolOutcome,
    ToolResultBlocked,
    UIBridge,
    as_recorded,
    compact_result,
)
from korvid.tools.registry import TOOLS_BY_NAME, ToolDef
from korvid.tools.structured import ERROR_PREFIX, load_structured_document


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
        "helm_list_releases",
        "diagnose_pod",
        "diagnose_workload",
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


async def test_get_resource_masks_an_aws_credential_env_value() -> None:
    """`AWS_SECRET_ACCESS_KEY` is the name people actually paste into a pod."""
    kube = FakeKube()
    kube.manifest = {
        "kind": "Pod",
        "metadata": {"name": "p", "namespace": "d"},
        "spec": {
            "containers": [
                {
                    "name": "main",
                    "env": [
                        {"name": "AWS_SECRET_ACCESS_KEY", "value": "aws-producer-sentinel"},
                        {"name": "AWS_REGION", "value": "eu-west-1"},
                    ],
                }
            ]
        },
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "p", "namespace": "d"}
    )
    assert "aws-producer-sentinel" not in out
    assert "eu-west-1" in out


#: Marker values that must never reach any consumer of a tool result.
NESTED_SECRET_SENTINEL = "bmVzdGVkLWNyZWQ="
LONG_NAME_ENV_SENTINEL = "primary-db-admin-pw"


def oversized_crd_with_nested_credentials() -> dict[str, Any]:
    """A CRD too large for the result budget that buries two classifiers.

    The embedded Secret's `kind` and the env entry's long `name` are what
    tell a redactor that `data.kubeconfig` and `value` are credentials.
    Both are removed by *size* reduction (mapping elision, scalar
    clamping), so anything that bounds before it redacts can no longer
    recognize the values it is about to ship.
    """
    return {
        "apiVersion": "apps.example.com/v1",
        "kind": "CompositeApp",
        "metadata": {"name": "composite-0", "namespace": "prod"},
        "spec": {
            "secretTemplate": {
                "data": {"kubeconfig": NESTED_SECRET_SENTINEL},
                **{f"annotationTemplate{index}": "x" * 200 for index in range(240)},
                "kind": "Secret",
                "apiVersion": "v1",
            },
            "podTemplate": {
                "containers": [
                    {
                        "name": "api",
                        "env": [
                            {
                                "name": "PRIMARY_DATABASE_CONNECTION_STRING_ADMIN_PASSWORD",
                                "value": LONG_NAME_ENV_SENTINEL,
                            }
                        ],
                    }
                ]
            },
        },
    }


async def test_get_resource_redacts_nested_credentials_before_size_reduction() -> None:
    """Size reduction must never precede redaction (PR #197 review).

    Elision drops the nested `kind: Secret`, and clamping cuts the
    credential word off a long env `name`; a document reduced first
    arrives at the central policy with the values still in it and no
    remaining evidence that they are secrets.
    """
    kube = FakeKube()
    kube.manifest = oversized_crd_with_nested_credentials()
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )
    assert NESTED_SECRET_SENTINEL not in out
    assert LONG_NAME_ENV_SENTINEL not in out
    # Still a bounded, parseable document that identifies its object.
    assert len(out) <= MAX_RESULT_CHARS
    loaded = yaml.safe_load(out)
    assert loaded["kind"] == "CompositeApp"
    assert loaded["metadata"]["name"] == "composite-0"


async def test_get_resource_keeps_non_sensitive_content_readable() -> None:
    """Redacting before bounding must not blank out ordinary manifests."""
    kube = FakeKube()
    kube.manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "api-0", "namespace": "prod"},
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "image": "example/api:1.2.3",
                    "env": [
                        {"name": "LOG_LEVEL", "value": "debug"},
                        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db"}}},
                    ],
                }
            ]
        },
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "api-0", "namespace": "prod"}
    )
    loaded = yaml.safe_load(out)
    container = loaded["spec"]["containers"][0]
    assert container["image"] == "example/api:1.2.3"
    assert container["env"][0] == {"name": "LOG_LEVEL", "value": "debug"}
    assert container["env"][1]["valueFrom"] == {"secretKeyRef": {"name": "db"}}


async def test_get_resource_fails_closed_on_an_unredactable_manifest() -> None:
    """Data the redactor cannot reason about is refused, not forwarded."""
    kube = FakeKube()
    kube.manifest = {"kind": "Pod", "metadata": {"name": "p"}, "spec": {1: "unmasked-value"}}
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "p", "namespace": "d"}
    )
    assert out.startswith("ERROR:")
    assert "unmasked-value" not in out


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

    async def agent_submit_write_proposal(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
        *,
        session_id: str = "",
        client_name: str = "",
        client_version: str = "",
    ) -> str:
        self.calls.append(
            (
                "submit_proposal",
                {
                    "action": action,
                    "kind": kind,
                    "name": name,
                    "namespace": namespace,
                    "replicas": replicas,
                    "resources": resources,
                    "session_id": session_id,
                    "client_name": client_name,
                    "client_version": client_version,
                },
            )
        )
        return "proposal abc123 pending"

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        self.calls.append(("get_proposal", {"proposal_id": proposal_id}))
        return "proposal pending"

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        self.calls.append(
            ("cancel_proposal", {"proposal_id": proposal_id, "session_id": session_id})
        )
        return "proposal cancelled"


def make_ui_executor(bridge: Any) -> ToolExecutor:
    kube: Any = FakeKube()
    # proposal_tools mirrors the MCP server's wiring — the only surface the
    # write-proposal tools may dispatch from.
    return ToolExecutor(kube, {"pods": PODS_META}, ui=bridge, proposal_tools=True)


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

    async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
        summaries: list[Any] = []
        for obj in self.objects.values():
            if obj.get("kind") != meta.kind:
                continue
            metadata = obj.get("metadata") or {}
            if meta.namespaced and namespace is not None and metadata.get("namespace") != namespace:
                continue
            summaries.append(summary_for(meta.kind, obj))
        return summaries

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


def test_diagnose_pod_schema_documents_the_log_container_cap() -> None:
    schema = next(t for t in READ_TOOLS if t["function"]["name"] == "diagnose_pod")
    description = schema["function"]["description"]
    assert "up to 3" in description


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


async def test_diagnose_pod_puts_current_health_before_historical_restart_evidence() -> None:
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["status"]["phase"] = "Running"
    pod["status"]["conditions"] = [{"type": "Ready", "status": "True"}]
    pod["status"]["containerStatuses"] = [
        {
            "name": "app",
            "ready": True,
            "restartCount": 2,
            "state": {"running": {"startedAt": "2026-07-27T06:01:00Z"}},
            "lastState": {"terminated": {"exitCode": 255, "reason": "Error"}},
        }
    ]
    kube.events = []
    kube.log_lines = ["lost connection to peer, exiting for restart"]

    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )

    assert "CURRENT HEALTH\n  READY NOW" in out
    assert out.index("READY NOW") < out.index("lost connection to peer")


async def test_diagnose_pod_includes_pvc_storage_class_and_warning_events() -> None:
    class PvcEventKube(FakeDiagnoseKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            if kind == "PersistentVolumeClaim":
                return [
                    {
                        "type": "Warning",
                        "reason": "ProvisioningFailed",
                        "message": 'storageclass.storage.k8s.io "fast-ssd" not found',
                        "count": 9,
                    }
                ]
            return await super().list_events_for(namespace, name, kind=kind, uid=uid)

    kube = PvcEventKube()
    pvc = kube.objects[("persistentvolumeclaims", "data-claim")]
    pvc["metadata"]["uid"] = "pvc-uid"
    pvc["spec"] = {"storageClassName": "fast-ssd"}
    pvc["status"] = {"phase": "Pending"}

    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )

    assert "pvc data-claim: Pending storageClass=fast-ssd" in out
    assert "ProvisioningFailed (9x)" in out
    assert 'storageclass.storage.k8s.io "fast-ssd" not found' in out


async def test_diagnose_pod_distinguishes_pvc_event_failure_from_pvc_read() -> None:
    class PvcEventsDeniedKube(FakeDiagnoseKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            if kind == "PersistentVolumeClaim":
                raise ApiStatusError(403, "PVC events forbidden")
            return await super().list_events_for(namespace, name, kind=kind, uid=uid)

    out = await _diagnose_executor(PvcEventsDeniedKube()).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert "pvc data-claim: Bound storageClass=(default)" in out
    assert "pvc data-claim warning events: unavailable" in out
    assert "pvc data-claim: unavailable" not in out


async def test_diagnose_pod_distinguishes_default_and_explicit_no_storage_class() -> None:
    kube = FakeDiagnoseKube()
    pod = kube.objects[("pods", "api-1")]
    pod["spec"]["volumes"] = [
        {"name": "defaulted", "persistentVolumeClaim": {"claimName": "defaulted"}},
        {"name": "classless", "persistentVolumeClaim": {"claimName": "classless"}},
    ]
    kube.objects[("persistentvolumeclaims", "defaulted")] = {
        "metadata": {"name": "defaulted"},
        "spec": {},
        "status": {"phase": "Pending"},
    }
    kube.objects[("persistentvolumeclaims", "classless")] = {
        "metadata": {"name": "classless"},
        "spec": {"storageClassName": ""},
        "status": {"phase": "Pending"},
    }

    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )

    assert "pvc defaulted: Pending storageClass=(default)" in out
    assert "pvc classless: Pending storageClass=(none)" in out


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


async def test_diagnose_pod_reads_current_logs_for_a_currently_failed_termination() -> None:
    """A container terminated non-zero *right now* logged that failure in the
    current instance — previous=True would fetch the penultimate crash."""
    kube = FakeDiagnoseKube()
    kube.objects[("pods", "api-1")]["status"]["containerStatuses"][0]["state"] = {
        "terminated": {"exitCode": 1, "reason": "Error"}
    }
    out = await _diagnose_executor(kube).execute(
        "diagnose_pod", {"pod": "api-1", "namespace": "default"}
    )
    assert [(c["container"], c["previous"]) for c in kube.log_calls] == [("app", False)]
    assert "(previous instance)" not in out


def test_render_log_blocks_survives_a_non_positive_budget() -> None:
    executor = _diagnose_executor(FakeDiagnoseKube())
    blocks = [["[app]", "line 1", "line 2"], ["[sidecar]", "line 3"]]
    lines = executor._render_log_blocks(blocks, -50)
    assert "  [app]" in lines
    assert "  [sidecar]" in lines


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


# --- diagnose_workload ------------------------------------------------------


def test_diagnose_workload_schema_prefers_one_call_for_rollout_failures() -> None:
    schema = next(t for t in READ_TOOLS if t["function"]["name"] == "diagnose_workload")
    description = schema["function"]["description"]
    assert "Deployment" in description
    assert "ReplicaSet" in description
    assert "pod" in description


async def test_diagnose_workload_follows_deployment_to_the_failing_pod() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {
            "replicas": 2,
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                    "message": 'ReplicaSet "api-6f" timed out progressing',
                }
            ],
        },
    }
    replicaset = kube.objects[("replicasets", "api-6f")]
    replicaset["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [
                {"kind": "Deployment", "name": "api", "uid": "deploy-uid", "controller": True}
            ],
        }
    )
    replicaset["spec"] = {"replicas": 1}
    replicaset["status"] = {"replicas": 1, "readyReplicas": 0}
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "name": "api-6f", "uid": "rs-uid", "controller": True}
    ]
    pod["status"]["phase"] = "Pending"
    pod["status"]["containerStatuses"][0]["state"] = {
        "waiting": {
            "reason": "ImagePullBackOff",
            "message": 'Back-off pulling image "api:v9-typo"',
        }
    }

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert not out.startswith("ERROR:")
    assert "WORKLOAD — Deployment default/api" in out
    assert "ProgressDeadlineExceeded" in out
    assert "ReplicaSet api-6f" in out
    assert "POD DIAGNOSIS — default/api-1" in out
    assert "ImagePullBackOff" in out
    assert len(out) <= MAX_RESULT_CHARS


async def test_diagnose_workload_projects_deployment_replica_status() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 3},
        "status": {
            "replicas": 3,
            "updatedReplicas": 2,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "unavailableReplicas": 2,
        },
    }
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "desired=3 current=3 updated=2 ready=1 available=1 unavailable=2" in out


async def test_diagnose_workload_budget_keeps_every_selected_pod_header() -> None:
    class PriorityEvidenceKube(FakeDiagnoseKube):
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
            for index in range(200):
                yield LogLine(
                    pod=pod,
                    container=container,
                    text=f"noise {index} " + "x" * 220,
                )
            yield LogLine(pod=pod, container=container, text=f"DECISIVE EVIDENCE {pod}")

    kube = PriorityEvidenceKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 3},
        "status": {"replicas": 3},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    source = kube.objects[("pods", "api-1")]
    source["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}
    ]
    for index in range(2, 4):
        pod = copy.deepcopy(source)
        pod["metadata"]["name"] = f"api-{index}"
        pod["metadata"]["uid"] = f"pod-{index}"
        if index == 2:
            pod["status"]["containerStatuses"][0]["state"] = {
                "waiting": {"reason": "ImagePullBackOff"}
            }
        else:
            pod["status"]["phase"] = "Pending"
            pod["status"]["containerStatuses"][0]["state"] = {
                "waiting": {"reason": "ContainerCreating"}
            }
        kube.objects[("pods", f"api-{index}")] = pod
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert len(out) <= MAX_RESULT_CHARS
    for name in ("api-1", "api-2", "api-3"):
        assert f"POD DIAGNOSIS — default/{name}" in out
        assert f"POD DIAGNOSIS — default/{name}" in compact_result(out, 3_000)
    visible = compact_result(out, 3_000)
    assert "POD DIAGNOSIS — default/api-2: phase=ImagePullBackOff" in visible
    assert "POD DIAGNOSIS — default/api-3: phase=ContainerCreating" in visible
    assert "DECISIVE EVIDENCE api-3" in visible


async def test_diagnose_workload_prefers_newest_replicaset_pods() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 4},
        "status": {"replicas": 4},
    }
    old_rs = kube.objects[("replicasets", "api-6f")]
    old_rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "old-rs",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "annotations": {"deployment.kubernetes.io/revision": "1"},
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    new_rs = copy.deepcopy(old_rs)
    new_rs["metadata"].update(
        {
            "name": "api-new",
            "uid": "new-rs",
            "creationTimestamp": "2026-02-01T00:00:00Z",
            "annotations": {"deployment.kubernetes.io/revision": "2"},
        }
    )
    kube.objects[("replicasets", "api-new")] = new_rs
    source = kube.objects[("pods", "api-1")]
    source["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "old-rs", "name": "api-6f"}
    ]
    for index in range(2, 5):
        pod = copy.deepcopy(source)
        pod["metadata"]["name"] = f"api-old-{index}"
        pod["metadata"]["uid"] = f"old-pod-{index}"
        kube.objects[("pods", f"api-old-{index}")] = pod
    new_pod = copy.deepcopy(source)
    new_pod["metadata"]["name"] = "api-new-1"
    new_pod["metadata"]["uid"] = "new-pod"
    new_pod["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "new-rs", "name": "api-new"}
    ]
    kube.objects[("pods", "api-new-1")] = new_pod

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "POD DIAGNOSIS — default/api-new-1" in out


async def test_diagnose_workload_bounds_omitted_pod_names() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 30},
        "status": {"replicas": 30},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    source = kube.objects[("pods", "api-1")]
    source["metadata"]["ownerReferences"] = [
        {"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}
    ]
    for index in range(2, 31):
        pod = copy.deepcopy(source)
        pod["metadata"]["name"] = f"api-{index}-" + "x" * 200
        pod["metadata"]["uid"] = f"pod-{index}"
        kube.objects[("pods", pod["metadata"]["name"])] = pod
    kube.log_lines = ["ERROR: useful evidence"]

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert len(out) <= MAX_RESULT_CHARS
    assert "(27 more non-ready pod(s) not expanded:" in out
    assert "useful evidence" in out
    omitted_line = next(line for line in out.splitlines() if "more non-ready pod(s)" in line)
    assert len(omitted_line) <= 1_300


async def test_diagnose_workload_rejects_unsupported_kinds_with_guidance() -> None:
    out = await _diagnose_executor(FakeDiagnoseKube()).execute(
        "diagnose_workload",
        {"kind": "nodes", "name": "node-a", "namespace": "default"},
    )
    assert out.startswith("ERROR:")
    assert "supports deployments" in out


async def test_diagnose_workload_uses_builtin_deployment_before_discovery() -> None:
    kube: Any = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {},
    }
    out = await ToolExecutor(kube, {"pods": PODS_META, "pod": PODS_META}).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert not out.startswith("ERROR: unknown kind")


async def test_diagnose_workload_keeps_parent_and_siblings_when_a_pod_read_fails() -> None:
    class FlakyPodKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            if meta.plural == "pods" and name == "api-1":
                raise RuntimeError("response decode failed")
            return await super().get_object(meta, namespace, name)

    kube = FlakyPodKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                }
            ]
        },
    }
    kube.objects[("replicasets", "api-6f")]["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [
                {
                    "kind": "Deployment",
                    "name": "api",
                    "uid": "deploy-uid",
                    "controller": True,
                }
            ],
        }
    )
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["ownerReferences"] = [
        {
            "kind": "ReplicaSet",
            "name": "api-6f",
            "uid": "rs-uid",
            "controller": True,
        }
    ]
    sibling = copy.deepcopy(pod)
    sibling["metadata"]["name"] = "api-2"
    sibling["metadata"]["uid"] = "pod-2"
    kube.objects[("pods", "api-2")] = sibling

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert not out.startswith("ERROR:")
    assert "ProgressDeadlineExceeded" in out
    assert "POD DIAGNOSIS — default/api-1" in out
    assert "unavailable (response decode failed)" in out
    assert "POD DIAGNOSIS — default/api-2" in out
    assert "CrashLoopBackOff" in out


async def test_diagnose_workload_includes_running_pod_with_failed_ready_condition() -> None:
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}]
    pod["status"]["phase"] = "Running"
    pod["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    pod["status"]["containerStatuses"][0]["ready"] = True
    pod["status"]["containerStatuses"][0]["state"] = {"running": {"startedAt": "x"}}
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "POD DIAGNOSIS — default/api-1" in out


async def test_diagnose_workload_rejects_same_name_replacement_uid() -> None:
    class ReplacedPodKube(FakeDiagnoseKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            obj = await super().get_object(meta, namespace, name)
            if meta.plural == "pods" and name == "api-1":
                obj = copy.deepcopy(obj)
                obj["metadata"]["uid"] = "replacement-uid"
            return obj

    kube = ReplacedPodKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "status": {},
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    pod = kube.objects[("pods", "api-1")]
    pod["metadata"]["uid"] = "original-uid"
    pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "uid": "rs-uid", "name": "api-6f"}]
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert "UID changed from original-uid to replacement-uid" in out


async def test_diagnose_workload_keeps_status_when_deployment_events_fail() -> None:
    class EventsDeniedKube(FakeDiagnoseKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            if kind == "Deployment":
                raise ApiStatusError(403, "events forbidden")
            return await super().list_events_for(namespace, name, kind=kind, uid=uid)

    kube = EventsDeniedKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {
            "replicas": 2,
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                }
            ],
        },
    }
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert not out.startswith("ERROR:")
    assert "ProgressDeadlineExceeded" in out
    assert "unavailable (API 403: events forbidden)" in out


@pytest.mark.parametrize(
    ("failed_plural", "expected_section"),
    [
        ("replicasets", "OWNED REPLICASETS\n  unavailable"),
        ("pods", "POD DIAGNOSES\n  unavailable"),
    ],
)
async def test_diagnose_workload_keeps_parent_when_child_list_fails(
    failed_plural: str,
    expected_section: str,
) -> None:
    class ChildListDeniedKube(FakeDiagnoseKube):
        async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
            if meta.plural == failed_plural:
                raise ApiStatusError(403, f"{failed_plural} forbidden")
            return await super().list_objects(meta, namespace)

    kube = ChildListDeniedKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {
            "replicas": 2,
            "readyReplicas": 1,
            "conditions": [
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                }
            ],
        },
    }
    rs = kube.objects[("replicasets", "api-6f")]
    rs["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [{"kind": "Deployment", "uid": "deploy-uid", "name": "api"}],
        }
    )
    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )
    assert not out.startswith("ERROR:")
    assert "ProgressDeadlineExceeded" in out
    assert expected_section in out


def test_compact_result_honors_tiny_limits() -> None:
    """The output-never-exceeds-limit contract must hold for any input: a
    limit shorter than the truncation marker cannot fit the marker, so it
    degrades to a hard cut instead of returning the whole marker."""
    from korvid.tools.executor import compact_result

    text = "x" * 200
    for limit in (0, 1, 10, 40):
        assert len(compact_result(text, limit)) <= limit
    assert compact_result(text, 0) == ""
    assert compact_result(text, 10) == "x" * 10


def _ui_def(name: str, dispatch: str) -> ToolDef:
    return ToolDef(
        name=name,
        schema={"type": "function", "function": {"name": name, "parameters": {}}},
        effect="ui_only",
        dispatch=dispatch,
        surfaces=frozenset({"full_agent"}),
        result_format="untrusted_text",
    )


async def test_ui_dispatch_follows_registry_dispatch_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry's validated dispatch key — not the tool name — picks the
    bridge handler: a new UI definition targeting `agent_set_filter` must call
    set_filter, never fall through to open_describe."""
    monkeypatch.setitem(TOOLS_BY_NAME, "weird_filter", _ui_def("weird_filter", "agent_set_filter"))
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute("weird_filter", {"pattern": "web"})
    assert out == "filter set to 'web'"
    assert bridge.calls == [("set_filter", {"pattern": "web"})]


async def test_ui_dispatch_without_adapter_is_an_error_not_a_fallthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A UI dispatch key with no argument adapter must produce an explicit
    error instead of silently invoking a different handler."""
    monkeypatch.setitem(TOOLS_BY_NAME, "odd_ui", _ui_def("odd_ui", "agent_request_write"))
    bridge = FakeBridge()
    out = await make_ui_executor(bridge).execute("odd_ui", {"kind": "pods", "name": "x"})
    assert "no argument adapter" in out
    assert bridge.calls == []


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
        surfaces=frozenset({"full_agent"}),
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
        surfaces=frozenset({"full_agent"}),
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


# --- Malformed Secret metadata is fail-closed (issue #189, review round 4) ---

#: A serialized Secret with unmasked `data`, as `kubectl apply` stores it.
MALFORMED_SECRET_SENTINEL = "UkFXLVNFQ1JFVA=="
_SERIALIZED_SECRET = f'{{"kind":"Secret","data":{{"tls.key":"{MALFORMED_SECRET_SENTINEL}"}}}}'


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"annotations": _SERIALIZED_SECRET}, id="annotations-string"),
        pytest.param({"annotations": [_SERIALIZED_SECRET]}, id="annotations-list"),
        pytest.param(_SERIALIZED_SECRET, id="metadata-string"),
        pytest.param([{"annotations": {"x": _SERIALIZED_SECRET}}], id="metadata-list"),
    ],
)
async def test_get_resource_refuses_a_secret_with_malformed_metadata(
    metadata: Any,
) -> None:
    """A shape the redactor cannot search is refused, not walked.

    `kubectl apply` puts the whole pre-apply manifest in a metadata
    annotation. The removal rule reaches it through mappings only, so a
    non-mapping `metadata`/`annotations` on a Secret shipped a serialized
    Secret verbatim (PR #197 review round 4).
    """
    kube = FakeKube()
    kube.manifest = {"kind": "Secret", "metadata": metadata, "data": {"a": "Yg=="}}

    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "db", "namespace": "prod"}
    )

    assert out.startswith("ERROR:")
    assert MALFORMED_SECRET_SENTINEL not in out


async def test_get_resource_still_returns_a_well_formed_secret() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {"name": "db", "annotations": {"team": "sre"}},
        "data": {"password": "Yg=="},
    }

    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "db", "namespace": "prod"}
    )

    loaded = yaml.safe_load(out)
    assert loaded["metadata"]["annotations"] == {"team": "sre"}
    assert loaded["data"]["password"] == MASK_PLACEHOLDER


async def test_execute_still_returns_a_plain_string_for_mcp_and_eval_callers() -> None:
    """MCP and the eval runner call `execute` and hand the value on as text."""
    kube = FakeKube()
    kube.manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "web", "labels": {"app": "we\x07ird"}},
    }

    result = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "web", "namespace": "d"}
    )

    assert type(result) is str
    assert "\x07" not in result


async def test_execute_recorded_reports_what_the_producer_removed() -> None:
    kube = FakeKube()
    kube.manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "web",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": '{"kind":"Secret"}'
            },
            "labels": {"app": "we\x07ird"},
        },
    }
    executor = make_executor(kube)
    args = {"kind": "pods", "name": "web", "namespace": "d"}

    outcome = await executor.execute_recorded("get_resource", args)

    assert outcome.text == await executor.execute("get_resource", args)
    reasons = {item.reason for item in outcome.redactions}
    assert "last-applied-configuration" in reasons
    assert "control-character" in reasons


async def test_execute_recorded_reports_nothing_for_a_text_tool() -> None:
    kube = FakeKube()

    outcome = await make_executor(kube).execute_recorded("get_events", {"namespace": "default"})

    assert outcome.redactions == ()
    assert isinstance(outcome.text, str)


# --- A redaction failure is not an ordinary tool error (round 6) ------------
#
# `redact_document` refuses shapes it cannot reason about — a `kind:
# Secret` whose metadata is not a mapping, a cycle, a non-string key.
# Collapsing that into an `ERROR: ...` string made it indistinguishable
# from "the API said no", so the agent kept the turn going.

_UNREDACTABLE_SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": "not-a-mapping",
    "data": {"password": "cmF3LXNlY3JldA=="},
}


async def test_execute_recorded_raises_when_a_result_cannot_be_redacted() -> None:
    kube = FakeKube()
    kube.manifest = _UNREDACTABLE_SECRET

    with pytest.raises(ToolResultBlocked, match="could not redact the result"):
        await make_executor(kube).execute_recorded(
            "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
        )


async def test_a_blocked_result_carries_no_raw_data() -> None:
    kube = FakeKube()
    kube.manifest = _UNREDACTABLE_SECRET

    with pytest.raises(ToolResultBlocked) as caught:
        await make_executor(kube).execute_recorded(
            "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
        )

    assert "cmF3LXNlY3JldA==" not in str(caught.value)


async def test_execute_still_returns_a_safe_error_string_when_redaction_fails() -> None:
    """MCP and the eval runner take strings; they must not start raising."""
    kube = FakeKube()
    kube.manifest = _UNREDACTABLE_SECRET

    result = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )

    assert type(result) is str
    assert result.startswith(ERROR_PREFIX)
    assert "cmF3LXNlY3JldA==" not in result


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


# --- The recorded-execution contract is an ABC (round 6) -------------------
#
# The agent loop used to runtime-check a private Protocol it declared
# itself, which put the interface in the consuming layer. AGENTS.md
# places boundary interfaces in the owning layer, as abc.ABC.


def test_the_real_executor_satisfies_the_recorded_contract() -> None:
    assert isinstance(make_executor(FakeKube()), RecordedExecution)


async def test_a_string_only_executor_reports_no_producer_records() -> None:
    """The default implementation: the capability stays optional."""

    class Plain(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    outcome = await Plain().execute_recorded("get_resource", {})

    assert outcome == ToolOutcome(text="ok")


async def test_as_recorded_adapts_an_executor_that_only_has_execute() -> None:
    """Duck-typed executors keep working without subclassing anything."""

    class Duck:
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "ok"

    adapted = as_recorded(Duck())

    assert isinstance(adapted, RecordedExecution)
    assert await adapted.execute("get_resource", {}) == "ok"
    assert (await adapted.execute_recorded("get_resource", {})).redactions == ()


def test_as_recorded_does_not_wrap_what_already_satisfies_the_contract() -> None:
    executor = make_executor(FakeKube())

    assert as_recorded(executor) is executor


# --- Shaped text is redacted before it is cut (round 9) --------------------

_LOG_SECRET = "9f3c1a7e42b85d06c7e1f0a2b3d4e5f60718293a4b5c6d7e8f90"


def _credential_log_kube(assignment: str) -> FakeDiagnoseKube:
    """A rollout failure whose log excerpts carry a credential assignment."""
    kube = FakeDiagnoseKube()
    kube.objects[("deployments", "api")] = {
        "kind": "Deployment",
        "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
        "spec": {"replicas": 2},
        "status": {"replicas": 2, "readyReplicas": 1, "conditions": []},
    }
    replicaset = kube.objects[("replicasets", "api-6f")]
    replicaset["metadata"].update(
        {
            "namespace": "default",
            "uid": "rs-uid",
            "ownerReferences": [
                {"kind": "Deployment", "name": "api", "uid": "deploy-uid", "controller": True}
            ],
        }
    )
    replicaset["spec"] = {"replicas": 1}
    replicaset["status"] = {"replicas": 1, "readyReplicas": 0}
    base = kube.objects[("pods", "api-1")]
    for index in range(2, 4):
        clone = copy.deepcopy(base)
        clone["metadata"]["name"] = f"api-{index}"
        kube.objects[("pods", f"api-{index}")] = clone
    for key, obj in list(kube.objects.items()):
        if key[0] != "pods":
            continue
        obj["metadata"]["ownerReferences"] = [
            {"kind": "ReplicaSet", "name": "api-6f", "uid": "rs-uid", "controller": True}
        ]
        obj["metadata"].setdefault("namespace", "default")
        obj["status"]["phase"] = "Pending"
    # Long enough that each pod block still exceeds its share after
    # masking, so the head+tail cut genuinely fires on this fixture.
    kube.log_lines = [
        "." * (index % 11) + f"level=error {assignment} retry " + "z" * 520 for index in range(200)
    ]
    return kube


@pytest.mark.parametrize(
    "assignment",
    [
        f"api_key={_LOG_SECRET}",
        f"password={_LOG_SECRET}",
        f"AWS_SECRET_ACCESS_KEY={_LOG_SECRET}",
    ],
    ids=["api_key", "password", "aws"],
)
async def test_a_shaped_report_is_redacted_before_it_is_compacted(assignment: str) -> None:
    """Head+tail compaction cuts at a byte offset, so an assignment that
    straddles the cut loses the keyword that classifies it and strands the
    value in the tail. Redaction has to run first (PR #197 review)."""
    outcome = await _diagnose_executor(_credential_log_kube(assignment)).execute_recorded(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert _LOG_SECRET not in outcome.text
    assert MASK_PLACEHOLDER in outcome.text
    assert outcome.redactions
    assert not outcome.error


async def test_a_compacted_report_keeps_its_evidence_and_its_bound() -> None:
    kube = _credential_log_kube(f"api_key={_LOG_SECRET}")

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert "WORKLOAD — Deployment default/api" in out
    assert "POD DIAGNOSIS — default/api-1" in out
    assert "middle truncated" in out
    assert len(out) <= MAX_RESULT_CHARS


async def test_a_report_without_credentials_is_left_alone() -> None:
    kube = _credential_log_kube("level=error image pull failed")

    outcome = await _diagnose_executor(kube).execute_recorded(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert MASK_PLACEHOLDER not in outcome.text
    assert not outcome.redactions
    assert "image pull failed" in outcome.text


async def test_the_string_api_reports_the_same_redacted_report() -> None:
    """`execute()` is what the MCP host and the eval grader take; the
    producer's redaction is on that path too, not only the recorded one."""
    out = await _diagnose_executor(_credential_log_kube(f"api_key={_LOG_SECRET}")).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert _LOG_SECRET not in out
    assert MASK_PLACEHOLDER in out


# --- The parent sections are redacted too (round 10 final review) ----------
#
# Round 9 redacted the per-pod blocks, which is where a log excerpt lands.
# The rest of the compound report — the workload's own conditions, its
# Warning events, the owned-ReplicaSet lines, the child-LIST error — is
# assembled from cluster strings just as attacker-influenced as a log
# line, and went out unredacted.

PARENT_SECRET = "4d2e6b8a1c0f73951e8a0d2c4b6e8f01"


class ParentCredentialKube(FakeDiagnoseKube):
    """A rollout failure whose *parent* sections carry the credential.

    The pod blocks are clean on purpose: they have been redacted since
    round 9, so only an assignment in a workload condition, a workload
    Warning event, or a failed child LIST can show whether the assembled
    parent report is covered too.
    """

    def __init__(
        self,
        *,
        condition_message: str = "replicas are unavailable",
        event_message: str = "failed to create pods",
        list_error: str = "",
        pod_log_line: str = "connection refused",
    ) -> None:
        super().__init__()
        self.list_error = list_error
        self.objects[("deployments", "api")] = {
            "kind": "Deployment",
            "metadata": {"name": "api", "namespace": "default", "uid": "deploy-uid"},
            "spec": {"replicas": 2},
            "status": {
                "replicas": 2,
                "readyReplicas": 1,
                "conditions": [
                    {
                        "type": "Available",
                        "status": "False",
                        "reason": "MinimumReplicasUnavailable",
                        "message": condition_message,
                    }
                ],
            },
        }
        replicaset = self.objects[("replicasets", "api-6f")]
        replicaset["metadata"].update(
            {
                "namespace": "default",
                "uid": "rs-uid",
                "ownerReferences": [
                    {"kind": "Deployment", "name": "api", "uid": "deploy-uid", "controller": True}
                ],
            }
        )
        replicaset["spec"] = {"replicas": 1}
        replicaset["status"] = {"replicas": 1, "readyReplicas": 0}
        for key, obj in list(self.objects.items()):
            if key[0] != "pods":
                continue
            obj["metadata"]["ownerReferences"] = [
                {"kind": "ReplicaSet", "name": "api-6f", "uid": "rs-uid", "controller": True}
            ]
            obj["metadata"].setdefault("namespace", "default")
            obj["status"]["phase"] = "Pending"
        self.log_lines = [pod_log_line]
        self.workload_events: list[dict[str, Any]] = [
            {
                "type": "Warning",
                "reason": "FailedCreate",
                "message": event_message,
                "count": 3,
                "lastTimestamp": "2026-07-27T06:30:00Z",
            }
        ]

    async def list_objects(self, meta: Any, namespace: str | None) -> list[Any]:
        if self.list_error and meta.kind == "Pod":
            raise ApiStatusError(500, self.list_error)
        return await super().list_objects(meta, namespace)

    async def list_events_for(
        self, namespace: str, name: str, *, kind: str | None = None, uid: str | None = None
    ) -> list[dict[str, Any]]:
        # The workload's own events, not the pod's: this fixture has to be
        # able to plant a credential in exactly one of the two.
        return self.workload_events if kind == "Deployment" else self.events


_WORKLOAD_ARGS = {"kind": "deployments", "name": "api", "namespace": "default"}


async def test_a_workload_condition_credential_never_leaves_the_producer() -> None:
    """A Deployment condition message is cluster text like any other."""
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET} at startup"
    )

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert MASK_PLACEHOLDER in outcome.text
    assert [record.reason for record in outcome.redactions] == ["credential-assignment"]
    assert "MinimumReplicasUnavailable" in outcome.text
    assert not outcome.error


async def test_a_workload_warning_event_credential_never_leaves_the_producer() -> None:
    kube = ParentCredentialKube(event_message=f"registry auth failed password={PARENT_SECRET}")

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert "FailedCreate (3x" in outcome.text
    assert [record.reason for record in outcome.redactions] == ["credential-assignment"]


async def test_a_failed_child_list_credential_never_leaves_the_producer() -> None:
    """The LIST error is interpolated straight from the API exception."""
    kube = ParentCredentialKube(list_error=f"denied for AWS_SECRET_ACCESS_KEY={PARENT_SECRET}")

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert MASK_PLACEHOLDER in outcome.text
    assert "POD DIAGNOSES" in outcome.text
    assert outcome.redactions


async def test_the_parent_report_is_redacted_exactly_once() -> None:
    """Two assignments, two records: the parent must not be passed through
    redaction twice, which would inflate the inventory the inspector shows."""
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET}",
        event_message=f"registry auth failed password={PARENT_SECRET}",
    )

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert [record.reason for record in outcome.redactions] == ["credential-assignment"] * 2
    assert {record.path for record in outcome.redactions} == {"report"}


async def test_parent_and_pod_redactions_share_one_record_trail() -> None:
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET}",
        pod_log_line=f"level=error api_key={_LOG_SECRET} retry",
    )

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert _LOG_SECRET not in outcome.text
    # One record from the parent condition, one from the single expanded
    # pod block: two passes, one trail, nothing counted twice.
    assert [record.reason for record in outcome.redactions] == ["credential-assignment"] * 2


async def test_the_masked_parent_report_keeps_its_sections_and_its_bound() -> None:
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET}",
        event_message=f"registry auth failed password={PARENT_SECRET}",
    )

    out = await _diagnose_executor(kube).execute("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in out
    for title in (
        "WORKLOAD — Deployment default/api",
        "SELECTED NON-READY PODS",
        "WORKLOAD CONDITIONS (failing first)",
        "WORKLOAD WARNING EVENTS (newest first)",
        "OWNED REPLICASETS",
    ):
        assert title in out
    assert out.index("WORKLOAD CONDITIONS") < out.index("WORKLOAD WARNING EVENTS")
    assert out.index("OWNED REPLICASETS") < out.index("\nPOD DIAGNOSIS — default/api-1\n")
    assert "MinimumReplicasUnavailable" in out
    assert len(out) <= MAX_RESULT_CHARS


async def test_a_parent_report_without_credentials_is_left_alone() -> None:
    outcome = await _diagnose_executor(ParentCredentialKube()).execute_recorded(
        "diagnose_workload", _WORKLOAD_ARGS
    )

    assert MASK_PLACEHOLDER not in outcome.text
    assert not outcome.redactions
    assert "replicas are unavailable" in outcome.text


# --- Recursion exhaustion is a refusal, not a result (round 9) -------------


def _deeply_nested_secret(depth: int = 1500) -> dict[str, Any]:
    """A CRD burying a `Secret` deeper than the interpreter can recurse."""
    document: Any = {
        "kind": "Secret",
        "metadata": {"name": "db"},
        "data": {"password": "cmF3LXNlY3JldA=="},
    }
    for _ in range(depth):
        document = {"spec": {"nested": document}}
    return {"apiVersion": "v1", "kind": "CompositeApp", **document}


class _DeepKube:
    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        return self._document


async def test_a_manifest_too_deep_to_redact_is_blocked_not_reported() -> None:
    """Running out of stack means the redactor never finished, so it can
    promise nothing about the document (PR #197 review)."""
    executor = ToolExecutor(_DeepKube(_deeply_nested_secret()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    with pytest.raises(ToolResultBlocked, match="too deeply nested"):
        await executor.execute_recorded(
            "get_resource", {"kind": "pods", "name": "a", "namespace": "b"}
        )


async def test_a_manifest_too_deep_to_serialize_is_blocked_not_reported() -> None:
    """The redacted document still has to be written out, and that walk
    is just as recursive."""
    document = _deeply_nested_secret()
    executor = ToolExecutor(_DeepKube(document), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    with (
        mock.patch.object(executor_module, "_mask_manifest", return_value=(document, [])),
        pytest.raises(ToolResultBlocked, match="too deeply nested"),
    ):
        await executor.execute_recorded(
            "get_resource", {"kind": "pods", "name": "a", "namespace": "b"}
        )


async def test_the_string_api_reports_a_deep_manifest_as_a_safe_error() -> None:
    """MCP hosts have no turn to stop, so they get the same safe string
    every other refusal produces — naming the shape, never the document."""
    executor = ToolExecutor(_DeepKube(_deeply_nested_secret()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    out = await executor.execute("get_resource", {"kind": "pods", "name": "a", "namespace": "b"})

    assert out.startswith(ERROR_PREFIX)
    assert "too deeply nested" in out
    assert "cmF3LXNlY3JldA==" not in out
    assert "recursion" not in out


async def test_an_unrelated_recursion_failure_stays_an_ordinary_error() -> None:
    """Only the redaction and serialization walk is normalized; a handler
    bug elsewhere must not be reported as a redaction refusal."""

    class _RecursingKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            def spin(n: int) -> int:
                return spin(n + 1)

            return {"kind": "Pod", "depth": spin(0)}

    executor = ToolExecutor(_RecursingKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "b"}
    )

    assert outcome.error
    assert outcome.text.startswith(ERROR_PREFIX)


# --- A bounded manifest still says what it is (round 10) ------------------


def identity_last_crd() -> dict[str, Any]:
    """An oversized CRD whose identity keys are last in insertion order."""
    document: dict[str, Any] = {f"extensionField{index:02d}": "y" * 400 for index in range(60)}
    document["apiVersion"] = "example.com/v1"
    document["kind"] = "CompositeApp"
    document["metadata"] = {
        "labels": {f"team-{index}": "z" * 60 for index in range(40)},
        "name": "composite-0",
        "namespace": "prod",
    }
    return document


async def test_a_bounded_manifest_still_names_its_object() -> None:
    """A result the model cannot identify is not evidence, and the
    reduction used to drop identity whenever the document listed it last
    (PR #197 review)."""
    executor = ToolExecutor(_DeepKube(identity_last_crd()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )

    manifest = yaml.safe_load(outcome.text)
    assert len(outcome.text) <= MAX_RESULT_CHARS
    assert manifest["kind"] == "CompositeApp"
    assert manifest["apiVersion"] == "example.com/v1"
    assert manifest["metadata"]["name"] == "composite-0"
    assert manifest["metadata"]["namespace"] == "prod"


def test_as_recorded_refuses_something_that_cannot_execute_a_tool() -> None:
    """Fail at composition, not at the first tool call of a live session."""

    class NotAnExecutor:
        pass

    with pytest.raises(TypeError, match="async execute"):
        as_recorded(NotAnExecutor())


# --- What the producer writes, the boundary can read (round 13) -----------


def _ambiguous_key_manifest() -> dict[str, Any]:
    """Annotation keys that YAML would resolve to bools, nulls and numbers.

    Serialized carelessly they collapse into one another; the boundary
    refuses a document whose keys collapse, so the producer has to emit
    them as the distinct strings they are.
    """
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "flags",
            "annotations": {
                "true": "a",
                "yes": "b",
                "null": "c",
                "~": "d",
                "1": "e",
                "1.0": "f",
                "on": "g",
            },
        },
    }


async def test_a_produced_manifest_survives_the_strict_reader() -> None:
    """The boundary re-reads every structured result, so a document the
    producer writes must never look ambiguous when it is read back."""
    executor = ToolExecutor(_DeepKube(_ambiguous_key_manifest()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "flags", "namespace": "prod"}
    )
    loaded = load_structured_document(outcome.text)

    assert loaded == _ambiguous_key_manifest()
    assert len(loaded["metadata"]["annotations"]) == 7


async def test_a_bounded_produced_manifest_survives_the_strict_reader() -> None:
    """Reduction rewrites the document; what it emits has to stay readable."""
    executor = ToolExecutor(_DeepKube(identity_last_crd()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )
    loaded = load_structured_document(outcome.text)

    assert loaded["kind"] == "CompositeApp"
