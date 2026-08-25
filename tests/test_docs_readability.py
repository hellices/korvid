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
    "agent.md": ("agent-poster.png", "deterministic synthetic-cluster walkthrough"),
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
    assert "not a live-model quality claim" in agent_raw
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


def test_administration_guides_keep_their_empirical_and_contract_detail() -> None:
    """Administration pages may be shortened, but not stripped of evidence."""
    performance = _source("performance.md")
    for marker in ("1,000", "environment", "methodology", "Raw artifacts"):
        assert marker.lower() in performance.lower()

    threat = _source("threat-model.md")
    for marker in ("Assets", "Trust boundaries", "Mitigations", "Residual risks", "does not prove"):
        assert marker in threat

    provider = _source("provider-plugins.md")
    for heading in (
        "## API-v1: exact public surface",
        "## Event contract and exact limits",
        "## Options contract, immutability, and secret policy",
        "## Lifecycle and compatibility",
    ):
        assert heading in provider


def _slugify(heading: str) -> str:
    """Python-Markdown's default `toc` slugify, which MkDocs inherits."""
    value = re.sub(r"[^\w\s-]", "", heading).strip().lower()
    return re.sub(r"[-\s]+", "-", value)


def _heading_slugs(name: str) -> set[str]:
    return {
        _slugify(text)
        for text in re.findall(r"^#{1,6}\s+(.+?)\s*$", _source(name), flags=re.MULTILINE)
    }


def test_mcp_observability_reads_emit_an_activity_note_instead_of_navigating() -> None:
    """Finding 1: the prose polarity must match the code, the Mermaid node,
    and the tools table — external reads always surface as an activity note
    and never navigate (`korvid.mcp.server` routes `external_read` straight
    to `_note_read`)."""
    source = _source("mcp.md")
    flat = " ".join(source.split())
    assert "never emit that note" not in flat
    assert re.search(
        r"Prometheus and Loki reads[^.]*activity note[^.]*(?:instead of navigating|"
        r"rather than navigating)",
        flat,
    ), "mcp.md must say observability reads emit an activity note instead of navigating"
    # The diagram and the tools table already state it; they must stay.
    assert "Bounded observability tools<br/>activity note only" in source
    assert "Activity note only — never followable navigation" in source


def test_tui_scopes_request_relative_wording_to_the_ratio_columns() -> None:
    """Finding 2: only `%CPU/R`/`%MEM/R` are request-relative (`CPU`/`MEM`
    render absolute usage), and the no-limit fallback is the request ratio
    capped at yellow."""
    flat = " ".join(_source("tui.md").split())
    assert "`CPU`/`MEM` and `%CPU/R`/`%MEM/R` columns are always relative" not in flat
    assert re.search(r"`%CPU/R`\s*/\s*`%MEM/R`[^.]*relative to the declared request", flat), (
        "tui.md must scope the request-relative claim to the ratio columns"
    )
    assert "capped at yellow" in flat
    assert re.search(r"falls? back to the request ratio", flat), (
        "tui.md must restore the no-limit fallback to the request ratio"
    )
    assert "most severe **limit**" in flat


def _documented_remap() -> dict[str, str]:
    block = _source("keybindings.md").split("```yaml", 1)[1].split("```", 1)[0]
    pairs = re.findall(r"^\s{2}([a-z_]+):\s*(\S+)", block, flags=re.MULTILINE)
    return dict(pairs)


def test_keybindings_remap_example_uses_keys_no_default_already_owns() -> None:
    """Finding 3: the shipped example must be one the planner accepts —
    `ctrl+x` is `interrupt_agent`'s default and `g` is `relationships`'."""
    assert _documented_remap() == {"delete_resource": "ctrl+k", "sort_by_age": "z"}


def test_agent_offers_an_installable_entra_command_for_tool_users() -> None:
    """Finding 4: `uv sync --extra entra` only works in a source checkout."""
    source = _source("agent.md")
    version = re.search(
        r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.MULTILINE
    )
    assert version is not None
    assert f"uv tool install --force 'korvid[all,entra]=={version.group(1)}'" in source
    assert f"pipx install --force 'korvid[all,entra]=={version.group(1)}'" in source
    assert "uv sync --extra entra" in source


def test_agent_keeps_the_github_copilot_provider_warning() -> None:
    """Finding 5: Copilot support rides an unofficial internal API and needs
    an active subscription."""
    flat = " ".join(_source("agent.md").split()).lower()
    assert "unofficial" in flat
    assert "internal api" in flat
    assert "active github copilot subscription" in flat


