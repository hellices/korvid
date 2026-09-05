"""Tests for the provider-free catalog vocabulary (design §Public Agent Boundary)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from korvid.agent.model_profiles import (
    AuthMethodDescriptor,
    EndpointRequirement,
    ModelCatalog,
    ModelEntry,
    ModelEntrySource,
    SetupField,
    SetupFieldKind,
    SpecialFlow,
    SpecialFlowRegistry,
    split_reference,
)


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("openai/gpt-4o", ("openai", "gpt-4o")),
        ("ollama/qwen3:8b", ("ollama", "qwen3:8b")),
        (
            "bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
            ("bedrock", "anthropic.claude-3-5-sonnet-20240620-v1:0"),
        ),
        ("openrouter/openai/gpt-4o", ("openrouter", "openai/gpt-4o")),
        ("gpt-4o", ("", "gpt-4o")),
    ],
)
def test_a_reference_splits_on_the_first_slash_only(
    reference: str, expected: tuple[str, str]
) -> None:
    assert split_reference(reference) == expected


def test_catalog_entries_default_every_capability_to_unknown() -> None:
    entry = ModelEntry(reference="x/y", provider_id="x", display_name="y")
    assert entry.context_window_tokens is None
    assert entry.supports_tools is None
    assert entry.supports_reasoning is None
    assert entry.source is ModelEntrySource.LITELLM


def test_the_vocabulary_is_immutable() -> None:
    field = SetupField(key="k", label="l", kind=SetupFieldKind.TEXT)
    with pytest.raises(AttributeError, match="cannot assign"):
        field.key = "other"  # type: ignore[misc]  # proving frozen-ness


def test_a_special_flow_declares_data_not_behaviour() -> None:
    flow = SpecialFlow(
        prefix="example-flow",
        display_name="Example",
        auth_methods=(AuthMethodDescriptor(id="device-login", display_name="Device login"),),
        endpoint=EndpointRequirement.OPTIONAL,
    )
    assert flow.claims_option is None
    assert not [
        name
        for name in vars(type(flow))
        if callable(getattr(flow, name, None)) and not name.startswith("__")
    ]


def test_special_flow_claim_normalizes_provider_prefix_separators() -> None:
    flow = SpecialFlow(prefix="github-copilot", display_name="GitHub Copilot", auth_methods=())
    registry = SpecialFlowRegistry((flow,))

    assert registry.claim("github-copilot/gpt-4o") is flow
    assert registry.claim("github_copilot/gpt-4o") is flow


def test_the_public_boundary_imports_no_provider_or_model_sdk() -> None:
    """`ui/` imports this module, and the base install has neither
    `korvid.providers` on its allowed-import list nor `litellm` on disk."""
    source = Path("src/korvid/agent/model_profiles.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
            if node.module.startswith("korvid."):
                imported.add(node.module)
    assert "litellm" not in imported
    assert not any(name.startswith("korvid.providers") for name in imported)


def test_the_catalog_contract_has_no_adapter_list() -> None:
    """A `descriptors()`-shaped method would reintroduce the compiled-in
    vendor table this design exists to remove."""
    names = {name for name in vars(ModelCatalog) if not name.startswith("_")}
    assert "descriptors" not in names
    assert {
        "search",
        "entry",
        "auth_methods",
        "option_fields",
        "endpoint_requirement",
        "discover",
        "test",
    } <= names
