# Task 7 Verification Report

- **Status:** BLOCKED
- **Worktree:** `/Users/hwang-inhwan/workspace/kube/.worktrees/docs-official-site`
- **Branch:** `docs/visual-storytelling`
- **HEAD:** `9e27e75`
- **Verification scope:** Task 7 only; no tracked source edits, no commit, no push.
- **uv fallback note:** Every required `uv run --frozen ...` command executed locally without dependency-download failure, so no `.venv/bin/...` fallback was needed.

## Summary

Targeted tests and the strict production docs build passed, and the built static site's no-JS fallback worked when served directly from `site/` with the built storytelling controller renamed out of the way. Verification is **blocked** because:

1. the required formatter check fails on `tests/test_docs_build_config.py`;
2. the exact runtime URL scan command reports many matches (all canonical links, not executable/media assets);
3. integrated browser viewport access was unavailable for the required responsive/keyboard/reduced-motion/lazy-load checks (`document.visibilityState === "hidden"`, `window.innerWidth === 0`, `window.innerHeight === 0`).

## Step 1 — Targeted lint and formatting checks

### 1A. Ruff check

```bash
uv run --frozen ruff check docs/demo/demo.py \
  tests/test_docs_visual_assets.py \
  tests/test_docs_landing_design.py \
  tests/test_docs_build_config.py
```

- **Exit:** 0
- **Result:** `All checks passed!`

### 1B. Ruff format check

```bash
uv run --frozen ruff format --check docs/demo/demo.py \
  tests/test_docs_visual_assets.py \
  tests/test_docs_landing_design.py \
  tests/test_docs_build_config.py
```

- **Exit:** 1
- **Result:** formatter failure in `tests/test_docs_build_config.py`
- **Formatter output:**

```text
unformatted: File would be reformatted
   --> tests/test_docs_build_config.py:333:47
    |
332 |     config = _load_mkdocs_config()
    -     assert config.get("extra_javascript") == [
    -         "assets/javascripts/visual-storytelling.js"
    -     ]
333 +     assert config.get("extra_javascript") == ["assets/javascripts/visual-storytelling.js"]
334 |     assert VISUAL_STORYTELLING.is_file()
    |

1 file would be reformatted, 3 files already formatted
```

- **Root cause investigation:** inspected `tests/test_docs_build_config.py:331-336`; the assertion is wrapped across three lines and Ruff wants the list literal collapsed to one line.

## Step 2 — Documentation and packaging tests

```bash
uv run --frozen pytest -p no:tach -q \
  tests/test_docs_visual_assets.py \
  tests/test_docs_landing_design.py \
  tests/test_docs_build_config.py \
  tests/test_docs_workflow.py \
  tests/test_docs_site_entrypoints.py \
  tests/test_mcp_follow_demo_asset.py \
  tests/test_wheel_packaging.py
```

- **Exit:** 0
- **Result:** `94 passed in 4.14s`
- **Warnings:** none printed

## Step 3 — Strict production build

```bash
uv run --frozen --group docs mkdocs build --strict
```

- **Exit:** 0
- **Result:** build succeeded; `site/` rebuilt
- **Notable output:**
  - Material for MkDocs emitted its upstream MkDocs 2.0 warning banner.
  - MkDocs printed INFO messages about docs pages present outside `nav`.
- **Built index asset evidence:**

```bash
rg -n 'visual-storytelling\.js|<script[^>]+src=|<video[^>]+(src|poster)=|<img[^>]+src=' site/index.html
```

- **Exit:** 0
- **Result summary:** `site/index.html` references only local `assets/...` scripts/media for the storytelling implementation, including `assets/javascripts/visual-storytelling.js` and local poster/video files.

## Step 4 — Third-party runtime asset URL scan

### 4A. Exact required scan

```bash
rg -n '(<script[^>]+src|<link[^>]+href|<(img|video)[^>]+(src|poster))="https?://' site
```

- **Exit:** 0
- **Result:** **matches found**
- **Observed matches:** all reported matches were `<link rel="canonical" href="https://hellices.github.io/korvid/...">` lines across built pages, including `site/index.html:13` and `site/demo/visual-storytelling/index.html:13`.

### 4B. Root-cause follow-up (diagnostic, not a substitute for 4A)

```bash
rg -n '(<script[^>]+src|<img[^>]+src|<video[^>]+(src|poster))="https?://' site
```

- **Exit:** 1
- **Result:** no matches
- **Interpretation:** the required scan is tripping over canonical page URLs rather than external executable/media assets. I did **not** modify the branch or scan command.

## Step 5 — No-JavaScript fallback

### 5A. Exact plan flow attempted first

```bash
mv site/assets/javascripts/visual-storytelling.js \
  site/assets/javascripts/visual-storytelling.js.disabled
uv run --frozen --group docs mkdocs serve -a 127.0.0.1:8765
```

- **Rename result:** success
- **Serve result:** server started successfully at `http://127.0.0.1:8765/korvid/`
- **Root-cause finding:** this route is **not a faithful no-JS verification of the built artifact**. `mkdocs serve` served the site with the `visual-storytelling.js` URL still loadable from docs source, so renaming only the built file under `site/` did not disable the controller for that served page.
- **Evidence command:**

