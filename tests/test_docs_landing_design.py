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

import html
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCS = ROOT / "docs"
INDEX = DOCS / "index.md"
EXTRA_CSS = DOCS / "stylesheets" / "extra.css"
OVERRIDES = DOCS / "overrides"
COPYRIGHT_PARTIAL = OVERRIDES / "partials" / "copyright.html"
MARK = DOCS / "assets" / "korvid-mark.svg"
DEMO_README = DOCS / "demo" / "README.md"
STORYTELLING_JS = DOCS / "assets" / "javascripts" / "visual-storytelling.js"
SWITCHER_HARNESS = ROOT / "tests" / "js" / "scene_switcher_harness.mjs"
VISUAL_STORYTELLING_PLAN = DOCS / "superpowers" / "plans" / "2026-08-22-visual-storytelling.md"

MATERIAL_ATTRIBUTION = "https://squidfunk.github.io/mkdocs-material/"

#: The Evidence label the Agent scene ships. `ScriptedAgentRuntime` in
#: `docs/demo/demo.py` discards the prompt and the screen context it is
#: handed, contacts no provider, runs no read tool, and yields a fixed
#: tool/citation event sequence, so the capture shows scripted events — not
#: the bounded fresh reads the product performs against a cluster.
AGENT_SCENE_EVIDENCE = (
    "Scripted tool events and an E1 marker the panel flags as unsupported, not bounded reads"
)

#: Claims no surface built on `agent-poster.png`/`agent-demo.mp4` may make.
SCRIPTED_AGENT_OVERCLAIMS = (
    "bounded fresh reads",
    "bounded reads",
    "fresh reads",
    "live tool",
    "validated citation",
    "validated evidence",
    "checkable evidence",
    "cited evidence",
)

#: Cues that turn a banned phrase into an allowed truthful denial, so a
#: surface may state "not bounded reads" without tripping the ban. Matched as
#: whole words/phrases anywhere in the phrase's clause, not just immediately
#: before it, so "not evidence of X, Y, or Z" covers the whole enumeration.
NEGATION_CUES = ("no", "not", "never", "without", "rather than", "instead of")

_NEGATION_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(cue) for cue in NEGATION_CUES) + r")\b"
)

#: `alt` and `aria-label` values are the load-bearing accessibility
#: description of the site's media, and a prior overclaim shipped in exactly
#: one of these attributes while the visible copy stayed honest. Tag-only
#: stripping would discard them along with the markup, so they are extracted
#: and decoded before the rest of the tags are dropped.
_ATTR_VALUE = re.compile(r'\b(?:alt|aria-label)\s*=\s*"([^"]*)"', re.IGNORECASE)

#: Explicit subjects that mark a comma-joined coordinate clause as making its
#: *own* claim rather than continuing an enumeration ("X, Y, and Z"). Only
#: these nouns/pronouns count, so "not evidence of X, Y, and validated
#: citations" still reads as one negated list — "validated citations" is not
#: one of these subjects.
_COORDINATE_SUBJECTS = ("it", "this", "that", "the capture", "the scene", "the video", "the media")

#: A clause boundary is a sentence terminator, a semicolon, or a comma-joined
#: `and`/`but` that introduces one of `_COORDINATE_SUBJECTS`. The last form
#: lets ", and it proves ..." start a fresh clause — so an unrelated "no"
#: earlier in the sentence cannot suppress it — while ", and validated
#: citations" (no explicit subject) stays part of the same enumeration.
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;]"
    r"|,\s*(?:and|but)\s+(?:"
    + "|".join(re.escape(subject) for subject in _COORDINATE_SUBJECTS)
    + r")\b"
)


def _flatten(text: str) -> str:
    """Normalise markup into lowercase prose for phrase scanning.

    Args:
        text: Markup or prose to scan.

    Returns:
        Lowercase, whitespace-collapsed prose containing both the visible
        text and every decoded `alt`/`aria-label` attribute value, each
        appended as its own sentence so a clause boundary always separates
        one attribute's claim from the next and from the surrounding copy.
    """
    attribute_values = [html.unescape(match.group(1)) for match in _ATTR_VALUE.finditer(text)]
    body = re.sub(r"<[^>]+>", " ", text)
    combined = ". ".join([body, *attribute_values])
    return " ".join(combined.lower().split())


def _clause_is_negated(flattened: str, index: int) -> bool:
    """Whether a negation cue governs the clause ending at `index`.

    The clause is everything since the nearest preceding clause boundary — a
    sentence terminator (`.`, `!`, `?`), a semicolon, or a comma-joined
    `and`/`but` that introduces an explicit new subject (see
    `_COORDINATE_SUBJECTS`) — so a denial earlier in an unrelated sentence or
    coordinate clause cannot suppress a later, separate positive claim, while
    a negation anywhere earlier in the *same* clause — including across a
    comma/`or` enumeration — still covers a phrase buried inside it.
    """
    clause_start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(flattened[:index]):
        clause_start = boundary.end()
    return bool(_NEGATION_PATTERN.search(flattened[clause_start:index]))


def _unnegated(text: str, phrase: str) -> bool:
    """Report whether `phrase` appears in `text` as a claim rather than a denial.

    Args:
        text: Markup or prose to scan; tags are normalised away but `alt`/
            `aria-label` attribute values are folded back in first.
        phrase: The lowercase claim to look for.

    Returns:
        True when at least one whole-word occurrence of `phrase` is not
        governed by a negation cue anywhere earlier in its own clause.
    """
    flattened = _flatten(text)
    pattern = re.compile(rf"(?<!\w){re.escape(phrase)}\b")
    for match in pattern.finditer(flattened):
        if not _clause_is_negated(flattened, match.start()):
            return True
    return False


def test_unnegated_respects_clause_scoped_negation_across_an_enumeration() -> None:
    """One negation governs an entire "not X, Y, or Z" enumeration.

    `fresh reads` and `live tool` are only reachable here as tail fragments
    of a longer denied claim, so a check that only looks at the word
    immediately before the match — rather than the whole clause since the
    negation cue — would misread each fragment as an unnegated claim.
    """
    text = (
        "<p>This capture is not evidence of bounded fresh reads, "
        "live tool execution, or validated citation.</p>"
    )
    for phrase in ("bounded fresh reads", "fresh reads", "live tool", "validated citation"):
        assert not _unnegated(text, phrase), (
            f"{phrase!r} sits inside one negated enumeration and must not be flagged"
        )


def test_unnegated_respects_never_across_an_or_list() -> None:
    text = "<p>The capture never claims bounded fresh reads or validated citation.</p>"
    for phrase in ("bounded fresh reads", "fresh reads", "validated citation"):
        assert not _unnegated(text, phrase), (
            f"{phrase!r} is covered by the leading 'never' and must not be flagged"
        )


def test_unnegated_still_flags_a_genuine_positive_claim() -> None:
    text = "<p>This capture proves bounded fresh reads and validated citation for every answer.</p>"
    for phrase in ("bounded fresh reads", "fresh reads", "validated citation"):
        assert _unnegated(text, phrase), (
            f"{phrase!r} is an unqualified positive claim and must be flagged"
        )


def test_unnegated_does_not_let_an_earlier_sentence_suppress_a_later_claim() -> None:
    """A negation in one sentence must not launder an overclaim in the next."""
    text = (
        "<p>This capture does not execute korvid's provider or tool pipeline. "
        "It proves bounded fresh reads for every session.</p>"
    )
    assert _unnegated(text, "bounded fresh reads"), (
        "the second sentence's positive claim is a separate clause from the "
        "first sentence's denial and must still be flagged"
    )


def test_unnegated_isolates_a_new_clause_introduced_by_a_coordinate_conjunction() -> None:
    """A comma-joined coordinate clause with its own explicit subject is separate.

    "There is no limit ..., and it proves ..." is two coordinate clauses, not
    one long enumeration: the second clause introduces its own subject ("it")
    and makes an unrelated, genuine positive claim. An unrelated "no" earlier
    in the sentence must not launder it.
    """
    text = (
        "<p>There is no limit on concurrency, and it proves bounded fresh "
        "reads for every session.</p>"
    )
    assert _unnegated(text, "bounded fresh reads"), (
        "the coordinate clause after ', and it' introduces a new subject and "
        "must not inherit the earlier, unrelated negation"
    )


