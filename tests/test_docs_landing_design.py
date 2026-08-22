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
STORYTELLING_JS = DOCS / "assets" / "javascripts" / "visual-storytelling.js"

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


def _media_blocks(css: str, query: str) -> list[str]:
    """Return the brace-balanced body of every `@media <query> { … }` block.

    Args:
        css: Full stylesheet text.
        query: The exact at-rule prelude, e.g.
            `"@media (prefers-reduced-motion: reduce)"`.

    Returns:
        One string per matching block, containing only that block's own
        nested rules.
    """
    stripped = _strip_css_comments(css)
    blocks: list[str] = []
    for match in re.finditer(re.escape(query) + r"\s*\{", stripped):
        opening = match.end() - 1
        depth = 0
        for position in range(opening, len(stripped)):
            if stripped[position] == "{":
                depth += 1
            elif stripped[position] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(stripped[opening + 1 : position])
                    break
        else:  # pragma: no cover - only reachable on malformed CSS
            raise AssertionError(f"unterminated {query} block in extra.css")
    return blocks


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
    """Anything that transitions or transforms must be neutralised under reduce.

    This reads the balanced `@media (prefers-reduced-motion: reduce)` blocks
    themselves. An earlier version sliced from the first reduce query to the
    end of the file, so every later rule in the stylesheet counted as
    "reduced-motion CSS" — including ordinary footer `transition` values,
    which would have satisfied the assertions on their own.
    """
    blocks = _media_blocks(_css(), "@media (prefers-reduced-motion: reduce)")
    assert blocks, "the stylesheet must neutralise motion under a reduce preference"
    declarations = " ".join(" ".join(block.split()) for block in blocks)
    assert "transition: none" in declarations
    assert "transform: none" in declarations
    assert "transform: translate(" not in declarations, (
        "a reduce block that restates the element's own base transform changes "
        "nothing; drop the no-op rather than pad the block"
    )
    for block in blocks:
        assert "none" in block, (
            f"every reduce block must actually switch motion off; found: {block!r}"
        )


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


def test_only_the_visible_scene_ships_an_eagerly_fetched_poster() -> None:
    """`preload="none"` suppresses video bytes but never the poster image.

    The preload scanner fetches every `<video poster>` before the
    end-of-body controller can hide the inactive panels, so the two
    below-fold scene posters were downloaded at first paint. They move to
    `data-poster`, which the controller promotes to a real `poster` when
    that scene is actually selected; only the default scene keeps an
    attribute the browser may act on immediately.
    """
    switcher = _scene_switcher()
    videos = re.findall(r"<video[^>]*>", switcher)
    assert len(videos) == 3, "each scene keeps its own controllable video"
    eager = [video for video in videos if re.search(r'(?<![-\w])poster="', video)]
    assert len(eager) == 1, (
        "only the default scene may carry a `poster` attribute the preload "
        f"scanner can act on; found {len(eager)}"
    )
    assert 'poster="assets/scenes/cockpit-poster.png"' in eager[0]
    deferred = [video for video in videos if "data-poster=" in video]
    assert len(deferred) == 2
    for video in deferred:
        assert not re.search(r'(?<![-\w])poster="', video), (
            f"a deferred scene must not also declare an eager poster: {video}"
        )
    assert 'data-poster="assets/scenes/agent-poster.png"' in switcher
    assert 'data-poster="assets/scenes/mcp-poster.png"' in switcher


def test_deferred_scene_posters_keep_a_local_no_javascript_image() -> None:
    """A visitor without JavaScript must still see each scene's real frame.

    `<noscript>` content is parsed as text — never as DOM — while scripting
    is enabled, so this fallback costs the enhanced rendering nothing and
    cannot double-fetch a poster the controller will promote.
    """
    switcher = _scene_switcher()
    fallbacks = re.findall(r"<noscript>(.*?)</noscript>", switcher, re.DOTALL)
    assert len(fallbacks) == 2, "exactly the two deferred scenes need a no-JavaScript poster image"
    assets = []
    for fallback in fallbacks:
        match = re.fullmatch(r'\s*<img src="([^"]+)"[^>]*alt="[^"]+"[^>]*>\s*', fallback)
        assert match is not None, f"the fallback must be one described local image: {fallback!r}"
        source = match.group(1)
        assert source.startswith("assets/"), "no-JS fallbacks must stay local, never remote"
        assets.append(source)
    assert sorted(assets) == [
        "assets/scenes/agent-poster.png",
        "assets/scenes/mcp-poster.png",
    ]


