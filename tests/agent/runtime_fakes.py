"""Shared runtime test fakes and builders."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

from korvid.agent.profiles import build_profile
from korvid.agent.runtime import AgentRuntime
from korvid.k8s.discovery import PODS_META
from korvid.tools.executor import READ_TOOLS, RecordedExecution, ToolExecutor


class ScriptedProvider:
    """Each call to complete() pops the next scripted event list."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = turns
        self.calls: list[list[dict[str, Any]]] = []
        self.tool_surfaces: list[list[dict[str, Any]]] = []

    @property
    def name(self) -> str:
        return "scripted"

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, stream: bool = True
    ) -> AsyncIterator[dict[str, Any]]:
        self.calls.append([dict(m) for m in messages])
        self.tool_surfaces.append(tools)
        for ev in self.turns.pop(0):
            yield ev


class EchoExecutor(RecordedExecution):
    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return f"result-of-{name}"


async def collect(
    runtime: AgentRuntime,
    text: str,
    screen_context: str = "view=pods ns=default",
) -> list[Any]:
    return [e async for e in runtime.run_turn(text, screen_context)]


def _read_tools_request_ceiling(non_tool_request_budget: int) -> int:
    return len(json.dumps(READ_TOOLS, separators=(",", ":"))) + non_tool_request_budget


class RaisingExecutor(RecordedExecution):
    async def execute(self, name: str, arguments: dict[str, object]) -> str:
        raise RuntimeError("boom")


def _bulk_pod_manifest(*, labels: int) -> dict[str, Any]:
    """A benign but bulky Pod manifest — no Secret object, just size.

    Real workloads exceed the 8k ingest cap easily (annotations, long
    label sets, status conditions), so this is the ordinary case that must
    reach the model, not an attack.
    """
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": "api-0",
            "namespace": "prod",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
                    {"stringData": {"password": "last-applied-hunter2"}}
                ),
                "operator.example.com/notes": "rollout completed cleanly. " * 40,
            },
            "labels": {f"team-{index}": f"squad-{index}" for index in range(labels)},
        },
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "image": "registry.example.com/api:1.2.3",
                    "env": [
                        {"name": "DB_PASSWORD", "value": "env-hunter2"},
                        {"name": "API_KEY", "value": "env-raw-key"},
                        {"name": "LOG_LEVEL", "value": "debug"},
                    ],
                }
            ]
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {"type": f"Ready-{index}", "status": "True", "message": "all good " * 20}
                for index in range(labels // 4 or 1)
            ],
        },
    }


class _ManifestKube:
    """Minimal ReadOps stand-in for the get_resource path."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        return copy.deepcopy(self.manifest)


def _manifest_executor(manifest: dict[str, Any]) -> Any:
    """A real ToolExecutor over a fake cluster returning `manifest`."""
    return ToolExecutor(_ManifestKube(manifest), {"pods": PODS_META, "pod": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps


def _get_resource_provider() -> ScriptedProvider:
    return ScriptedProvider(
        [
            [
                {
                    "type": "tool_call",
                    "id": "c1",
                    "name": "get_resource",
                    "arguments": '{"kind":"pods","name":"api-0","namespace":"prod"}',
                },
                {"type": "done"},
            ],
            [{"type": "text_delta", "text": "the pod is healthy"}, {"type": "done"}],
        ]
    )


def _bulk_text_executor(chars: int) -> Any:
    class BulkExecutor(RecordedExecution):
        async def execute(self, name: str, arguments: dict[str, Any]) -> str:
            return "log line evidence. " * (chars // 19)

    return BulkExecutor()


def _tool_then_text_script(tool_iterations: int, answer: str) -> list[list[dict[str, Any]]]:
    script: list[list[dict[str, Any]]] = [
        [
            {
                "type": "tool_call",
                "id": f"c{index}",
                "name": "get_logs",
                "arguments": '{"pod":"api-0","namespace":"prod"}',
            },
            {"type": "done"},
        ]
        for index in range(tool_iterations)
    ]
    script.append([{"type": "text_delta", "text": answer}, {"type": "done"}])
    return script


def _profile_runtime(profile_name: str, provider: Any, executor: Any) -> AgentRuntime:
    profile = build_profile(profile_name, readonly=True, resize_supported=False)
    return AgentRuntime(
        provider,
        executor,
        tools=profile.tools,
        max_iterations=profile.max_iterations,
        max_history_chars=profile.max_history_chars,
        max_result_chars=profile.max_result_chars,
        max_tool_calls_per_iteration=profile.max_tool_calls_per_iteration,
        strict_history_budget=profile.strict_history_budget,
        system_prompt=profile.system_prompt,
        ui_prompt=profile.ui_prompt,
    )


def _one_tool_turn(tool: str) -> list[list[dict[str, Any]]]:
    return [
        [{"type": "tool_call", "id": "c1", "name": tool, "arguments": "{}"}, {"type": "done"}],
        [{"type": "text_delta", "text": "ok"}, {"type": "done"}],
    ]


def _get_resource_turn() -> list[list[dict[str, Any]]]:
    return [
        [
            {
                "type": "tool_call",
                "id": "c1",
                "name": "get_resource",
                "arguments": '{"kind": "pods", "name": "web", "namespace": "prod"}',
            },
            {"type": "done"},
        ],
        [{"type": "text_delta", "text": "ok"}, {"type": "done"}],
    ]


def _text_turn(count: int = 1) -> list[list[dict[str, Any]]]:
    return [[{"type": "text_delta", "text": "ok"}, {"type": "done"}] for _ in range(count)]