def test_unnegated_isolates_a_new_clause_after_but_or_a_semicolon() -> None:
    """`, but <subject>` and a bare semicolon both start a new clause too."""
    comma_but = (
        "<p>There is no support for retries, but this proves bounded fresh "
        "reads for every call.</p>"
    )
    semicolon = (
        "<p>There is no support for retries; this proves bounded fresh reads for every call.</p>"
    )
    for text in (comma_but, semicolon):
        assert _unnegated(text, "bounded fresh reads"), (
            "a clause introduced by ', but this' or a semicolon must not "
            "inherit an earlier, unrelated negation"
        )


def test_unnegated_keeps_a_truthful_oxford_comma_list_denial_intact() -> None:
    """The final ", and <item>" of a negated Oxford-comma list is not a new clause.

    Only a coordinate conjunction followed by an explicit subject (`it`,
    `this`, `the capture`, ...) starts a new clause; "and validated
    citations" is just the last item of one negated list and must stay
    covered by the leading "not evidence of".
    """
    text = (
        "<p>This capture is not evidence of bounded fresh reads, live tool "
        "execution, and validated citations.</p>"
    )
    for phrase in ("bounded fresh reads", "live tool", "validated citation"):
        assert not _unnegated(text, phrase), (
            f"{phrase!r} sits inside one negated oxford-comma list; the "
            "'and' before its final item must not split it into a new clause"
        )


def test_unnegated_scans_alt_and_aria_label_attribute_values() -> None:
    """Alt text and aria-label values carry load-bearing a11y claims too.

    A prior overclaim shipped in exactly these attributes while the visible
    copy stayed honest, so stripping tags wholesale — including their
    attribute values — before scanning would have missed it again.
    """
    scene = (
        '<video aria-label="Bounded fresh reads validated by live tool execution">'
        "fallback</video>"
        '<img alt="Validated citation for every scripted answer">'
    )
    assert _unnegated(scene, "bounded fresh reads"), "aria-label overclaims must be caught"
    assert _unnegated(scene, "live tool"), "aria-label overclaims must be caught"
    assert _unnegated(scene, "validated citation"), "alt overclaims must be caught"


def test_unnegated_accepts_a_negated_alt_attribute() -> None:
    scene = '<img alt="Not a claim of bounded fresh reads or validated citation">'
    for phrase in ("bounded fresh reads", "validated citation"):
        assert not _unnegated(scene, phrase), (
            f"a truthfully negated alt attribute must not flag {phrase!r}"
        )


def test_unnegated_accepts_the_shipped_agent_scene_and_tile_copy() -> None:
    """Regression pin: the real Agent scene and evidence tile stay clean."""
    scene = _section('<article id="scene-agent"', "</article>")
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    card = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "agent-poster.png" in card
    )
    for overclaim in SCRIPTED_AGENT_OVERCLAIMS:
        assert not _unnegated(scene, overclaim), (
            f"the shipped Agent scene must not claim {overclaim!r}"
        )
        assert not _unnegated(card, overclaim), (
            f"the shipped agent evidence tile must not claim {overclaim!r}"
        )


def _index() -> str:
    return INDEX.read_text(encoding="utf-8")


def _css() -> str:
    return EXTRA_CSS.read_text(encoding="utf-8")


def _plan() -> str:
    return VISUAL_STORYTELLING_PLAN.read_text(encoding="utf-8")


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


def _selector_list(css: str, anchor: str) -> str:
    """Return the full comma-separated selector list (prelude) for `anchor`'s rule.

    Unlike `_rule`, which returns the declaration block, this returns the
    text between the *previous* rule's closing brace and this rule's opening
    brace — the complete prelude, including every selector the rule is
    grouped with, regardless of where `anchor` falls in that list.

    Args:
        css: Full stylesheet text.
        anchor: A selector substring that appears somewhere in the target
            rule's prelude.

    Returns:
        The full selector list text for the rule containing `anchor`.
    """
    stripped = _strip_css_comments(css)
    index = stripped.index(anchor)
    open_brace = stripped.index("{", index)
    prev_close = stripped.rfind("}", 0, index)
    return stripped[prev_close + 1 : open_brace]


def _outline_offset_px(rule_block: str) -> float:
    """Parse the numeric `outline-offset` value (in px) out of a declaration block."""
    match = re.search(r"outline-offset:\s*(-?[\d.]+)px", rule_block)
    assert match is not None, f"expected an outline-offset declaration in {rule_block!r}"
    return float(match.group(1))


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


def _fenced_block_after(source: str, marker: str, language: str) -> str:
    start = source.index(marker)
    fence = f"```{language}\n"
    body_start = source.index(fence, start) + len(fence)
    body_end = source.index("\n```", body_start)
    return source[body_start:body_end]


def _compact(text: str) -> str:
    return " ".join(text.split())


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


