"""Retired agent surfaces stay absent without loading optional transports."""

from __future__ import annotations

import importlib.util

import pytest


@pytest.mark.parametrize(
    "module",
    ["korvid.agent.provider_plugin", "korvid.providers.plugin_registry"],
)
def test_unwired_provider_construction_modules_are_absent(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_agent_package_has_no_symbol_reexport_facade() -> None:
    import korvid.agent

    assert "__getattr__" not in vars(korvid.agent)
    assert "__all__" not in vars(korvid.agent)


def test_unconsumed_screen_string_adapter_is_absent() -> None:
    from korvid.agent import outbound

    assert not hasattr(outbound, "sanitize_screen_context")


def test_removed_prompt_registries_have_no_startup_compatibility_hint() -> None:
    import korvid.__main__

    assert not hasattr(korvid.__main__, "_PROMPT_PACKAGING_HINT")