```bash
curl -s http://127.0.0.1:8765/korvid/ | rg -n 'scene-direct|scene-agent|scene-mcp|hidden|visual-storytelling.js'
```

- **Exit:** 0
- **Result:** served HTML still included `<script src="assets/javascripts/visual-storytelling.js"></script>` and emitted no `hidden` attributes in source.

### 5B. Built-site fallback verification (diagnostic to verify the actual built artifact)

With `site/assets/javascripts/visual-storytelling.js` still renamed to `.disabled`, I served the built `site/` directory directly:

```bash
python3 -m http.server 8766 --bind 127.0.0.1 --directory site
```

- **Server status:** started, then stopped after verification

Browser result at `http://127.0.0.1:8766/`:
- the page logged a 404 for `assets/javascripts/visual-storytelling.js`;
- all three scene panels were visible simultaneously and in document order;
- scene guide links were present as `tui/`, `agent/`, and `mcp/`.

DOM evidence captured from the static built site with the built controller absent:

- `scene-direct`: `hidden=false`, `display=grid`
- `scene-agent`: `hidden=false`, `display=grid`
- `scene-mcp`: `hidden=false`, `display=grid`

Guide-link target check:

```bash
for path in tui/ agent/ mcp/; do
  url="http://127.0.0.1:8766/$path"
  code=$(curl -s -o /dev/null -w '%{http_code}' "$url")
  echo "$path $code"
done
```

- **Exit:** 0
- **Result:**

```text
tui/ 200
agent/ 200
mcp/ 200
```

### 5C. Cleanup

```bash
mv site/assets/javascripts/visual-storytelling.js.disabled \
  site/assets/javascripts/visual-storytelling.js
```

- **Exit:** 0
- **Result:** built controller restored before finish
- **Server cleanup:** both temporary local servers were stopped before completion

## Step 6 — Responsive / keyboard / reduced-motion browser verification

### Browser-access status

I attempted integrated-browser verification against the built static site, but the automation context reported:

- `document.visibilityState === "hidden"`
- `window.innerWidth === 0`
- `window.innerHeight === 0`
- `window.outerWidth === 0`
- `window.outerHeight === 0`

That means the integrated page was not exposed as a real visible viewport, so the required visual checks at **390x844**, **768x1024**, and **1440x900** could not be completed reliably here.

### Exact outstanding checks for the controller

Because viewport access was unavailable, these checks remain outstanding for a controller with real browser visibility:

1. at **390x844**, **768x1024**, and **1440x900**, verify the product occupies at least half of the desktop hero and no horizontal page scroll appears;
2. at those viewports, verify all media controls are reachable and poster fallbacks remain legible;
3. at those viewports, verify **Left/Right/Home/End** change scene tabs, selected state, and focus correctly;
4. at those viewports, verify inactive scene videos pause;
5. at those viewports, verify the product-contract and write-path reading order matches their visual order;
6. at those viewports, verify the audit-failure branch remains visible without hover;
7. at those viewports, verify evidence images are not loaded before scrolling near them;
8. at those viewports, verify all focus indicators are visible; and
9. with reduced motion enabled, verify no decorative transition occurs.

### Limited hidden-page observations (informational only; not claimed as visual sign-off)

While the page was hidden, DOM-level probing suggested:
- scene-tab selection can change via keyboard events, but focus did not appear to move reliably with the selected tab in the hidden automation context;
- media playback verification was blocked by browser policy because the hidden page rejected `play()` with `AbortError: The play() request was interrupted because video-only background media was paused to save power`.

These observations were collected under a hidden/zero-viewport page and should **not** be treated as final visual sign-off.

## Step 7 — Final diff / status checks

### 7A. Diff check

```bash
git diff --check origin/main...HEAD
```

- **Exit:** 0
- **Result:** no whitespace / patch-format errors

### 7B. Diff stat

```bash
git --no-pager diff --stat origin/main...HEAD
```

- **Exit:** 0
- **Result summary:** branch diff covers the expected docs/storytelling implementation set, including docs, assets, tests, plan/spec files, and task 6 report.
- **Diff stat excerpt:** `30 files changed, 3678 insertions(+), 413 deletions(-)`

### 7C. Status checks

```bash
git --no-pager status --short
```

- **Exit:** 0
- **Result:** no tracked-file changes shown (clean tracked status)

```bash
git --no-pager status --short --ignored site .superpowers/sdd/task-7-report.md
```

- **Exit:** 0
- **Result:** `!! site/`
- **Interpretation:** only the ignored built-site tree is present; no tracked-source modifications were left behind by verification.

## Concerns

