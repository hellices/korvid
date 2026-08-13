"""The `external_read` effect and the two observability tools (issue #193).

An external read is not a cluster read: it leaves the cluster, it needs a
configured backend, and its results are a different kind of evidence.
These tests pin the policy dimensions that distinction implies.
"""

from __future__ import annotations

import pytest

from korvid.tools.executor import ToolExecutor, UIBridge
from korvid.tools.registry import (
    TOOL_DEFS,
    TOOLS_BY_NAME,
    ToolDef,
    agent_tool_schemas,
    mcp_tool_schemas,
    validate_dispatch_targets,
    validate_tool_defs,
)

OBSERVABILITY_TOOLS = ("query_metrics", "search_logs")


def _schema(name: str) -> dict[str, object]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


class TestTheEffectIsDeclared:
    @pytest.mark.parametrize("name", OBSERVABILITY_TOOLS)
    def test_the_tool_is_registered_as_an_external_read(self, name: str) -> None:
        assert TOOLS_BY_NAME[name].effect == "external_read"

    @pytest.mark.parametrize("name", OBSERVABILITY_TOOLS)
    def test_the_tool_carries_no_write_policy(self, name: str) -> None:
        definition = TOOLS_BY_NAME[name]
        assert definition.write_action is None
        assert definition.approval == "none"

    @pytest.mark.parametrize("name", OBSERVABILITY_TOOLS)
    def test_the_result_is_treated_as_untrusted_text(self, name: str) -> None:
        """Backend content is not korvid's text and never a korvid document."""
        assert TOOLS_BY_NAME[name].result_format == "untrusted_text"

    @pytest.mark.parametrize("name", OBSERVABILITY_TOOLS)
    def test_the_tool_dispatches_on_the_executor(self, name: str) -> None:
        assert callable(getattr(ToolExecutor, TOOLS_BY_NAME[name].dispatch, None))

    def test_an_external_read_routed_at_the_ui_bridge_is_rejected(self) -> None:
        """The executor owns external reads; a bridge method is not one."""
        bad = ToolDef(
            name="sneaky",
            schema=_schema("sneaky"),
            effect="external_read",
            dispatch="agent_navigate",
            surfaces=frozenset({"full_agent"}),
            result_format="untrusted_text",
        )
        with pytest.raises(ValueError, match="executor"):
            validate_dispatch_targets([bad], executor_cls=ToolExecutor, bridge_cls=UIBridge)

    def test_an_external_read_with_a_write_action_is_rejected(self) -> None:
        bad = ToolDef(
            name="sneaky",
            schema=_schema("sneaky"),
            effect="external_read",
            dispatch="_query_metrics",
            surfaces=frozenset({"full_agent"}),
            result_format="untrusted_text",
            write_action="delete",
        )
        with pytest.raises(ValueError, match="write_action"):
            validate_tool_defs([bad])

    def test_an_external_read_on_the_proposal_surface_is_rejected(self) -> None:
        bad = ToolDef(
            name="sneaky",
            schema=_schema("sneaky"),
            effect="external_read",
            dispatch="_query_metrics",
            surfaces=frozenset({"mcp_proposal"}),
            result_format="untrusted_text",
        )
        with pytest.raises(ValueError, match="mcp_proposal"):
            validate_tool_defs([bad])

    def test_every_external_read_declares_a_backend_capability(self) -> None:
        """Without one the tool would be offered on a cluster with no backend."""
        for definition in TOOL_DEFS:
            if definition.effect == "external_read":
                assert definition.capability in ("metrics_backend", "logs_backend")


class TestTheToolsAreGatedOnConfiguration:
    def test_no_backend_means_neither_tool_is_offered(self) -> None:
        names = _names(agent_tool_schemas("full_agent", readonly=True, resize_supported=False))
        assert "query_metrics" not in names
        assert "search_logs" not in names

    def test_a_metrics_backend_offers_only_the_metrics_tool(self) -> None:
        names = _names(
            agent_tool_schemas(
                "full_agent",
                readonly=True,
                resize_supported=False,
                observability_backends=frozenset({"metrics"}),
            )
        )
        assert "query_metrics" in names
        assert "search_logs" not in names

    def test_a_logs_backend_offers_only_the_logs_tool(self) -> None:
        names = _names(
            agent_tool_schemas(
                "full_agent",
                readonly=True,
                resize_supported=False,
                observability_backends=frozenset({"logs"}),
            )
        )
        assert "search_logs" in names
        assert "query_metrics" not in names

    def test_the_mcp_surface_is_gated_the_same_way(self) -> None:
        assert "query_metrics" not in _names(mcp_tool_schemas())
        assert "query_metrics" in _names(
            mcp_tool_schemas(observability_backends=frozenset({"metrics"}))
        )

    def test_an_unknown_backend_name_offers_nothing(self) -> None:
        names = _names(
            agent_tool_schemas(
                "full_agent",
                readonly=True,
                resize_supported=False,
                observability_backends=frozenset({"tracing"}),
            )
        )
        assert "query_metrics" not in names
        assert "search_logs" not in names

    def test_readonly_mode_still_offers_them(self) -> None:
        """They are reads: a read-only session is exactly where they belong."""
        names = _names(
            agent_tool_schemas(
                "full_agent",
                readonly=True,
                resize_supported=False,
                observability_backends=frozenset({"metrics", "logs"}),
            )
        )
        assert {"query_metrics", "search_logs"} <= set(names)


def _names(schemas: list[dict[str, object]]) -> list[str]:
    return [s["function"]["name"] for s in schemas]  # type: ignore[index]  # schema shape
