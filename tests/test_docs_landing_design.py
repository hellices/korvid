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
   inside a 1188px panel. The hero now pairs the copy with the actual product
   media, so the panel is balanced by real product evidence rather than a key
   legend.

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


def _scene_switcher() -> str:
    return _section('<section class="scene-switcher"', "</section>")


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


def _section(opening: str, closing: str) -> str:
    source = _index()
    start = source.find(opening)
    assert start != -1, f"missing opening marker: {opening!r}"
    end = source.find(closing, start)
    assert end != -1, f"missing closing marker: {closing!r} after opening marker {opening!r}"
    return source[start : end + len(closing)]


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


# --- 3. the hero is balanced by the actual product media at wide widths ------


def test_hero_leads_with_real_korvid_media() -> None:
    hero = _section('<section class="hero">', "</section>")
    assert 'class="hero-demo"' in hero
    assert 'src="assets/demo.mp4"' in hero
    assert 'poster="assets/scenes/cockpit-poster.png"' in hero
    assert "hero-panel" not in hero


def test_hero_media_is_controllable_and_has_a_text_fallback() -> None:
    hero = _section('<section class="hero">', "</section>")
    video = re.search(r"<video\b[^>]*>", hero)
    assert video is not None
    opening = video.group(0)
    assert "aria-label=" in opening or "aria-labelledby=" in opening, (
        "the hero video must have an accessible name"
    )
    for attribute in ("controls", "muted", "loop", "playsinline"):
        assert re.search(rf"\b{attribute}\b", opening)
    assert 'preload="metadata"' in opening
    assert "autoplay" not in opening
    assert "Your browser does not support the korvid demo video." in hero
    assert "<figcaption>" in hero


def test_hero_gives_the_product_at_least_half_the_wide_layout() -> None:
    css = _css()
    wide_hero = re.search(
        r"@media \(min-width: 960px\).*?\.md-typeset \.hero \{(?P<body>.*?)\}",
        _strip_css_comments(css),
        re.DOTALL,
    )
    assert wide_hero is not None
    assert "grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr)" in wide_hero.group("body")


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


def test_hero_keeps_heading_then_demo_then_copy_in_source_and_desktop_grid() -> None:
    """Mobile/tablet flow must surface the product before the supporting copy."""
    hero = _section('<section class="hero">', "</section>")
    heading = hero.find('class="hero-heading"')
    demo = hero.find('class="hero-demo"')
    copy = hero.find('class="hero-copy-column"')
    assert heading != -1, "the hero must keep a dedicated .hero-heading wrapper"
    assert demo != -1, "the hero must keep the real product demo figure"
    assert copy != -1, "the hero must keep a dedicated .hero-copy-column wrapper"
    assert heading < demo < copy, (
        "source order must stay headline → product demo → supporting copy so "
        "mobile and tablet reading/tab order matches the visual stack"
    )

    css = _css()
    wide_css = re.search(
        r"@media \(min-width: 960px\) \{(?P<body>.*?)\n\}",
        _strip_css_comments(css),
        re.DOTALL,
    )
    assert wide_css is not None
    demo_rule = _rule(wide_css.group("body"), ".md-typeset .hero-demo {")
    assert "grid-column: 2" in demo_rule
    assert "grid-row: 1 / span 2" in demo_rule, (
        "desktop layout must keep the demo in column 2 spanning both copy rows"
    )


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
    sources = (_index(), COPYRIGHT_PARTIAL.read_text(encoding="utf-8"))
    referenced = {
        match
        for source in sources
        for match in re.findall(r'(?:src|poster)="(assets/[A-Za-z0-9_./-]+)"', source)
    }
    assert referenced, "the landing page should reference at least one local asset"
    for relative in referenced:
        assert (DOCS / relative).is_file(), f"docs/{relative} is referenced but missing"
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


# --- 4. one incident, three drivers -----------------------------------------


