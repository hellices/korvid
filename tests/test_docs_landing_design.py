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

#: The Evidence label the Agent scene ships. `build_demo_agent_runtime` in
#: `docs/demo/agent_story.py` wires korvid's own `AgentRuntime` to the real
#: `ToolExecutor` over the synthetic `DemoReadOps` fixture, so the capture's
#: reads are the product's real read tools — the deterministic part is the
#: cluster they read and the offline provider that chose them.
AGENT_SCENE_EVIDENCE = "Real read tools over a deterministic synthetic cluster"

#: Claims no surface built on `agent-poster.png`/`agent-demo.mp4` may make.
#: The recording runs the real runtime, executor and evidence ledger, so the
#: read path is no longer the overclaim; the model and the cluster are.
#: `DemoAgentProvider` opens no socket and always chooses the same two tool
#: calls, and every byte those tools read is a fixture — so nothing here may
#: be sold as a live model, a live cluster, or an answer-quality result.
AGENT_CAPTURE_OVERCLAIMS = (
    "live model",
    "live provider",
    "live cluster",
    "real cluster",
    "production cluster",
    "model quality",
    "answer quality",
)

#: Phrases every capture-specific Agent surface must state, so a visitor is
#: told both halves of the boundary: the pipeline is real, the model and the
#: cluster are not. Compared against `_flatten`, which folds hyphens.
AGENT_CAPTURE_DISCLOSURES = (
    "deterministic synthetic cluster walkthrough",
    "real read tools",
    "not a live model",
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
        Hyphens and en-dashes inside a claim are folded to spaces, because
        hyphenation is a writer's choice and must never decide whether a
        claim is scanned: "a live-model quality claim" is the same statement
        as "a live model quality claim".
    """
    attribute_values = [html.unescape(match.group(1)) for match in _ATTR_VALUE.finditer(text)]
    body = re.sub(r"<[^>]+>", " ", text)
    combined = ". ".join([body, *attribute_values])
    return " ".join(re.sub(r"[-\u2010-\u2013]", " ", combined.lower()).split())


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


def test_unnegated_folds_hyphenation_so_it_cannot_launder_a_claim() -> None:
    """`live-model` and `live model` are the same claim to a reader."""
    assert _unnegated("<p>This capture is a live-model quality claim.</p>", "live model"), (
        "a hyphenated overclaim must still be flagged"
    )
    assert not _unnegated("<p>Not a live-model quality claim.</p>", "live model"), (
        "and its truthful negation must still be accepted"
    )


def test_unnegated_accepts_the_shipped_agent_scene_and_caption_copy() -> None:
    """Regression pin: the real Agent panel and its stage caption stay clean."""
    scene = _section('<article id="scene-agent"', "</article>")
    caption = _stage_caption()
    for overclaim in AGENT_CAPTURE_OVERCLAIMS:
        assert not _unnegated(scene, overclaim), (
            f"the shipped Agent panel must not claim {overclaim!r}"
        )
        assert not _unnegated(caption, overclaim), (
            f"the shipped stage caption must not claim {overclaim!r}"
        )


def _index() -> str:
    return INDEX.read_text(encoding="utf-8")


def _css() -> str:
    return EXTRA_CSS.read_text(encoding="utf-8")


def _plan() -> str:
    return VISUAL_STORYTELLING_PLAN.read_text(encoding="utf-8")


def _hero() -> str:
    """The hero section, which is also the page's only scene switcher."""
    return _section('<section class="hero hero--drivers"', "</section>")


def _scene_switcher() -> str:
    """The merged driver stage: the tab strip, the three panels, the caption."""
    return _section('<figure class="hero-demo hero-driver-stage"', "</figure>")


def _highlights() -> str:
    return _section('<section class="feature-highlights"', "</section>")


def _stage_caption() -> str:
    """The one caption that speaks for all three recordings' provenance."""
    caption = re.search(r"<figcaption>.*?</figcaption>", _scene_switcher(), re.DOTALL)
    assert caption is not None, "the driver stage must keep its shared provenance caption"
    return caption.group(0)


def _highlight(label: str) -> str:
    """One SEE / GROUND / CONTROL card of the highlights section."""
    cards = re.findall(r"<article>.*?</article>", _highlights(), re.DOTALL)
    matching = [card for card in cards if f">{label}<" in card]
    assert len(matching) == 1, f"exactly one highlight must carry the {label!r} promise"
    return matching[0]


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
    hero = _hero()
    assert 'class="hero-demo hero-driver-stage"' in hero, (
        "the hero's media column is now the driver stage itself, so the hero keeps "
        "its media box and gains the stage hook the compact CSS bounds"
    )
    assert "data-scene-switcher" in hero, (
        "the merged hero is the switcher root the controller enhances"
    )
    assert 'src="assets/demo.mp4"' in hero
    assert 'poster="assets/scenes/cockpit-poster.png"' in hero
    assert "hero-panel" not in hero


def test_hero_media_is_controllable_and_has_a_text_fallback() -> None:
    hero = _hero()
    video = re.search(r"<video\b[^>]*>", hero)
    assert video is not None
    hero_video = video.group(0)
    assert "aria-label=" in hero_video or "aria-labelledby=" in hero_video, (
        "the hero video must have an accessible name"
    )
    for attribute in ("controls", "muted", "loop", "playsinline"):
        assert re.search(rf"\b{attribute}\b", hero_video)
    assert 'preload="metadata"' in hero_video
    assert not re.search(r"(?<!data-)\bautoplay\b", hero_video)
    assert "data-autoplay-video" not in hero, (
        "the merged stage is driven by the scene-switcher half of the controller; a "
        "second `data-autoplay-video` hook would observe the same player twice"
    )
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
    hero = _hero()
    heading = hero.find('class="hero-heading"')
    demo = hero.find('class="hero-demo hero-driver-stage"')
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


def test_visual_storytelling_plan_is_marked_superseded_for_the_landing_structure() -> None:
    """The old plan is executable prose, so it must say what it no longer builds.

    `docs/superpowers/plans/2026-08-22-visual-storytelling.md` embeds the
    hero figure, the per-scene Input/Evidence/Result rows, the contract map,
    the write path and the six-card mosaic verbatim. The compact homepage
    deletes all five, so a contributor replaying those blocks would rebuild
    the long, repetitive page this change removes. The plan therefore has to
    point at the plan and design that replaced it, while staying the source
    of record for the media and controller the homepage still ships.
    """
    plan = _plan()
    header = plan[: plan.index("**For agentic workers:**")].lower()
    assert "superseded" in header, (
        "the plan must announce that its landing structure was replaced, before "
        "a reader reaches the first executable step"
    )
    for pointer in (
        "2026-08-27-compact-homepage.md",
        "2026-08-27-compact-homepage-design.md",
    ):
        assert pointer in plan, f"the superseded plan must name {pointer}"
    for retired in ("contract map", "write path", "evidence mosaic"):
        assert retired in header, (
            f"the note must name the {retired} among the blocks that are now history"
        )

    index = _index()
    for removed in ('class="contract-map', 'class="write-path', 'class="evidence-mosaic'):
        assert removed not in index, (
            f"{removed!r} is exactly what the supersede note says the homepage dropped"
        )


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


# --- the compact homepage is one media story --------------------------------


def test_homepage_is_one_media_story_not_repeated_sections() -> None:
    """The homepage must show the product once, not narrate it four times.

    The long page repeated its own evidence: the Direct recording played in
    both the hero and the switcher, the switcher restated in prose what its
    videos already showed, and the contract map, write path and six-card
    mosaic re-explained the same contract a third and fourth time. The
    compact page keeps one media stage, three highlights and one destination
    nav — so the counts are pinned here, where a re-expansion would show up
    before anyone re-reads a screenshot.
    """
    source = _index()
    assert len(source.split()) < 800, (
        f"the homepage must stay short; found {len(source.split())} words"
    )
    assert source.count("<video") == 3, "one recording per driver, and no duplicate"
    assert source.count('src="assets/demo.mp4"') == 1, (
        "the Direct recording is authored once, in the merged hero stage"
    )
    assert source.count("<section") + source.count("<nav") <= 3, (
        "at most three major blocks: the hero stage, the highlights, the destinations"
    )
    for removed in ("contract-map", "write-path", "evidence-mosaic"):
        assert f'class="{removed}' not in source, (
            f"{removed!r} restated what the recordings and the focused guides "
            "already say; it must not come back"
        )


def test_homepage_highlights_the_three_product_promises() -> None:
    """The three highlights carry the product contract the removed prose did.

    Compacting the page must not drop the claims it exists to make, so the
    SEE / GROUND / CONTROL cards keep keyboard-first operation, bounded
    evidence, the fresh approval keystroke, and the fail-closed audit.
    """
    source = _index()
    for label in ("SEE", "GROUND", "CONTROL"):
        assert f">{label}<" in source, f"the highlights must keep the {label} promise"
    for claim in ("Keyboard", "Bounded", "Fresh approval", "Fail-closed audit"):
        assert claim.lower() in source.lower(), (
            f"the compact page must still make the {claim!r} claim"
        )


# --- 4. one incident, three drivers -----------------------------------------


def test_landing_presents_one_incident_through_three_drivers() -> None:
    """One stage, three tabs — the tabs are what name the drivers now.

    The compact homepage deletes the second heading and the per-scene prose,
    so the driver story is carried entirely by the tab strip and the three
    recordings behind it. Each tab must still name its driver and address
    its own panel.
    """
    switcher = _scene_switcher()
    for scene, label in (
        ("direct", "You drive"),
        ("agent", "Agent delegates"),
        ("mcp", "MCP connects"),
    ):
        assert f'id="scene-tab-{scene}"' in switcher
        assert f'aria-controls="scene-{scene}"' in switcher
        assert f'id="scene-{scene}"' in switcher
        assert f">{label}</button>" in switcher, (
            f"the {scene} tab must keep naming its driver: {label!r}"
        )
    assert "same evidence" not in switcher.lower()
    assert "One incident. Three ways to drive it." not in _index(), (
        "the merged stage replaces the second heading; the hero headline leads alone"
    )


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


def test_deferred_scene_posters_keep_a_local_lazy_fallback_image() -> None:
    """A visitor without JavaScript must still see each scene's real frame.

    The fallback must be real DOM so it also covers a controller-load failure,
    not only a browser with JavaScript disabled. Both images sit below the fold
    and must defer their bytes exactly like the mosaic captures do.
    """
    switcher = _scene_switcher()
    fallbacks = re.findall(r'<img class="scene-panel__fallback[^"]*"[^>]+>', switcher)
    assert len(fallbacks) == 2, "exactly the two deferred scenes need a fallback poster image"
    assets = []
    for fallback in fallbacks:
        match = re.search(r'src="([^"]+)"[^>]*alt="[^"]+"', fallback)
        assert match is not None, f"the fallback must be one described local image: {fallback!r}"
        source = match.group(1)
        assert source.startswith("assets/"), "no-JS fallbacks must stay local, never remote"
        assert 'loading="lazy"' in fallback, (
            "a fallback poster renders below the fold, so it must "
            f"defer its own bytes like every other below-fold capture: {fallback!r}"
        )
        assets.append(source)
    assert sorted(assets) == [
        "assets/scenes/agent-poster.png",
        "assets/scenes/mcp-poster.png",
    ]


def test_deferred_scenes_keep_media_when_the_controller_never_runs() -> None:
    """Always-parsed fallbacks must cover no-JS and controller-load failure."""
    switcher = _scene_switcher()
    assert "<noscript>" not in switcher
    fallbacks = re.findall(r'<img class="scene-panel__fallback[^"]*"[^>]+>', switcher)
    assert len(fallbacks) == 2
    assert all('loading="lazy"' in fallback for fallback in fallbacks)

    css = _css()
    hidden_video = ".md-typeset .scene-panel video[data-poster] {"
    restored_video = ".md-typeset .scene-panel video:not([data-poster]) + .scene-panel__fallback {"
    assert "display: none" in _rule(css, hidden_video)
    assert "display: none" in _rule(css, restored_video)


def test_no_javascript_fallback_replaces_deferred_videos_with_posters() -> None:
    """No-JS rendering must show one media surface per deferred scene."""
    selector = ".md-typeset .scene-panel video[data-poster] {"
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

    A visitor who scrolls the whole switcher out of the viewport must not
    keep its video decoding indefinitely — nothing else watches the
    switcher's own visibility. The controller must observe each
    `[data-scene-switcher]`'s intersection with the viewport, and when it
    stops intersecting, pause every video inside it (selected or not).
    Playback may resume only for the selected scene, and only when the
    switcher is visible again and `prefers-reduced-motion` allows it.
    `IntersectionObserver` support must be feature-detected so its absence
    cannot break the switcher.
    """
    script = STORYTELLING_JS.read_text(encoding="utf-8")
    assert re.search(r'typeof IntersectionObserver === "function"', script), (
        "IntersectionObserver support must be feature-detected so unsupported "
        "browsers still get a working — if never-autoplaying — switcher"
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
    assert "promoteVideo(video)" in rollback, (
        "promoting `data-poster` is what uncovers the `<video>` and hides the "
        "`.scene-panel__fallback` image, so the rollback must promote `data-src` "
        "in the same pass or it replaces a real product frame with a dead player"
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


def test_controller_obeys_a_reduced_motion_preference_turned_on_mid_visit() -> None:
    """Round-6 review: the preference was only ever read *before* a play call.

    `matchMedia("(prefers-reduced-motion: reduce)").matches` was evaluated
    inside `startFromBeginning`, so a visitor who turned the preference on
    while a hero or scene was already playing kept watching it — the
    controller had no way to hear about the change and nothing to pause. One
    shared `MediaQueryList` is built for the page, every video the controller
    may start is registered, and a `change` subscription pauses all of them
    the moment `matches` becomes true. Relaxing the preference must not
    resume anything: motion may only come back through an ordinary
    visibility or selection event, or the native controls.
    """
    script = _strip_js_comments(STORYTELLING_JS.read_text(encoding="utf-8"))

    assert script.count('matchMedia("(prefers-reduced-motion: reduce)")') == 1, (
        "one MediaQueryList must be shared by the whole page; a throwaway query "
        "per play attempt can never deliver a change event to anybody"
    )
    assert re.search(r'typeof matchMedia === "function"', script), (
        "a browser without matchMedia states no preference and must keep its motion"
    )
    assert re.search(r'typeof \w+\.addEventListener === "function"', script), (
        "a MediaQueryList without addEventListener must not break the controller"
    )

    assert "managedVideos" in script, "the controller must track what it may start"
    assert re.search(r"managedVideos\.add\(", script), "hero and scene videos must be registered"
    assert re.search(r"for \(const video of managedVideos\) video\.pause\(\);", script), (
        "the registry exists so every managed video can be paused at once"
    )

    subscription = script.index('addEventListener("change"')
    handler = script[subscription : script.index("});", subscription) + 3]
    assert re.search(r"\.matches", handler), "the handler must branch on the new preference value"
    assert re.search(r"pause\w*\(", handler), (
        "turning the preference on must pause what is already playing"
    )
    assert not re.search(r"\bplay\(|startFromBeginning", handler), (
        "the controller must never start playback from a preference change; turning "
        "`reduce` back off is not a request for motion"
    )


def test_controller_withholds_autoplay_where_visibility_is_unknown() -> None:
    """Round-6 review: no `IntersectionObserver` meant "assume on screen".

    Playback is a visible-only contract. The switcher initialised
    `switcherVisible` to `true` where `IntersectionObserver` was missing and
    the hero started immediately in the same case, so exactly the browsers
    that cannot report visibility were the ones that autoplayed unconditionally
    — decoding video a visitor may never scroll to, and never pausing it.
    Unknown visibility must withhold autoplay instead; the poster, the native
    controls, and the whole tab strip still work.
    """
    script = _strip_js_comments(STORYTELLING_JS.read_text(encoding="utf-8"))
    switcher_loop = script.index('document.querySelectorAll("[data-scene-switcher]")')
    enhance_block = script[script.index("const enhance") : switcher_loop]

    assert "let switcherVisible = false;" in enhance_block, (
        "a switcher whose visibility has not been reported yet is not visible"
    )
    assert 'typeof IntersectionObserver !== "function"' not in enhance_block, (
        "the absence of an observer must not be turned into an assumption of "
        "visibility; the only supported branch is 'observe when we can'"
    )
    assert "if (switcherVisible) startFromBeginning(selectedVideo);" in enhance_block, (
        "selection may still start a scene, but only in a switcher known to be visible"
    )

    hero_block = script[script.index('document.querySelectorAll("[data-autoplay-video]")') :]
    observer_at = hero_block.index("new IntersectionObserver(")
    starts = [match.start() for match in re.finditer(r"startFromBeginning\(", hero_block)]
    assert starts, "the hero must still start when it is reported visible"
    assert all(at > observer_at for at in starts), (
        "the hero may only be started from an intersection callback: without an "
        "observer its visibility is unknown, and unknown must not autoplay"
    )


def test_controller_leaves_modified_navigation_keys_to_the_browser() -> None:
    """Round-6 review: `Alt+ArrowLeft` was swallowed as tab navigation.

    The keydown handler matched on `event.key` alone, so a tab strip that
    happened to have focus consumed browser and OS commands built on the very
    same keys — `Alt+ArrowLeft`/`Alt+ArrowRight` (history back/forward),
    `Ctrl/Cmd+Home`/`Ctrl/Cmd+End` (jump to the ends of the document),
    `Shift+Arrow` (extend a selection) — calling `preventDefault` on them and
    switching scenes instead. The guard has to run before `preventDefault`,
    or the command is already lost by the time the key is recognised.
    """
    script = _strip_js_comments(STORYTELLING_JS.read_text(encoding="utf-8"))
    handler_at = script.index('addEventListener("keydown"')
    before_prevent = script[handler_at : script.index("preventDefault", handler_at)]

    for modifier in ("altKey", "ctrlKey", "metaKey", "shiftKey"):
        assert f"event.{modifier}" in before_prevent, (
            f"a {modifier} chord is a browser or OS command; the handler must check "
            "it before it prevents anything"
        )
    assert re.search(r"if \([^)]*altKey[^)]*\)\s*return;", before_prevent), (
        "a modified key must leave the handler untouched, not fall through to selection"
    )
    assert "event.preventDefault();" in script, (
        "unmodified arrow/Home/End navigation must still be claimed by the tab strip"
    )
    guard_at = before_prevent.index("altKey")
    assert guard_at < before_prevent.index("keys.get("), (
        "the modifier guard must precede key recognition and selection entirely"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_switcher_controller_behaves_correctly_against_a_minimal_dom() -> None:
    """Run the shipped controller, unmodified, against a stub DOM.

    Reading the source proves the shape of the fix; only executing it proves
    the behaviour — that a broken switcher ends up in exactly the
    no-JavaScript state (every panel visible, every revealed video holding a
    real source rather than a dead player), that the next switcher still
    initializes, that posters are promoted on selection, that tab and
    off-screen pauses still happen, that a visible switcher starts and
    restarts the selected scene, that `prefers-reduced-motion` suppresses that
    autoplay — including a preference turned on mid-visit, which must pause
    every managed video at once and never resume one when it is turned back
    off — that a modified `Alt`/`Ctrl`/`Cmd`/`Shift` chord keeps its browser
    behaviour, and that a browser-blocked `play()` promise never rolls the
    switcher back. The standalone `[data-autoplay-video]` hero has no tab strip
    and therefore no rollback of its own, so it is covered separately: it must
    start from zero on entry, pause on exit, restart on return, stay still
    under reduced motion, and stay paused where `IntersectionObserver` does not
    exist — unknown visibility is not visibility.
    `tests/js/scene_switcher_harness.mjs` implements only the
    DOM surface the controller touches, so this needs no JavaScript dependency.
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
        "starts the selected scene and restarts",
        "prefers-reduced-motion suppresses autoplay",
        "turning on reduced motion mid-visit pauses every managed video",
        "a reduced-motion flip is still honored",
        "MediaQueryList has no addEventListener",
        "without matchMedia",
        "rejected play() promise is swallowed",
        "prototype-named keys are ignored",
        "modified arrow and Home/End keys keep their browser behavior",
        "left in the no-JavaScript state",
        "outside its own switcher is rejected",
        "without IntersectionObserver gets a working switcher that never autoplays",
        "a hero without IntersectionObserver stays paused",
        "a standalone hero plays only while on screen",
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

    mcp_video = re.search(r"<video[^>]*mcp-follow-demo\.mp4[^>]*>", switcher)
    assert mcp_video is not None, "the MCP scene keeps its video"
    assert 'class="mcp-media"' in mcp_video.group(0), (
        f"the MCP scene video must claim its own ratio class: {mcp_video.group(0)}"
    )

    mcp_fallback = re.search(r"<img[^>]*mcp-poster\.png[^>]*>", switcher)
    assert mcp_fallback is not None, "the MCP scene keeps its no-JavaScript poster"
    assert re.search(r'class="[^"]*\bmcp-media\b[^"]*"', mcp_fallback.group(0)), (
        f"the MCP fallback image must claim its own ratio class: {mcp_fallback.group(0)}"
    )
    assert 'width="1280" height="710"' in mcp_fallback.group(0)

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
    generic = ".md-typeset .scene-panel video"
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
        assert re.search(r'class="[^"]*\bmcp-media\b[^"]*"', image), (
            f"a capture that is not 16:9 must reserve its own ratio: {image}"
        )
        assert f"aspect-ratio: {width} / {height}" in _rule(css, "video.mcp-media"), (
            "the override rule must reserve exactly the declared geometry"
        )


def test_visual_storytelling_plan_mcp_ratio_snippets_match_the_shipped_sources() -> None:
    """A plan replay must not restore the stretched 16:9 MCP box.

    The compact homepage drops the plan's per-scene copy and its evidence
    mosaic, so only the media elements are still comparable — and they are
    exactly the parts that carry the ratio class the override depends on.
    """
    plan = _plan()
    scene_markup = _fenced_block_after(
        plan,
        "- [ ] **Step 3: Replace the numbered cards with complete scene markup**",
        "html",
    )
    shipped_scene = _section('<article id="scene-mcp"', "</article>")
    for element in re.findall(r"<(?:video|img)\b[^>]*>", shipped_scene):
        assert _compact(element) in _compact(scene_markup), (
            "the plan's MCP scene media must stay the shipped element, ratio class "
            f"included; {element!r} is not in the plan"
        )

    assert "aspect-ratio: 1280 / 710" in plan, (
        "the plan's CSS must ship the MCP ratio override it tells contributors to build"
    )


def test_scene_videos_never_autoplay_and_below_fold_media_preloads_nothing() -> None:
    """Bandwidth and motion are the visitor's choice on every landing video.

    No `<video>` may declare the native `autoplay` attribute: playback is
    driven entirely by the visibility-aware controller, gated on
    `prefers-reduced-motion`, and never by the browser's own eager-fetch
    behavior. The merged stage is one switcher, so the Direct clip is the
    page's single eager medium and keeps its real `src`; the Agent and MCP
    clips defer their bytes behind `data-src` until the controller promotes
    them on selection, so an unselected driver never downloads video before
    a visitor picks it.
    """
    videos = re.findall(r"<video[^>]*>", _index())
    assert len(videos) == 3, "one video per driver, authored once each"
    for video in videos:
        assert not re.search(r"(?<!data-)\bautoplay\b", video), (
            f"no landing video may declare the native autoplay attribute: {video}"
        )
    direct_video, *deferred_scene_videos = videos
    assert 'src="assets/demo.mp4"' in direct_video, (
        "the default scene must keep a real, immediately playable source"
    )
    assert 'preload="metadata"' in direct_video, (
        "the selected driver is the page's lead evidence, so its metadata may load"
    )
    assert "data-autoplay-video" not in _index(), (
        "there is no standalone video left: every player belongs to the one switcher"
    )
    for video in deferred_scene_videos:
        assert 'data-src="' in video, f"an unselected scene must defer its source: {video}"
        assert not re.search(r'(?<!data-)src="', video), (
            f"a deferred scene must not also declare a real, eagerly-fetchable src: {video}"
        )
        assert 'preload="none"' in video, f"below-fold scene media must fetch nothing: {video}"


def test_design_asset_rule_states_the_playback_contract_the_controller_ships() -> None:
    """Video round 1, finding 4: the design must describe the shipped playback.

    The asset rule was written for a page whose only motion was the hero, and
    it still said so. The shipped controller now also starts the *selected*
    scene of a visible switcher (`select()` calls `startFromBeginning` while
    `switcherVisible`), pauses every unselected panel's video and every video
    of an off-screen switcher, and suppresses all of that under
    `prefers-reduced-motion`. A design document that still forbids anything
    beyond "the single muted hero" is an inaccurate source for the next
    maintainer, so it is pinned to the behaviour the controller and the
    landing contracts already enforce.
    """
    design = (
        DOCS / "superpowers" / "specs" / "2026-08-22-visual-storytelling-design.md"
    ).read_text(encoding="utf-8")
    lowered = " ".join(design.lower().split())

    assert "do not autoplay more than the single muted hero demonstration" not in lowered, (
        "the selected visible scene autoplays too; this rule contradicts the controller"
    )
    assert re.search(
        r"autoplay only the muted hero[^;]*and the currently selected scene of a visible switcher",
        lowered,
    ), "the asset rule must name both surfaces that autoplay: the hero and the selected scene"
    assert "no other media may start on its own" in lowered, (
        "the rule must still forbid every *other* video starting by itself"
    )
    assert "pause inactive or off-screen scene media" in lowered, (
        "the pause half of the contract must survive the correction"
    )
    assert "prefers-reduced-motion" in lowered, (
        "reduced motion must keep suppressing programmatic playback"
    )
    assert "programmatic autoplay" in lowered, (
        "the design must keep naming what reduced motion suppresses"
    )

    controller = STORYTELLING_JS.read_text(encoding="utf-8")
    assert "startFromBeginning(selectedVideo)" in controller, (
        "the design is only accurate while the controller really does start the "
        "selected scene; update both together"
    )
    assert "prefers-reduced-motion: reduce" in controller, (
        "the controller must keep feature-detecting the reduced-motion preference"
    )


def test_ground_highlight_keeps_the_read_paths_truthful() -> None:
    """The contract map's one load-bearing claim survives its own deletion.

    The map explained, in three lanes and a truth sentence, that the
    watch-backed table, korvid's own fresh describe/log reads, and each
    driver's bounded fresh reads are taken at different moments. Collapsing
    the TUI's own describe/log reads into either the watch-backed table or
    the agent/MCP "bounded fresh reads" phrase would silently drop one of the
    three lanes, so each is pinned as its own fact below. The compact page
    states all three once, in the GROUND highlight, and must never trade any
    of them for a "same evidence" shortcut.
    """
    ground = _highlight("GROUND")
    lowered = " ".join(re.sub(r"<[^>]+>", " ", ground).lower().split())
    for fact in (
        "watch-backed table",
        "own fresh describe and log reads",
        "bounded fresh reads",
        "different moments",
        "snapshots can differ",
    ):
        assert fact in lowered, f"the GROUND highlight must keep stating {fact!r}: {lowered!r}"
    assert lowered.count("fresh") >= 2, (
        "the TUI's own fresh describe/log reads must stay a distinct lane from the "
        f"agent/MCP bounded fresh reads, not merged into one mention: {lowered!r}"
    )
    for overclaim in ("same evidence", "same snapshot", "one snapshot"):
        assert overclaim not in lowered, (
            f"the drivers read at different moments, so {overclaim!r} would be false"
        )
    for destination in ('href="agent/"', 'href="mcp/"'):
        assert destination in ground, (
            f"the GROUND highlight replaces the removed scene links, so it must keep {destination}"
        )


def test_no_landing_surface_reduces_the_tui_to_a_watch_backed_snapshot() -> None:
    """The TUI lane is not one watch-backed snapshot from end to end.

    The Direct recording filters the watch-backed pod table, then presses
    Describe — which fetches the manifest and that object's events in
    `KorvidApp.action_describe` — and opens a live log stream in
    `KorvidApp._live_log_stream`. Labelling that "watch-backed TUI snapshot"
    hides the very freshness distinction this page exists to explain.

    The compact page carries the distinction in one sentence instead of a
    three-lane map, and the three drivers stay distinct: the agent clip is
    described by its own read tools and the MCP clip by its own tool-specific
    read-only requests, so none of them collapses into a shared snapshot.
    """
    watch_only = "watch-backed tui snapshot"
    index_lowered = _index().lower()
    assert watch_only not in index_lowered, (
        "no landing surface may still label the TUI's evidence as watch-backed only"
    )

    ground = " ".join(re.sub(r"<[^>]+>", " ", _highlight("GROUND")).lower().split())
    assert "snapshots can differ" in ground
    assert "identical" not in ground

    agent = _flatten(_section('<article id="scene-agent"', "</article>"))
    assert "real diagnose_pod and get_logs reads" in agent, (
        "the agent clip keeps its own distinct read description — the real tool "
        "reads it runs, not the watch-backed table the direct driver reads"
    )
    mcp = _flatten(_section('<article id="scene-mcp"', "</article>"))
    assert "real read only mcp requests" in mcp, (
        "the MCP clip keeps its own distinct read-only request language"
    )


def test_agent_scene_states_the_grounded_deterministic_walkthrough() -> None:
    """The Agent scene ships a real turn over a deterministic synthetic cluster.

    `docs/demo/demo.py` wires the `agent` scene to `build_demo_agent_runtime`,
    which is korvid's own `AgentRuntime` over the real `ToolExecutor` and the
    synthetic `DemoReadOps` fixture. The prompt really is submitted through
    the real `AgentPanel`; `diagnose_pod` and `get_logs` really execute; the
    real `EvidenceLedger` mints `[E1]`/`[E2]` and validates the answer's
    markers against them, so the frame carries no unsupported-citation
    warning. What the capture still cannot speak for is a live model, a live
    cluster, or answer quality — `DemoAgentProvider` is offline and always
    chooses the same two calls.

    So the stage must state both halves, and must never again describe
    injected events, an empty ledger, or a flagged citation. The compact page
    has no per-scene prose left, so the accessible descriptions of the media
    and the stage caption carry the disclosure between them.
    """
    stage = _scene_switcher()
    scene = _section('<article id="scene-agent"', "</article>")
    lowered = " ".join(stage.lower().split())
    flattened = _flatten(stage)

    description = re.search(r'<video[^>]*data-src="[^"]*agent-demo\.mp4"[^>]*>', scene)
    assert description is not None, "the Agent panel keeps its video"
    aria = _flatten(description.group(0))
    for token in ("real agentpanel", "real diagnose_pod and get_logs reads", "grounded answer"):
        assert token in aria, (
            "the Agent media's accessible description must credit the real panel, "
            f"the real read tools, and the grounded answer; {token!r} missing from {aria!r}"
        )
    assert "ui drive" not in aria, (
        "the capture's screen changes are `agent.follow` mirroring the reads, not "
        f"a UI-drive tool call; found {aria!r}"
    )

    for phrase in AGENT_CAPTURE_DISCLOSURES:
        assert phrase in flattened, f"the media stage must state {phrase!r}; found {flattened!r}"
    assert "deterministic synthetic-cluster walkthrough" in lowered, (
        "the stage must ship the exact hyphenated label every other surface uses"
    )
    assert "unsupported citation" not in lowered, (
        "the shipped turn validates both markers, so no surface may still say "
        "the panel flags an unsupported citation"
    )
    for stale in ("scripted", "injected", "nothing is read"):
        assert stale not in lowered, (
            f"the old panel-only story must not survive anywhere in the stage: {stale!r}"
        )

    described = [
        re.search(r'aria-label="([^"]+)"', scene),
        re.search(r"<video[^>]*>([^<]+)</video>", scene),
        re.search(r'<img class="scene-panel__fallback[^"]*"[^>]*alt="([^"]+)"', scene),
    ]
    assert all(part is not None for part in described), (
        "the panel keeps an aria-label, an in-video fallback, and a fallback image"
    )
    for part in described:
        assert part is not None  # narrowed above; keeps mypy and the reader honest
        copy = " ".join(part.group(1).lower().replace("-", " ").split())
        assert "deterministic synthetic cluster walkthrough" in copy, (
            f"every description of this media must carry its one label: {copy!r}"
        )

    for overclaim in AGENT_CAPTURE_OVERCLAIMS:
        assert not _unnegated(stage, overclaim), (
            f"the media stage must not claim {overclaim!r} from a deterministic capture"
        )

    assert 'href="agent/"' in _highlight("GROUND"), (
        "the highlight that replaced the scene copy keeps the link to the real "
        "embedded-agent documentation"
    )


def test_agent_scene_credits_follow_rather_than_a_ui_drive_or_a_write() -> None:
    """The describe pane in the frame is a mirror, not the agent driving.

    `AgentUiController._maybe_follow_read` sends each successful read
    through `korvid.tools.follow.mirror_read`, the same `UIBridge` mapping
    MCP follow uses, so the capture's screen change
    is `agent.follow` reflecting a read. Describing it as UI drive would
    claim a tool call the recording never makes, and describing it as a
    change to the cluster would cross the write boundary entirely.
    """
    scene = _section('<article id="scene-agent"', "</article>")
    flattened = _flatten(scene)
    assert "follow" in flattened, (
        "the scene must name the follow mirror that opens the describe pane "
        f"beside the panel; found {flattened!r}"
    )
    for write_claim in ("writes", "mutates", "applies", "restarts the"):
        assert write_claim not in flattened, (
            f"the capture performs no write, so it must not say {write_claim!r}"
        )


def test_mcp_scene_states_the_real_read_only_requests_and_follow() -> None:
    """The MCP scene is a real SDK exchange, and must be described as one.

    `docs/demo/mcp_client.py` is an MCP SDK `ClientSession` speaking
    Streamable HTTP to the `KorvidMCPServer` the `mcp` demo scene binds on
    loopback, and every view the left pane opens is korvid's own follow
    bridge mirroring the answer just received. Nothing in the clip comes
    from a third-party client any more, so no visitor-facing surface may
    describe it as one.
    """
    scene = _section('<article id="scene-mcp"', "</article>")
    published_mcp_description = _flatten(scene)

    assert "real read only mcp requests" in published_mcp_description, (
        "the MCP scene must state the requests are real and read-only; "
        f"found {published_mcp_description!r}"
    )
    for signal in ("follow", "sdk client", "streamable http"):
        assert signal in published_mcp_description, (
            f"the MCP copy must name {signal!r}; found {published_mcp_description!r}"
        )
    assert "third party" not in published_mcp_description, (
        "the clip is recorded from this repository alone; no visitor-facing MCP "
        "copy may still derive it from a third-party client"
    )
    assert "disposable local cluster" not in published_mcp_description, (
        "the clip is recorded against the in-memory synthetic fixture the `mcp` "
        "demo scene serves, not a disposable cluster"
    )
    assert 'href="mcp/"' in _highlight("GROUND"), (
        "the highlight that replaced the scene copy keeps the link to the MCP guide"
    )


def test_mcp_landing_copy_keeps_the_production_write_and_follow_limits() -> None:
    """Recording a truthful read demo must not soften the production limits."""
    control = _flatten(_highlight("CONTROL"))
    assert "proposal" in control, "MCP writes stay opt-in proposals on the landing page"
    assert "off by default" in control, "and the page must keep saying they are off"


def test_agent_fallback_frame_claims_only_the_grounded_deterministic_capture() -> None:
    """The fallback image must match the frame it renders, in both directions.

    The evidence tile that used to render this poster is gone with the
    mosaic, but the same frame still ships as the Agent panel's
    no-JavaScript fallback — so its description carries the same duty. It
    used to deny live tool execution and validated evidence, because the old
    capture had neither. The shipped frame has both, over a synthetic
    fixture, so denying them now understates the product exactly as badly as
    the old copy overstated it. What stays denied is the model and the
    cluster.
    """
    scene = _section('<article id="scene-agent"', "</article>")
    fallback = re.search(r'<img class="scene-panel__fallback[^>]*>', scene)
    assert fallback is not None, "the agent panel keeps its no-JavaScript frame"

    alt = re.search(r'alt="([^"]+)"', fallback.group(0))
    assert alt is not None
    alt_text = " ".join(alt.group(1).lower().replace("-", " ").split())
    for signal in ("deterministic synthetic cluster walkthrough", "prompt", "diagnose_pod", "e1"):
        assert signal in alt_text, (
            "the alt must describe the grounded turn this frame ends on; "
            f"{signal!r} missing from {alt.group(1)!r}"
        )
    for token in ("agentpanel", "walkthrough"):
        assert token in alt_text, (
            f"the alt must name the agent panel walkthrough it shows: {alt.group(1)!r}"
        )

    caption_text = _flatten(_stage_caption())
    for stale in ("scripted", "no live tool execution", "no validated evidence"):
        assert stale not in caption_text, (
            "the shipped capture executes real read tools and validates its own "
            f"citations, so the stage must not still deny {stale!r}: {caption_text!r}"
        )
    assert "not a live model" in caption_text, (
        f"the stage must keep the one denial that is still true: {caption_text!r}"
    )

    for overclaim in AGENT_CAPTURE_OVERCLAIMS:
        assert not _unnegated(scene, overclaim), f"the agent panel must not claim {overclaim!r}"


def test_agent_capture_surfaces_state_the_validated_citations_they_show() -> None:
    """Every capture-specific surface tells the same, current story.

    The panel, the stage caption that speaks for it and the `docs/agent.md`
    storyboard all render the same frame, so a stale disclosure on any one of
    them contradicts the other two — and contradicts the frame itself, which
    shows `[E1]`/`[E2]` with no warning beneath them. The production turn flow
    beside the storyboard is untouched.
    """
    scene = _section('<article id="scene-agent"', "</article>")
    caption = _stage_caption()
    agent_page = (DOCS / "agent.md").read_text(encoding="utf-8")
    storyboard = agent_page[
        agent_page.index('<section class="docs-storyboard"') : agent_page.index("</section>")
        + len("</section>")
    ]
    figure = storyboard[storyboard.index("<figure>") : storyboard.index("</figure>")]

    for name, surface in (
        ("panel", scene),
        ("stage caption", caption),
        ("storyboard figure", figure),
    ):
        flattened = _flatten(surface)
        assert "deterministic synthetic cluster walkthrough" in flattened, (
            f"the {name} must carry the shared label for this media: {flattened!r}"
        )
        assert "unsupported" not in flattened, (
            f"the {name} must not still report a citation warning the frame does "
            f"not contain: {flattened!r}"
        )
        assert "scripted" not in flattened, (
            f"the {name} must not still call the capture scripted: {flattened!r}"
        )
        for overclaim in AGENT_CAPTURE_OVERCLAIMS:
            assert not _unnegated(surface, overclaim), (
                f"the {name} must not claim {overclaim!r} from a deterministic capture"
            )

    ordered_list = storyboard[storyboard.index("<ol>") :]
    assert "Evidence references remain selectable and validated." in ordered_list, (
        "the production turn flow keeps its validated-citation claim"
    )
    assert "unsupported" not in ordered_list.lower(), (
        "and must not import a capture-specific warning into the description of "
        "what a real turn does"
    )

    design = (
        DOCS / "superpowers" / "specs" / "2026-08-22-visual-storytelling-design.md"
    ).read_text(encoding="utf-8")
    lowered_design = " ".join(design.lower().split())
    assert "unsupported citation" not in lowered_design, (
        "the design must not keep requiring the warning the shipped capture no longer produces"
    )
    assert "deterministic synthetic-cluster walkthrough" in lowered_design, (
        "the design must record the label every shipping surface uses"
    )
    assert "the agent performs bounded reads" in lowered_design, (
        "the production capability statement stays exactly as strong"
    )


def test_agent_capture_never_presents_the_selected_row_as_grounding() -> None:
    """The demo's highlighted row is not the evidence the answer rests on.

    The answer is grounded in the two tool reads the runtime dispatched —
    `diagnose_pod` and `get_logs` — and the `[E1]`/`[E2]` references the
    evidence ledger minted for them. Whatever row the demo table happens to
    have selected is not that grounding, so no capture-specific surface may
    offer it as evidence.
    """
    scene = _section('<article id="scene-agent"', "</article>")
    for surface in (scene, _stage_caption()):
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
        "is not the grounding for the answer"
    )