def test_hero_demo_video_focus_outline_is_inset_to_avoid_clipping() -> None:
    """The frame that hosts the hero video clips overflow, so its ring must sit inside it.

    `.hero-demo__frame` sets `overflow: hidden` to keep the rounded corners
    and drop shadow tidy. A positive `outline-offset` on the video draws the
    ring *outside* the video's border box, and the frame's clip silently
    swallows it — a keyboard user tabs to the video and sees no focus
    indicator at all. The video needs its own inset ring, split out of the
    selector group that still serves the un-clipped hero links, install
    command, and footer links (those keep their outward ring).
    """
    css = _css()
    frame = _rule(css, ".md-typeset .hero-demo__frame {")
    assert "overflow: hidden" in frame, "this test only makes sense while the frame still clips"

    video_rule = _rule(css, ".md-typeset .hero-demo video:focus-visible")
    assert re.search(r"outline\s*:\s*none\b", video_rule) is None, (
        "the inset ring must still be a visible outline, not `outline: none`"
    )
    assert _outline_offset_px(video_rule) < 0, (
        "the clipped video's focus ring must be inset (negative outline-offset), got "
        f"{_compact(video_rule)!r}"
    )

    video_prelude = _selector_list(css, ".md-typeset .hero-demo video:focus-visible")
    assert ".hero a:focus-visible" not in video_prelude, (
        "the video must no longer share a selector group with the outward-focused hero "
        "links/install-command/footer links, or their positive offset would clip it too"
    )

    outward_rule = _rule(css, ".md-typeset .hero a:focus-visible")
    assert _outline_offset_px(outward_rule) > 0, (
        "the un-clipped hero links must keep their outward focus ring"
    )
    outward_prelude = _selector_list(css, ".md-typeset .hero a:focus-visible")
    assert ".install-command:focus-visible" in outward_prelude, (
        "the install command must still share the outward hero focus ring"
    )
    assert ".korvid-footer a:focus-visible" in outward_prelude, (
        "the footer links must still share the outward hero focus ring"
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


def test_hero_demo_fills_its_product_media_column() -> None:
    """Material's fit-content figure width must not shrink-wrap the hero."""
    demo_rule = _rule(_css(), ".md-typeset .hero-demo {")
    assert "width: 100%" in demo_rule


def test_visual_storytelling_plan_hero_markup_matches_the_shipped_sources() -> None:
    """The plan's hero markup must stay identical to the shipped landing page."""
    plan_hero = _fenced_block_after(
        _plan(),
        "- [ ] **Step 4: Replace the hero markup and remove the separate demo figure**",
        "html",
    )
    shipped_hero = _section('<section class="hero">', "</section>")
    assert _compact(plan_hero) == _compact(shipped_hero)


def test_visual_storytelling_plan_hero_css_matches_the_shipped_rules() -> None:
    """The plan's hero CSS must stay synced with the shipped source snippets."""
    plan_css = _fenced_block_after(
        _plan(),
        "- [ ] **Step 5: Replace the legend/product-demo CSS with the product stage**",
        "css",
    )
    shipped_css = _css()

    plan_demo = _rule(plan_css, ".md-typeset .hero-demo {")
    shipped_demo = _rule(shipped_css, ".md-typeset .hero-demo {")
    assert _compact(plan_demo) == _compact(shipped_demo)

    plan_wide = next(
        block
        for block in _media_blocks(plan_css, "@media (min-width: 960px)")
        if ".hero .hero-heading" in block
    )
    shipped_wide = next(
        block
        for block in _media_blocks(shipped_css, "@media (min-width: 960px)")
        if ".hero .hero-heading" in block
    )
    assert _compact(plan_wide) == _compact(shipped_wide)


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


def test_scene_panel_focus_outline_is_inset_to_avoid_clipping() -> None:
    """`.scene-panels` clips overflow the same way the hero frame does.

    A positive `outline-offset` on `.scene-panel:focus-visible` draws the
    ring outside the panel's border box, and `.scene-panels { overflow:
    hidden }` silently swallows it. The panel needs its own inset ring,
    split out of the selector group it currently shares with the (un-clipped)
    scene-tab buttons, which keep their outward ring.
    """
    css = _css()
    container = _rule(css, ".md-typeset .scene-panels {")
    assert "overflow: hidden" in container, (
        "this test only makes sense while the container still clips"
    )

    panel_rule = _rule(css, ".md-typeset .scene-panel:focus-visible")
    assert re.search(r"outline\s*:\s*none\b", panel_rule) is None, (
        "the inset ring must still be a visible outline, not `outline: none`"
    )
    assert _outline_offset_px(panel_rule) < 0, (
        "the clipped panel's focus ring must be inset (negative outline-offset), got "
        f"{_compact(panel_rule)!r}"
    )

    panel_prelude = _selector_list(css, ".md-typeset .scene-panel:focus-visible")
    assert ".scene-tabs button:focus-visible" not in panel_prelude, (
        "the panel must no longer share a selector group with the outward-focused tab "
        "buttons, or their positive offset would clip it too"
    )

    tabs_rule = _rule(css, ".md-typeset .scene-tabs button:focus-visible")
    assert _outline_offset_px(tabs_rule) > 0, (
        "the un-clipped scene-tab buttons must keep their outward focus ring"
    )


def test_visual_storytelling_plan_scene_focus_css_matches_the_shipped_rules() -> None:
    """The plan's scene-switcher CSS snippet must stay synced with the shipped focus rules.

    Step 4 of the plan embeds the scene-tabs/scene-panel focus-visible rules
    verbatim; if the shipped stylesheet ungroups them to fix outline
    clipping, the plan's executable snippet must ungroup identically or a
    future re-run of the plan would reintroduce the clipped ring.
    """
    plan_css = _fenced_block_after(
        _plan(),
        "- [ ] **Step 4: Add responsive switcher styling**",
        "css",
    )
    shipped_css = _css()

    plan_tabs = _rule(plan_css, ".md-typeset .scene-tabs button:focus-visible")
    shipped_tabs = _rule(shipped_css, ".md-typeset .scene-tabs button:focus-visible")
    assert _compact(plan_tabs) == _compact(shipped_tabs)
    assert _outline_offset_px(plan_tabs) > 0

    plan_panel = _rule(plan_css, ".md-typeset .scene-panel:focus-visible")
    shipped_panel = _rule(shipped_css, ".md-typeset .scene-panel:focus-visible")
    assert _compact(plan_panel) == _compact(shipped_panel)
    assert _outline_offset_px(plan_panel) < 0

    plan_prelude = _selector_list(plan_css, ".md-typeset .scene-panel:focus-visible")
    assert ".scene-tabs button:focus-visible" not in plan_prelude, (
        "the plan snippet must not re-group the panel with the tab buttons"
    )


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
    cannot double-fetch a poster the controller will promote. With scripting
    off all three panels render at once, so both fallbacks sit below the
    fold and must defer their own bytes exactly like the mosaic captures do.
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
        assert 'loading="lazy"' in fallback, (
            "a no-JavaScript fallback poster renders below the fold, so it must "
            f"defer its own bytes like every other below-fold capture: {fallback!r}"
        )
        assets.append(source)
    assert sorted(assets) == [
        "assets/scenes/agent-poster.png",
        "assets/scenes/mcp-poster.png",
    ]


def test_no_javascript_fallback_replaces_deferred_videos_with_posters() -> None:
    """No-JS rendering must show one media surface per deferred scene."""
    selector = ".md-typeset .scene-switcher:not([data-enhanced]) .scene-panel video[data-poster] {"
    for label, source in (("stylesheet", _css()), ("plan", _plan())):
        assert selector in source, f"{label} must target deferred unenhanced videos"
        assert "display: none" in _rule(source, selector)


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


def test_controller_pauses_off_screen_switcher_media_via_intersection_observer() -> None:
    """Scrolling a playing switcher off-screen must not leave it decoding forever.

    The controller already pauses a panel's video when a visitor switches
    tabs, but a visitor who presses play on the *selected* scene and then
    scrolls the whole switcher out of the viewport keeps that video decoding
    indefinitely — nothing watches the switcher's own visibility. The
    controller must observe each `[data-scene-switcher]`'s intersection with
    the viewport, and when it stops intersecting, pause every video inside
    it (selected or not). It must never call `play()` itself; only a user
    keystroke or click may resume playback. `IntersectionObserver` support
    must be feature-detected so its absence cannot break the switcher.
    """
    script = STORYTELLING_JS.read_text(encoding="utf-8")
    assert re.search(r'typeof IntersectionObserver === "function"', script), (
        "IntersectionObserver support must be feature-detected so unsupported "
        "browsers still get a working, if never-paused-by-scroll, switcher"
    )
    start = script.index("new IntersectionObserver(")
    end = script.index("observer.observe(switcher);", start) + len("observer.observe(switcher);")
    observer_block = script[start:end]
    assert "isIntersecting" in observer_block, (
        "the observer callback must branch on the switcher's own intersection state"
    )
    assert 'switcher.querySelectorAll("video")' in observer_block, (
        "an off-screen switcher must pause every video it contains, not only "
        "the panels that were already inactive"
    )
    assert ".pause()" in observer_block
    assert "observer.observe(switcher)" in observer_block, (
        "every switcher instance must register itself with the observer"
    )
    assert ".play(" not in script, (
        "the controller must never resume playback itself; user control is authoritative"
    )


def _strip_js_comments(source: str) -> str:
    """Drop `/* … */` and `// …` comments so prose cannot satisfy a code contract."""
    without_blocks = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", " ", without_blocks)


def test_controller_never_builds_a_selector_out_of_an_authored_id() -> None:
    """`aria-controls` is author data, so it must never become a selector.

    `switcher.querySelector("#" + id)` reads an id through the CSS parser: a
    value with a space becomes a descendant selector that can match a
    different element, a value with a dot becomes an id-plus-class selector,
    and a value starting with a digit throws a `SyntaxError` that takes the
    whole enhancement down instead of reporting one missing panel.
    `getElementById` takes the id verbatim, and a containment check keeps
    one switcher from adopting another's panel.
    """
    script = _strip_js_comments(STORYTELLING_JS.read_text(encoding="utf-8"))
    assert "document.getElementById(" in script, (
        "panels must be resolved by id, not by an interpolated CSS selector"
    )
    assert "switcher.contains(" in script, (
        "a resolved panel must still be inside the switcher that claims it"
    )
    assert not re.search(r"querySelector(?:All)?\(\s*[`\"']#", script), (
        "no selector may be built from an id at all"
    )
    assert not re.search(r"querySelector(?:All)?\([^)]*\$\{", script), (
        "no selector may interpolate author data"
    )


def test_controller_validates_every_panel_before_touching_any_state() -> None:
    """A switcher that cannot be driven must never be half-rewritten.

    The panel lookup ran inside the selection loop, so a switcher whose last
    tab pointed at a missing panel had already had `data-enhanced` set, two
    panels hidden and the tab strip revealed before the lookup threw — and
    the throw escaped the top-level loop, so every later switcher on the
    page stayed unenhanced too. Resolution now happens up front, and the
    enhancement hook is only set once the initial selection has succeeded.
    """
    script = _strip_js_comments(STORYTELLING_JS.read_text(encoding="utf-8"))
    enhance = script[script.index("const enhance") :]
    resolution = enhance.index("new Map(tabs.map(")
    first_select = enhance.index("select(", resolution)
    enhanced = enhance.index('switcher.dataset.enhanced = "true"')
    assert resolution < first_select < enhanced, (
        "panels must all resolve, then the initial selection must succeed, and "
        "only then may the switcher advertise itself as enhanced"
    )

    for mutation in ("panel.hidden = ", 'tab.setAttribute("aria-selected"', "tab.tabIndex = "):
        assert mutation in enhance, f"the enhancement still writes {mutation!r}"
        assert enhance.index(mutation) > resolution, (
            f"no switcher state may be written before every panel resolves: {mutation!r}"
        )


def test_controller_rolls_one_broken_switcher_back_and_keeps_going() -> None:
    """One bad switcher costs itself, not the rest of the page."""
    script = STORYTELLING_JS.read_text(encoding="utf-8")
    assert re.search(r"try \{\s*enhance\(switcher, tabs\);\s*\} catch", script), (
        "each switcher must be initialized independently inside its own try/catch"
    )
    rollback = script[script.index("const restoreFallback") : script.index("const enhance")]
    assert 'switcher.removeAttribute("data-enhanced")' in rollback, (
        "a rolled-back switcher must drop the hook that reveals its inert tab strip"
    )
    assert ".scene-panel" in rollback, (
        "the rollback must reach every scene panel, not only the selected one"
    )
    assert "panel.hidden = false" in rollback, (
        "every scene panel must be shown again, exactly as with no JavaScript"
    )
    assert 'tab.setAttribute("aria-selected", selected)' in rollback, (
        "the authored tab state must be restored, not left mid-selection"
    )
    assert "console.error(" in script, "the failure must be reported, never swallowed"

    design = (
        DOCS / "superpowers" / "specs" / "2026-08-22-visual-storytelling-design.md"
    ).read_text(encoding="utf-8")
    boundary = " ".join(design.lower().split())
    assert "restored to its no-javascript rendering" in boundary, (
        "the design document owns this boundary, so it must state that a switcher "
        "which cannot be enhanced falls back instead of shipping a broken one"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_switcher_controller_behaves_correctly_against_a_minimal_dom() -> None:
    """Run the shipped controller, unmodified, against a stub DOM.

    Reading the source proves the shape of the fix; only executing it proves
    the behaviour — that a broken switcher ends up in exactly the
    no-JavaScript state, that the next switcher still initializes, that
    posters are promoted on selection, that tab and off-screen pauses still
    happen, and that nothing ever calls `play()`.
    `tests/js/scene_switcher_harness.mjs` implements only the DOM surface the
    controller touches, so this needs no JavaScript dependency.
    """
    result = subprocess.run(
        ["node", str(SWITCHER_HARNESS)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout, result.stdout
    for scenario in (
        "healthy switchers enhance",
        "prototype-named keys are ignored",
        "left in the no-JavaScript state",
        "outside its own switcher is rejected",
        "without IntersectionObserver",
    ):
        assert scenario in result.stdout, f"the DOM harness must cover {scenario!r}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_shipped_controller_is_syntactically_valid_javascript() -> None:
    """A syntax error in the only shipped script is otherwise found by a visitor."""
    result = subprocess.run(
        ["node", "--check", str(STORYTELLING_JS)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_mcp_media_reserves_its_own_intrinsic_ratio_not_the_generic_16_9() -> None:
    """1280x710 is the MCP recording's real geometry, so its box must be 1280x710.

    Every other capture on this page is 1280x720, so both media rules
    reserve `16 / 9`. The MCP clip and poster are 1280x710 — the terminal
    geometry of the reviewed recording — and a 16:9 box is 4px taller than
    their content at full width: the `<video>` letterboxes inside a box that
    does not match it, and the poster tile's `object-fit: cover` crops the
    top or bottom of the client's own prompt. Both also declare
    `width="1280" height="710"`, so a 16:9 box makes the reserved space
    disagree with the attributes the browser lays out from.

    The two MCP elements therefore carry a class, and a class-qualified rule
    restates their real ratio with higher specificity than the generic
    element rules. That reservation is only truthful because the clip is
    encoded with square pixels — `tests/test_docs_visual_assets.py` reads the
    shipped MP4's own geometry boxes and fails if its display box ever stops
    matching the 1280x710 this rule reserves.
    """
    css = _strip_css_comments(_css())
    switcher = _scene_switcher()
    mosaic = _section('<section class="evidence-mosaic"', "</section>")

    mcp_video = re.search(r"<video[^>]*mcp-follow-demo\.mp4[^>]*>", switcher)
    assert mcp_video is not None, "the MCP scene keeps its video"
    assert 'class="mcp-media"' in mcp_video.group(0), (
        f"the MCP scene video must claim its own ratio class: {mcp_video.group(0)}"
    )

    mcp_tile = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "mcp-poster.png" in card
    )
    tile_image = re.search(r"<img[^>]*>", mcp_tile)
    assert tile_image is not None
    assert 'class="mcp-media"' in tile_image.group(0), (
        f"the MCP evidence tile image must claim its own ratio class: {tile_image.group(0)}"
    )
    assert 'width="1280" height="710"' in tile_image.group(0)

    ratio_rule = _rule(css, "video.mcp-media")
    assert "aspect-ratio: 1280 / 710" in ratio_rule, (
        "the MCP override must reserve the capture's real intrinsic ratio; found "
        f"{_compact(ratio_rule)!r}"
    )

    prelude = css[
        css.rindex("}", 0, css.index("video.mcp-media")) + 1 : css.index(
            "{", css.index("video.mcp-media")
        )
    ]
    for generic in (".md-typeset .scene-panel video", ".md-typeset .evidence-card img"):
        qualified = f"{generic}.mcp-media"
        assert qualified in prelude, (
            f"the override must qualify `{generic}` with the class so it wins on "
            f"specificity, not on source order alone; prelude was {_compact(prelude)!r}"
        )
        assert css.index(generic + " {") < css.index("video.mcp-media"), (
            f"the generic `{generic}` rule must come first so the MCP override "
            "cannot be undone by a later declaration"
        )

    assert "aspect-ratio: 16 / 9" in _rule(css, ".md-typeset .scene-panel video {"), (
        "the 1280x720 captures keep the generic 16:9 reservation"
    )
    assert "aspect-ratio: 16 / 9" in _rule(css, ".md-typeset .evidence-card img {")


def test_no_landing_media_declares_a_box_its_asset_cannot_fill() -> None:
    """Declared attributes and reserved CSS box must agree for every capture.

    The `width`/`height` attributes are what the browser reserves before an
    image loads; a CSS `aspect-ratio` that disagrees with them silently wins
    and re-crops or letterboxes the asset. This walks every landing image
    with declared dimensions and requires a 16:9 asset to sit under the
    generic rule and a non-16:9 asset to carry an override class.
    """
    css = _strip_css_comments(_css())
    for image in re.findall(r"<img[^>]*>", _index()):
        declared = re.search(r'width="(\d+)" height="(\d+)"', image)
        if declared is None:
            continue
        width, height = int(declared.group(1)), int(declared.group(2))
        if (width, height) == (1280, 720):
            assert "mcp-media" not in image, f"a 16:9 capture needs no override: {image}"
            continue
        assert (width, height) == (1280, 710), f"unexpected capture geometry: {image}"
        assert 'class="mcp-media"' in image, (
            f"a capture that is not 16:9 must reserve its own ratio: {image}"
        )
        assert f"aspect-ratio: {width} / {height}" in _rule(css, "video.mcp-media"), (
            "the override rule must reserve exactly the declared geometry"
        )


def test_visual_storytelling_plan_mcp_ratio_snippets_match_the_shipped_sources() -> None:
    """A plan replay must not restore the stretched 16:9 MCP box."""
    plan = _plan()
    scene_markup = _fenced_block_after(
        plan,
        "- [ ] **Step 3: Replace the numbered cards with complete scene markup**",
        "html",
    )
    shipped_scene = _section('<article id="scene-mcp"', "</article>")
    assert _compact(shipped_scene) in _compact(scene_markup), (
        "the plan's MCP scene snippet must be the shipped one, ratio class included"
    )

    mosaic_markup = _fenced_block_after(
        plan,
        "Delete the old “Find your flight path” list and add:",
        "html",
    )
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    shipped_tile = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "mcp-poster.png" in card
    )
    assert _compact(shipped_tile) in _compact(mosaic_markup), (
        "the plan's MCP evidence tile must carry the shipped ratio class"
    )

    assert "aspect-ratio: 1280 / 710" in plan, (
        "the plan's CSS must ship the MCP ratio override it tells contributors to build"
    )


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
        "watch-backed table",
        "fresh describe and log reads",
        "model / provider",
        "bounded fresh reads",
        "editor / external assistant",
        "active cluster context",
        "navigation semantics",
        "snapshots can differ",
    ):
        assert fact in lowered
    assert "same evidence" not in lowered


def test_human_lane_and_direct_scene_name_the_fresh_tui_reads_the_demo_shows() -> None:
    """The TUI lane is not one watch-backed snapshot from end to end.

    The Direct recording filters the watch-backed pod table, then presses
    Describe — which fetches the manifest and that object's events in
    `KorvidApp.action_describe` — and opens a live log stream in
    `KorvidApp._live_log_stream`. Labelling either the scene or the human
    contract lane "watch-backed TUI snapshot" hides the very freshness
    distinction this page exists to explain, and it makes the contract map
    factually incomplete by showing fresh reads only for agent and MCP.

    The fix must stay honest in the other direction too: korvid's own
    describe/log reads are not the agent's tool reads and not MCP's
    tool-specific reads, so the three lanes keep three distinct labels and
    none of them may collapse into one shared snapshot.
    """
    mixed = "watch-backed table + fresh describe and log reads"
    watch_only = "watch-backed tui snapshot"

    scene = _section('<article id="scene-direct"', "</article>")
    scene_evidence = re.search(r"<div><strong>Evidence</strong>\s*([^<]+)</div>", scene)
    assert scene_evidence is not None, "the Direct scene must keep an Evidence label"
    assert " ".join(scene_evidence.group(1).lower().split()) == mixed, (
        "the Direct scene shows a watch-backed table, a fresh Describe read, and a "
        f"live log stream; its Evidence label must say so, found: {scene_evidence.group(1)!r}"
    )

    contract = _section('<section class="contract-map"', "</section>")
    human_lane = re.search(
        r"<article><span>Human operator</span><strong>([^<]+)</strong></article>", contract
    )
    assert human_lane is not None, "the contract map must keep the human operator lane"
    assert " ".join(human_lane.group(1).lower().split()) == mixed, (
        "the human contract lane must carry the same mixed evidence claim as the "
        f"Direct scene, found: {human_lane.group(1)!r}"
    )

    truth = re.search(r'<p class="contract-map__truth">(.*?)</p>', contract, re.DOTALL)
    assert truth is not None, "the contract map must keep its freshness truth sentence"
    truth_text = " ".join(re.sub(r"<[^>]+>", " ", truth.group(1)).lower().split())
    assert watch_only not in truth_text, (
        "the truth sentence must not reduce the TUI to a single watch-backed "
        f"snapshot either, found: {truth_text!r}"
    )
    assert "snapshots can differ" in truth_text
    for shared_snapshot in ("same snapshot", "one snapshot", "identical"):
        assert shared_snapshot not in truth_text, (
            f"the lanes must not be described as sharing a snapshot: {shared_snapshot!r}"
        )

    index_lowered = _index().lower()
    assert watch_only not in index_lowered, (
        "no landing surface may still label the TUI's evidence as watch-backed only"
    )

    agent = _section('<article id="scene-agent"', "</article>")
    assert AGENT_SCENE_EVIDENCE in agent, (
        "the agent scene keeps its own distinct label — and that label describes "
        "the scripted capture it ships, not a bounded read it never performs"
    )
    mcp = _section('<article id="scene-mcp"', "</article>")
    assert "Tool-specific bounded fresh reads" in mcp, (
        "the MCP scene keeps its own distinct tool-specific bounded-fresh-read language"
    )
    assert "Bounded fresh reads over MCP" in contract


def test_agent_scene_describes_the_scripted_walkthrough_it_actually_captured() -> None:
    """The Agent scene ships a scripted panel walkthrough, not a real turn.

    `docs/demo/demo.py`'s `ScriptedAgentRuntime` discards both the prompt text
    and the screen context the panel hands it, contacts no provider, executes
    no read tool, and yields a hard-coded
    `ToolCallStarted`/`ToolCallFinished`/`TextDelta`/`TurnComplete` sequence
    with a fixed `[E1]` marker. The recording therefore proves korvid's real
    `AgentPanel` input, submission, and rendering path — and nothing about the
    provider, tool, or evidence pipeline. Labelling it "Bounded fresh reads +
    citations" with a "UI drive" result sold the capture as proof of exactly
    the pipeline it replaces.

    The scene stays linked to `agent/`, where the production behaviour is
    documented in full; only the claims made *about this media* are narrowed.
    """
    scene = _section('<article id="scene-agent"', "</article>")

    labels = dict(re.findall(r"<div><strong>(\w+)</strong>\s*([^<]+)</div>", scene))
    assert set(labels) == {"Input", "Evidence", "Result"}, (
        f"the Agent scene keeps its three visible labels; found {sorted(labels)}"
    )
    assert "AgentPanel" in labels["Input"], (
        "the recording types into and submits through the real AgentPanel input, "
        f"so the Input label may say so plainly; found {labels['Input']!r}"
    )
    assert labels["Evidence"].strip() == AGENT_SCENE_EVIDENCE, (
        "the Evidence label must name the scripted tool/citation events the "
        f"capture actually contains; found {labels['Evidence']!r}"
    )
    result = labels["Result"].lower()
    assert "rendering" in result, (
        f"the capture proves real panel rendering; found {labels['Result']!r}"
    )
    assert "ui drive" not in result, (
        "the scripted runtime drives nothing; the capture never shows the agent "
        f"navigating the TUI, found {labels['Result']!r}"
    )

    disclosure = re.search(r"<p>(.*?)</p>", scene, re.DOTALL)
    assert disclosure is not None, "the Agent scene must keep one body sentence"
    disclosure_text = " ".join(disclosure.group(1).lower().split())
    assert "does not execute" in disclosure_text, (
        "one concise sentence must disclose that the capture does not execute "
        f"korvid's provider/tool pipeline; found {disclosure.group(1)!r}"
    )
    for pipeline_word in ("provider", "tool"):
        assert pipeline_word in disclosure_text, (
            f"the disclosure must name the {pipeline_word} pipeline the capture "
            f"skips; found {disclosure.group(1)!r}"
        )

    described = [
        re.search(r'aria-label="([^"]+)"', scene),
        re.search(r"<video[^>]*>([^<]+)</video>", scene),
        re.search(r"<noscript><img[^>]*alt=\"([^\"]+)\"", scene),
    ]
    assert all(part is not None for part in described), (
        "the scene keeps an aria-label, an in-video fallback, and a noscript image"
    )
    for part in described:
        assert part is not None  # narrowed above; keeps mypy and the reader honest
        copy = part.group(1).lower()
        assert "scripted" in copy, (
            f"every description of this media must say it is scripted: {copy!r}"
        )
        assert "agentpanel" in copy or "agent panel" in copy or "agent input" in copy, (
            f"and must name the real AgentPanel it does capture: {copy!r}"
        )

    for overclaim in SCRIPTED_AGENT_OVERCLAIMS:
        assert not _unnegated(scene, overclaim), (
            f"the Agent scene must not claim {overclaim!r} from a scripted capture"
        )

    assert 'href="agent/"' in scene, (
        "narrowing the capture's claims must not cost the link to the real "
        "embedded-agent documentation"
    )


def test_agent_evidence_tile_claims_only_the_scripted_panel_rendering() -> None:
    """The mosaic tile named a product capability, not the frame it shows.

    "Agent with citations" plus "keep checkable evidence in the answer" reads
    as validated citation evidence. The frame is one scripted panel render:
    the typed prompt, a scripted `diagnose_pod` tool line, and a scripted
    answer carrying an `[E1]` marker that no evidence store ever validated.
    """
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    card = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "agent-poster.png" in card
    )

    caption = re.search(r"<figcaption>([^<]+)</figcaption>", card)
    assert caption is not None, "the agent tile keeps a caption"
    caption_text = caption.group(1).lower()
    assert "agent with citations" not in caption_text, (
        "the tile must not be named for a citation contract the capture never "
        f"exercised; found {caption.group(1)!r}"
    )
    for token in ("agent", "walkthrough"):
        assert token in caption_text, (
            f"the caption must name the agent panel walkthrough it shows: {caption.group(1)!r}"
        )

    alt = re.search(r'<img[^>]*alt="([^"]+)"', card)
    assert alt is not None
    alt_text = alt.group(1).lower()
    for signal in ("scripted", "prompt", "tool", "cit"):
        assert signal in alt_text, (
            f"the alt must describe the prompt, tool event, and cited answer this "
            f"scripted frame renders; {signal!r} missing from {alt.group(1)!r}"
        )

    body = re.search(r"<p>(.*?)</p>", card, re.DOTALL)
    assert body is not None
    body_text = " ".join(body.group(1).lower().split())
    assert "scripted" in body_text, (
        f"the tile's copy must say the capture is scripted; found {body.group(1)!r}"
    )
    for denial in ("live tool execution", "validated evidence"):
        assert denial in body_text, (
            f"the tile must explicitly deny {denial!r} rather than leave a visitor "
            f"to assume it; found {body.group(1)!r}"
        )

    for overclaim in SCRIPTED_AGENT_OVERCLAIMS:
        assert not _unnegated(card, overclaim), (
            f"the agent evidence tile must not claim {overclaim!r}"
        )


def test_agent_capture_surfaces_explain_the_flagged_scripted_citation() -> None:
    """The frame shows korvid's unsupported-citation warning; the copy must own it.

    `ScriptedAgentRuntime` mints no evidence, so its `[E1]` is reported in
    `TurnComplete.uncited` and the real panel renders its yellow
    "unsupported citation" note beneath the scripted answer. Publishing that
    frame while describing "a cited answer" would make korvid look broken in
    its own screenshot, so every capture-specific surface — the landing
    scene, the landing evidence tile and the `docs/agent.md` storyboard —
    says the marker is unsupported and that the panel flags it.

    The production turn flow beside the capture is untouched: real turns do
    validate citations, and that claim stays exactly as strong.
    """
    scene = _section('<article id="scene-agent"', "</article>")
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    tile = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "agent-poster.png" in card
    )
    agent_page = (DOCS / "agent.md").read_text(encoding="utf-8")
    storyboard = agent_page[
        agent_page.index('<section class="docs-storyboard"') : agent_page.index("</section>")
        + len("</section>")
    ]
    figure = storyboard[storyboard.index("<figure>") : storyboard.index("</figure>")]

    for name, surface in (("scene", scene), ("tile", tile), ("storyboard figure", figure)):
        flattened = _flatten(surface)
        assert "unsupported" in flattened, (
            f"the {name} must say the scripted marker is unsupported: {flattened!r}"
        )
        assert "e1" in flattened, f"the {name} must name the marker the panel flags"
        assert not _unnegated(surface, "cited answer"), (
            f"the {name} must not describe a flagged marker as a cited answer"
        )
        for overclaim in SCRIPTED_AGENT_OVERCLAIMS:
            assert not _unnegated(surface, overclaim), (
                f"the {name} must not claim {overclaim!r} from a scripted capture"
            )

    ordered_list = storyboard[storyboard.index("<ol>") :]
    assert "Evidence references remain selectable and validated." in ordered_list, (
        "the production turn flow must keep its validated-citation claim: only "
        "what the capture shows is narrowed"
    )
    assert "unsupported" not in ordered_list.lower(), (
        "the capture's flagged marker must not be written into the description "
        "of what a real turn does"
    )

    design = (
        DOCS / "superpowers" / "specs" / "2026-08-22-visual-storytelling-design.md"
    ).read_text(encoding="utf-8")
    lowered_design = " ".join(design.lower().split())
    assert "uncited" in lowered_design, (
        "the design must record which field the scripted harness reports, since "
        "that is what makes the frame's warning correct rather than a defect"
    )
    assert "unsupported citation" in lowered_design
    assert "the agent performs bounded reads" in lowered_design, (
        "the production capability statement stays exactly as strong"
    )


def test_agent_capture_never_presents_the_selected_row_as_grounding() -> None:
    """The demo's highlighted row is not context the scripted answer used.

    `ScriptedAgentRuntime` discards the screen context it is handed, so any
    resemblance between the selected pod and the scripted answer is
    coincidence. No capture-specific surface may present it as the grounding
    for the answer.
    """
    scene = _section('<article id="scene-agent"', "</article>")
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    tile = next(
        card
        for card in re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.S)
        if "agent-poster.png" in card
    )
    for surface in (scene, tile):
        flattened = _flatten(surface)
        for grounding in ("selected row", "selected pod", "screen context", "context grounding"):
            assert grounding not in flattened, (
                f"the capture must not offer {grounding!r} as evidence: {flattened!r}"
            )

    provenance = (DOCS / "demo" / "visual-storytelling.md").read_text(encoding="utf-8")
    section = provenance.split("## Embedded agent", 1)[1].split("\n## ", 1)[0]
    lowered = " ".join(section.lower().split())
    assert "selected row" in lowered, (
        "the provenance page must state plainly that the capture's selected row "
        "is not grounding for the scripted answer"
    )


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