def test_landing_presents_one_incident_through_three_drivers() -> None:
    switcher = _scene_switcher()
    assert "One incident. Three ways to drive it." in switcher
    for scene in ("direct", "agent", "mcp"):
        assert f'id="scene-tab-{scene}"' in switcher
        assert f'aria-controls="scene-{scene}"' in switcher
        assert f'id="scene-{scene}"' in switcher
    assert "same evidence" not in switcher.lower()


def test_scene_switcher_source_is_a_complete_no_javascript_fallback() -> None:
    switcher = _scene_switcher()
    panels = re.findall(r'<article id="scene-[^"]+"[^>]*role="tabpanel"[^>]*>', switcher)
    assert len(panels) == 3
    assert all(" hidden" not in panel for panel in panels)
    assert 'src="assets/demo.mp4"' in switcher
    assert 'src="assets/scenes/agent-demo.mp4"' in switcher
    assert 'src="assets/scenes/mcp-follow-demo.mp4"' in switcher


def test_scene_switcher_uses_the_aria_tab_contract() -> None:
    switcher = _scene_switcher()
    assert 'role="tablist"' in switcher
    assert switcher.count('role="tab"') == 3
    assert switcher.count('role="tabpanel"') == 3
    assert switcher.count('aria-selected="true"') == 1
    assert switcher.count('aria-selected="false"') == 2


def test_product_contract_map_keeps_the_read_paths_truthful() -> None:
    contract = _section('<section class="contract-map"', "</section>")
    lowered = " ".join(re.sub(r"<[^>]+>", " ", contract).lower().split())
    for fact in (
        "human operator",
        "watch-backed tui snapshot",
        "model / provider",
        "bounded fresh reads",
        "editor / external assistant",
        "active cluster context",
        "navigation semantics",
        "snapshots can differ",
    ):
        assert fact in lowered
    assert "same evidence" not in lowered


def test_guarded_write_path_orders_confirmation_audit_and_execution() -> None:
    path = _section('<section class="write-path"', "</section>")
    lowered = " ".join(re.sub(r"<[^>]+>", " ", path).lower().split())
    for origin in ("direct action", "agent proposal", "opt-in mcp proposal"):
        assert origin in lowered
    stages = ["observe", "propose", "confirm", "audit", "execute"]
    positions = [path.index(f'data-stage="{stage}"') for stage in stages]
    assert positions == sorted(positions)
    assert "fresh human keystroke" in lowered
    assert "audit write failed" in lowered
    assert "action blocked" in lowered
    assert "fail-closed" in lowered


def test_landing_keeps_agent_masking_distinct_from_mcp_disclosure() -> None:
    path = _section('<section class="write-path"', "</section>")
    lowered = " ".join(re.sub(r"<[^>]+>", " ", path).lower().split())
    assert "embedded provider payloads are masked" in lowered
    assert "mcp result disclosure is tool-specific" in lowered
    assert "secret values are masked before model calls" not in lowered


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


def test_capability_mosaic_contains_six_real_linked_product_scenes() -> None:
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    cards = re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.DOTALL)
    assert len(cards) == 6
    for card in cards:
        assert "<img" in card
        assert 'loading="lazy"' in card
        assert "<figcaption>" in card
        assert re.search(r'href="[^"]+/(?:#[^"]+)?"', card)
        paragraphs = re.findall(r"<p>(.*?)</p>", card, re.DOTALL)
        assert len(paragraphs) == 1
        assert len(re.sub(r"<[^>]+>", " ", paragraphs[0]).split()) <= 30


def test_flight_paths_are_four_compact_user_destinations() -> None:
    paths = _section('<nav class="flight-paths"', "</nav>")
    for label, href in (
        ("Operate a cluster", "getting-started/"),
        ("Add the embedded agent", "agent/"),
        ("Connect an MCP client", "mcp/"),
        ("Evaluate production use", "performance/"),
    ):
        assert label in paths
        assert f'href="{href}"' in paths
    assert "Contributing?" not in paths


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