def test_landing_and_plan_cross_page_anchors_resolve_to_real_headings() -> None:
    """Finding 6: raw-HTML landing anchors and the plan's guide link must
    name headings that still exist in tui.md."""
    tui_slugs = _heading_slugs("tui.md")
    landing = re.findall(r'href="tui/#([^"]+)"', _source("index.md"))
    assert landing, "index.md must keep its deep links into the TUI guide"
    for anchor in landing:
        assert anchor in tui_slugs, f"index.md links tui/#{anchor}, which tui.md has no heading for"
    assert {"work-with-logs", "follow-one-signal"} <= set(landing)

    plan = (DOCS / "dev" / "plans" / "2026-08-15-graph-derived-blast-radius.md").read_text(
        encoding="utf-8"
    )
    for anchor in re.findall(r"\(tui\.md#([^)]+)\)", plan):
        assert anchor in tui_slugs, (
            f"blast-radius plan links tui.md#{anchor}, which no longer exists"
        )


def test_helm_storyboard_inline_code_is_real_markup_not_literal_backticks() -> None:
    """Finding 7: markdown is not parsed inside this raw-HTML block, so
    backticks would ship verbatim to the reader."""
    source = _source("helm-operators.md")
    block = source.split('<section class="docs-storyboard"', 1)[1].split("</section>", 1)[0]
    assert "`" not in block, "helm storyboard must not rely on unparsed markdown backticks"
    assert "<code>helm search repo</code>" in block
    assert "<code>--dry-run</code>" in block


#: Narrowest realistic content column: Material renders the 360 px viewport
#: body at 360 px minus the .md-content padding either side.
_MOBILE_CONTENT_PX = 328.0
#: Below this the map's smallest label stops being readable on a handset.
_MIN_RENDERED_PX = 12.0


def _keymap_source() -> str:
    return (DOCS / "assets" / "keybindings-context-map.svg").read_text(encoding="utf-8")


def test_keybindings_context_map_is_stacked_for_a_legible_mobile_render() -> None:
    """Finding 8: at a 1200-unit viewBox the 20 px labels rendered at ~5 px
    on a handset. A narrow, stacked viewBox keeps every label readable."""
    source = _keymap_source()
    view_box = re.search(r'viewBox="0 0 (\d+(?:\.\d+)?) (\d+(?:\.\d+)?)"', source)
    assert view_box is not None
    width, height = float(view_box.group(1)), float(view_box.group(2))
    assert width <= 520, "the map must use a narrow viewBox to survive a phone-width column"
    assert height > width, "the map must stack its contexts rather than lay them out in a row"

    sizes = [float(px) for px in re.findall(r"font-size:\s*(\d+(?:\.\d+)?)px", source)]
    assert sizes
    rendered = min(sizes) * _MOBILE_CONTENT_PX / width
    assert rendered >= _MIN_RENDERED_PX, (
        f"smallest keymap label renders at {rendered:.1f}px in a {_MOBILE_CONTENT_PX:.0f}px column"
    )


def test_keybindings_context_map_keeps_its_accessible_name_and_every_label() -> None:
    source = _keymap_source()
    root = ET.parse(DOCS / "assets" / "keybindings-context-map.svg").getroot()
    assert root.get("aria-labelledby") == "keymap-title keymap-desc"
    assert root.get("role") == "img"
    texts = {(node.text or "").strip() for node in root.iter() if node.text}
    for label in ("GLOBAL", "TABLE", "LOGS", "WRITE", "Fresh approval keystroke"):
        assert label in texts, f"keymap lost the {label!r} label"
    for key in (":", "?", "/", "0", "Enter", "d", "g", "l", "f", "w", "p", "r", "S", "Ctrl-D"):
        assert key in texts, f"keymap lost the {key!r} key chip"
    assert '<title id="keymap-title">' in source
    assert '<desc id="keymap-desc">' in source


def test_keybindings_page_declares_the_maps_intrinsic_geometry() -> None:
    """A mismatched width/height reserves the wrong box and shifts layout."""
    source = _keymap_source()
    view_box = re.search(r'viewBox="0 0 (\d+(?:\.\d+)?) (\d+(?:\.\d+)?)"', source)
    assert view_box is not None
    page = _source("keybindings.md")
    tag = re.search(r"<img[^>]*keybindings-context-map\.svg[^>]*>", page)
    assert tag is not None
    assert f'width="{view_box.group(1)}"' in tag.group(0)
    assert f'height="{view_box.group(2)}"' in tag.group(0)


def test_agent_intro_is_not_duplicated_by_the_sections_below_it() -> None:
    """Finding 9: the lede restated the whole guide before the sections."""
    source = _source("agent.md")
    intro = source.split("<section class=", 1)[0]
    assert "Requires the `[agent]` extra" in intro
    assert "Press `Ctrl-A` to open the agent panel" in intro
    for duplicated in ("diagnose_pvc", "8,000 characters", "confirmation dialog"):
        assert duplicated not in intro, (
            f"intro repeats {duplicated!r}, already covered by a section"
        )
    assert len(" ".join(intro.split()).split()) <= 90


def test_redesign_does_not_add_a_script_bundle() -> None:
    source = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    block = source.split("extra_javascript:", 1)[1].split("\nextra_", 1)[0]
    scripts = [
        line.removeprefix("  - ").strip() for line in block.splitlines() if line.startswith("  - ")
    ]
    assert scripts == ["assets/javascripts/visual-storytelling.js"]
