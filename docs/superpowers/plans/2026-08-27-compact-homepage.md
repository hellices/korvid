# Compact Homepage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the long, repetitive homepage with one bounded Direct/Agent/MCP media stage, three product highlights, and concise destination links.

**Architecture:** The existing scene-switcher controller remains the interaction layer, but its markup moves into the hero so Direct is authored once. A new `.feature-highlights` component replaces the contract map, write path, and evidence mosaic. CSS makes the stage own the aspect-ratio box and lets each native video fit with `object-fit: contain`.

**Tech Stack:** MkDocs Material, raw HTML in Markdown, CSS, dependency-free JavaScript DOM harness, pytest.

## Global Constraints

- Preserve the existing headline, install command, primary actions, and all three approved recordings.
- Do not regenerate or edit binary media.
- Author exactly three videos; Direct source appears once.
- Keep no more than three major homepage blocks after front matter.
- Keep homepage source below 800 words.
- Preserve keyboard tabs, reduced-motion behavior, native controls, deferred Agent/MCP loading, and media-error fallback.
- Cap the media stage at 540px and 58vh; never stretch or crop a clip.
- Preserve SEE / GROUND / CONTROL claims for keyboard-first operation, bounded evidence, fresh approval, and fail-closed audit.

---

### Task 1: Merge the hero and driver switcher

**Files:**
- Modify: `docs/index.md`
- Modify: `tests/test_docs_landing_design.py`
- Modify: `tests/test_docs_visual_assets.py`

**Interfaces:**
- Consumes: `[data-scene-switcher]`, tab/panel ARIA contract, current three video sources.
- Produces: one `.hero` containing one `.hero-driver-stage` with Direct/Agent/MCP tabs and panels.

- [ ] **Step 1: Write failing compact-home contracts**

Add assertions equivalent to:

```python
def test_homepage_is_one_media_story_not_repeated_sections() -> None:
    source = LANDING.read_text(encoding="utf-8")
    assert len(source.split()) < 800
    assert source.count("<video") == 3
    assert source.count('src="assets/demo.mp4"') == 1
    assert source.count("<section") + source.count("<nav") <= 3
    for removed in ("contract-map", "write-path", "evidence-mosaic"):
        assert f'class="{removed}' not in source


def test_homepage_highlights_the_three_product_promises() -> None:
    source = LANDING.read_text(encoding="utf-8")
    for label in ("SEE", "GROUND", "CONTROL"):
        assert f">{label}<" in source
    for claim in ("Keyboard", "Bounded", "Fresh approval", "Fail-closed audit"):
        assert claim.lower() in source.lower()
```

Keep existing assertions for video sources, posters, labels, privacy, and links.

- [ ] **Step 2: Run and verify RED**

```bash
.venv/bin/pytest -p no:tach \
  tests/test_docs_landing_design.py \
  tests/test_docs_visual_assets.py -q
```

Expected: current 1,132-word, four-video, six-block page fails.

- [ ] **Step 3: Replace homepage markup**

Rebuild `docs/index.md` as:

```html
<section class="hero hero--drivers" data-scene-switcher>
  <!-- existing heading/copy/actions/install -->
  <figure class="hero-driver-stage">
    <div class="scene-tabs" role="tablist">...</div>
    <div class="scene-panels">
      <!-- Direct, Agent, MCP: video/poster only -->
    </div>
    <figcaption>Real korvid, synthetic cluster.</figcaption>
  </figure>
</section>

<section class="feature-highlights">
  <article><span>SEE</span><p>...</p><ul>...</ul></article>
  <article><span>GROUND</span><p>...</p><ul>...</ul></article>
  <article><span>CONTROL</span><p>...</p><ul>...</ul></article>
</section>

<nav class="flight-paths">...</nav>
```

Remove `contract-map`, `write-path`, and `evidence-mosaic`.

- [ ] **Step 4: Update exact markup contracts**

Update existing tests that intentionally pin the old duplicate Direct video or removed sections. Do not remove source/poster/privacy/accessibility assertions for retained media.

- [ ] **Step 5: Verify GREEN**

```bash
.venv/bin/pytest -p no:tach \
  tests/test_docs_landing_design.py \
  tests/test_docs_visual_assets.py \
  tests/test_docs_links.py -q
```

Expected: pass; `wc -w docs/index.md` is below 800.

- [ ] **Step 6: Commit**

```bash
git add docs/index.md tests/test_docs_landing_design.py \
  tests/test_docs_visual_assets.py tests/test_docs_links.py
git commit -m "docs: merge the homepage into one media story"
```

### Task 2: Bound the media stage and compact highlights

**Files:**
- Modify: `docs/stylesheets/extra.css`
- Modify: `docs/assets/javascripts/visual-storytelling.js` only if the merged markup exposes a real controller gap
- Modify: `tests/test_docs_landing_design.py`
- Modify: `tests/js/scene_switcher_harness.mjs` only if controller behavior changes
- Modify: `docs/superpowers/plans/2026-08-22-visual-storytelling.md` if the pinned controller changes
- Modify: `tests/test_docs_build_config.py` if the controller digest changes