def test_guarded_write_failure_arrow_keeps_readable_spacing() -> None:
    """Inline tags must not collapse the failure copy around the arrow."""
    pattern = re.compile(r"</strong>\s+<span aria-hidden=\"true\">→</span>\s+action blocked")
    for label, source in (("landing", _index()), ("plan", _plan())):
        assert pattern.search(source), f"{label} must render spaces around the visual arrow"


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


def test_visual_storytelling_plan_write_path_css_matches_the_shipped_rules() -> None:
    """The plan's write-path snippets must stay synced with the shipped source."""
    plan_css = _fenced_block_after(
        _plan(),
        "- [ ] **Step 4: Style the semantic diagrams without relying on color**",
        "css",
    )
    shipped_css = _css()

    plan_base = _rule(plan_css, ".md-typeset ol.write-path__stages {")
    shipped_base = _rule(shipped_css, ".md-typeset ol.write-path__stages {")
    assert _compact(plan_base) == _compact(shipped_base)

    plan_mobile = _media_blocks(plan_css, "@media (max-width: 799px)")
    shipped_mobile = _media_blocks(shipped_css, "@media (max-width: 799px)")
    assert len(plan_mobile) == len(shipped_mobile) == 1
    assert _compact(plan_mobile[0]) == _compact(shipped_mobile[0])


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