1. **Formatter gate is red:** `uv run --frozen ruff format --check ...` fails on `tests/test_docs_build_config.py:333`.
2. **Exact asset-scan command does not currently pass cleanly:** it flags canonical `https://hellices.github.io/korvid/...` links, even though diagnostic follow-up found no external script/img/video/poster runtime assets.
3. **Plan no-JS serve command is misleading for the built artifact:** renaming the built file under `site/` does not disable the controller when using `mkdocs serve`; direct static serving of `site/` was required to verify the built artifact's fallback.
4. **Required viewport/browser sign-off remains outstanding:** integrated browser access was not exposed as a visible viewport in this environment.
5. **Build output includes upstream/non-failing warnings/info:** the Material for MkDocs warning banner and MkDocs `nav` INFO messages remained present during strict build.

## Verification blocker fixes

- **Changed files:** `tests/test_docs_build_config.py`, `docs/superpowers/plans/2026-08-22-visual-storytelling.md`, `.superpowers/sdd/task-7-report.md`
- **Commands/results:**
  - `uv run --frozen ruff format tests/test_docs_build_config.py` → reformatted 1 file
  - `uv run --frozen ruff check docs/demo/demo.py tests/test_docs_visual_assets.py tests/test_docs_landing_design.py tests/test_docs_build_config.py` → exit 0
  - `uv run --frozen ruff format --check docs/demo/demo.py tests/test_docs_visual_assets.py tests/test_docs_landing_design.py tests/test_docs_build_config.py` → exit 0
  - `uv run --frozen pytest -p no:tach -q tests/test_docs_visual_assets.py tests/test_docs_landing_design.py tests/test_docs_build_config.py tests/test_docs_workflow.py tests/test_docs_site_entrypoints.py tests/test_mcp_follow_demo_asset.py tests/test_wheel_packaging.py` → `94 passed`
  - `uv run --frozen --group docs mkdocs build --strict` → exit 0
  - corrected external-asset scans on `site/` → all five `rg` commands exited 1 as expected
  - `python3 -m http.server 8766 --bind 127.0.0.1 --directory site` with `site/assets/javascripts/visual-storytelling.js` renamed out of the way → `/`, `/tui/`, `/agent/`, and `/mcp/` returned 200; the controller path returned 404; the script was restored afterward
- **Commit:** `test: correct visual docs verification gate`
- **Remaining browser-only checks:** real viewport verification at 390x844, 768x1024, and 1440x900 for hero layout, media controls, keyboard scene switching, inactive video pausing, reading order, audit-failure visibility, lazy-loaded evidence images, visible focus indicators, and reduced-motion behavior

## Responsive browser fix

- **RED evidence:** `uv run pytest -p no:tach -q tests/test_docs_landing_design.py::test_hero_keeps_heading_then_demo_then_copy_in_source_and_desktop_grid` failed with `AssertionError: the hero must keep a dedicated .hero-heading wrapper`, proving the source still rendered copy before the demo on narrow screens.
- **GREEN evidence:** after the hero-only DOM/CSS change, `uv run pytest -p no:tach -q tests/test_docs_landing_design.py tests/test_docs_links.py tests/test_docs_build_config.py tests/test_docs_visual_assets.py tests/test_docs_site_entrypoints.py tests/test_docs_workflow.py` passed (`87 passed`), and `uv run --frozen --group docs mkdocs build --strict` succeeded.
- **Exact DOM/CSS decision:** `docs/index.md` now orders the hero as `.hero-heading` → unchanged `.hero-demo` → `.hero-copy-column`; `docs/stylesheets/extra.css` keeps the narrow layout in DOM flow with grid gap spacing, then at `@media (min-width: 960px)` places `.hero-heading` in column 1 row 1, `.hero-copy-column` in column 1 row 2, and `.hero-demo` in column 2 with `grid-row: 1 / span 2`, preserving the existing `0.8fr / 1.2fr` desktop split without CSS reordering.
- **Commit:** `fix: show the product above the fold on narrow screens`
- **Outstanding verification:** controller browser re-verification in a real visible viewport still remains required for 390x844, 768x1024, and 1440x900.

## Write-path cascade fix

### RED

- The write-path stage grid still used a plain class selector, so Material's
  ordered-list default (`ol:not([hidden]) { display: flow-root; }`) could win
  the cascade and suppress the intended grid behavior.
- A focused regression was added first to pin the selector shape and the
  Material specificity explanation.

### GREEN

- `docs/stylesheets/extra.css` now targets `.md-typeset ol.write-path__stages`
  in both the base rule and the `max-width: 799px` fallback.
- `tests/test_docs_landing_design.py` now includes
  `test_write_path_stage_grid_targets_the_ordered_list_specificity`.
- Verification reran cleanly:
  - `uv run --frozen ruff check tests/test_docs_landing_design.py`
  - `uv run --frozen ruff format --check tests/test_docs_landing_design.py`
  - `uv run --frozen pytest -p no:tach -q tests/test_docs_landing_design.py tests/test_docs_build_config.py`
  - `uv run --frozen --group docs mkdocs build --strict`

### Browser recheck

- Served the rebuilt `site/` on `http://127.0.0.1:8777/` for a browser
  recheck, but the integrated browser tool could not create a page in this
  environment (`Cannot read properties of undefined (reading '_page')`), so no
  additional visual readback was available here.
