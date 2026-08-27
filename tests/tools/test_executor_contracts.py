"""Executor recorded-result and contract tests."""

from __future__ import annotations

from typing import Any

import pytest

from korvid.k8s.errors import ApiStatusError
from korvid.tools.executor import RecordedExecution, ToolOutcome, as_recorded
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
        surfaces=frozenset({"high_agent"}),
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


async def test_ui_only_bridge_failure_text_is_reported_as_an_error() -> None:
    """A `UIBridge` method may fail without raising: the class contract
    (executor.py: "implementations must not raise") has every method return
    an `ERROR: ...` string on failure instead, e.g. `agent_navigate` when an
    approval dialog is already open. `execute_recorded` wrapped that plain
    string with the default `error=False`, so a real bridge failure looked
    identical to a clean navigation to the redaction/provenance policy and
    to any other `outcome.error` consumer."""

    class DenyingBridge(FakeBridge):
        async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
            return f"{ERROR_PREFIX} an approval dialog is open — the user is deciding"

    outcome = await make_ui_executor(DenyingBridge()).execute_recorded("navigate", {"view": "pods"})

    assert outcome.error is True
    assert outcome.text.startswith(ERROR_PREFIX)


async def test_ui_only_bridge_success_text_is_not_an_error() -> None:
    outcome = await make_ui_executor(FakeBridge()).execute_recorded("navigate", {"view": "pods"})

    assert outcome.error is False
    assert outcome.text == "switched to pods"


async def test_write_bridge_failure_text_is_reported_as_an_error() -> None:
    class DenyingWriteBridge(FakeBridge):
        async def agent_request_write(
            self,
            action: str,
            kind: str,
            name: str,
            namespace: str | None = None,
            replicas: int | None = None,
            resources: dict[str, dict[str, dict[str, str]]] | None = None,
        ) -> str:
            return f"{ERROR_PREFIX} UI not ready"

    outcome = await make_ui_executor(DenyingWriteBridge()).execute_recorded(
        "rollout_restart",
        {"kind": "deployments", "name": "web", "namespace": "shop"},
    )

    assert outcome.error is True
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


def test_as_recorded_refuses_something_that_cannot_execute_a_tool() -> None:
    """Fail at composition, not at the first tool call of a live session."""

    class NotAnExecutor:
        pass

    with pytest.raises(TypeError, match="async execute"):
        as_recorded(NotAnExecutor())


async def test_get_events_reports_the_incarnation_it_scoped_to() -> None:
    """The UID must leave the executor, or a citation cannot check it.

    `get_events` already scopes to the live object's UID so events belong
    to one incarnation. Keeping that UID inside the handler means a pod
    recreated under the same name is opened as though it were the cited
    evidence (#250).
    """
    outcome = await make_executor(FakeEventKube()).execute_recorded(
        "get_events", {"kind": "Pod", "namespace": "d", "name": "web"}
    )

    assert outcome.incarnation == "abc-123"


async def test_get_events_reports_no_incarnation_when_the_object_is_gone() -> None:
    """A read that fell back to name scope has no incarnation to promise."""

    class GoneKube(FakeEventKube):
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            raise ApiStatusError(404, "NotFound")

    outcome = await make_executor(GoneKube()).execute_recorded(
        "get_events", {"kind": "pods", "namespace": "d", "name": "web"}
    )

    assert outcome.incarnation is None


async def test_get_logs_reports_the_container_it_resolved() -> None:
    """The reader picked a container; the citation must not pick again.

    `get_logs` with no container streams the pod's *first* one. Re-deriving
    that rule at open time is a second implementation of the same choice,
    and the two can disagree (#250).
    """

    outcome = await make_executor(FakeLogKube()).execute_recorded(
        "get_logs", {"pod": "web", "namespace": "d"}
    )

    assert outcome.container == "app"


async def test_get_logs_with_an_explicit_container_reports_it_unchanged() -> None:
    """No resolution happened, so nothing may be invented about identity."""
    outcome = await make_executor(FakeLogKube()).execute_recorded(
        "get_logs", {"pod": "web", "namespace": "d", "container": "sidecar"}
    )

    assert outcome.container == "sidecar"


async def test_get_resource_reports_the_incarnation_it_fetched() -> None:
    """The most-cited read had no identity at all.

    `get_resource` holds the exact manifest, so it knows which instance it
    returned. Dropping the UID left the commonest citation unable to
    detect a replacement - the failure this change exists to remove
    (#250 review).
    """
    kube = FakeKube()
    kube.manifest = {"kind": "Pod", "metadata": {"name": "web", "uid": "res-uid"}}

    outcome = await make_executor(kube).execute_recorded(
        "get_resource", {"kind": "pods", "namespace": "d", "name": "web"}
    )

    assert outcome.incarnation == "res-uid"


async def test_get_logs_does_not_claim_an_identity_it_cannot_vouch_for() -> None:
    """The manifest lookup and the log stream are two name-based reads.

    A pod recreated between them returns the replacement's lines under the
    old UID, which downstream would treat as a positive identification and
    warn about the wrong thing. The container it resolved is still
    reported - that much it does know (#250 review).
    """
    kube = FakeLogKube()
    kube.manifest["metadata"]["uid"] = "pod-uid"

    outcome = await make_executor(kube).execute_recorded(
        "get_logs", {"pod": "web", "namespace": "d"}
    )

    assert outcome.incarnation is None
    assert outcome.container == "app"
