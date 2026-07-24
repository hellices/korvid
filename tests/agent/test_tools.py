"""Tests for read-only agent tools (ToolExecutor + READ_TOOLS schema)."""

from __future__ import annotations

from typing import Any

from korvid.agent.tools import MAX_RESULT_CHARS, READ_TOOLS, ToolExecutor
from korvid.k8s.discovery import PODS_META


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