def test_korvid_figures_left_align_against_materials_centred_typeset_default() -> None:
    """Material centres every `figure`; korvid's evidence blocks must not inherit it.

    Material for MkDocs ships `.md-typeset figure { text-align: center }`, and
    `text-align` inherits. Measured at 1440px on the built site, that centred
    the `tui.md` pin legend's numbered list and every mosaic caption against
    left-aligned body copy around them — a legend whose markers and text
    disagree. Each korvid figure container therefore has to restate the
    alignment its own content assumes.
    """
    css = _css()
    for selector in (
        ".md-typeset .evidence-card figure",
        ".md-typeset .docs-visual,",
        ".md-typeset .docs-storyboard figure",
    ):
        block = _rule(css, selector)
        assert "text-align: left" in block, (
            f"`{selector.rstrip(',')}` must declare `text-align: left` so Material's "
            "centred `figure` default cannot reach its caption; found: "
            f"{' '.join(block.split())!r}"
        )


def test_korvid_figcaptions_defeat_materials_narrow_italic_caption_default() -> None:
    """`figcaption { font-style: italic; margin: 1em auto; max-width: 24rem }` must lose.

    Material's caption default is a 24rem (480px at the theme's 125% root)
    auto-centred italic column. On a 688px content column that rendered the
    concept-page captions and the `tui.md` legend as a narrow floating block
    beside their own full-width image, and italicised copy that is not an
    aside. Every korvid caption restates the box and the style; the hero
    keeps its own deliberate 34rem measure and is intentionally untouched
    here.
    """
    css = _css()
    for selector in (
        ".md-typeset .evidence-card figcaption {",
        ".md-typeset .docs-visual figcaption,",
    ):
        block = _rule(css, selector)
        for declaration in ("max-width: none", "font-style: normal"):
            assert declaration in block, (
                f"`{selector.rstrip(' {,')}` must declare `{declaration}`; found: "
                f"{' '.join(block.split())!r}"
            )
    concept = _rule(css, ".md-typeset .docs-visual figcaption,")
    assert "margin: 0.8rem 0 0" in concept, (
        "the concept-page caption must set all four margins so Material's "
        f"`margin: 1em auto` cannot re-centre it; found: {' '.join(concept.split())!r}"
    )
    card = _rule(css, ".md-typeset .evidence-card figcaption,")
    assert "margin-left: 1rem" in card, (
        "the mosaic caption stays aligned with the card's own gutter, not centred; "
        f"found: {' '.join(card.split())!r}"
    )
    hero = _rule(css, ".md-typeset .hero-demo figcaption")
    assert "max-width: 34rem" in hero, "the hero caption keeps its own deliberate measure"


