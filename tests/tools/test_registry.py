"""Validated tool metadata registry (issue #91 Finding A).

The registry is the single source of tool metadata: identity, dispatch,
effect, approval policy, capability gate, and exposure surfaces. These
tests pin the validation rules and that every derived surface matches the
pre-registry lists exactly.
"""

from __future__ import annotations

from typing import Any

import pytest

from korvid.tools import registry as registry_mod
from korvid.tools.executor import ToolExecutor, UIBridge
from korvid.tools.registry import (
    TOOL_DEFS,
    ToolDef,
    agent_tool_schemas,
    mcp_tool_schemas,
    validate_dispatch_targets,
    validate_tool_defs,
)


def _names(schemas: list[dict[str, Any]]) -> list[str]:
    return [t["function"]["name"] for t in schemas]


# --- registry contents -------------------------------------------------


def test_tool_names_unique() -> None:
    names = [d.name for d in TOOL_DEFS]
    assert len(names) == len(set(names))


def test_schema_names_agree_with_tool_names() -> None:
    for d in TOOL_DEFS:
        assert d.schema["type"] == "function"
        assert d.schema["function"]["name"] == d.name


def test_every_tool_has_a_dispatch_target() -> None:
    """Rule 3: dispatch keys resolve against the real executor/bridge
    classes — a typo'd handler name must fail here, not at call time."""
    validate_dispatch_targets(TOOL_DEFS, executor_cls=ToolExecutor, bridge_cls=UIBridge)
    for d in TOOL_DEFS:
        assert d.dispatch


def test_cluster_writes_require_confirmation_and_write_action() -> None:
    for d in TOOL_DEFS:
        if d.effect == "cluster_write":
            assert d.approval == "user_confirmation"
            assert d.write_action
        else:
            assert d.approval == "none"
            assert d.write_action is None


def test_no_mcp_tool_has_cluster_write_effect() -> None:
    """Independent of list placement: the MCP surface can never contain a
    cluster write or an approval-gated tool (issue #11 non-goal)."""
    for d in TOOL_DEFS:
        if "mcp" in d.surfaces:
            assert d.effect != "cluster_write"
            assert d.approval == "none"


def test_resize_is_capability_gated() -> None:
    resize = next(d for d in TOOL_DEFS if d.name == "resize_pod")
    assert resize.capability == "pod_resize"
    others = [d for d in TOOL_DEFS if d.name != "resize_pod"]
    assert all(d.capability == "none" for d in others)


def test_every_tool_declares_an_outbound_result_format() -> None:
    assert {d.result_format for d in TOOL_DEFS} <= {
        "structured_yaml",
        "untrusted_text",
    }
    assert registry_mod.tool_result_format("get_resource") == "structured_yaml"
    assert all(
        registry_mod.tool_result_format(d.name) == "untrusted_text"
        for d in TOOL_DEFS
        if d.name != "get_resource"
    )


# --- derived surfaces ---------------------------------------------------


def test_full_agent_surface_matches_pre_registry_order() -> None:
    from korvid.tools.executor import READ_TOOLS, RESIZE_TOOLS, UI_TOOLS, WRITE_TOOLS

    schemas = agent_tool_schemas("full_agent", readonly=False, resize_supported=True)
    assert schemas == READ_TOOLS + UI_TOOLS + WRITE_TOOLS + RESIZE_TOOLS


def test_full_agent_surface_readonly_omits_writes() -> None:
    from korvid.tools.executor import READ_TOOLS, UI_TOOLS

    schemas = agent_tool_schemas("full_agent", readonly=True, resize_supported=True)
    assert schemas == READ_TOOLS + UI_TOOLS


def test_full_agent_surface_gates_resize_on_capability() -> None:
    schemas = agent_tool_schemas("full_agent", readonly=False, resize_supported=False)
    assert "resize_pod" not in _names(schemas)
    assert "delete_resource" in _names(schemas)


def test_small_agent_surface_offers_two_ui_tools() -> None:
    from korvid.tools.executor import READ_TOOLS

    schemas = agent_tool_schemas("small_agent", readonly=True, resize_supported=False)
    assert _names(schemas) == [*_names(READ_TOOLS), "open_logs", "open_describe"]


def test_mcp_surface_is_read_plus_ui_drive() -> None:
    from korvid.tools.executor import READ_TOOLS, UI_TOOLS

    assert mcp_tool_schemas() == READ_TOOLS + UI_TOOLS