def test_controller_promotes_a_deferred_poster_only_when_its_scene_is_selected() -> None:
    """The deferred posters are worthless if the controller never promotes them."""
    script = STORYTELLING_JS.read_text(encoding="utf-8")
    assert "data-poster" in script, "the controller must read the deferred attribute"
    assert re.search(
        r"if \(selected\) \{\s*promotePoster\(panel\);\s*\}",
        script,
    ), "poster promotion must be gated on the panel actually being selected"
    assert re.search(
        r'video\.setAttribute\("poster", poster\);\s*video\.removeAttribute\("data-poster"\);',
        script,
    ), "promotion must set the real attribute once and stop deferring it"


def test_scene_videos_never_autoplay_and_below_fold_media_preloads_nothing() -> None:
    """Bandwidth and motion are the visitor's choice on every landing video."""
    videos = re.findall(r"<video[^>]*>", _index())
    assert len(videos) == 4, "the hero video plus one per scene"
    for video in videos:
        assert "autoplay" not in video, f"no landing video may autoplay: {video}"
    hero, *scenes = videos
    assert 'preload="metadata"' in hero, (
        "the hero video is the page's lead evidence, so its metadata may load"
    )
    for video in scenes:
        assert 'preload="none"' in video, f"below-fold scene media must fetch nothing: {video}"


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


def test_write_path_stage_grid_targets_the_ordered_list_specificity() -> None:
    """The write-path grid must keep targeting the ordered list element directly.

    The write-path stages are an ordered list, so the grid rule has to target
    `.md-typeset ol.write-path__stages` directly in both the base rule and the
    narrow-width fallback. If the selector were only `.md-typeset .write-path__stages`,
    Material's ordered-list default would keep control and the stage list would
    stop behaving like a grid.
    """
    raw_css = _css()
    css = _strip_css_comments(raw_css)
    assert css.count(".md-typeset ol.write-path__stages {") == 2
    assert ".md-typeset .write-path__stages {" not in css


def test_evidence_figures_reserve_the_full_card_width_before_images_load() -> None:
    """Material's `figure { width: fit-content }` must lose to a real override.

    The mosaic images are `loading="lazy"`, so until each one decodes the
    `<figure>` has nothing but its `<figcaption>` to shrink-wrap to.
    Material for MkDocs ships `.md-typeset figure { width: fit-content }`,
    which won against the branch's `margin`-only reset and let every tile
    render at caption width and then jump 2-6x on load. The override has to
    restate the box itself — the declarations, not a comment describing
    them, are what the browser cascade sees.
    """
    block = _rule(_css(), ".md-typeset .evidence-card figure")
    for declaration in ("width: 100%", "display: block", "margin: 0"):
        assert declaration in block, (
            f"`.md-typeset .evidence-card figure` must declare `{declaration}` so an "
            "unloaded lazy figure still reserves its card's box; found: "
            f"{' '.join(block.split())!r}"
        )


def test_scene_tabs_stay_hidden_until_the_controller_enhances_the_switcher() -> None:
    """Without the controller the tab strip is inert, so it must not render.

    The tabs only switch panels when `visual-storytelling.js` runs. With the
    script blocked, all three panels are already visible in document order,
    so a visible tab strip would offer two controls that do nothing, keep a
    hard-coded `tabindex="-1"`, and advertise `aria-selected="true"` on one
    of three simultaneously rendered panels. The controller sets
    `data-enhanced` on the switcher; the stylesheet consumes it.
    """
    css = _strip_css_comments(_css())
    selector = ".md-typeset [data-scene-switcher]:not([data-enhanced]) .scene-tabs"
    assert selector in css, (
        "the stylesheet must gate the tab strip on the controller's "
        f"`data-enhanced` hook via `{selector}`"
    )
    assert "display: none" in _rule(_css(), selector)
    script = STORYTELLING_JS.read_text(encoding="utf-8")
    assert 'switcher.dataset.enhanced = "true"' in script, (
        "the controller must set the hook the no-JS gate depends on"
    )
    switcher = _scene_switcher()
    assert "data-scene-switcher" in switcher
    assert "data-enhanced" not in switcher, (
        "the enhancement hook must be applied by the controller at runtime, "
        "never hard-coded into the source"
    )
    panels = re.findall(r'<article id="scene-([^"]+)"', switcher)
    assert panels == ["direct", "agent", "mcp"], (
        "every panel must stay in the source order the no-JS fallback reads"
    )


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