**Interfaces:**
- Consumes: Task 1 `.hero-driver-stage`, `.feature-highlights`, existing scene controller.
- Produces: a capped 16:9 stage whose videos use `object-fit: contain`, responsive highlights, and unchanged playback behavior.

- [ ] **Step 1: Write failing CSS contracts**

Add:

```python
def test_home_media_stage_is_bounded_without_distorting_clips() -> None:
    css = EXTRA_CSS.read_text(encoding="utf-8")
    assert ".hero-driver-stage" in css
    assert "max-width: 54rem" in css
    assert "max-height: min(58vh, 540px)" in css
    assert "aspect-ratio: 16 / 9" in css
    assert "object-fit: contain" in css
    assert "min-width: 0" in css
```

Add responsive assertions for stacked mobile layout and three-to-one-column highlights.

- [ ] **Step 2: Run and verify RED**

```bash
.venv/bin/pytest -p no:tach tests/test_docs_landing_design.py -q
```

Expected: new selectors and caps are absent.

- [ ] **Step 3: Implement compact CSS**

Add component rules:

```css
.md-typeset .hero-driver-stage {
  width: 100%;
  min-width: 0;
  max-width: 54rem;
  margin: 0;
}

.md-typeset .hero-driver-stage .scene-panels {
  width: 100%;
  aspect-ratio: 16 / 9;
  max-height: min(58vh, 540px);
}

.md-typeset .hero-driver-stage .scene-panel,
.md-typeset .hero-driver-stage video,
.md-typeset .hero-driver-stage .scene-panel__fallback {
  width: 100%;
  height: 100%;
}

.md-typeset .hero-driver-stage video,
.md-typeset .hero-driver-stage .scene-panel__fallback {
  object-fit: contain;
}
```

Make `.feature-highlights__grid` three columns on desktop and one column below the existing mobile/tablet breakpoint. Remove obsolete landing-only CSS for deleted components when no other page uses it.

- [ ] **Step 4: Verify controller behavior**

Run:

```bash
node tests/js/scene_switcher_harness.mjs
```

If the existing controller passes unchanged, do not edit JavaScript. If it fails because of the merged hero markup, add a failing DOM scenario before the minimal controller fix and update the pinned script/digest.

- [ ] **Step 5: Verify GREEN**

```bash
.venv/bin/pytest -p no:tach \
  tests/test_docs_landing_design.py \
  tests/test_docs_visual_assets.py -q
node tests/js/scene_switcher_harness.mjs
.venv/bin/mkdocs build --strict
```

- [ ] **Step 6: Commit**

```bash
git add docs/stylesheets/extra.css tests/test_docs_landing_design.py \
  tests/js/scene_switcher_harness.mjs \
  docs/assets/javascripts/visual-storytelling.js \
  docs/superpowers/plans/2026-08-22-visual-storytelling.md \
  tests/test_docs_build_config.py
git commit -m "docs: bound the homepage media stage"
```

### Task 3: Verify the compact homepage and update PR

**Files:**
- Modify only for direct regressions found by verification.

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: verified desktop/mobile homepage, refreshed local preview, updated PR #315.

- [ ] **Step 1: Run the documentation gate**

```bash
.venv/bin/ruff check tests/test_docs_*.py
.venv/bin/ruff format --check tests/test_docs_*.py
.venv/bin/pytest -p no:tach \
  tests/test_docs_build_config.py \
  tests/test_docs_links.py \
  tests/test_docs_readability.py \
  tests/test_docs_visual_assets.py \
  tests/test_docs_landing_design.py \
  tests/test_docs_agent_contracts.py \
  tests/test_mcp_follow_demo_asset.py -q
.venv/bin/mkdocs build --strict
node tests/js/scene_switcher_harness.mjs
```

- [ ] **Step 2: Verify measurable outcomes**

```bash
test "$(rg -c '<video' docs/index.md)" -eq 3
test "$(rg -c 'src=\"assets/demo.mp4\"' docs/index.md)" -eq 1
test "$(wc -w < docs/index.md)" -lt 800
git diff --exit-code -- uv.lock docs/assets/scenes docs/assets/mcp-follow-demo.gif
```

- [ ] **Step 3: Verify local routes and responsive output**

Verify `http://127.0.0.1:8981/korvid/` returns 200. Check desktop and 390px layouts for:

- one selected video;
- no horizontal overflow;
- no media taller than 58vh or 540px;
- readable tabs and highlights;
- landing height materially below the previous page;
- reduced-motion and native controls intact.

- [ ] **Step 4: Run final review**

Generate a review package from the pre-feature SHA through HEAD and request a broad review for markup, accessibility, media sizing, fallback, and stale CSS.

- [ ] **Step 5: Push and update PR #315**

Push without force. Update the PR with:

- old/new homepage words, blocks, and video counts;
- removed duplicate sections;
- media cap behavior;
- test/build/visual verification;
- unchanged binary media.