def test_unknown_surface_rejected() -> None:
    with pytest.raises(ValueError, match="unknown surface"):
        agent_tool_schemas("mcp", readonly=False, resize_supported=False)


# --- validation of malformed definitions --------------------------------


def _tool(name: str, **overrides: Any) -> ToolDef:
    fields: dict[str, Any] = {
        "name": name,
        "schema": {"type": "function", "function": {"name": name, "parameters": {}}},
        "effect": "cluster_read",
        "dispatch": "_handler",
        "surfaces": frozenset({"full_agent"}),
        "result_format": "untrusted_text",
    }
    fields.update(overrides)
    return ToolDef(**fields)


def test_validate_rejects_duplicate_names() -> None:
    with pytest.raises(ValueError, match="duplicate tool name"):
        validate_tool_defs([_tool("a"), _tool("a")])


def test_validate_rejects_schema_name_mismatch() -> None:
    bad = _tool("a", schema={"type": "function", "function": {"name": "b", "parameters": {}}})
    with pytest.raises(ValueError, match="schema name"):
        validate_tool_defs([bad])


def test_validate_rejects_missing_dispatch() -> None:
    with pytest.raises(ValueError, match="dispatch"):
        validate_tool_defs([_tool("a", dispatch="")])


def test_validate_rejects_unknown_result_format() -> None:
    bad = _tool("a", result_format="opaque")
    with pytest.raises(ValueError, match="result format"):
        validate_tool_defs([bad])


def test_validate_rejects_write_without_action() -> None:
    bad = _tool("a", effect="cluster_write", approval="user_confirmation")
    with pytest.raises(ValueError, match="write_action"):
        validate_tool_defs([bad])


def test_validate_rejects_unapproved_write() -> None:
    bad = _tool("a", effect="cluster_write", write_action="delete")
    with pytest.raises(ValueError, match="approval"):
        validate_tool_defs([bad])


def test_validate_rejects_write_action_on_non_write() -> None:
    bad = _tool("a", write_action="delete")
    with pytest.raises(ValueError, match="write_action"):
        validate_tool_defs([bad])


def test_validate_rejects_mcp_exposed_write() -> None:
    bad = _tool(
        "a",
        effect="cluster_write",
        approval="user_confirmation",
        write_action="delete",
        surfaces=frozenset({"full_agent", "mcp"}),
    )
    with pytest.raises(ValueError, match="mcp"):
        validate_tool_defs([bad])


def test_validate_dispatch_targets_rejects_unknown_handler() -> None:
    class _Empty:
        pass

    bad = _tool("a", dispatch="_missing_handler")
    with pytest.raises(ValueError, match="_missing_handler"):
        validate_dispatch_targets([bad], executor_cls=_Empty, bridge_cls=_Empty)


def test_validate_dispatch_targets_rejects_read_tool_naming_bridge_method() -> None:
    """Rule: a cluster read must dispatch to the executor — a bridge-only
    method name (here `agent_navigate`) must fail import-time validation,
    not surface later as a runtime AttributeError."""
    bad = _tool("a", dispatch="agent_navigate")
    with pytest.raises(ValueError, match="executor"):
        validate_dispatch_targets([bad], executor_cls=ToolExecutor, bridge_cls=UIBridge)


def test_validate_dispatch_targets_rejects_ui_tool_naming_executor_method() -> None:
    bad = _tool("a", effect="ui_only", dispatch="_list_resources")
    with pytest.raises(ValueError, match="bridge"):
        validate_dispatch_targets([bad], executor_cls=ToolExecutor, bridge_cls=UIBridge)


def test_validate_dispatch_targets_rejects_non_proposal_tool_on_proposal_entrypoint() -> None:
    """Reserved proposal entrypoints require effect == write_proposal: a
    ui_only tool routed at `agent_submit_write_proposal` would expose
    proposal submission on the ordinary MCP surface, skipping the separate
    proposal capability check keyed off PROPOSAL_TOOL_NAMES."""
    bad = _tool("a", effect="ui_only", dispatch="agent_submit_write_proposal")
    with pytest.raises(ValueError, match="proposal entrypoint"):
        validate_dispatch_targets([bad], executor_cls=ToolExecutor, bridge_cls=UIBridge)


def test_validate_dispatch_targets_rejects_write_tool_naming_executor_method() -> None:
    bad = _tool(
        "a",
        effect="cluster_write",
        approval="user_confirmation",
        write_action="delete",
        dispatch="_list_resources",
    )
    with pytest.raises(ValueError, match="agent_request_write"):
        validate_dispatch_targets([bad], executor_cls=ToolExecutor, bridge_cls=UIBridge)


