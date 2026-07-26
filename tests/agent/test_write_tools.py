"""Agent write tools: approval-gated cluster mutations (issue #16, spec §6.2).

The agent never mutates the cluster directly — every write tool routes
through UIBridge.agent_request_write, whose only implementation opens a
ConfirmScreen the *user* must approve with a real keystroke.
"""

from typing import Any

from korvid.agent.runtime import SYSTEM_PROMPT, WRITE_PROMPT, AgentRuntime
from korvid.agent.tools import (
    READ_TOOLS,
    UI_TOOLS,
    WRITE_TOOL_NAMES,
    WRITE_TOOLS,
    ToolExecutor,
    UIBridge,
)


class _FakeBridge(UIBridge):
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return "ok"

    async def agent_set_filter(self, pattern: str) -> str:
        return "ok"

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return "ok"

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return "ok"

    async def agent_drill_down(self, name: str) -> str:
        return "ok"

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        self.writes.append(
            {
                "action": action,
                "kind": kind,
                "name": name,
                "namespace": namespace,
                "replicas": replicas,
            }
        )
        return f"approved and executed: {action} {kind}/{name}"


def test_write_tool_schemas() -> None:
    names = {t["function"]["name"] for t in WRITE_TOOLS}
    assert names == {"delete_resource", "scale_resource", "rollout_restart"}
    # resize_pod lives outside WRITE_TOOLS (conditionally registered) but is
    # always dispatchable, so the name set includes it.
    assert frozenset(names) | {"resize_pod"} == WRITE_TOOL_NAMES
    for tool in WRITE_TOOLS:
        fn = tool["function"]
        required = set(fn["parameters"]["required"])
        assert required >= {"kind", "name"}
        # Every write tool must tell the model about the user-approval gate.
        assert "approv" in fn["description"].lower()
    scale = next(t for t in WRITE_TOOLS if t["function"]["name"] == "scale_resource")
    assert "replicas" in scale["function"]["parameters"]["required"]


async def test_executor_routes_delete_to_bridge() -> None:
    bridge = _FakeBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]  # kube unused for write dispatch
    result = await executor.execute(
        "delete_resource", {"kind": "pods", "name": "web-1", "namespace": "default"}
    )
    assert "approved and executed" in result
    assert bridge.writes == [
        {
            "action": "delete",
            "kind": "pods",
            "name": "web-1",
            "namespace": "default",
            "replicas": None,
        }
    ]


async def test_executor_routes_scale_with_replicas() -> None:
    bridge = _FakeBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
    await executor.execute(
        "scale_resource",
        {"kind": "deployments", "name": "web", "namespace": "default", "replicas": 3},
    )
    assert bridge.writes[0]["action"] == "scale"
    assert bridge.writes[0]["replicas"] == 3


async def test_executor_routes_rollout_restart() -> None:
    bridge = _FakeBridge()
    executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
    await executor.execute(
        "rollout_restart", {"kind": "deployments", "name": "web", "namespace": "default"}
    )
    assert bridge.writes[0]["action"] == "rollout_restart"


async def test_executor_rejects_non_integer_replicas() -> None:
    """Tool schemas are not runtime validation: coercing 1.9 or true to an
    int would show the user an operation the model never requested."""
    for bad in (1.9, True, "3"):
        bridge = _FakeBridge()
        executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
        result = await executor.execute(
            "scale_resource",
            {"kind": "deployments", "name": "web", "namespace": "default", "replicas": bad},
        )
        assert result.startswith("ERROR:")
        assert "replicas" in result
        assert bridge.writes == []  # never reached the approval path


async def test_executor_write_without_ui_is_error() -> None:
    executor = ToolExecutor(kube=None, aliases={}, ui=None)  # type: ignore[arg-type]
    result = await executor.execute("delete_resource", {"kind": "pods", "name": "web-1"})
    assert result.startswith("ERROR:")


class _NullProvider:
    def complete(self, messages: Any, tools: Any, *, stream: bool = True) -> Any:
        raise NotImplementedError


class _NullExecutor:
    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        return "ok"


def _system_prompt(runtime: AgentRuntime) -> str:
    return str(runtime._messages[0]["content"])  # asserting the composed prompt


def test_prompt_mentions_approval_when_write_tools_armed() -> None:
    runtime = AgentRuntime(
        _NullProvider(), _NullExecutor(), tools=READ_TOOLS + UI_TOOLS + WRITE_TOOLS
    )
    prompt = _system_prompt(runtime)
    assert WRITE_PROMPT in prompt
    assert "approv" in prompt.lower()


def test_prompt_offers_kubectl_when_write_tools_absent() -> None:
    runtime = AgentRuntime(_NullProvider(), _NullExecutor(), tools=READ_TOOLS)
    prompt = _system_prompt(runtime)
    assert WRITE_PROMPT not in prompt
    assert "kubectl" in prompt


def test_base_system_prompt_has_no_write_claims() -> None:
    """The base prompt must not hard-code 'no write tools' — that claim is
    now conditional on whether WRITE_TOOLS were armed."""
    assert "no write tools" not in SYSTEM_PROMPT.lower()


def test_namespaced_write_schemas_require_namespace() -> None:
    """scale/rollout targets are all namespaced apps/* workloads: the schema
    must not advertise calls that are guaranteed to fail validation. Delete
    keeps namespace optional (cluster-scoped kinds are deletable)."""
    by_name = {t["function"]["name"]: t["function"]["parameters"]["required"] for t in WRITE_TOOLS}
    assert "namespace" in by_name["scale_resource"]
    assert "namespace" in by_name["rollout_restart"]
    assert "namespace" not in by_name["delete_resource"]


async def test_executor_rejects_non_string_write_arguments() -> None:
    """Coercing a schema-invalid numeric name like 123 would turn it into the
    valid Kubernetes target "123" and reach the approval path."""
    cases = [
        {"kind": 5, "name": "web", "namespace": "default"},
        {"kind": "deployments", "name": 123, "namespace": "default"},
        {"kind": "deployments", "name": "web", "namespace": 7},
    ]
    for args in cases:
        bridge = _FakeBridge()
        executor = ToolExecutor(kube=None, aliases={}, ui=bridge)  # type: ignore[arg-type]
        result = await executor.execute("delete_resource", args)
        assert result.startswith("ERROR:")
        assert bridge.writes == []  # never reached the approval path


def test_write_prompt_forbids_retrying_expired_requests() -> None:
    """An expired (unanswered) request must be as non-retryable as an
    explicit denial - reissuing it would keep reopening approval dialogs
    the user is not acting on."""
    assert "Never retry a denied or expired request" in WRITE_PROMPT


def test_write_prompt_enumerates_armed_write_tools() -> None:
    """The system instruction must list exactly the write tools that were
    armed: enumerating a fixed trio steers the model away from resize_pod
    on clusters that support it."""
    from korvid.agent.tools import RESIZE_TOOLS

    runtime = AgentRuntime(
        _NullProvider(),
        _NullExecutor(),
        tools=READ_TOOLS + WRITE_TOOLS + RESIZE_TOOLS,
    )
    assert "resize_pod" in _system_prompt(runtime)


def test_write_prompt_omits_unarmed_resize_tool() -> None:
    """Without RESIZE_TOOLS armed the prompt must not advertise resize_pod."""
    runtime = AgentRuntime(_NullProvider(), _NullExecutor(), tools=READ_TOOLS + WRITE_TOOLS)
    prompt = _system_prompt(runtime)
    assert "resize_pod" not in prompt
    assert "delete_resource" in prompt
