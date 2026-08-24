from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"

VISUAL_MARKERS = {
    "keybindings.md": ("keybindings-context-map.svg",),
    "tui.md": ("cockpit-poster.png", 'class="docs-visual docs-visual--annotated"'),
    "ops.md": ("```mermaid", "Fresh user keystroke", "Audit append"),
    "resource-relationships.md": ("relationship-graph.png", "Resolution", "Coverage"),
    "helm-operators.md": ('class="docs-storyboard"', "Install", "Rollback"),
    "observability.md": ("```mermaid", "Prometheus", "Loki"),
    "agent.md": ("agent-poster.png", "deterministic AgentPanel walkthrough"),
    "mcp.md": ("```mermaid", "External MCP client", "tool-specific"),
    "airgap.md": ("```mermaid", "Internal"),
    "performance.md": ("Supported envelope", "Known limits", "Raw artifacts"),
    "threat-model.md": ("```mermaid", "Residual risks"),
}


def _source(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _table_rows(source: str) -> int:
    return sum(bool(re.match(r"^\|.*\|$", line)) for line in source.splitlines())


def test_redesigned_guides_keep_their_selected_visual_evidence() -> None:
    for page, markers in VISUAL_MARKERS.items():
        source = _source(page)
        for marker in markers:
            assert marker in source, f"{page} must retain {marker!r}"


def test_keybindings_is_a_compact_contextual_reference() -> None:
    source = _source("keybindings.md")
    assert _table_rows(source) <= 32
    assert "Action names:" not in source
    assert "approval dialogs' confirm keys are **not remappable**" in source
    assert source.count("```yaml") == 1


def test_core_guides_do_not_retain_catalog_scale_outlines() -> None:
    limits = {
        "tui.md": 8,
        "ops.md": 8,
        "resource-relationships.md": 9,
        "helm-operators.md": 7,
        "observability.md": 8,
        "agent.md": 9,
        "mcp.md": 7,
        "airgap.md": 7,
        "performance.md": 8,
        "threat-model.md": 8,
    }
    for page, maximum in limits.items():
        headings = re.findall(r"^#{2,3} ", _source(page), flags=re.MULTILINE)
        assert len(headings) <= maximum, f"{page} still has {len(headings)} subsections"


def test_safety_and_evidence_invariants_remain_explicit() -> None:
    ops = " ".join(_source("ops.md").split()).lower()
    assert "fresh user keystroke" in ops
    assert "audit" in ops
    assert "blocked" in ops
    assert "best-effort" in ops or "operation-specific" in ops

    agent = " ".join(_source("agent.md").split()).lower()
    assert "approval" in agent
    assert "write" in agent
    assert "provider" in agent
    assert "payload" in agent

    mcp = " ".join(_source("mcp.md").split()).lower()
    assert "tool-specific" in mcp
    assert "opt-in" in mcp
    assert "write proposal" in mcp


def test_redesign_does_not_add_a_script_bundle() -> None:
    source = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    block = source.split("extra_javascript:", 1)[1].split("\nextra_", 1)[0]
    scripts = [
        line.removeprefix("  - ").strip() for line in block.splitlines() if line.startswith("  - ")
    ]
    assert scripts == ["assets/javascripts/visual-storytelling.js"]