# --- golden surface order (pre-registry literals) ------------------------

_READ_ORDER = [
    "list_resources",
    "get_resource",
    "get_logs",
    "get_events",
    "list_operators",
    "helm_list_releases",
    "diagnose_pod",
    "diagnose_workload",
]
_UI_ORDER = ["navigate", "set_filter", "open_logs", "open_describe", "drill_down"]
_WRITE_ORDER = ["delete_resource", "scale_resource", "rollout_restart"]


def test_full_agent_surface_matches_golden_order() -> None:
    """Byte-identical-order criterion pinned against literals, not against
    lists derived from the same registry (which would move together)."""
    schemas = agent_tool_schemas("full_agent", readonly=False, resize_supported=True)
    assert _names(schemas) == _READ_ORDER + _UI_ORDER + _WRITE_ORDER + ["resize_pod"]


def test_small_agent_surface_matches_golden_order() -> None:
    schemas = agent_tool_schemas("small_agent", readonly=True, resize_supported=False)
    assert _names(schemas) == [*_READ_ORDER, "open_logs", "open_describe"]


def test_mcp_surface_matches_golden_order() -> None:
    assert _names(mcp_tool_schemas()) == _READ_ORDER + _UI_ORDER


def test_validate_dispatch_targets_rejects_write_bypassing_approval_entrypoint() -> None:
    """Security invariant: every agent write routes through the
    approval-gated `agent_request_write` — any other bridge method, even a
    callable one, must be rejected at import time."""
    bad = _tool(
        "a",
        effect="cluster_write",
        approval="user_confirmation",
        write_action="delete",
        dispatch="agent_navigate",
    )
    with pytest.raises(ValueError, match="agent_request_write"):
        validate_dispatch_targets([bad], executor_cls=ToolExecutor, bridge_cls=UIBridge)


# --- schema isolation (issue #97, from the #91 revalidation) ------------


def _first_schema(schemas: list[dict[str, Any]]) -> dict[str, Any]:
    assert schemas, "surface derivation returned no schemas"
    return schemas[0]


def test_agent_surface_schemas_are_isolated_from_the_registry() -> None:
    """Providers are plugins: a provider mutating a schema it was handed
    must not corrupt the registry (the single source of tool metadata)."""
    exported = _first_schema(
        agent_tool_schemas("full_agent", readonly=False, resize_supported=True)
    )
    name = exported["function"]["name"]
    canonical = next(d.schema for d in TOOL_DEFS if d.name == name)
    exported["function"]["name"] = "tampered"
    exported["function"]["parameters"]["properties"]["injected"] = {"type": "string"}
    assert canonical["function"]["name"] == name
    assert "injected" not in canonical["function"]["parameters"]["properties"]


def test_agent_surface_schemas_are_fresh_per_call() -> None:
    first = _first_schema(agent_tool_schemas("full_agent", readonly=False, resize_supported=True))
    first["function"]["description"] = "tampered"
    second = _first_schema(agent_tool_schemas("full_agent", readonly=False, resize_supported=True))
    assert second["function"]["description"] != "tampered"


def test_mcp_surface_schemas_are_isolated_from_the_registry() -> None:
    exported = _first_schema(mcp_tool_schemas())
    name = exported["function"]["name"]
    canonical = next(d.schema for d in TOOL_DEFS if d.name == name)
    exported["function"]["name"] = "tampered"
    assert canonical["function"]["name"] == name


def _mutable_ids(obj: Any) -> set[int]:
    """ids of every mutable container reachable from *obj*: a shallow
    `.copy()` still shares the nested function/parameters dicts, and this
    test must catch that."""
    ids: set[int] = set()
    if isinstance(obj, dict):
        ids.add(id(obj))
        for value in obj.values():
            ids |= _mutable_ids(value)
    elif isinstance(obj, list):
        ids.add(id(obj))
        for item in obj:
            ids |= _mutable_ids(item)
    return ids