def test_evidence_copy_claims_only_what_its_capture_actually_shows() -> None:
    """A caption must not promise a signal the screenshot cannot contain.

    The cockpit capture comes from an in-memory fixture with no metrics
    source, so every CPU/MEM column renders an em-dash placeholder. Claiming
    the frame shows "utilization" made the page's own headline evidence
    contradict itself.
    """
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    cockpit = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "cockpit-poster.png" in card
    )
    assert "utilization" not in cockpit.lower(), (
        "the cockpit capture has no live metrics, so its caption must describe "
        "what it does show (status, scope, restarts) instead"
    )
    for signal in ("status", "scope", "restart"):
        assert signal in cockpit.lower()


def test_generic_grouped_containers_carry_a_role_aria_can_use() -> None:
    """`aria-label` is ignored on `role=generic`, so the label needs a role.

    Both containers are bare `div`s that group several related items and
    name that grouping for assistive technology. Without an explicit role
    the name is silently dropped and the group reads as loose text.
    """
    index = _index()
    for class_name in ("contract-map__shared", "write-path__origins"):
        match = re.search(rf'<div class="{class_name}"([^>]*)>', index)
        assert match is not None, f"the landing page must keep the {class_name} grouping"
        attributes = match.group(1)
        assert "aria-label=" in attributes
        assert 'role="group"' in attributes, (
            f"{class_name} carries an aria-label, so it needs an explicit role "
            f"assistive technology can name; found: {attributes.strip()!r}"
        )


def test_every_evidence_capture_links_to_its_full_resolution_asset() -> None:
    """A 1280x720 terminal capture is unreadable at a third of its size.

    Each mosaic tile renders at ~374px wide, which shrinks 15px terminal
    text to about 4px. Wrapping the image in a plain link to the asset
    itself keeps the evidence checkable without a lightbox, a framework, or
    any additional runtime code.
    """
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    cards = re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.DOTALL)
    assert len(cards) == 6
    for card in cards:
        image = re.search(r'<img src="(assets/scenes/[^"]+)"', card)
        assert image is not None
        link = re.search(
            r'<a class="evidence-card__full" href="([^"]+)" aria-label="([^"]+)">\s*<img',
            card,
        )
        assert link is not None, (
            f"the capture in this tile must be a link to its own full-resolution "
            f"asset with an accessible label: {card[:120]!r}"
        )
        assert link.group(1) == image.group(1), "the link must open the very asset the tile renders"
        assert "full-resolution" in link.group(2).lower()


def test_landing_provenance_matches_every_captures_real_source() -> None:
    """One tile was captured against a disposable local cluster, not a fixture.

    `mcp-poster.png` comes from a real k3d cluster (`ctx:k3d-korvid-demo`),
    which `docs/demo/visual-storytelling.md` states plainly. A blanket
    "synthetic cluster data" claim above it is not true of every tile.
    """
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    heading = mosaic[: mosaic.index('<div class="evidence-mosaic__grid">')]
    assert "synthetic or disposable local cluster" in heading.lower(), (
        "the mosaic's provenance line must cover the disposable-cluster capture "
        f"as well as the in-memory ones; found: {heading.strip()[-160:]!r}"
    )


def test_single_pod_log_evidence_is_not_labelled_as_a_merged_stream() -> None:
    """`merged-logs.png` shows the single-pod `l` view, not the merged `L` view.

    Its header reads `payment-worker-.../app [json] - streaming`, one pod and
    one container. Korvid does have a multi-log view; this capture is not it,
    so the tile must not be named for a screen it does not show.
    """
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    card = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "merged-logs.png" in card
    )
    text = re.sub(r"<[^>]+>", " ", card).lower()
    for overclaim in ("merged logs", "merge"):
        assert overclaim not in text, (
            f"this capture is a single-pod stream, so it must not claim {overclaim!r}"
        )
    assert "stream" in text

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
