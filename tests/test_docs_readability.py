from __future__ import annotations

import re
import xml.etree.ElementTree as ET
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


def test_keybindings_context_map_is_well_formed_svg() -> None:
    root = ET.parse(DOCS / "assets" / "keybindings-context-map.svg").getroot()
    assert root.tag.endswith("svg")


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

    agent_raw = _source("agent.md")
    assert "not live provider execution or grounded tool calls" in agent_raw
    assert "fresh user keystroke" in agent_raw

    mcp = " ".join(_source("mcp.md").split()).lower()
    assert "tool-specific" in mcp
    assert "opt-in" in mcp
    assert "write proposal" in mcp

    mcp_raw = _source("mcp.md")
    assert "not necessarily the same snapshot" in mcp_raw
    assert "activity note" in mcp_raw
    assert "does not make the read followable" in mcp_raw

    relationships = " ".join(_source("resource-relationships.md").split()).lower()
    for term in ("direct", "bounded", "confidence", "unresolved", "incomplete"):
        assert term in relationships

    observability = " ".join(_source("observability.md").split()).lower()
    assert "watch-backed" in observability
    assert "independent read" in observability
    assert "mask" in observability


def test_task3_review_safety_caveats_are_preserved() -> None:
    """Focused assertions for Task 3 review findings."""
    rel = " ".join(_source("resource-relationships.md").split()).lower()
    # (1a) PDBs do not gate controller scale-down deletes
    assert "pdb" in rel or "poddisruptionbudget" in rel
    # (1b) HPA reconciliation can overwrite replicas
    assert "hpa" in rel or "horizontalpodautoscaler" in rel
    # (1c) StatefulSet PVC retention policy is not evaluated
    assert "pvc retention" in rel or "volumeclaimtemplates" in rel or "pvc" in rel
    # (2) max_target_lists default 32 in the compact limits table
    assert "max_target_lists" in rel
    assert "32" in rel

    helm = " ".join(_source("helm-operators.md").split()).lower()
    # (3) --hide-secret masking guarantee in dry-run
    assert "--hide-secret" in helm or "hide-secret" in helm

    obs = " ".join(_source("observability.md").split()).lower()
    # (4) token header-value validation/refusal for control/non-ASCII characters
    assert "control" in obs or "non-ascii" in obs


def test_redesign_does_not_add_a_script_bundle() -> None:
    source = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    block = source.split("extra_javascript:", 1)[1].split("\nextra_", 1)[0]
    scripts = [
        line.removeprefix("  - ").strip() for line in block.splitlines() if line.startswith("  - ")
    ]
    assert scripts == ["assets/javascripts/visual-storytelling.js"]
