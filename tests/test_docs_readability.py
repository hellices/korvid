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


def test_helm_preview_masking_keeps_the_old_helm_compatibility_caveat() -> None:
    """Video round 1, finding 1: `--hide-secret` is a helm 3.15+ flag.

    `HelmCLI._dry_run` re-renders *without* it on older helm purely to learn
    the render verdict, discards that unmasked output, and still raises
    `HelmPreviewUnsupported` — so `HelmController._preview` returns `None`
    and the confirmation opens marked "preview unavailable", while a real
    render error keeps stopping the flow before the dialog. Promising that
    every preview carries the flag would tell those users to expect a masked
    preview they never get.
    """
    flat = " ".join(_source("helm-operators.md").split())
    lowered = flat.lower()

    assert "--hide-secret" in flat, "the masking guarantee itself must survive this caveat"
    assert "never surfaced in a tool result" in flat, (
        "the raw-manifest guarantee must not be weakened by the compatibility note"
    )
    assert "always passed `--hide-secret`" not in flat, (
        "helm < 3.15 rejects the flag, so no preview can always be passed it"
    )
    assert "3.15" in flat, "the caveat must name the helm version that introduced the flag"
    assert "preview unavailable" in lowered, (
        "on older helm the confirmation opens without a preview; the page must say so"
    )
    assert "discard" in lowered, (
        "the unmasked fallback render is discarded rather than shown — the masking "
        "claim rests on exactly that"
    )
    assert re.search(r"still stops|still blocks|keeps stopping", lowered), (
        "issue #139's rule survives on old helm: a render failure the real command "
        "would hit must still stop the approval flow"
    )