def test_control_highlight_orders_confirmation_audit_and_execution() -> None:
    """The write path's guarantee survives the diagram that carried it.

    The five-stage strip is gone, but its promise is a security invariant:
    every write previews first, waits for a fresh human keystroke, and is
    blocked outright when the fail-closed audit append fails. The CONTROL
    highlight is now the only place the landing page states it, so it must
    state all three parts, in that order, for every origin the product has.
    """
    control = _highlight("CONTROL")
    lowered = " ".join(re.sub(r"<[^>]+>", " ", control).lower().split())

    assert "every write" in lowered, (
        f"the guarantee must cover every origin, not one of them: {lowered!r}"
    )
    ordered = ["preview", "fresh approval keystroke", "audit"]
    positions = [lowered.find(stage) for stage in ordered]
    assert all(position != -1 for position in positions), (
        f"the CONTROL copy must name preview, approval and audit: {lowered!r}"
    )
    assert positions == sorted(positions), (
        f"approval comes after the preview and before the audit gate: {lowered!r}"
    )
    assert "fail-closed" in lowered, "the audit gate must keep its fail-closed name"
    assert "blocks the action" in lowered, (
        f"a failed audit append must be shown to block the write: {lowered!r}"
    )
    assert "opt-in mcp" not in lowered or "off by default" in lowered
    assert 'href="ops/"' in control, "the highlight links the approval and audit reference"


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
            rf'<img class="scene-panel__fallback[^"]*"[^>]*{re.escape(poster)}[^>]*>',
            shipped_switcher,
        )
        assert shipped is not None
        planned = re.search(
            rf'<img class="scene-panel__fallback[^"]*"[^>]*{re.escape(poster)}[^>]*>',
            plan_markup,
        )
        assert planned is not None, f"the plan must keep the {poster} fallback poster snippet"
        assert 'loading="lazy"' in planned.group(0), (
            f"the plan's deferred {poster} fallback poster must stay lazy so the "
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


def test_visual_storytelling_plan_keeps_no_watch_only_claim_after_the_merge() -> None:
    """The plan is an executable recipe, so its snippets carry the same claim.

    `docs/superpowers/plans/2026-08-22-visual-storytelling.md` embedded the
    scene markup, the contract-map markup and the contract test verbatim. The
    compact homepage retired the contract map and the per-scene evidence
    labels, so those blocks are marked superseded rather than kept in sync —
    but the one claim they must never restore is the watch-only
    "Watch-backed TUI snapshot" label the shipped page does not make.
    """
    plan = _plan()

    assert "watch-backed tui snapshot" not in plan.lower(), (
        "no plan snippet or constraint may still reduce the TUI's evidence to a "
        "watch-backed snapshot"
    )
    assert "watch-backed table" in plan.lower(), (
        "the plan must keep the mixed-evidence language the shipped GROUND highlight still states"
    )
    superseded = plan[: plan.index("\n## ")].lower()
    assert "superseded" in superseded, (
        "a plan whose landing snippets no longer match the shipped page must say "
        "so above its first section, or a contributor will replay them"
    )
    for retired in ("contract map", "evidence mosaic"):
        assert retired in superseded, (
            f"the supersede notice must name the retired {retired!r} block by name"
        )


def test_visual_storytelling_plan_agent_snippets_match_the_shipped_copy() -> None:
    """The plan is executable, so a replay must not restore stale copy.

    `docs/superpowers/plans/2026-08-22-visual-storytelling.md` embeds the
    Agent scene media, the `docs/agent.md` storyboard and the provenance page
    verbatim. Whatever the shipped pages say about the capture, those
    snippets have to say too, or the next contributor following the recipe
    reintroduces a panel-only scripted walkthrough for media that now runs
    korvid's real runtime, executor and evidence ledger. Only the media
    elements are still compared: the compact homepage dropped the per-scene
    prose the plan's snippet wrapped them in.
    """
    plan = _plan()

    scene_markup = _fenced_block_after(
        plan,
        "- [ ] **Step 3: Replace the numbered cards with complete scene markup**",
        "html",
    )
    shipped_scene = _section('<article id="scene-agent"', "</article>")
    for element in re.findall(r"<(?:video|img)\b[^>]*>", shipped_scene):
        assert _compact(element) in _compact(scene_markup), (
            "the plan's Agent media must stay the shipped element, grounded-capture "
            f"description included; {element!r} is not in the plan"
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
        "its capture note and its separated production turn flow"
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
            f"no plan snippet may still ship {overclaim!r} for this capture"
        )
    for stale in ("scripted tool events", "unsupported citation", "nothing is read"):
        assert stale not in plan.lower(), (
            f"no plan snippet may still describe the replaced panel-only capture: {stale!r}"
        )


def test_visual_storytelling_design_separates_agent_capability_from_the_capture() -> None:
    """The design doc must keep the product claim and the proof claim apart.

    Its production statement ("the agent performs bounded reads, cites
    evidence, and drives the TUI") is true of the shipped runtime and stays
    exactly as strong. The capture rule beside it has to describe the media
    this branch actually ships: a deterministic synthetic-cluster
    walkthrough that runs the real pipeline over a fixture, behind an
    offline provider — not the panel-only scripted replay it replaced, and
    not a live-model or answer-quality claim either.
    """
    design = (
        DOCS / "superpowers" / "specs" / "2026-08-22-visual-storytelling-design.md"
    ).read_text(encoding="utf-8")
    lowered = " ".join(design.lower().split())

    for rule in (
        "deterministic synthetic-cluster walkthrough",
        "not evidence of a live model, a live cluster, or answer quality",
    ):
        assert rule in lowered, f"the design must state the capture rule: {rule!r}"
    for stale in (
        "deterministic scripted agentpanel walkthrough",
        "not evidence of bounded fresh reads, live tool execution, or validated citations",
        "unsupported citation",
    ):
        assert stale not in lowered, (
            f"the design must not keep the replaced capture's rule: {stale!r}"
        )

    assert "embedded-agent answers with validated citations" not in lowered, (
        "the mosaic criterion still names the walkthrough, not a product capability"
    )
    assert "the agent performs bounded reads" in lowered, (
        "the production capability statement stays: only the claim made about "
        "the capture is narrowed"
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
    switcher = _hero()
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
    """Two different boundaries, named separately, on the one control surface.

    Provider masking protects what leaves for the embedded agent's model;
    MCP result disclosure is decided per tool. Collapsing them into "secret
    values are masked before model calls" would promise a guarantee neither
    boundary makes, so the CONTROL highlight names both and links the page
    that documents them.
    """
    control = _highlight("CONTROL")
    lowered = " ".join(re.sub(r"<[^>]+>", " ", control).lower().split())
    assert "provider masking" in lowered, (
        f"the control surface must name the embedded provider's masking: {lowered!r}"
    )
    assert "mcp disclosure" in lowered, (
        f"and must name MCP's tool-specific disclosure separately: {lowered!r}"
    )
    assert "secret values are masked before model calls" not in lowered
    assert 'href="threat-model/"' in control, (
        "the two boundaries must link to the page that states their limits"
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


def test_feature_highlights_are_three_linked_promises() -> None:
    """The mosaic's six tiles compress into three promises, still linked.

    The mosaic sold six captures; the compact page sells one contract in
    three parts. Each part still has to be short enough to scan and still has
    to hand the visitor a real destination, or the page trades length for
    a dead end.
    """
    cards = re.findall(r"<article>.*?</article>", _highlights(), re.DOTALL)
    assert len(cards) == 3, f"exactly three promises; found {len(cards)}"
    labels = [re.search(r"<span>([^<]+)</span>", card) for card in cards]
    assert [label.group(1) for label in labels if label is not None] == [
        "SEE",
        "GROUND",
        "CONTROL",
    ], "the promises stay in the order a visitor meets them"
    for card in cards:
        paragraphs = re.findall(r"<p>(.*?)</p>", card, re.DOTALL)
        assert len(paragraphs) == 1, "one paragraph per promise keeps the page scannable"
        assert len(re.sub(r"<[^>]+>", " ", paragraphs[0]).split()) <= 40
        links = re.findall(r'<a href="([^"]+)"', card)
        assert len(links) >= 2, f"a promise must hand over real destinations: {card[:80]!r}"
        for href in links:
            assert re.fullmatch(r"[a-z0-9-]+/(?:#[a-z0-9-]+)?", href), (
                f"highlight links stay inside the docs site: {href!r}"
            )


def test_evidence_copy_claims_only_what_its_capture_actually_shows() -> None:
    """A description must not promise a signal the recording cannot contain.

    The cockpit capture comes from an in-memory fixture with no metrics
    source, so every CPU/MEM column renders an em-dash placeholder. Claiming
    the frame shows "utilization" made the page's own headline evidence
    contradict itself, and the merge deleted the caption that used to carry
    the risk — so the ban now covers the whole page.
    """
    assert "utilization" not in _index().lower(), (
        "the cockpit capture has no live metrics, so no landing surface may "
        "describe it as showing utilization"
    )
    direct = _section('<article id="scene-direct"', "</article>")
    described = _flatten(direct)
    for signal in ("browsing", "filtering", "describing", "following logs"):
        assert signal in described, (
            f"the Direct media must describe what it does show; {signal!r} missing "
            f"from {described!r}"
        )


def test_generic_grouped_containers_carry_a_role_aria_can_use() -> None:
    """`aria-label` is ignored on `role=generic`, so the label needs a role.

    A bare `div` that groups several related items and names that grouping
    for assistive technology drops the name silently unless it declares a
    role, and the group reads as loose text. The rule is enforced for every
    labelled `div` on the page, not a fixed list, so it survives the next
    time the landing markup is rewritten.
    """
    labelled = [
        match.group(0)
        for match in re.finditer(r"<div\b[^>]*>", _index())
        if "aria-label=" in match.group(0)
    ]
    assert labelled, "the landing page must keep at least one named grouping"
    for tag in labelled:
        assert re.search(r'\brole="[a-z]+"', tag), (
            "this div carries an aria-label, so it needs an explicit role "
            f"assistive technology can name; found: {tag!r}"
        )


def test_landing_provenance_matches_every_captures_real_source() -> None:
    """One caption now speaks for all three recordings, so it must be true.

    Every shipped clip is recorded against the deterministic in-memory
    fixture the `docs/demo` harness serves — never a live cluster. The merged
    stage states that once, above all three panels, and must keep saying both
    halves: the product is real, the cluster is not.
    """
    caption = _flatten(_stage_caption())
    assert "real korvid" in caption, (
        f"the caption must keep crediting the real product: {caption!r}"
    )
    assert "synthetic cluster" in caption, (
        f"and must keep disclosing the synthetic source of every clip: {caption!r}"
    )
    for overclaim in ("live cluster", "real cluster", "production cluster"):
        assert not _unnegated(_stage_caption(), overclaim), (
            f"no recording may be sold as a {overclaim!r}"
        )


def test_landing_never_labels_a_single_pod_stream_as_a_merged_one() -> None:
    """Korvid has a merged log view; none of the shipped media shows it.

    The retired mosaic rendered `merged-logs.png`, a single-pod `l` view
    whose header reads `payment-worker-.../app [json] - streaming`. Nothing
    on the compact page may reintroduce that name for a screen it does not
    show, and the destinations it hands over must be the three the compact
    page promises.
    """
    assert "merge" not in _index().lower(), (
        "the landing media shows single-pod streams, so nothing may claim a merge"
    )

    paths = _section('<nav class="flight-paths"', "</nav>")
    destinations = re.findall(r'<a href="([^"]+)"><strong>([^<]+)</strong>', paths)
    assert destinations == [
        ("getting-started/", "Start operating"),
        ("agent/", "Explore Agent and MCP"),
        ("performance/", "Evaluate production use"),
    ], f"three destinations, install first and production last; found {destinations}"
    for _, label in destinations:
        assert len(label.split()) <= 4, f"a destination label must stay scannable: {label!r}"
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


def test_each_scene_panel_owns_exactly_one_video_and_nothing_adds_more() -> None:
    """Round-9 (comment 3859789134): the switcher's single-player contract.

    The review read `select()`'s asymmetry — every `<video>` in a deselected
    panel is paused, but only `querySelector("video")` is promoted and
    started in the selected one — as a latent bug for a panel holding two
    players. It is only a bug if such a panel can exist, and it cannot: each
    scene panel ships exactly one `<video>`, and the controller never
    creates, clones, moves or inserts one, so `querySelector` and
    `querySelectorAll` address the same element by construction.

    Pinning that here is what makes the asymmetry safe to leave alone. A
    second player in a panel — or a script that injects one — fails this
    test instead of silently shipping an unpromoted, sourceless frame.
    """
    panels = re.findall(
        r'<article[^>]*class="[^"]*scene-panel[^"]*"[^>]*>.*?</article>',
        _scene_switcher(),
        re.DOTALL,
    )
    assert len(panels) == 3, f"the switcher must keep its three scene panels; found {len(panels)}"
    for panel in panels:
        identifier = re.search(r'id="([^"]+)"', panel)
        assert identifier is not None
        assert panel.count("<video") == 1, (
            f"{identifier.group(1)} must own exactly one <video>; the controller promotes "
            "the selected panel's player with querySelector, so a second one would stay "
            f"sourceless. Found {panel.count('<video')}"
        )

    script = STORYTELLING_JS.read_text(encoding="utf-8")
    for mutator in ("createElement", "cloneNode", "insertBefore", "appendChild", "innerHTML"):
        assert mutator not in script, (
            f"the scene controller must not use {mutator!r}: the one-video-per-panel "
            "contract holds because the static page is the only thing that creates players"
        )