def test_concept_visual_containers_keep_the_full_content_width() -> None:
    """Material's `figure { width: fit-content }` also reaches `.docs-visual`.

    `.docs-visual` *is* a `<figure>`, so the same shrink-wrap that broke the
    mosaic tiles applies here; only the image's own `width: 100%` currently
    keeps the box open. Restating the box makes the panel's width independent
    of whether its lazy image has decoded.
    """
    block = _rule(_css(), ".md-typeset .docs-visual,")
    for declaration in ("display: block", "width: 100%"):
        assert declaration in block, (
            f"the concept visual container must declare `{declaration}`; found: "
            f"{' '.join(block.split())!r}"
        )


def test_visual_storytelling_plan_relationship_snippet_matches_the_shipped_sources() -> None:
    """The plan embeds its markup and CSS verbatim, so drift reproduces old defects.

    `docs/superpowers/plans/2026-08-22-visual-storytelling.md` is written as
    an executable recipe. Its relationship-figure snippet still carried the
    pre-recapture alt naming only the ConfigMap dependency, and its
    `docs-visual` CSS predates the Material `figure`/`figcaption` overrides —
    a contributor following it would reintroduce both.
    """
    plan = _plan()
    relationships = (DOCS / "resource-relationships.md").read_text(encoding="utf-8")

    shipped_alt = re.search(r'relationship-graph\.png"[^>]*alt="([^"]+)"', relationships)
    assert shipped_alt is not None
    assert "dependency" in shipped_alt.group(1).lower()
    assert "service" in shipped_alt.group(1).lower(), (
        "the shipped capture proves a dependency and a dependent; its alt must say so"
    )
    assert f'alt="{shipped_alt.group(1)}"' in plan, (
        "the plan's relationship snippet must carry the alt the page ships, not the "
        f"pre-recapture one; expected {shipped_alt.group(1)!r}"
    )

    shipped_caption = re.search(r"<figcaption>(The two [^<]+)</figcaption>", relationships)
    assert shipped_caption is not None
    assert shipped_caption.group(1) in plan, (
        "the plan's relationship snippet must carry the shipped caption"
    )

    for declaration in ("text-align: left", "max-width: none", "font-style: normal"):
        assert declaration in plan, (
            f"the plan's core-visual CSS must include `{declaration}`, or following it "
            "reintroduces Material's centred italic caption defaults"
        )