def _representative_tool_rows() -> dict[str, str]:
    """The `Representative tools` table, mapping each family to its disclosure."""
    section = _source("mcp.md").split("## Representative tools", 1)[1].split("\n## ", 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if not re.match(r"^\|.*\|$", line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 4 or cells[0] in {"Family", "---"} or set(cells[0]) <= {"-"}:
            continue
        rows[f"{cells[0]} :: {cells[1]}"] = cells[3]
    return rows


def test_mcp_tools_table_separates_producer_side_redaction_from_shaping() -> None:
    """Video round 1, finding 2, corrected by round 4: the summary must not overclaim.

    Two Kubernetes reads are redacted where they are produced, by two
    different passes (`ToolExecutor._get_resource` → `_mask_manifest`, a
    recursive walk of the parsed document; `_diagnose_workload` →
    `redacted_and_compacted`, credential-*pattern* text masking of a shaped
    report). Round 1 gave them one shared row, which promised the
    structural pass on a report that never gets one. Logs, events, lists,
    single-pod diagnoses and Helm status get their own shaping and size caps
    only, and can carry credential-shaped text verbatim — which the prose
    above the table already discloses. A single "tool-specific redaction"
    cell covering every Kubernetes read contradicts it, and this table is the
    part a reader skims.
    """
    rows = _representative_tool_rows()
    assert rows, "mcp.md must keep its representative-tools table"

    redacted = [key for key, cell in rows.items() if "redact" in cell.lower()]
    assert len(redacted) == 2, f"the two producer-side passes need one row each; found {redacted}"
    by_tool = {
        tool: next(key for key in redacted if tool in key)
        for tool in ("get_resource", "diagnose_workload")
    }
    assert by_tool["get_resource"] != by_tool["diagnose_workload"], (
        "a structural document pass and a text-pattern pass cannot share a row"
    )
    for tool in ("get_logs", "list_resources", "diagnose_pod", "helm_list_releases"):
        for redacted_row in redacted:
            assert tool not in redacted_row, (
                f"{tool} is not credential-pattern masked; it must not sit in a redacted row"
            )

    shaped = [
        key
        for key, cell in rows.items()
        if "shaping" in cell.lower() and "redact" not in cell.lower()
    ]
    assert len(shaped) == 1, f"the shaped-only Kubernetes reads need their own row; found {shaped}"
    shaped_row, shaped_cell = shaped[0], rows[shaped[0]]
    for tool in ("list_resources", "get_logs", "diagnose_pod", "helm_list_releases"):
        assert tool in shaped_row, f"the shaping-only row must name {tool}"
    lowered_cell = shaped_cell.lower()
    assert "not" in lowered_cell, (
        f"the shaping-only row must deny the masking, not merely omit it: {shaped_cell!r}"
    )
    assert "mask" in lowered_cell, (
        f"the shaping-only row must name credential-pattern masking as what it lacks: "
        f"{shaped_cell!r}"
    )


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


def _ops_section(heading: str) -> str:
    """The flattened body of one `## ` section of `ops.md`."""
    body = _source("ops.md").split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
    return " ".join(body.split())


def test_ops_preview_fallthrough_excludes_a_real_helm_render_failure() -> None:
    """Video round 2, finding 1 (comment 3859012099).

    `HelmController._preview` turns a `HelmError` from `HelmCLI._dry_run`
    into `_HelmRenderFailure`, which stops the flow *before* the
    confirmation dialog — the mutation would hit the same render error
    (issue #139). Only advisory or unsupported previews fall through: a
    failed or timed-out SSAR check warns, and old helm rejecting the
    preview-only `--hide-secret` flag opens the dialog marked "preview
    unavailable". An absolute "none of these previews ever blocks
    approval" tells operators a doomed Helm command still reaches an
    approval dialog.
    """
    section = _ops_section("Operation-specific evidence")
    lowered = section.lower()

    assert "none of these previews ever blocks approval" not in lowered, (
        "a real helm render failure does block approval; the absolute claim is false"
    )
    assert "advisory" in lowered, (
        "the page must name the advisory class of preview that falls through"
    )
    assert re.search(r"ssar check.{0,120}(warns|falls through)", lowered), (
        "a failed or timed-out SSAR check must still be shown as warn-and-fall-through"
    )
    assert "helm" in lowered, "the exception must be attributed to the helm dry-run"
    assert re.search(r"(stops|blocks|stopping|halts).{0,60}before the confirmation", lowered), (
        "a helm render error the mutation would share must be described as stopping "
        "the flow before the confirmation dialog"
    )
    assert "preview unavailable" in lowered, (
        "an unsupported preview (old helm, no --hide-secret) is not a verdict: the "
        "dialog opens marked 'preview unavailable'"
    )
    assert "gated" in lowered, "the fall-through path must stay described as still gated"
    assert "audited" in lowered, "the fall-through path must stay described as still audited"


def test_ops_helm_render_exception_is_scoped_to_install_and_upgrade() -> None:
    """Round 2 follow-up: `HelmController._change_preview` is the only Helm
    preview whose render failure stops the flow before confirmation
    (install/upgrade — issue #139). `_rollback_preview` and
    `_uninstall_preview` both wrap their render in a bare `except Exception:
    return None`, so a broken `helm diff rollback` or `helm uninstall
    --dry-run` falls through to the (still gated, still audited) dialog
    exactly like an unsupported preview — it is advisory, not a verdict. The
    prior wording ("The Helm dry-run is the exception") swept every Helm
    write into the blocking claim, telling operators a broken rollback or
    uninstall preview would also stop them before approval, which the code
    does not do.
    """
    section = _ops_section("Operation-specific evidence")
    lowered = section.lower()

    assert "the helm dry-run is the exception" not in lowered.replace("*", ""), (
        "the blocking exception must be scoped to install/upgrade, not every helm dry-run"
    )

    blocking = re.search(r"([^.]*install[^.]*upgrade[^.]*\.)", lowered)
    assert blocking, "the blocking sentence must name install and upgrade explicitly"
    blocking_sentence = blocking.group(1)
    assert re.search(
        r"(stops|blocks|stopping|halts).{0,60}before the confirmation", blocking_sentence
    ), (
        "the install/upgrade sentence must say the render failure stops the flow "
        "before the confirmation dialog"
    )
    assert "rollback" not in blocking_sentence, (
        "rollback must not be swept into the install/upgrade blocking claim"
    )
    assert "uninstall" not in blocking_sentence, (
        "uninstall must not be swept into the install/upgrade blocking claim"
    )

    assert re.search(r"rollback.{0,120}(falls? through|advisory|without a preview)", lowered), (
        "a failed rollback preview must be described as falling through/advisory, not blocking"
    )
    assert re.search(r"uninstall.{0,120}(falls? through|advisory|without a preview)", lowered), (
        "a failed uninstall preview must be described as falling through/advisory, not blocking"
    )


def test_ops_debug_shell_lifecycles_are_not_conflated() -> None:
    """Video round 2, finding 2 (comment 3859012123).

    `ui/debug.py` injects an ephemeral container into the *existing* pod —
    Kubernetes has no API to remove that spec entry again, which is why the
    module warns that each retry "permanently adds another ephemeral
    container entry to the pod spec". Only `ui/shell.py`'s node path creates
    a separate `node-debugger-…` pod, whose uid is captured so cleanup can
    delete precisely that pod. Saying both "clean up their pod by UID" gives
    operators the wrong lifecycle expectation for the pod path.
    """
    section = _ops_section("Sessions that outlive the screen")
    lowered = section.lower()

    assert "clean up their pod by uid" not in lowered, (
        "the ephemeral-container path cleans up nothing; only the node debugger pod "
        "is deleted by uid"
    )
    assert "ephemeral" in lowered, "the pod path must stay named as an ephemeral container"
    assert re.search(r"(existing|same) pod", lowered), (
        "the ephemeral container is injected into the pod that is already running"
    )
    assert re.search(r"(cannot|can't|no api to) remove", lowered), (
        "Kubernetes cannot remove an ephemeral container entry from a pod spec; the "
        "page must say so rather than promise cleanup"
    )
    assert re.search(r"(replaced|recreated).{0,40}(or|and).{0,20}delet", lowered), (
        "the entry survives until the pod itself is replaced or deleted"
    )
    assert "node-debugger" in lowered, (
        "only the node path creates a separate node-debugger pod; name it"
    )
    assert re.search(r"(deleted|removed).{0,40}by uid", lowered), (
        "the node-debugger pod is the thing deleted by uid when the shell exits"
    )
    assert "approval" in lowered, "both debug paths stay approval-gated"
    assert "audited" in lowered, "both debug paths stay audited"


def test_readability_design_agent_limitation_matches_the_shipped_capture() -> None:
    """Video round 2, finding 3 (comment 3859012143).

    `docs/demo/agent_story.py` drives the real `AgentRuntime` over the real
    `ToolExecutor` (`diagnose_pod`, then `get_logs`) with a deterministic
    offline provider, and the real `EvidenceLedger` mints `[E1]`/`[E2]`. The
    design bullet must keep the live-provider/live-cluster/answer-quality
    limitation without claiming the clip lacks grounded tool calls.
    """
    spec = _source("superpowers/specs/2026-08-25-documentation-readability-design.md")
    flat = " ".join(spec.split())
    lowered = flat.lower()

    assert "not live provider execution or grounded tool calls" not in lowered, (
        "the capture does run grounded tool calls through the shipped runtime"
    )
    assert "not grounded" not in lowered, "no wording may deny the capture's grounding"
    for real_path in ("AgentRuntime", "ToolExecutor", "EvidenceLedger", "AgentPanel"):
        assert real_path in flat, f"the bullet must name {real_path} as a real code path"
    for marker in ("`[E1]`", "`[E2]`"):
        assert marker in flat, f"the ledger mints {marker}; the bullet must name it"
    assert re.search(r"(offline|deterministic) provider", lowered), (
        "the provider behind the capture is deterministic and offline"
    )
    assert "synthetic" in lowered, "the tools read a synthetic fixture, not a live cluster"
    assert re.search(r"live(-| )model|live provider", lowered), (
        "the live provider/model limitation must survive"
    )
    assert "live cluster" in lowered, "the live-cluster limitation must survive"
    assert "quality" in lowered, "the answer-quality limitation must survive"


def test_observability_token_validation_is_per_call_not_backend_disabling() -> None:
    """Video round 2, finding 4 (comment 3859012162).

    `obs/credentials.py::resolve_token` runs on every call (`obs/http.py::
    _headers`) and raises `ConnectorError("config")` for a control or
    non-ASCII byte — that refuses *that tool call*; the constructed backend
    is untouched, so a corrected or rotated token works on the next call
    without restarting korvid. Structurally invalid static config (both
    token sources, an inline secret, a TLS opt-out) is different: that
    disables the backend when the configuration is loaded.
    """
    section = " ".join(
        _source("observability.md")
        .split("\n## Credentials and TLS\n", 1)[1]
        .split("\n## ", 1)[0]
        .split()
    )
    lowered = section.lower()

    assert not re.search(r"(control|non-ascii).{0,120}backend to be disabled", lowered), (
        "an invalid token value fails the call, it does not disable the backend"
    )
    assert re.search(r"(refus|fail)\w*.{0,60}(that|the) call", lowered), (
        "the page must say the invalid value refuses that call"
    )
    assert "`config`" in section, "the per-call refusal surfaces as a `config` error"
    assert re.search(r"(control|non-ascii)", lowered), (
        "the validated header-value classes must stay named"
    )
    assert re.search(r"(correct|rotat)\w*.{0,120}without restarting", lowered), (
        "correcting or rotating the token must be described as restoring subsequent "
        "calls without a restart"
    )
    assert re.search(r"(both|inline).{0,160}disables the backend", lowered), (
        "structurally invalid static config still disables the backend; keep the "
        "distinction visible"
    )
    assert re.search(r"(when the configuration is loaded|at startup|at config load)", lowered), (
        "static-config disabling happens once, when the configuration is loaded"
    )
    assert "tls verification cannot be disabled" in lowered, (
        "the TLS invariant must survive this edit"
    )


def test_ops_approval_claim_matches_what_the_confirm_dialog_can_check() -> None:
    """Round-3 review: the approval sentence must not outrun the code.

    `ui/widgets/confirm_screen.py` compares each key event's `time` with
    the moment the dialog was constructed and discards anything older —
    input buffered while a pre-check ran can never answer a prompt the
    user has not seen yet. That is the whole mechanism. The dialog cannot
    tell a key repeat delivered *after* construction from a deliberate
    press, and nothing in korvid inspects whether an event came from a
    human or from OS-level input automation. Promising that approval is
    "never satisfied by a stale answer, a held key, or an automated
    replay" claimed all three checks; the page must instead state the
    timestamp/buffer rule it actually implements, keep the fresh-keystroke
    intent and the typed gates, and say plainly that no agent or MCP path
    has an approval API.
    """
    section = " ".join(
        _source("ops.md").split("\n## What approval proves\n", 1)[1].split("\n## ", 1)[0].split()
    )
    lowered = section.lower()

    for absolute in ("stale answer", "held key", "automated replay"):
        assert absolute not in lowered, (
            f"{absolute!r} claims a classification ConfirmScreen never makes; "
            "the page may only claim the construction-time comparison"
        )

    assert "fresh user keystroke" in lowered, "the fresh-approval intent must survive the rewrite"
    assert "timestamp" in lowered, "the page must name the mechanism: an event-timestamp comparison"
    assert "every key event" not in lowered, (
        "ConfirmScreen timestamps the plain y confirmation, not every decline or dismiss key"
    )
    assert re.search(r"(plain )?(<kbd>)?y(</kbd>)?.{0,80}confirm", lowered), (
        "the timestamp claim must be scoped to the plain y confirmation event"
    )
    assert re.search(r"buffered.{0,120}(discard|dropp|ignor)", lowered), (
        "the page must say input buffered before the dialog existed is discarded"
    )
    assert re.search(r"(cannot|never).{0,60}approve", lowered), (
        "the stale-input guarantee prevents approval; it does not classify every rejection key"
    )
    assert re.search(r"(after|once).{0,80}dialog.{0,80}(construct|open|exist)", lowered) or (
        re.search(r"dialog.{0,60}(was|is) (constructed|built|created)", lowered)
    ), "approval must be described as a confirm key delivered after dialog construction"
    assert re.search(r"(agent|mcp).{0,160}(no|never).{0,60}approval api", lowered) or re.search(
        r"(no|neither).{0,80}approval api", lowered
    ), "the page must keep saying no agent or MCP path can answer the dialog"
    assert "no tool path reaches the dialog" not in lowered, (
        "agent writes and MCP proposal review do open the shared dialog; tools only lack "
        "the ability to answer it"
    )
    assert re.search(r"agent.{0,80}tool.{0,80}(request|open).{0,80}dialog", lowered), (
        "the page must distinguish an agent opening the dialog from approving it"
    )
    assert re.search(r"mcp.{0,100}(queue|proposal).{0,100}:proposals", lowered), (
        "MCP tools only queue proposals; the user opens their dialog through :proposals"
    )
    assert re.search(r"no tool.{0,80}(answer|resolve|approve)", lowered), (
        "the invariant is that no tool can answer the dialog"
    )
    assert re.search(r"(key repeat|repeat|repeated key)", lowered), (
        "the page must disclaim post-construction key-repeat classification"
    )
    assert re.search(r"(os-level|operating-system|input automation)", lowered), (
        "the page must disclaim OS-level input automation detection"
    )
    assert "typed" in lowered, "the typed resource/context gates must stay described"


def _section(name: str, heading: str) -> str:
    """The body of one `## heading` section of `name`."""
    return _source(name).split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]


def _table_row(source: str, needle: str) -> str:
    """The single markdown table row of `source` containing `needle`."""
    rows = [line for line in source.splitlines() if line.startswith("|") and needle in line]
    assert len(rows) == 1, f"expected exactly one table row naming {needle!r}, found {len(rows)}"
    return rows[0]


def test_performance_live_event_to_render_is_diagnostic_not_a_budget_verdict() -> None:
    """Round-4 review, finding 1: a 24 ev/s no-op diff cannot miss a 20 ev/s budget.

    The budget is "event-to-render p95 <= 250 ms at 20 ev/s" over churn that
    changes a rendered cell. Run `i186`'s live driver is metadata-only, so
    its recorded interval ends at a table diff that writes nothing, and it
    ran at 24 ev/s — above the rate the budget names. A figure taken on a
    different workload at a higher rate is not a lower bound *at 20 ev/s*,
    so it can neither pass nor fail that contract. The empirical values and
    the render-path optimisation story stay; only the verdict changes.
    """
    live = _section("performance.md", "Live 1,000-pod results")
    # `i186`'s own baseline/optimised table and prose, not the later `i279` run's.
    i186 = live.split("**Run `i279", 1)[0]
    row = _table_row(i186, "527 ms")
    verdict = row.rstrip("|").rsplit("|", 1)[1].strip().strip("*").lower()
    flat = " ".join(i186.split())
    lowered = flat.lower()

    assert "miss" not in verdict, (
        f"{verdict!r} classifies a 20 ev/s rendered-cell budget from a 24 ev/s "
        "metadata-only no-op diff; the row can only be diagnostic"
    )
    assert re.search(r"diagnostic|unqualified|not measured", verdict), (
        f"the verdict cell must say the result is unqualified, not {verdict!r}"
    )

    for value in ("527 ms", "299 ms", "24 ev/s", "≤ 250 ms @ 20 ev/s"):
        assert value in flat, f"the empirical record must keep {value!r}"
    assert re.search(r"(neither pass\w* nor|cannot pass or fail|can neither pass nor)", lowered), (
        "the page must say this run can neither pass nor fail the qualification budget"
    )
    assert re.search(r"(rendered[- ]cell|writes? (a )?cell)", lowered), (
        "the budget is about churn that changes a rendered cell; say so"
    )
    assert re.search(r"(above|higher than|faster than).{0,80}20 ev/s", lowered), (
        "the rate mismatch — 24 ev/s measured against a 20 ev/s budget — must be stated"
    )
    assert not re.search(r"refut\w+", lowered), (
        "a figure from a different workload at a higher rate refutes nothing"
    )
    assert not re.search(r"\*{0,2}miss\*{0,2} verdict.{0,40}holds", lowered), (
        "the miss verdict does not hold; it was never a verdict this run could reach"
    )
    for kept in ("memoised", "get_row", "format_age", "phase_style"):
        assert kept in flat, f"the optimisation discussion must keep {kept!r}"


def test_mcp_separates_document_redaction_from_credential_pattern_masking() -> None:
    """Round-4 review, finding 2: two different passes, two different tools.

    `tools/registry.py` declares `get_resource` as `structured_yaml`, so
    `executor._get_resource` runs `_mask_manifest` — `redact_document`
    over the whole parsed manifest — before the document is bounded.
    `diagnose_workload` is `untrusted_text`: `_diagnose_deployment` shapes
    its own sections and applies `redact_text` (credential-*pattern*
    masking) to each of them, and to each embedded pod block *before*
    `compact_result` cuts it. Calling both "recursive redaction" promises
    the structural pass on a report that never gets one.
    """
    from korvid.tools.registry import tool_result_format

    assert tool_result_format("get_resource") == "structured_yaml"
    assert tool_result_format("diagnose_workload") == "untrusted_text"

    source = _source("mcp.md")
    bullets = _section("mcp.md", "Evidence crosses a tool boundary").split("\n- ")[1:]
    manifest = [b for b in bullets if "`get_resource`" in b]
    workload = [b for b in bullets if "`diagnose_workload`" in b]
    assert len(manifest) == 1, "`get_resource` needs its own bullet"
    assert len(workload) == 1, (
        "`diagnose_workload` needs its own bullet: it is disclosed by a different pass"
    )
    manifest_text = " ".join(manifest[0].split())
    workload_text = " ".join(workload[0].split())

    assert "`diagnose_workload`" not in manifest_text, (
        "the recursive-document bullet must not also claim `diagnose_workload`"
    )
    assert re.search(r"recursiv", manifest_text, re.I), (
        "`get_resource` is the structural pass: say it is recursively redacted"
    )
    assert re.search(r"last-applied-configuration", manifest_text), (
        "the structural pass's evidence (Secret data, last-applied annotation) must survive"
    )
    assert not re.search(r"recursiv", workload_text, re.I), (
        "`diagnose_workload` never gets recursive document redaction"
    )
    assert re.search(r"credential-pattern", workload_text, re.I), (
        "`diagnose_workload` gets credential-pattern text redaction; name the pass"
    )
    assert re.search(r"before.{0,80}(compact|bound|cut)", workload_text, re.I), (
        "the text pass runs before compaction — that ordering is the guarantee"
    )

    manifest_row = _table_row(source, "`get_resource`")
    workload_row = _table_row(source, "`diagnose_workload`")
    assert manifest_row != workload_row, "one table row cannot describe both passes"
    assert re.search(r"recursiv", manifest_row, re.I)
    # An explicit "not a recursive …" denial is the point; a positive claim is not.
    claimed = re.sub(r"not a recursive[^;,|]*", "", workload_row, flags=re.I)
    assert not re.search(r"recursiv", claimed, re.I), (
        "the compound-diagnosis row may deny recursion, never claim it"
    )
    assert re.search(r"credential-pattern", workload_row, re.I)

    shaped_row = _table_row(source, "`get_logs`")
    assert re.search(r"not\*{0,2} credential-pattern masked", shaped_row, re.I), (
        "shaped-only reads must stay explicitly not credential-pattern masked"
    )
    assert "`diagnose_pod`" in shaped_row or "`diagnose_pod`/" in shaped_row, (
        "single-pod diagnoses stay in the shaped-only family"
    )


def test_ops_audit_section_distinguishes_mutations_disclosures_and_reads() -> None:
    """Audit gates mutations and sensitive disclosure, not ordinary reads."""
    section = _section("ops.md", "What happens when audit fails")
    lowered = " ".join(section.split()).lower()

    assert "blocked before the mutation happens" in lowered, (
        "the blocked-before-mutation guarantee must survive"
    )
    assert "nothing reaches the cluster" not in lowered, (
        "that phrasing extends a write-path invariant over every read korvid makes"
    )
    assert re.search(r"no mutation reaches the cluster without.{0,40}audit record", lowered), (
        "the invariant must be stated over mutations"
    )
    assert re.search(r"read\w*.{0,120}(no|not|never).{0,40}audit", lowered), (
        "ordinary reads write no audit entry; say so rather than implying they do"
    )
    assert re.search(r"secret.{0,80}(reveal|copy).{0,80}fail-closed", lowered), (
        "Secret reveal/copy is a non-mutating but fail-closed-audited disclosure"
    )
    assert re.search(r"append failure.{0,80}(hidden|not shown|not copied)", lowered), (
        "a failed Secret disclosure audit must keep the value unavailable"
    )
    assert "port-forward" in lowered, "port-forward lifecycle events can be audited"
    assert "file downloads" in lowered, "file downloads can be audited"
    assert "not only mutations and not every read" in lowered, (
        "the summary must avoid both over-broad and over-narrow audit claims"
    )
    assert "fail-closed" in lowered, "the fail-closed name must survive"


def test_observability_bounds_table_publishes_the_enforced_concurrency_cap() -> None:
    """Round-4 review, finding 4: the concurrency bound is enforced, not absent.

    `obs/connector.py::QueryLimits.max_concurrency` defaults to 2 and
    `obs/http.py` holds an `asyncio.Semaphore` of that size *inside* the
    `asyncio.timeout(timeout_seconds)` block, so a queued call spends the
    same whole-call budget waiting for a slot. A bounds table that omits
    it under-reports what korvid enforces per backend.
    """
    from korvid.core.config import ObservabilityBackend
    from korvid.obs.connector import QueryLimits

    assert QueryLimits().max_concurrency == 2
    assert ObservabilityBackend(url="https://p.example.com").max_concurrency == 2

    section = _section("observability.md", "Bounds and masking")
    row = _table_row(section, "concurren")
    lowered = " ".join(section.split()).lower()

    assert re.search(r"\|\s*2\s*(per backend)?\s*\|", row), (
        f"the concurrency row must publish the enforced default of 2: {row!r}"
    )
    assert re.search(r"queue", row, re.I), "excess calls queue rather than being refused"
    assert re.search(r"(semaphore|slot)", row, re.I), "name the gate that queues them"
    assert re.search(r"(timeout|whole call|budget)", row, re.I), (
        "the wait for a slot is inside the whole-call timeout; the row must say so"
    )
    assert "max_concurrency" in section, "name the configurable key, as the other bounds do"

    for kept in ("time window", "response bytes", "request timeout", "60 min (max 360)"):
        assert kept in section, f"the existing bounds must survive: {kept!r}"
    assert "credential-shaped" in lowered, "the credential-shaped masking pass must survive"
    assert "`mask_labels`" in section, "the mask_labels pass must survive"


def test_airgap_names_both_the_pod_debug_and_node_shell_image_keys() -> None:
    """Round-6 review: two separate keys pull two separate images.

    `kubectl debug` on a *pod* attaches an ephemeral container built from
    `debug.default_image` (or one of `debug.images`), while `s` on a *node*
    creates the privileged `node-debugger-…` pod from `node_shell.image`.
    They are parsed from different config sections
    (`korvid.core.config`: `debug_default_image`/`debug_images` vs
    `node_shell_image`), so an air-gap checklist that lists only the
    `debug.*` keys leaves the node shell pulling korvid's built-in default
    from the public internet — the one image an operator reaches for when
    a node is already unreachable.
    """
    from korvid.core.config import KorvidConfig

    fields = set(KorvidConfig.__dataclass_fields__)
    assert {"debug_default_image", "debug_images", "node_shell_image"} <= fields, (
        "this test pins documentation against the real config keys; update both together"
    )

    source = _source("airgap.md")
    internalize = _section("airgap.md", "Internalize the remaining dependencies")
    lowered = " ".join(internalize.split()).lower()

    for key in ("`debug.default_image`", "`debug.images`", "`node_shell.image`"):
        assert key in internalize, f"the air-gap setup must name {key}"

    assert re.search(r"ephemeral", lowered), (
        "the pod path must be named for what it is: an ephemeral debug container"
    )
    assert re.search(r"`s` on a shell-less pod.{0,100}ephemeral", internalize, re.S | re.I), (
        "the pod debug image applies to the shell-less fallback reached through `s`"
    )
    assert re.search(r"node[ -]?shell.{0,200}`node_shell\.image`", internalize, re.S | re.I), (
        "the node shell's own image key must be attached to the node shell, not "
        "left to be inferred from the `debug.*` keys beside it"
    )
    assert re.search(
        r"`node_shell\.image`.{0,240}(internal|mirror|registry)", internalize, re.S | re.I
    ), "the node shell image must also be pointed at the internal registry"

    checklist = _section("airgap.md", "Readiness checklist (detect, don't assume)")
    assert "node_shell" in checklist, (
        "the detection checklist must inspect the node-shell image too, or an "
        "operator who runs it still cannot see the second image source"
    )
    assert "korvid never pulls" in source, "the existing ownership boundary must survive"


def test_tui_puts_the_status_row_where_the_poster_measured_it() -> None:
    """Round-7 review, finding 1: the status row sits at the *bottom*.

    `KorvidApp.compose` yields `TopBar()` first — and `TopBar`'s own CSS
    docks it `top` — while `StatusBar()` is yielded last, below a
    `height: 1fr` workspace. The annotated poster agrees and
    `tests/test_docs_build_config.py::test_tui_annotation_pins_match_the_poster_layout`
    pins it: the `ctx:… ns:…` row is at `--y: 97%`, the effective-key
    legend at `--y: 3%`. Calling the status row a top row contradicted both
    the code and the figure directly beneath the sentence.
    """
    source = _source("tui.md")
    flat = " ".join(source.split())
    assert "status row at the top" not in flat, (
        "tui.md must not place the status row at the top; the poster pins it at 97%"
    )
    assert re.search(r"status row[^.]*\bbottom\b", flat), (
        "tui.md must say the status row runs along the bottom of the screen"
    )
    assert re.search(r"status row[^.]*always\b", flat), (
        "the status row's always-on-screen guarantee must survive the correction"
    )
    # The `~`-collapsible legend is a separate, top-docked row; it must keep
    # its own accurate description rather than be folded into the status row.
    assert re.search(r"legend[^.]*\btop\b|\btop\b[^.]*legend", flat), (
        "tui.md must place the collapsible key legend at the top, where TopBar docks"
    )
    assert "collapsed by default" in flat
    assert "`~` expands the full grouped legend" in flat
    # Both pins the build-config test asserts must still be explained.
    assert "--x: 12%; --y: 97%;" in source
    assert "--x: 50%; --y: 3%;" in source


def test_tui_calls_the_watch_backed_row_a_live_not_static_snapshot() -> None:
    """Round-7 review, finding 2: a watch-backed row *is* a snapshot.

    Every table renders `ResourceStore`'s cached objects, kept current by
    watches. Saying it "is not a snapshot" overclaims: the row is a
    snapshot that watches keep live, and it is still distinct from the
    fresh reads `d`/`l`/agent tools issue against the API server.
    """
    flat = " ".join(_source("tui.md").split())
    assert "The selected row is not a snapshot" not in flat, (
        "a watch-backed row is a snapshot kept live, not the absence of one"
    )
    assert re.search(r"not a (?:static|frozen) snapshot", flat), (
        "tui.md must call it a live rather than a static snapshot"
    )
    assert "watch-backed" in flat, "the reason it stays live must survive"
    assert re.search(r"update[s]? live", flat)
    assert re.search(r"(?:fresh|own) read", flat), (
        "the distinction from a fresh describe/log/tool read must be stated, "
        "not left for the reader to infer"
    )


def test_tui_describes_g_as_direct_edges_and_d_as_the_bounded_expansion() -> None:
    """Round-7 review, finding 3: `g` opens *one hop, both directions*.

    `RelationshipScreen._render_table` always renders the root's direct
    dependencies and direct dependents, and only walks further when
    `self._expanded` — the flag `d` (`toggle_expand`) flips, defaulting to
    `False`. Describing `g`'s initial view as "dependents and
    dependents-of-dependents" both drops the dependencies pane and implies
    an automatic second hop that no keystroke asked for.
    """
    from korvid.core.relationships import GraphLimits

    flat = " ".join(_source("tui.md").split())
    assert "dependents-of-dependents" not in flat, (
        "`g` does not expand to a second dependents hop on its own; `d` does"
    )
    assert re.search(r"`g`[^.]*direct dependencies and[^.]*direct dependents", flat), (
        "`g`'s initial view is both direct panes, not the dependents side alone"
    )
    assert re.search(r"`d` toggles[^.]*bounded[^.]*expansion", flat), (
        "tui.md must attribute the bounded transitive expansion to `d`"
    )
    assert "coverage banner" in flat, "the coverage-completeness banner must survive"
    assert "resource-relationships.md" in flat, (
        "the caps belong on the relationships page; tui.md must link there"
    )

    # The caps stay documented where tui.md sends the reader, at the values
    # `GraphLimits` actually defaults to.
    limits = GraphLimits()
    relationships = " ".join(_source("resource-relationships.md").split())
    assert f"`max_depth = {limits.max_depth}`" in relationships
    assert f"`max_nodes = {limits.max_nodes}`" in relationships


def test_tui_states_the_multi_pod_log_stream_cap() -> None:
    """Round-7 review, finding 4: `L` streams the first 8 pods, not all.

    `LogController._build_multi_stream_triples` truncates the visible pod
    keys to `_MAX_MULTI_STREAM_PODS` and notifies "Streaming first N of M
    matching pods". Promising every visible pod's logs sets up a reader to
    read that notification as a bug.
    """
    from korvid.ui.log_controller import _MAX_MULTI_STREAM_PODS

    assert _MAX_MULTI_STREAM_PODS == 8, (
        "this test pins documentation against the real cap; update both together"
    )

    flat = " ".join(_source("tui.md").split())
    assert "`L` streams every currently visible pod" not in flat
    assert "`L` merges every currently filtered pod" not in flat
    assert re.search(rf"`L`[^.]*first {_MAX_MULTI_STREAM_PODS}", flat), (
        "tui.md must state the cap `L` actually applies"
    )
    assert re.search(
        rf"`L`[^.]*first {_MAX_MULTI_STREAM_PODS}[^.]*notif",
        flat,
        re.I,
    ), "the notification that names the cap and the match count must be mentioned"
    # The facts that were already right must survive the correction.
    assert "`[pod/container]`" in flat
    assert "bounded ring buffer of 5000" in flat
    assert "reconnect automatically" in flat
