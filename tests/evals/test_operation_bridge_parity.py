"""`OperationUIBridgeProxy` must track `UIBridge` exactly.

`tests/evals/operation_app.py` may not import the production composition
root's private proxy, so it defines its own late-binding one. These tests
fail the moment `UIBridge` gains, loses, or changes a method — which is
exactly when the proxy would otherwise start silently degrading a real
tool call to "UI not ready". They also pin that the harness imports no
private production name, never wraps the audit log, reads exactly one
private app attribute, and arms the shipped `small` surface unchanged.
"""

from __future__ import annotations

import ast
import inspect
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from korvid.agent.profiles import build_profile
from korvid.evals.__main__ import prompt_fingerprint
from korvid.tools.executor import UIBridge

from . import operation_app
from .operation_app import OperationUIBridgeProxy

#: One representative call per `UIBridge` method: (method, args, kwargs).
#: `test_the_call_table_covers_every_uibridge_method` fails if a method is
#: added to the interface without a case here.
_CALLS: tuple[tuple[str, tuple[Any, ...], dict[str, Any]], ...] = (
    ("agent_navigate", ("pods",), {"namespace": "shop-a"}),
    ("agent_set_filter", ("checkout",), {}),
    ("agent_open_logs", ("checkout-a-1", "shop-a"), {"container": "checkout"}),
    ("agent_open_describe", ("Deployment", "checkout-a"), {"namespace": "shop-a"}),
    ("agent_drill_down", ("checkout-a",), {}),
    (
        "agent_request_write",
        ("scale", "deployments", "checkout-a"),
        {"namespace": "shop-a", "replicas": 3},
    ),
    (
        "agent_submit_write_proposal",
        ("scale", "deployments", "checkout-a"),
        {"namespace": "shop-a", "replicas": 3, "session_id": "session-1"},
    ),
    ("agent_get_write_proposal", ("proposal-1",), {}),
    ("agent_cancel_write_proposal", ("proposal-1",), {"session_id": "session-1"}),
)


def _interface_methods() -> frozenset[str]:
    return frozenset(UIBridge.__abstractmethods__)