def test_visual_storytelling_plan_scene_switcher_markup_matches_the_shipped_sources() -> None:
    plan_markup = _fenced_block_after(
        _plan(),
        "- [ ] **Step 3: Replace the numbered cards with complete scene markup**",
        "html",
    )
    shipped_switcher = _scene_switcher()

    for poster in ("agent-poster.png", "mcp-poster.png"):
        shipped = re.search(
            rf"<noscript><img[^>]*{re.escape(poster)}[^>]*></noscript>",
            shipped_switcher,
        )
        assert shipped is not None
        planned = re.search(
            rf"<noscript><img[^>]*{re.escape(poster)}[^>]*></noscript>",
            plan_markup,
        )
        assert planned is not None, f"the plan must keep the {poster} noscript poster snippet"
        assert 'loading="lazy"' in planned.group(0), (
            f"the plan's deferred {poster} noscript poster must stay lazy so the "
            "fallback matches the shipped source"
        )
        assert _compact(planned.group(0)) == _compact(shipped.group(0))


def test_visual_storytelling_plan_scene_controller_matches_the_shipped_script() -> None:
    """The plan's embedded controller must stay byte-identical to the shipped one.

    A contributor replaying `Step 5` verbatim must reproduce the exact script
    that ships today, including the off-screen pause behaviour, or the plan
    silently drifts from the checksum-pinned source it claims to build.
    """
    plan_script = _fenced_block_after(
        _plan(),
        "Create `docs/assets/javascripts/visual-storytelling.js` with LF endings and a\n"
        "final newline:",
        "javascript",
    )
    shipped_script = STORYTELLING_JS.read_text(encoding="utf-8")
    assert _compact(plan_script) == _compact(shipped_script)


def test_visual_storytelling_plan_contract_snippets_match_the_shipped_evidence_lanes() -> None:
    """The plan is an executable recipe, so its snippets carry the same claim.

    `docs/superpowers/plans/2026-08-22-visual-storytelling.md` embeds the
    scene markup, the contract-map markup, and the contract test verbatim. A
    contributor replaying those blocks would reintroduce the watch-only
    "Watch-backed TUI snapshot" label the shipped page no longer makes, so
    the plan has to move with `docs/index.md`.
    """
    plan = _plan()
    mixed = "Watch-backed table + fresh describe and log reads"

    scene_markup = _fenced_block_after(
        plan,
        "- [ ] **Step 3: Replace the numbered cards with complete scene markup**",
        "html",
    )
    assert f"<div><strong>Evidence</strong> {mixed}</div>" in scene_markup, (
        "the plan's Direct scene snippet must carry the shipped mixed evidence label"
    )
    assert f"<div><strong>Evidence</strong> {AGENT_SCENE_EVIDENCE}</div>" in scene_markup
    assert "<div><strong>Evidence</strong> Tool-specific bounded fresh reads</div>" in scene_markup

    contract_markup = _fenced_block_after(
        plan,
        "- [ ] **Step 3: Replace the long safety paragraph with the two visual flows**",
        "html",
    )
    shipped_contract = _section('<section class="contract-map"', "</section>")
    for lane in re.findall(
        r"<article><span>[^<]+</span><strong>[^<]+</strong></article>", shipped_contract
    ):
        assert lane in contract_markup, f"the plan's contract map must ship the lane {lane!r}"
    shipped_truth = re.search(
        r'<p class="contract-map__truth">.*?</p>', shipped_contract, re.DOTALL
    )
    assert shipped_truth is not None
    assert _compact(shipped_truth.group(0)) in _compact(contract_markup), (
        "the plan's contract map must carry the shipped freshness truth sentence"
    )

    contract_test = _fenced_block_after(
        plan,
        "Replace `test_safety_section_converges_every_actor_on_one_write_path` with:",
        "python",
    )
    for fact in ('"watch-backed table"', '"fresh describe and log reads"'):
        assert fact in contract_test, (
            f"the plan's embedded contract test must assert on {fact}, or replaying the "
            "plan reproduces the watch-only claim"
        )

    assert "watch-backed tui snapshot" not in plan.lower(), (
        "no plan snippet or constraint may still reduce the TUI's evidence to a "
        "watch-backed snapshot"
    )


def test_visual_storytelling_plan_agent_snippets_match_the_shipped_scripted_copy() -> None:
    """The plan is executable, so a replay must not restore the overclaim.

    `docs/superpowers/plans/2026-08-22-visual-storytelling.md` embeds the
    Agent scene, the agent evidence tile, the `docs/agent.md` storyboard and
    the provenance page verbatim. Whatever the shipped pages say about the
    scripted capture, those snippets have to say too, or the next contributor
    following the recipe reintroduces "Bounded fresh reads + citations" and
    "Agent with citations" for media that executes neither.
    """
    plan = _plan()

    scene_markup = _fenced_block_after(
        plan,
        "- [ ] **Step 3: Replace the numbered cards with complete scene markup**",
        "html",
    )
    shipped_scene = _section('<article id="scene-agent"', "</article>")
    assert _compact(shipped_scene) in _compact(scene_markup), (
        "the plan's Agent scene snippet must be the shipped Agent scene, "
        "scripted-capture labels and disclosure sentence included"
    )

    mosaic_markup = _fenced_block_after(
        plan,
        "Delete the old “Find your flight path” list and add:",
        "html",
    )
    shipped_mosaic = _section('<section class="evidence-mosaic"', "</section>")
    shipped_tile = next(
        card
        for card in re.findall(
            r'<article class="evidence-card[^"]*".*?</article>', shipped_mosaic, re.S
        )
        if "agent-poster.png" in card
    )
    assert _compact(shipped_tile) in _compact(mosaic_markup), (
        "the plan's agent evidence tile must carry the shipped caption, alt and copy"
    )

    storyboard_markup = _fenced_block_after(
        plan,
        "- [ ] **Step 5: Add the embedded-agent storyboard**",
        "html",
    )
    agent_page = (DOCS / "agent.md").read_text(encoding="utf-8")
    shipped_storyboard = agent_page[
        agent_page.index('<section class="docs-storyboard"') : agent_page.index("</section>")
        + len("</section>")
    ]
    assert _compact(shipped_storyboard) == _compact(storyboard_markup), (
        "the plan's storyboard snippet must be the shipped storyboard, including "
        "its scripted-capture caption and its separated production turn flow"
    )

    provenance = _fenced_block_after(plan, "## Embedded agent", "sh")
    assert "vhs docs/demo/agent.tape" in provenance
    plan_agent_prose = plan[plan.index("## Embedded agent") : plan.index("## Relationship graph")]
    lowered_prose = " ".join(plan_agent_prose.lower().split())
    assert "does not prove" in lowered_prose, (
        "the plan's provenance snippet must carry the same proves/does-not-prove "
        "boundary the shipped provenance page states"
    )

    for overclaim in ("Bounded fresh reads + citations", "Agent with citations"):
        assert overclaim not in plan, (
            f"no plan snippet may still ship {overclaim!r} for the scripted capture"
        )


