"""Structural invariants for the landing page's design acceptance fixes (Task 4).

Rendered-site acceptance exposed three defects that are structural, not
cosmetic, so they are pinned here rather than left to a human re-reading a
screenshot:

1. **The install command wrapped mid-token on narrow viewports** — the hero
   rendered `brew install hellices/korvid/ko` / `rvid`, which is not a
   command anyone can copy. The fix restores wrapping to token boundaries
   only and lets the container scroll if a single token still does not fit,
   which means the container can be a scrollable region and therefore must
   be keyboard reachable and labelled.
2. **The footer was the bare Material default** ("Made with Material for
   MkDocs" and nothing else). The site now ships an original korvid footer
   partial that carries the mark, the tagline, the useful destinations, and
   the license — while keeping the upstream theme attribution.
3. **The hero was prose in an empty panel at wide widths** — 840px of copy
   inside a 1188px panel. The hero now pairs the copy with an original
   korvid cockpit panel (a terminal motif listing real korvid keys), so the
   panel is balanced by content rather than padding.

These tests read the *sources* (`docs/index.md`, `docs/overrides/**`,
`docs/stylesheets/extra.css`, `mkdocs.yml`) because that is what a future
edit would regress. They deliberately assert on structure and declared
behaviour, not on exact colours or pixel values.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
INDEX = DOCS / "index.md"
EXTRA_CSS = DOCS / "stylesheets" / "extra.css"
OVERRIDES = DOCS / "overrides"
COPYRIGHT_PARTIAL = OVERRIDES / "partials" / "copyright.html"
MARK = DOCS / "assets" / "korvid-mark.svg"
DEMO_README = DOCS / "demo" / "README.md"

MATERIAL_ATTRIBUTION = "https://squidfunk.github.io/mkdocs-material/"


def _index() -> str:
    return INDEX.read_text(encoding="utf-8")


def _css() -> str:
    return EXTRA_CSS.read_text(encoding="utf-8")


def _product_model_intro() -> str:
    """Return the prose between the product-model heading and the feature grid.

    Scoping shared-state assertions to this paragraph (rather than the whole
    of `docs/index.md`) matters because "evidence", "navigation", "approval",
    and "audit" all appear elsewhere on the page too (the safety section and
    the flight-path list). An unscoped search would keep passing even if a
    future edit dropped the shared-state claim from *this* intro specifically,
    as long as the words survived somewhere else on the page.
    """
    index = _index()
    heading = "## One operational experience. Three ways to drive it."
    start = index.index(heading)
    end = index.index('<div class="feature-grid">', start)
    return index[start:end]


def _strip_css_comments(css: str) -> str:
    """Remove `/* … */` comments so prose can never be read as a selector.

    The stylesheet documents each fix in a comment directly above the rule it
    explains, and those comments quote selectors and declarations verbatim. A
    naive text search would therefore match the *explanation* instead of the
    rule, and a test could pass on a stylesheet whose real declaration had
    been deleted.

    Args:
        css: Full stylesheet text.

    Returns:
        The same text with every comment replaced by a single space, so
        neighbouring tokens cannot be accidentally joined.
    """
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


def _rule(css: str, selector: str) -> str:
    """Return the declaration block of the first rule whose selector list matches.

    Args:
        css: Full stylesheet text.
        selector: A selector substring that must appear in the rule's prelude.

    Returns:
        The text between `{` and the matching `}` for that rule.
    """
    stripped = _strip_css_comments(css)
    index = stripped.index(selector)
    open_brace = stripped.index("{", index)
    close_brace = stripped.index("}", open_brace)
    return stripped[open_brace + 1 : close_brace]


# --- 1. the install command never breaks mid-token ---------------------------


def test_install_command_is_a_labelled_keyboard_reachable_scroll_region() -> None:
    """The hero's install command must be focusable and named, not a bare div.

    Keeping the command on one line means it can overflow its container on a
    390px viewport. A scrollable region that only a mouse or a touch drag can
    reach fails WCAG 2.1.1, so the container carries `tabindex="0"` and an
    accessible name.
    """
    index = _index()
    match = re.search(r"<div class=\"install-command\"[^>]*>", index)
    assert match is not None, "hero must still render a .install-command container"
    opening_tag = match.group(0)
    assert 'tabindex="0"' in opening_tag, (
        "the install command scrolls horizontally on narrow viewports, so it must be "
        'keyboard reachable (tabindex="0")'
    )
    assert "aria-label=" in opening_tag or "aria-labelledby=" in opening_tag, (
        "a focusable scroll region needs an accessible name"
    )


def test_install_command_never_breaks_inside_a_token() -> None:
    """Wrapping is allowed at spaces, forbidden inside `hellices/korvid/korvid`.

    Material's `.md-typeset code` ships `word-break: break-word`, which is
    what produced `hellices/korvid/ko` + `rvid` on a 390px viewport. Both
    `word-break` and `overflow-wrap` must be restated as `normal` so a path
    can never be split; the container then scrolls if a single token still
    does not fit.
    """
    css = _css()
    block = _rule(css, ".install-command code")
    assert "word-break: normal" in block, (
        "the install command must not break inside a token: "
        "`hellices/korvid/ko` + `rvid` is not a copyable command"
    )
    assert "overflow-wrap: normal" in block, (
        "overflow-wrap must also be restated, or Material's break-word wins"
    )
    container = _rule(css, ".md-typeset .hero .install-command {")
    assert "overflow-x: auto" in container, (
        "an unbreakable token wider than the hero must scroll, not overflow it"
    )
    assert "max-width: 100%" in container, "the container must never exceed the hero's width"


def test_install_command_scroll_region_has_a_visible_focus_ring() -> None:
    """Focus visibility is the other half of making the region keyboard reachable."""
    css = _css()
    assert ".install-command:focus-visible" in css, (
        "the focusable install command needs a visible focus indicator"
    )


# --- 2. the footer is korvid's, and still credits the theme ------------------


def test_footer_is_overridden_with_an_original_korvid_partial() -> None:
    """A `partials/copyright.html` override replaces the bare Material default."""
    assert COPYRIGHT_PARTIAL.is_file(), (
        "docs/overrides/partials/copyright.html must exist so the footer is korvid's, "
        "not the theme's unfinished default"
    )


def test_footer_carries_the_mark_the_tagline_and_the_license() -> None:
    """The footer must identify the project rather than only the generator."""
    footer = COPYRIGHT_PARTIAL.read_text(encoding="utf-8")
    assert "korvid-mark.svg" in footer, "the footer should carry the original korvid mark"
    assert "korvid" in footer
    assert "Apache-2.0" in footer, "the footer must state the project's license"


def test_footer_links_are_built_from_mkdocs_urls_not_hardcoded_paths() -> None:
    """Internal footer links must go through MkDocs' `url` filter.

    The site is served from a subpath (`/korvid/`), so a hardcoded
    `/getting-started/` would 404 on the published site while working
    locally.
    """
    footer = COPYRIGHT_PARTIAL.read_text(encoding="utf-8")
    internal_hrefs = re.findall(r'href="([^"]+)"', footer)
    assert internal_hrefs, "the footer must offer links"
    for href in internal_hrefs:
        # Absolute destinations — literal `https://…` or MkDocs config values
        # like `{{ config.repo_url }}` — are already fully qualified.
        if href.startswith("http") or "config." in href:
            continue
        assert "| url }}" in href, (
            f"internal footer link {href!r} must be resolved through MkDocs' `url` "
            "filter so it survives the site's /korvid/ subpath"
        )


def test_footer_keeps_the_material_for_mkdocs_attribution() -> None:
    """Replacing the default footer must not drop the upstream theme credit."""
    footer = COPYRIGHT_PARTIAL.read_text(encoding="utf-8")
    assert MATERIAL_ATTRIBUTION in footer
    assert "Material for MkDocs" in footer


def test_footer_link_targets_are_real_documentation_pages() -> None:
    """Every internal footer destination must map to a page MkDocs actually builds."""
    footer = COPYRIGHT_PARTIAL.read_text(encoding="utf-8")
    slugs = re.findall(r"\{\{ '([a-z0-9\-/]+)/' \| url \}\}", footer)
    assert slugs, "the footer must offer at least one internal destination"
    for slug in slugs:
        assert (DOCS / f"{slug}.md").is_file(), (
            f"footer links to /{slug}/ but docs/{slug}.md does not exist"
        )


# --- 3. the hero is balanced by content at wide widths -----------------------


def test_hero_pairs_the_copy_with_an_original_cockpit_panel() -> None:
    """The hero must contain a second column, not 348px of empty charcoal."""
    index = _index()
    assert 'class="hero-copy-column"' in index, (
        "the hero's prose must live in its own column so a second column can balance it"
    )
    assert 'class="hero-panel"' in index, (
        "the hero needs an original korvid panel to balance the copy at wide widths"
    )


def test_hero_panel_is_a_keyboard_legend_of_real_korvid_keys() -> None:
    """The balancing panel must carry real product content, not filler shapes."""
    index = _index()
    panel_start = index.index('class="hero-panel"')
    panel = index[panel_start : index.index("</aside>", panel_start)]
    for key in ("<kbd>:</kbd>", "<kbd>/</kbd>", "<kbd>d</kbd>", "<kbd>l</kbd>"):
        assert key in panel, f"the hero panel must document the real korvid key {key}"
    keybindings = (DOCS / "keybindings.md").read_text(encoding="utf-8")
    assert "`Ctrl-A`" in keybindings, "keybindings.md must still document Ctrl-A"
    assert "Ctrl-A" in panel, "the hero panel must stay in sync with keybindings.md"


def test_hero_panel_has_an_accessible_name_and_hidden_decoration() -> None:
    """Screen readers get the legend; the terminal chrome is decorative only."""
    index = _index()
    panel_start = index.index("<aside")
    panel = index[panel_start : index.index("</aside>", panel_start)]
    assert "aria-label=" in panel or "aria-labelledby=" in panel, (
        "the hero panel is a landmark-ish region and needs an accessible name"
    )
    assert 'aria-hidden="true"' in panel, (
        "the panel's terminal chrome (prompt glyph, window dots) is decoration and "
        "must be hidden from assistive technology"
    )


def test_hero_becomes_two_columns_only_at_wide_widths() -> None:
    """Narrow viewports must keep the single-column stack that already works."""
    css = _css()
    match = re.search(
        r"@media \(min-width: (\d+)px\) \{\s*\.md-typeset \.hero \{",
        css,
    )
    assert match is not None, "the hero's two-column layout must be behind a min-width query"
    assert int(match.group(1)) >= 960, (
        "the hero must stay single-column on phones and small tablets; "
        f"found a {match.group(1)}px breakpoint"
    )


def test_hero_panel_is_not_a_second_copy_of_the_demo_asset() -> None:
    """The panel is CSS/HTML, so it must not re-embed the demo GIF."""
    index = _index()
    panel_start = index.index("<aside")
    panel = index[panel_start : index.index("</aside>", panel_start)]
    assert "demo.gif" not in panel
    assert "<img" not in panel or "korvid-mark.svg" in panel


def test_hero_key_note_preserves_fixed_confirmation_keys() -> None:
    """Only app-level actions are remappable; approval confirmation stays fixed."""
    index = _index()
    panel_start = index.index("<aside")
    panel = index[panel_start : index.index("</aside>", panel_start)]
    note_start = panel.index('class="hero-panel__note"')
    note = panel[note_start : panel.index("</p>", note_start)]
    lowered = note.lower()
    assert "app-level actions are remappable" in lowered
    assert "confirmation keys stay fixed" in lowered
    assert "always shows the effective set" in lowered
    assert "every key is remappable" not in lowered


def test_product_demo_has_native_motion_controls() -> None:
    """A demo longer than five seconds must be pausable without custom JavaScript."""
    index = _index()
    start = index.index('<figure class="product-demo">')
    demo = index[start : index.index("</figure>", start)]
    assert "<video" in demo, "the moving demo must use a controllable media element"
    assert 'src="assets/demo.mp4"' in demo
    for attribute in ("controls", "autoplay", "muted", "loop", "playsinline"):
        assert re.search(rf"<video\b[^>]*\b{attribute}\b", demo), (
            f"the demo video must declare {attribute!r}"
        )
    assert "demo.gif" not in demo, "an autoplaying GIF has no pause or stop control"


def test_demo_regeneration_updates_both_readme_and_site_formats() -> None:
    """One canonical recording must refresh the README GIF and site MP4 together."""
    instructions = DEMO_README.read_text(encoding="utf-8")
    assert "vhs docs/demo/demo.tape" in instructions
    assert "ffmpeg" in instructions
    assert "docs/assets/demo.gif" in instructions
    assert "docs/assets/demo.mp4" in instructions
    assert "-movflags +faststart" in instructions
    assert "yuv420p" in instructions


# --- the whole page stays original, asset-light, and script-free -------------


def test_landing_customizations_add_no_scripts_or_remote_assets() -> None:
    """Korvid's own landing sources add no scripts or remote CSS assets.

    Material itself emits JavaScript and can integrate Mermaid. The actual
    browser-runtime invariant is pinned in `test_docs_build_config.py` through
    `theme.font: false` and Material's privacy plugin, then verified against
    the built site. This source check is intentionally limited to korvid's
    customizations.
    """
    sources = [
        _index(),
        _css(),
        COPYRIGHT_PARTIAL.read_text(encoding="utf-8"),
        (OVERRIDES / "home.html").read_text(encoding="utf-8"),
    ]
    for source in sources:
        assert "<script" not in source
        assert "onclick=" not in source
    css = _css()
    external_urls = re.findall(r"url\((['\"]?)(https?:)?//", css)
    assert not external_urls, "extra.css must not pull in remote assets"


def test_local_assets_referenced_by_the_landing_page_exist() -> None:
    """No broken image: every locally referenced asset is checked into the repo."""
    referenced = set(re.findall(r"assets/([A-Za-z0-9._\-]+)", _index()))
    referenced |= set(
        re.findall(r"assets/([A-Za-z0-9._\-]+)", COPYRIGHT_PARTIAL.read_text(encoding="utf-8"))
    )
    assert referenced, "the landing page should reference at least one local asset"
    for name in referenced:
        assert (DOCS / "assets" / name).is_file(), f"docs/assets/{name} is referenced but missing"
    assert MARK.is_file()


def test_reduced_motion_is_respected_for_every_animated_landing_element() -> None:
    """Anything that transitions or transforms must be neutralised under reduce."""
    css = _css()
    reduce_start = css.index("@media (prefers-reduced-motion: reduce)")
    reduce_block = css[reduce_start:]
    assert "transition: none" in reduce_block
    assert "transform: none" in reduce_block


# --- the rule helper must read rules, never the prose that explains them -----


def test_rule_helper_ignores_selectors_that_only_appear_in_a_comment() -> None:
    """A commented-out or merely *described* rule must not satisfy a rule assertion.

    `extra.css` explains every fix in a comment above the rule it applies, and
    those comments quote selectors and declarations verbatim. Without comment
    stripping, `_rule` could return the first *comment* that mentions a
    selector, so a stylesheet that had lost the real declaration would still
    pass.
    """
    css = """
    /* .fake-selector { word-break: break-word; } explains the bug */
    .fake-selector {
      word-break: normal;
    }
    """
    block = _rule(css, ".fake-selector")
    assert "word-break: normal" in block, "the helper must return the real rule's block"
    assert "break-word" not in block, "the helper must not return a comment's contents"


# --- 4. one operational experience, three surfaces that drive it ------------


def test_landing_frames_one_experience_rather_than_three_separate_products() -> None:
    """The product model section must not read as three unrelated entry points.

    Korvid is a single agentic Kubernetes UI. A human operator, the embedded
    agent, and an external MCP-connected assistant are *actors* driving one
    operational experience, not three products bolted together, so the
    heading must say so.
    """
    index = _index()
    assert "## One cockpit. Three ways in." not in index, (
        "'three ways in' reads as three doors into three things; the site sells "
        "one operational experience with three ways to drive it"
    )
    assert "## One operational experience. Three ways to drive it." in index, (
        "the product-model heading must name the single experience the three surfaces share"
    )


def test_landing_names_the_boundaries_the_three_surfaces_actually_share() -> None:
    """Distinguish shared boundaries from independently timed cluster reads."""
    intro = _product_model_intro()
    lowered = " ".join(intro.lower().replace("*", "").split())
    assert "different surfaces. shared context and safety." in lowered
    for shared in ("active cluster context", "navigation", "approval", "audit"):
        assert shared in lowered, f"the product-model intro must name {shared!r}"
    assert "watch-backed" in lowered
    assert "fresh reads" in lowered
    assert "snapshots can differ" in lowered
    for overclaim in ("one operational state", "one resource cache", "same evidence"):
        assert overclaim not in lowered


def test_feature_cards_are_ways_to_drive_korvid_not_three_feature_silos() -> None:
    """Each card must describe an actor driving the shared experience."""
    index = _index()
    grid_start = index.index('<div class="feature-grid">')
    grid = index[grid_start : index.index("</div>", grid_start)]
    headings = re.findall(r"<h3>(.*?)</h3>", grid)
    assert len(headings) == 3, "the product model still has exactly three surfaces"
    joined = " ".join(headings).lower()
    assert "yourself" in joined or "you drive" in joined or "direct" in joined, (
        "the first card is the human operator driving korvid directly"
    )
    assert "delegate" in joined, "the second card is delegation to the embedded agent"
    assert "mcp" in joined, "the third card is connecting an external assistant over MCP"
    body = grid.lower()
    assert "active context" in body
    assert "safety" in body
    assert "same evidence" not in body
    assert "when enabled" in body or "opt-in" in body, (
        "the MCP card must not imply the optional MCP server is always enabled"
    )


def test_safety_section_converges_every_actor_on_one_write_path() -> None:
    """Whoever initiates a write, the gate and the audit path are the same one."""
    index = _index()
    section_start = index.index("## Sharp tools. Human hands.")
    section = index[section_start : index.index("[Read the safety model]", section_start)]
    lowered = section.lower()
    assert "agent" in lowered, (
        "the safety paragraph must name the actors it is making a claim about"
    )
    assert "mcp" in lowered, "the safety paragraph must name the actors it is making a claim about"
    assert "same" in lowered, (
        "the point of the paragraph is convergence: one confirmation path, one "
        "audit path, regardless of which actor initiated the operation"
    )
    assert "confirmation" in lowered or "confirm" in lowered
    assert "fail-closed" in lowered, "the audit path must still be described as fail-closed"
    assert "proposal" in lowered, (
        "MCP writes are proposals, never executed writes — the page must stay "
        "factually precise about that"
    )
    assert "when mcp is enabled" in lowered or "opt-in" in lowered, (
        "the landing page must not imply the optional MCP server is always enabled"
    )
    assert "embedded-agent provider" in lowered
    assert "mask" in lowered, "credential-pattern masking applies to embedded provider calls"
    assert "mcp" in lowered
    assert "disclosure" in lowered, "MCP results follow a separate per-tool disclosure boundary"
    assert "secret values are masked before model calls" not in lowered, (
        "that unqualified claim incorrectly includes MCP client models"
    )


def test_plan_preserves_the_mcp_disclosure_boundary() -> None:
    plan = (
        ROOT / "docs" / "superpowers" / "plans" / "2026-08-21-official-documentation-site.md"
    ).read_text(encoding="utf-8")
    lowered = plan.lower()
    assert "embedded-agent provider" in lowered
    assert "mask" in lowered
    assert "mcp" in lowered
    assert "disclosure" in lowered
    assert "secret values are masked before model calls" not in lowered


def test_landing_never_claims_the_surfaces_look_the_same() -> None:
    """Shared state is a true claim; identical screens is not.

    An MCP client renders korvid's data in the editor's own chat UI. Claiming
    the surfaces are identical would be false, so the page must not say it.

    Banning the bare word "identical" would also forbid the page from ever
    stating the *true* negation ("...without implying identical screens or
    interfaces"), which is exactly the disclaimer this design is built
    around (see the design doc's own phrasing). So only the overclaim
    itself — "same"/"identical" bound to screen(s)/UI/interface(s) — is
    banned, and a negation cue immediately before the match (e.g. "not",
    "never", "no", "without implying") makes it an allowed truthful
    negation rather than a violation.
    """
    lowered = _index().lower()
    negation_cues = ("not ", "never ", "no ", "without implying ", "isn't ", "aren't ")
    overclaim = re.compile(r"(same|identical)\s+(screens?|uis?|interfaces?)")
    violations: list[str] = []
    for match in overclaim.finditer(lowered):
        preceding = lowered[max(0, match.start() - 40) : match.start()]
        if any(preceding.endswith(cue) for cue in negation_cues):
            continue
        violations.append(match.group(0))
    assert not violations, (
        "the three actors share context and safety boundaries, not literal "
        f"screens; non-negated overclaims: {violations}"
    )


def test_hero_terminal_motif_reinforces_the_three_actors() -> None:
    """The cockpit motif carries the convergence idea with no new markup weight."""
    index = _index()
    panel_start = index.index('class="hero-panel"')
    aside_close = index.index("</aside>", panel_start)
    panel = index[panel_start:aside_close]

    bar_start = panel.index('class="hero-panel__bar"')
    # Search for the closing tag *from* bar_start, not from the start of the
    # panel: an unanchored `panel.index("</div>")` happens to find the right
    # tag today only because no earlier `<div>` exists before the bar, which
    # is a coincidence of the current markup rather than something this test
    # should rely on.
    bar_close = panel.index("</div>", bar_start)
    bar = panel[bar_start:bar_close]

    lowered = bar.lower()
    # Word-boundary, in-order matching: a bare substring check for "you"
    # would also match inside "your" (or "youtube", etc.), so it could pass
    # even if the actual actor token were replaced by something else that
    # merely contains the substring. Assert the literal, ordered sequence of
    # actor tokens instead.
    actor_sequence = re.compile(r"\byou\b.*\bagent\b.*\bmcp\b", re.DOTALL)
    assert actor_sequence.search(lowered), (
        "the terminal status line must name all three actors, in order, as "
        "whole words — 'you', then 'agent', then 'mcp' — reinforcing that "
        "all three actors drive one session"
    )
    assert "<img" not in bar, "the reinforcement must stay text-in-CSS-chrome, not a new asset"
    assert "svg" not in lowered, "the reinforcement must stay text-in-CSS-chrome, not a new asset"


# --- 5. every declared value must actually take effect ----------------------


def test_stylesheet_declares_no_inert_content_width_abstraction() -> None:
    """A custom property that never binds is worse than no abstraction at all.

    `--korvid-content-width: 72rem` resolved to 1440px (Material sets the root
    font size to 125%), while Material caps `.md-content__inner` at
    `61rem` = 1220px. Every `max-width: var(--korvid-content-width)` was
    therefore dead weight: editing it changed nothing until it dropped below
    the theme's own cap, so the stylesheet documented a landing width the site
    did not have.
    """
    css = _css()
    assert "--korvid-content-width" not in css, (
        "remove the inert width variable (or widen the landing grid so it binds); "
        "a value that only takes effect below the theme's own cap misleads the "
        "next editor"
    )


# --- 6. historical plan/design docs must not be a reproducible source of ----
# --- stale copy once the shipped positioning changes -------------------------


def test_design_document_records_the_agentic_ui_positioning() -> None:
    """The committed design doc must describe the same product model as the site."""
    design = (
        ROOT / "docs" / "superpowers" / "specs" / "2026-08-21-documentation-site-design.md"
    ).read_text(encoding="utf-8")
    lowered = design.lower()
    assert "one operational experience" in lowered, (
        "the design document still describes the landing page as three separate "
        "product parts; it must match the shipped agentic-UI positioning"
    )
    assert "surface" in lowered
    assert "proposal" in lowered, (
        "the design document must keep the factual limit that MCP writes are proposals"
    )
    assert "watch-backed" in lowered
    assert "fresh" in lowered
    assert "snapshots can differ" in lowered
    for overclaim in ("one operational state", "same operational state", "shared evidence"):
        assert overclaim not in lowered


def test_plan_document_records_the_agentic_ui_positioning() -> None:
    """The historical plan's embedded landing snippet must not be stale copy.

    `docs/superpowers/plans/2026-08-21-official-documentation-site.md` embeds
    the Task 2 landing markdown verbatim as an executable example. That
    snippet still read "## One cockpit. Three ways in." after `docs/index.md`
    was reframed around one agentic operational experience, so a future
    contributor treating the plan as the authoritative recipe would
    reproduce the pre-reframe positioning. This is the plan-side twin of
    `test_landing_frames_one_experience_rather_than_three_separate_products`
    above: the same heading must hold in both places.
    """
    plan = (
        ROOT / "docs" / "superpowers" / "plans" / "2026-08-21-official-documentation-site.md"
    ).read_text(encoding="utf-8")
    assert "## One cockpit. Three ways in." not in plan, (
        "the plan's embedded landing-content example still reads as three "
        "separate products bolted together; it must match docs/index.md's "
        "shipped 'one operational experience' positioning, or the plan is a "
        "reproducible source of stale copy"
    )
    assert "## One operational experience. Three ways to drive it." in plan, (
        "the plan's embedded landing-content example must carry the same "
        "product-model heading docs/index.md ships"
    )
    lowered = plan.lower()
    assert "watch-backed" in lowered
    assert "fresh reads" in lowered
    assert "snapshots can differ" in lowered
    for overclaim in ("one resource cache", "same evidence", "same operational state"):
        assert overclaim not in lowered


def test_design_and_css_describe_build_localized_runtime_assets_truthfully() -> None:
    """Generated vendor assets are allowed; third-party browser requests are not."""
    design = (
        ROOT / "docs" / "superpowers" / "specs" / "2026-08-21-documentation-site-design.md"
    ).read_text(encoding="utf-8")
    lowered = design.lower()
    assert "pinned client-side mermaid asset" not in lowered, (
        "Material/privacy manages Mermaid; the design must not claim a manually "
        "configured pin that does not exist"
    )
    assert "privacy" in lowered
    assert "build" in lowered
    assert "local" in lowered
    assert "browser" in lowered
    assert "third-party" in lowered

    css_intro = " ".join("\n".join(_css().splitlines()[:12]).lower().split())
    assert "generated local/vendor assets are allowed" in css_intro
    assert "browser runtime third-party requests are not" in css_intro