def test_executor_surface_lists_are_isolated_from_the_registry() -> None:
    """The module-level executor lists ride into AgentRuntime by default;
    their dicts must be deep copies: no mutable descendant may be shared
    with TOOL_DEFS."""
    from korvid.tools.executor import READ_TOOLS, RESIZE_TOOLS, UI_TOOLS, WRITE_TOOLS

    canonical_ids: set[int] = set()
    for d in TOOL_DEFS:
        canonical_ids |= _mutable_ids(d.schema)
    for surface in (READ_TOOLS, UI_TOOLS, WRITE_TOOLS, RESIZE_TOOLS):
        for schema in surface:
            assert _mutable_ids(schema).isdisjoint(canonical_ids)


# --- external write proposals (issue #110) -------------------------------


def test_proposal_tools_are_registered_with_the_proposal_effect() -> None:
    for name in ("propose_write", "get_write_proposal", "cancel_write_proposal"):
        tool = next(d for d in TOOL_DEFS if d.name == name)
        assert tool.effect == "write_proposal"
        assert tool.surfaces == frozenset({"mcp_proposal"})
        assert tool.approval == "none"
        assert tool.write_action is None


def test_mcp_surface_excludes_proposal_tools_by_default() -> None:
    names = _names(mcp_tool_schemas())
    assert "propose_write" not in names
    assert "get_write_proposal" not in names
    assert "cancel_write_proposal" not in names


def test_mcp_surface_offers_proposal_tools_only_when_enabled() -> None:
    names = _names(mcp_tool_schemas(write_proposals=True))
    assert "propose_write" in names
    assert "get_write_proposal" in names
    assert "cancel_write_proposal" in names
    # Direct write tools stay off the MCP surface even with proposals on.
    for direct in ("delete_resource", "scale_resource", "rollout_restart", "resize_pod"):
        assert direct not in names


def test_agent_surfaces_never_offer_proposal_tools() -> None:
    for surface in ("full_agent", "small_agent"):
        names = _names(agent_tool_schemas(surface, readonly=False, resize_supported=True))
        assert "propose_write" not in names


def test_validate_rejects_cluster_write_on_the_proposal_surface() -> None:
    bad = _tool(
        "a",
        effect="cluster_write",
        approval="user_confirmation",
        write_action="delete",
        dispatch="agent_request_write",
        surfaces=frozenset({"full_agent", "mcp_proposal"}),
    )
    with pytest.raises(ValueError, match="mcp"):
        validate_tool_defs([bad])


def test_validate_rejects_proposal_tool_outside_the_proposal_surface() -> None:
    bad = _tool(
        "a",
        effect="write_proposal",
        dispatch="agent_submit_write_proposal",
        surfaces=frozenset({"mcp"}),
    )
    with pytest.raises(ValueError, match="mcp_proposal"):
        validate_tool_defs([bad])


def test_validate_rejects_proposal_tool_with_write_action_or_approval() -> None:
    with pytest.raises(ValueError, match="write_action"):
        validate_tool_defs(
            [
                _tool(
                    "a",
                    effect="write_proposal",
                    dispatch="agent_submit_write_proposal",
                    surfaces=frozenset({"mcp_proposal"}),
                    write_action="delete",
                )
            ]
        )
    with pytest.raises(ValueError, match="approval"):
        validate_tool_defs(
            [
                _tool(
                    "a",
                    effect="write_proposal",
                    dispatch="agent_submit_write_proposal",
                    surfaces=frozenset({"mcp_proposal"}),
                    approval="user_confirmation",
                )
            ]
        )


def test_validate_dispatch_targets_rejects_proposal_tool_naming_the_write_entrypoint() -> None:
    # A proposal tool must never route into the direct write path: the
    # submit/status/cancel entrypoints are the only legal targets.
    bad = _tool(
        "a",
        effect="write_proposal",
        dispatch="agent_request_write",
        surfaces=frozenset({"mcp_proposal"}),
    )
    with pytest.raises(ValueError, match="proposal"):
        validate_dispatch_targets([bad], executor_cls=ToolExecutor, bridge_cls=UIBridge)


def test_proposal_tool_schemas_advertise_the_required_capability() -> None:
    """MCP hosts derive tool arguments from the advertised inputSchema; the
    capability token the server enforces must be discoverable there."""
    schemas = {s["function"]["name"]: s for s in mcp_tool_schemas(write_proposals=True)}
    for name in ("propose_write", "get_write_proposal", "cancel_write_proposal"):
        params = schemas[name]["function"]["parameters"]
        assert params["properties"]["capability"]["type"] == "string"
        assert "capability" in params["required"]


