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

MATERIAL_ATTRIBUTION = "https://squidfunk.github.io/mkdocs-material/"


def _index() -> str:
    return INDEX.read_text()


def _css() -> str:
    return EXTRA_CSS.read_text()


def _rule(css: str, selector: str) -> str:
    """Return the declaration block of the first rule whose selector list matches.

    Args:
        css: Full stylesheet text.
        selector: A selector substring that must appear in the rule's prelude.

    Returns:
        The text between `{` and the matching `}` for that rule.
    """
    index = css.index(selector)
    open_brace = css.index("{", index)
    close_brace = css.index("}", open_brace)
    return css[open_brace + 1 : close_brace]


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
    footer = COPYRIGHT_PARTIAL.read_text()
    assert "korvid-mark.svg" in footer, "the footer should carry the original korvid mark"
    assert "korvid" in footer
    assert "Apache-2.0" in footer, "the footer must state the project's license"


def test_footer_links_are_built_from_mkdocs_urls_not_hardcoded_paths() -> None:
    """Internal footer links must go through MkDocs' `url` filter.

    The site is served from a subpath (`/korvid/`), so a hardcoded
    `/getting-started/` would 404 on the published site while working
    locally.
    """
    footer = COPYRIGHT_PARTIAL.read_text()
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
    footer = COPYRIGHT_PARTIAL.read_text()
    assert MATERIAL_ATTRIBUTION in footer
    assert "Material for MkDocs" in footer


def test_footer_link_targets_are_real_documentation_pages() -> None:
    """Every internal footer destination must map to a page MkDocs actually builds."""
    footer = COPYRIGHT_PARTIAL.read_text()
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
    keybindings = (DOCS / "keybindings.md").read_text()
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


# --- the whole page stays original, asset-light, and script-free -------------


def test_landing_page_ships_no_javascript_and_no_external_assets() -> None:
    """The site is HTML/CSS/SVG only: no scripts, no CDN fonts, no trackers."""
    sources = [
        _index(),
        _css(),
        COPYRIGHT_PARTIAL.read_text(),
        (OVERRIDES / "home.html").read_text(),
    ]
    for source in sources:
        assert "<script" not in source
        assert "onclick=" not in source
    css = _css()
    external_urls = re.findall(r"url\((['\"]?)(https?:)?//", css)
    assert not external_urls, "extra.css must not pull in remote assets"
    for source in sources:
        for host in ("fonts.googleapis.com", "cdn.jsdelivr.net", "unpkg.com", "google-analytics"):
            assert host not in source, f"the docs site must not reference {host}"


def test_local_assets_referenced_by_the_landing_page_exist() -> None:
    """No broken image: every locally referenced asset is checked into the repo."""
    referenced = set(re.findall(r"assets/([A-Za-z0-9._\-]+)", _index()))
    referenced |= set(re.findall(r"assets/([A-Za-z0-9._\-]+)", COPYRIGHT_PARTIAL.read_text()))
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