class _RecordingBridge(UIBridge):
    """A bind target that records what the proxy delegated."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _record(self, name: str) -> str:
        self.calls.append(name)
        return name

    async def agent_navigate(self, view: str, namespace: str | None = None) -> str:
        return self._record("agent_navigate")

    async def agent_set_filter(self, pattern: str) -> str:
        return self._record("agent_set_filter")

    async def agent_open_logs(self, pod: str, namespace: str, container: str | None = None) -> str:
        return self._record("agent_open_logs")

    async def agent_open_describe(self, kind: str, name: str, namespace: str | None = None) -> str:
        return self._record("agent_open_describe")

    async def agent_drill_down(self, name: str) -> str:
        return self._record("agent_drill_down")

    async def agent_request_write(
        self,
        action: str,
        kind: str,
        name: str,
        namespace: str | None = None,
        replicas: int | None = None,
        resources: dict[str, dict[str, dict[str, str]]] | None = None,
    ) -> str:
        return self._record("agent_request_write")

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
        return self._record("agent_submit_write_proposal")

    async def agent_get_write_proposal(self, proposal_id: str) -> str:
        return self._record("agent_get_write_proposal")

    async def agent_cancel_write_proposal(self, proposal_id: str, *, session_id: str = "") -> str:
        return self._record("agent_cancel_write_proposal")


def _composition_root_source() -> str:
    path = operation_app.__file__
    assert path is not None
    return Path(path).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _tree() -> ast.Module:
    """One parse shared by every structural check.

    Parsing per helper would hand out different node objects for the same
    call, so `is` comparisons between them (the audit-log check below)
    would be meaningless.
    """
    return ast.parse(_composition_root_source())


def _imported_names() -> set[str]:
    """Every module/symbol the composition root imports, from its AST.

    AST rather than text: the module's own docstring names the production
    proxy in prose to explain why it is *not* imported, and a substring
    check could never tell those two apart.
    """
    names: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def _class_bases() -> dict[str, set[str]]:
    return {
        node.name: {ast.unparse(base) for base in node.bases}
        for node in ast.walk(_tree())
        if isinstance(node, ast.ClassDef)
    }


def _calls_named(name: str) -> list[ast.Call]:
    """Every direct call to *name* in the composition root, from its AST."""
    return [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _foreign_private_attributes() -> set[str]:
    """Private attributes the module reads off something other than `self`.

    Dunders (`super().__init__`) are not the private-API question here.
    """
    found: set[str] = set()
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        if node.attr.startswith("__"):
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            continue
        found.add(node.attr)
    return found


def test_the_proxy_implements_every_uibridge_method() -> None:
    assert _interface_methods() != frozenset()
    assert OperationUIBridgeProxy.__abstractmethods__ == frozenset()
    assert _interface_methods() <= frozenset(vars(OperationUIBridgeProxy))


@pytest.mark.parametrize("name", sorted(UIBridge.__abstractmethods__))
def test_every_proxy_signature_matches_the_interface(name: str) -> None:
    assert inspect.signature(getattr(OperationUIBridgeProxy, name)) == inspect.signature(
        getattr(UIBridge, name)
    )


def test_the_call_table_covers_every_uibridge_method() -> None:
    assert {name for name, _args, _kwargs in _CALLS} == _interface_methods()


@pytest.mark.parametrize(("name", "args", "kwargs"), _CALLS)
async def test_an_unbound_proxy_degrades_instead_of_raising(
    name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    proxy = OperationUIBridgeProxy()
    assert await getattr(proxy, name)(*args, **kwargs) == "ERROR: UI not ready"


@pytest.mark.parametrize(("name", "args", "kwargs"), _CALLS)
async def test_a_bound_proxy_delegates_every_call(
    name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    proxy = OperationUIBridgeProxy()
    bridge = _RecordingBridge()
    proxy.target = bridge
    assert await getattr(proxy, name)(*args, **kwargs) == name
    assert bridge.calls == [name]


def test_the_composition_root_imports_no_private_production_name() -> None:
    """No `korvid.__main__` import at all, and no private symbol from any
    package outside `korvid.evals` (whose eval-internal helpers the eval
    package already shares, e.g. `runner._CountingProvider`). Dunder
    modules such as `__future__` are not private production API."""
    imported = _imported_names()
    assert imported != set()
    assert not any(name.startswith("korvid.__main__") for name in imported)
    private = {
        name
        for name in imported
        if name.rpartition(".")[2].startswith("_")
        and not name.rpartition(".")[2].startswith("__")
        and not name.startswith("korvid.evals.")
    }
    assert private == set()


def test_the_composition_root_uses_the_shipped_audit_log_unwrapped() -> None:
    """AST, not formatted source.

    This is a security guard: it must not break because `ruff format`
    reflowed a call, and it must not pass because a matching string
    appeared in a docstring. So it asserts the *shape* — exactly one
    `AuditLog(...)`, constructed inline as `KorvidApp(audit=...)`, with a
    context, and nothing in the module subclassing it.
    """
    bases = _class_bases()
    assert "OperationUIBridgeProxy" in bases
    assert bases["OperationUIBridgeProxy"] == {"UIBridge"}
    assert all("AuditLog" not in base for names in bases.values() for base in names)
    audit_calls = _calls_named("AuditLog")
    assert len(audit_calls) == 1
    assert _keyword(audit_calls[0], "context") is not None
    app_calls = _calls_named("KorvidApp")
    assert len(app_calls) == 1
    assert _keyword(app_calls[0], "audit") is audit_calls[0]


def test_the_injected_write_ops_observes_the_real_audit_file() -> None:
    """The write fake is constructed with a probe over the same path the
    `AuditLog` above writes — the fail-closed ordering evidence."""
    write_ops = _keyword(_calls_named("KorvidApp")[0], "write_ops")
    assert isinstance(write_ops, ast.Call)
    probe = _keyword(write_ops, "audit_intent_probe")
    assert isinstance(probe, ast.Call)
    assert isinstance(probe.func, ast.Name)
    assert probe.func.id == "make_audit_intent_probe"


def test_the_harness_exposes_no_dialog_hook_parameter() -> None:
    """A fixture's mid-dialog action is declarative
    (`journey.dialog_intervention`), so pytest and the campaign run the
    same journey. A hook parameter would let a test give a fixture
    semantics the campaign could never reproduce."""
    parameters = inspect.signature(operation_app.run_operation_journey).parameters
    assert "on_dialog" not in parameters
    assert "dialog_intervention" not in parameters
    assert operation_app.MIN_APPROVAL_TIMEOUT >= 1.0


def test_the_only_private_app_attribute_use_is_the_documented_turn_settle() -> None:
    """The harness reads exactly one private attribute off a foreign object:
    `app._agent_task`, inside `_turn_task_settled`. Everything else — table
    lookup, panel state, turn boundaries — goes through public API. A
    docstring that merely names `_focused_table()` is not an access, which
    is why this is an AST check rather than a substring search."""
    assert _foreign_private_attributes() == {"_agent_task"}


def test_the_harness_arms_the_shipped_small_surface_unchanged() -> None:
    """Prompt/tool/config parity with production wiring (design: "Parity
    tests pin the `UIBridge` method set and prompt/tool/config fingerprint
    against production wiring").

    Slice A grades the shipped profile, never a harness variant: the same
    composed prompt, the same tool schemas, and the same budgets the
    composition root arms. Slice C's ablations build explicit variants and
    fingerprint them separately.
    """
    profile = build_profile("small", readonly=False, resize_supported=False)
    names = {str(tool.get("function", tool).get("name")) for tool in profile.tools}
    assert "scale_resource" in names
    assert "rollout_restart" in names
    # Armed but never legitimately used by a Slice A fixture: any
    # delete dialog is an `unrelated_write` hard failure.
    assert "delete_resource" in names
    assert "resize_pod" not in names  # resize_supported=False
    assert (profile.max_iterations, profile.max_history_chars) == (6, 24_000)
    assert (profile.max_result_chars, profile.max_tool_calls_per_iteration) == (3_000, 1)
    assert profile.strict_history_budget is True
    assert sorted(prompt_fingerprint(profile, tools=profile.tools)) == ["sha256", "source"]
    # AST again: the harness must build the shipped profile with exactly
    # these two flags, and a reflowed call is not a policy change.
    profile_calls = _calls_named("build_profile")
    assert len(profile_calls) == 1
    assert {keyword.arg for keyword in profile_calls[0].keywords} == {
        "readonly",
        "resize_supported",
    }
    assert all(
        isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        for keyword in profile_calls[0].keywords
    )