def test_propose_write_schema_encodes_action_specific_requirements() -> None:
    """MCP hosts generate arguments from the advertised schema; a schema
    that accepts calls the executor deterministically rejects (missing
    kind/replicas/resources, stray replicas on a delete) turns
    valid-by-schema calls into guaranteed errors. The per-action contract
    the executor enforces is mirrored as conditional schema branches."""
    schemas = {s["function"]["name"]: s for s in mcp_tool_schemas(write_proposals=True)}
    params = schemas["propose_write"]["function"]["parameters"]
    branches = {
        action: branch["then"]
        for branch in params["allOf"]
        for action in branch["if"]["properties"]["action"]["enum"]
    }
    assert set(branches) == {"delete", "scale", "rollout_restart", "resize"}
    # Every supported scale/restart/resize target is namespaced, so the
    # schema requires namespace there; delete may hit cluster-scoped kinds.
    assert "kind" in branches["delete"]["required"]
    assert "namespace" not in branches["delete"]["required"]
    assert set(branches["rollout_restart"]["required"]) == {"kind", "namespace"}
    for action in ("delete", "rollout_restart"):
        exclusions = branches[action]["not"]["anyOf"]
        assert {"required": ["replicas"]} in exclusions
        assert {"required": ["resources"]} in exclusions
    assert set(branches["scale"]["required"]) == {"kind", "replicas", "namespace"}
    assert branches["scale"]["not"] == {"required": ["resources"]}
    assert set(branches["resize"]["required"]) == {"resources", "namespace"}
    assert branches["resize"]["not"] == {"required": ["replicas"]}


# --- A custom tool has to say what its results are (round 10) -------------


def _schema(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": "d", "parameters": {}}}


def test_registry_tools_resolve_without_being_declared() -> None:
    resolved = registry_mod.resolve_result_formats([_schema("get_resource"), _schema("get_logs")])

    assert resolved == {"get_resource": "structured_yaml", "get_logs": "untrusted_text"}


def test_a_custom_tool_must_declare_its_result_format() -> None:
    """The boundary cannot guess, and guessing "text" let a custom tool
    return a `Secret` document that took only the text pass
    (PR #197 review)."""
    with pytest.raises(ValueError, match="result format"):
        registry_mod.resolve_result_formats([_schema("fetch_manifest")])


def test_a_declared_custom_tool_resolves() -> None:
    resolved = registry_mod.resolve_result_formats(
        [_schema("fetch_manifest")],
        [registry_mod.CustomToolResult("fetch_manifest", "structured_yaml")],
    )

    assert resolved == {"fetch_manifest": "structured_yaml"}


def test_a_declaration_for_a_tool_that_is_not_offered_is_rejected() -> None:
    with pytest.raises(ValueError, match="not offered"):
        registry_mod.resolve_result_formats(
            [_schema("get_logs")],
            [registry_mod.CustomToolResult("fetch_manifest", "untrusted_text")],
        )


def test_a_declaration_cannot_override_a_registry_tool() -> None:
    """Otherwise `get_resource` could be downgraded to the text pass by a
    caller, which is the exact hole this closes."""
    with pytest.raises(ValueError, match="registry"):
        registry_mod.resolve_result_formats(
            [_schema("get_resource")],
            [registry_mod.CustomToolResult("get_resource", "untrusted_text")],
        )


def test_a_duplicate_declaration_is_rejected() -> None:
    with pytest.raises(ValueError, match="more than once"):
        registry_mod.resolve_result_formats(
            [_schema("fetch_manifest")],
            [
                registry_mod.CustomToolResult("fetch_manifest", "structured_yaml"),
                registry_mod.CustomToolResult("fetch_manifest", "untrusted_text"),
            ],
        )


def test_an_invalid_result_format_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown result format"):
        registry_mod.resolve_result_formats(
            [_schema("fetch_manifest")],
            [registry_mod.CustomToolResult("fetch_manifest", "yaml")],  # type: ignore[arg-type]  # invalid on purpose
        )


def test_a_duplicate_tool_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="offered more than once"):
        registry_mod.resolve_result_formats([_schema("get_logs"), _schema("get_logs")])


def test_a_malformed_tool_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="tool schema"):
        registry_mod.resolve_result_formats([{"type": "function"}])


def test_a_non_function_tool_schema_is_rejected() -> None:
    schema = _schema("get_logs")
    schema["type"] = "image"

    with pytest.raises(ValueError, match="function schema"):
        registry_mod.resolve_result_formats([schema])


def test_an_unknown_tool_has_no_result_format() -> None:
    assert registry_mod.tool_result_format("fetch_manifest") is None