def test_visual_storytelling_design_separates_agent_capability_from_the_capture() -> None:
    """The design doc must keep the product claim and the proof claim apart.

    Its scene description ("the agent performs bounded reads, cites evidence,
    and drives the TUI") and its mosaic criterion ("embedded-agent answers
    with validated citations") are true of the product and false of the media
    this branch ships. The document keeps the capability statement, and adds
    the rule the landing contract now enforces: the scripted capture is not
    evidence of the pipeline it scripts.
    """
    design = (
        DOCS / "superpowers" / "specs" / "2026-08-22-visual-storytelling-design.md"
    ).read_text(encoding="utf-8")
    lowered = " ".join(design.lower().split())

    assert "scripted" in lowered, (
        "the design must acknowledge that the agent media comes from a scripted runtime"
    )
    for rule in (
        "deterministic scripted agentpanel walkthrough",
        "not evidence of bounded fresh reads, live tool execution, or validated citations",
    ):
        assert rule in lowered, f"the design must state the capture rule: {rule!r}"

    assert "embedded-agent answers with validated citations" not in lowered, (
        "no mosaic criterion may require validated-citation evidence from a "
        "capture whose citations are hard-coded"
    )
    assert "the agent performs bounded reads" in lowered, (
        "the production capability statement stays: only the claim made about "
        "the capture is narrowed"
    )


def test_visual_storytelling_plan_evidence_markup_matches_the_shipped_sources() -> None:
    plan_markup = _fenced_block_after(
        _plan(),
        "Delete the old “Find your flight path” list and add:",
        "html",
    )
    shipped_mosaic = _section('<section class="evidence-mosaic"', "</section>")
    shipped_links = re.findall(
        r'<a class="evidence-card__full" href="([^"]+)"([^>]*)>\s*<img[^>]*alt="([^"]+)"',
        shipped_mosaic,
        re.DOTALL,
    )
    planned_links = re.findall(
        r'<a class="evidence-card__full" href="([^"]+)"([^>]*)>\s*<img[^>]*alt="([^"]+)"',
        plan_markup,
        re.DOTALL,
    )
    assert len(planned_links) == len(shipped_links) == 6
    for shipped, planned in zip(shipped_links, planned_links, strict=True):
        assert planned[0] == shipped[0]
        assert planned[2] == shipped[2]
        assert "aria-label" not in planned[1], (
            "the plan's evidence links must let the nested image alt remain the "
            f"accessible name instead of overriding it: {planned[1]!r}"
        )


def test_visual_storytelling_plan_evidence_css_matches_the_shipped_rules() -> None:
    plan_css = _fenced_block_after(
        _plan(),
        "- [ ] **Step 4: Add the evidence mosaic and destination-card CSS**",
        "css",
    )
    shipped_css = _css()

    figure = _rule(plan_css, ".md-typeset .evidence-card figure")
    shipped_figure = _rule(shipped_css, ".md-typeset .evidence-card figure")
    assert _compact(figure) == _compact(shipped_figure)
    for declaration in ("display: block", "width: 100%", "margin: 0", "text-align: left"):
        assert declaration in figure, (
            "the plan's `.evidence-card figure` snippet must reserve the full tile box "
            f"before lazy images load; missing `{declaration}` in {' '.join(figure.split())!r}"
        )

    caption_gutter = _rule(plan_css, ".md-typeset .evidence-card figcaption,")
    shipped_caption_gutter = _rule(shipped_css, ".md-typeset .evidence-card figcaption,")
    assert _compact(caption_gutter) == _compact(shipped_caption_gutter)
    for declaration in ("margin-right: 1rem", "margin-left: 1rem"):
        assert declaration in caption_gutter, (
            "the plan's figcaption gutter must match the shipped card layout; missing "
            f"`{declaration}` in {' '.join(caption_gutter.split())!r}"
        )

    figcaption = _rule(plan_css, ".md-typeset .evidence-card figcaption {")
    shipped_figcaption = _rule(shipped_css, ".md-typeset .evidence-card figcaption {")
    assert _compact(figcaption) == _compact(shipped_figcaption)
    for declaration in (
        "margin-top: 0.85rem",
        "max-width: none",
        "font-style: normal",
    ):
        assert declaration in figcaption, (
            "the plan's `.evidence-card figcaption` rule must match the shipped source; "
            f"missing `{declaration}` in {' '.join(figcaption.split())!r}"
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

    The link must not carry its own `aria-label`: an accessible name on the
    `<a>` wins over the nested `<img alt>`, so a generic "open the
    full-resolution … capture" label would replace the one description that
    actually carries the evidence for a screen-reader visitor. The image's
    alt names the link instead.
    """
    mosaic = _section('<section class="evidence-mosaic"', "</section>")
    cards = re.findall(r'<article class="evidence-card[^"]*".*?</article>', mosaic, re.DOTALL)
    assert len(cards) == 6
    for card in cards:
        image = re.search(r'<img src="(assets/scenes/[^"]+)"', card)
        assert image is not None
        link = re.search(
            r'<a class="evidence-card__full" href="([^"]+)"([^>]*)>\s*<img[^>]*alt="([^"]+)"',
            card,
        )
        assert link is not None, (
            f"the capture in this tile must be a link to its own full-resolution "
            f"asset: {card[:120]!r}"
        )
        assert link.group(1) == image.group(1), "the link must open the very asset the tile renders"
        assert "aria-label" not in link.group(2), (
            "the link must let its nested image's descriptive alt be the accessible "
            f"name instead of overriding it: {link.group(0)[:160]!r}"
        )
        assert len(link.group(3).split()) >= 8, (
            "that alt is now the link's accessible name, so it has to describe the "
            f"capture, not label a control: {link.group(3)!r}"
        )


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


def test_visual_storytelling_design_matches_the_shipped_media_and_provenance() -> None:
    """The visual design doc must not outlive the media the PR actually ships.

    Three of its requirements went stale against the checked-in captures:

    1. the cockpit poster comes from an in-memory fixture with no metrics
       source, so every CPU/MEM column renders an em-dash — the landing
       contract in `test_evidence_copy_claims_only_what_its_capture_actually_shows`
       forbids selling that frame as utilization evidence, and the design
       must not require it either;
    2. `merged-logs.png` is the single-pod `l` view, which
       `test_single_pod_log_evidence_is_not_labelled_as_a_merged_stream`
       pins as a stream rather than a merge; and
    3. only the MCP scene comes from a disposable local cluster — the base,
       agent, relationship, diagnosis, and log frames come from the
       deterministic in-memory harness in `docs/demo/demo.py`, so a
       disposable-cluster-only rule contradicts the checked-in provenance
       and the landing page's own "synthetic or disposable" wording.

    Its privacy and real-UI requirements are not relaxed by any of that, so
    they are pinned here alongside.
    """
    design = (
        DOCS / "superpowers" / "specs" / "2026-08-22-visual-storytelling-design.md"
    ).read_text(encoding="utf-8")
    lowered = " ".join(design.lower().split())

    for claim in ("live utilization", "status and live utilization", "utilisation"):
        assert claim not in lowered, (
            "the demo has no metrics source, so no design requirement may promise "
            f"{claim!r} from the shipped cockpit capture"
        )
    assert "as evidence of utilization" in lowered, (
        "the design must state the prohibition the landing contract already "
        "enforces: a harness capture is not utilization evidence"
    )
    assert "resource browsing with status, scope, and restart signals" in lowered, (
        "the mosaic's cockpit criterion must name the signals the capture shows"
    )

    assert "merged, filtered logs" not in lowered, (
        "the shipped log capture is a single-pod stream, not the merged view"
    )
    assert "a filtered, live single-pod log stream" in lowered, (
        "the mosaic criterion must describe the single-pod live log stream that ships"
    )

    assert "disposable local cluster" in lowered, (
        "the MCP capture really does come from a disposable local cluster"
    )
    assert lowered.count("in-memory synthetic harness") >= 2, (
        "both the mosaic capture rule and the asset-production rule must accept "
        "the deterministic in-memory harness that produced most of the media"
    )
    for anchor in (
        "use real screens captured from the deterministic in-memory synthetic harness",
        "reproducible demo scenarios against the deterministic in-memory synthetic harness",
    ):
        assert anchor in lowered, f"the design must state the supported provenance: {anchor!r}"

    assert "real screens" in lowered, "captures must still come from the real product UI"
    assert "no runtime third-party" in lowered, "the privacy requirement stays intact"
    assert "no capture may contain a real cluster" in lowered, (
        "the design must keep the privacy rule that no capture shows real cluster data"
    )
