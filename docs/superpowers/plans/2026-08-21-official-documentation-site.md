# Official Documentation Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish a K9s-inspired korvid product landing page and searchable official documentation site at `https://hellices.github.io/korvid/`.

**Architecture:** MkDocs Material renders the existing `docs/` Markdown tree, a custom landing page, and one focused stylesheet into a static site. A strict local build validates content and navigation; a least-privilege GitHub Pages workflow builds pull requests and deploys reviewed `main` revisions.

**Tech Stack:** Python 3.11+, uv, MkDocs Material 9.7, Markdown, CSS, GitHub Pages

## Global Constraints

- Existing files under `docs/` remain the canonical source; generated HTML is never committed.
- The first release uses `https://hellices.github.io/korvid/` and no custom domain.
- The initial site has no analytics, localization, version switcher, blog, backend, or custom JavaScript application.
- The landing page uses korvid's own brand and copy; it borrows K9s's product-led structure without copying K9s assets or content.
- Internal links and navigation targets must pass `mkdocs build --strict`.
- Dependency locking must clear `UV_INDEX`, `UV_DEFAULT_INDEX`, `UV_INDEX_URL`, `UV_EXTRA_INDEX_URL`, and `UV_FIND_LINKS` and use `uv lock --no-config`.
- GitHub Actions must be pinned by full commit SHA and deployment permissions must be isolated to the deploy job.

---

### Task 1: Reproducible documentation build

**Files:**
- Create: `mkdocs.yml`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `Makefile`

**Interfaces:**
- Consumes: Existing Markdown files under `docs/`.
- Produces: `make docs-build`, `make docs-serve`, and a strict MkDocs configuration whose source directory is `docs/`.

- [ ] **Step 1: Add a deliberately incomplete site configuration**

Create `mkdocs.yml` with the final `site_name`, `site_url`, theme, Markdown extensions, and navigation. Include `index.md`, `getting-started.md`, and `stylesheets/extra.css` before those files exist so the first strict build proves the gate is active.

```yaml
site_name: korvid
site_description: A tool-using bird for your Kubernetes cluster
site_url: https://hellices.github.io/korvid/
repo_url: https://github.com/hellices/korvid
repo_name: hellices/korvid
docs_dir: docs
site_dir: site
strict: true
exclude_docs: |
  overrides/

theme:
  name: material
  custom_dir: docs/overrides
  language: en
  font: false
  logo: assets/korvid-mark.svg
  favicon: assets/korvid-mark.svg
  features:
    - content.code.copy
    - navigation.footer
    - navigation.indexes
    - navigation.sections
    - navigation.top
    - search.highlight
    - search.suggest
  palette:
    scheme: slate
    primary: black
    accent: amber

nav:
  - Home: index.md
  - Getting started:
      - Overview: overview.md
      - Install and quick start: getting-started.md
      - Keybindings: keybindings.md
  - Operate:
      - TUI: tui.md
      - Operations and safety: ops.md
      - Resource relationships: resource-relationships.md
      - Helm and operators: helm-operators.md
      - Observability: observability.md
  - AI and integrations:
      - Embedded agent: agent.md
      - MCP server: mcp.md
      - Provider plugins: provider-plugins.md
  - Administration:
      - Air-gapped operation: airgap.md
      - Performance and scale: performance.md
      - Threat model: threat-model.md
  - Project:
      - Architecture: dev/specs/2026-08-12-korvid-architecture.md
      - Contributor docs: dev/README.md
      - Quality gates: dev/quality-gates.md
      - Release notes:
          - v0.3.0: release-notes/v0.3.0.md
          - v0.2.0: release-notes/v0.2.0.md
          - v0.1.2: release-notes/v0.1.2.md

markdown_extensions:
  - admonition
  - attr_list
  - md_in_html
  - pymdownx.details
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.inlinehilite
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
  - tables
  - toc:
      permalink: true

plugins:
  - search
  - privacy:
      assets: true
      assets_fetch: true
      assets_expr_map:
        .js: '(?P<url>https://unpkg\.com/[^"'']+)'

extra_css:
  - stylesheets/extra.css
```

- [ ] **Step 2: Add the documentation dependency and commands**

Append `docs = ["mkdocs-material>=9.7.7,<10"]` under `[dependency-groups]`.
Add `docs-build` and `docs-serve` to `.PHONY`, then add:

```make
docs-build:
	uv run --frozen --group docs mkdocs build --strict

docs-serve:
	uv run --frozen --group docs mkdocs serve
```

- [ ] **Step 3: Lock only against public PyPI**

Run:

```bash
env -u UV_INDEX -u UV_DEFAULT_INDEX -u UV_INDEX_URL \
    -u UV_EXTRA_INDEX_URL -u UV_FIND_LINKS \
    uv lock --no-config
```

Expected: `uv.lock` contains only `pypi.org` and `files.pythonhosted.org` URL/registry hosts. This also removes the pre-existing private mirror URLs that make `tests/test_homebrew_formula.py::test_every_resource_carries_a_pypi_url_and_a_sha256` fail on the baseline commit.

- [ ] **Step 4: Run the red build**

Run: `make docs-build`

Expected: FAIL because `docs/index.md`, `docs/getting-started.md`, `docs/overrides/home.html`, `docs/assets/korvid-mark.svg`, or `docs/stylesheets/extra.css` do not exist yet.

- [ ] **Step 5: Commit the build foundation**

```bash
git add mkdocs.yml pyproject.toml uv.lock Makefile
git commit -m "build: add reproducible documentation site"
```

---

### Task 2: K9s-inspired korvid landing experience

**Files:**
- Create: `docs/index.md`
- Create: `docs/getting-started.md`
- Create: `docs/overrides/home.html`
- Create: `docs/assets/korvid-mark.svg`
- Create: `docs/stylesheets/extra.css`

**Interfaces:**
- Consumes: `docs/assets/demo.mp4`, existing user guides, and the navigation/configuration from Task 1.
- Produces: A responsive product landing page, install guide, and korvid-specific visual identity rendered through MkDocs.

- [ ] **Step 1: Create the landing content**

Create `docs/index.md` with:

```markdown
---
template: home.html
title: korvid
hide:
  - navigation
  - toc
---

<section class="hero">
  <p class="eyebrow">AI-NATIVE KUBERNETES TUI</p>
  <h1>A tool-using bird<br>for your cluster.</h1>
  <p class="hero-copy">Browse, diagnose, and operate Kubernetes from a keyboard-first cockpit. Add an agent that sees your screen—or let your editor inspect the cluster over MCP—without giving either one an unchecked write path.</p>
  <div class="hero-actions">
    <a class="md-button md-button--primary" href="getting-started/">Start flying</a>
    <a class="md-button" href="https://github.com/hellices/korvid">View on GitHub</a>
  </div>
  <div class="install-command"><code>brew install hellices/korvid/korvid</code></div>
</section>

<figure class="product-demo">
  <video src="assets/demo.mp4" controls autoplay muted loop playsinline aria-label="korvid browsing pods, filtering resources, describing a pod, following logs, and opening help">Your browser does not support the korvid demo video.</video>
  <figcaption>The cockpit works with your kubeconfig alone. AI is optional.</figcaption>
</figure>

## One operational experience. Three ways to drive it.

Korvid runs one session: one resource cache, one evidence trail, one
approval gate. **Different surfaces. One operational state.** A human
operator's keyboard, the embedded agent's tool calls, and an external MCP
client's requests all read the same navigation model and the same evidence,
and every write — however it starts — crosses the same approval and audit
boundary.

<div class="feature-grid">
  <article><span>01</span><h3>Drive it yourself</h3><p>Browse, filter, follow relationships, and run guarded operations straight from the keyboard — the same resource view, evidence, and safety gate every other actor shares.</p></article>
  <article><span>02</span><h3>Delegate to the agent</h3><p>An embedded agent reads the same evidence you see, cites it, and drives korvid on your behalf. Its writes still cross the same confirmation gate as yours.</p></article>
  <article><span>03</span><h3>Connect over MCP</h3><p>When enabled, give VS Code, Claude Code, Cursor, or Zed bounded reads of the same cluster state and evidence. Write proposals stay opt-in and always need a human to confirm.</p></article>
</div>

## Sharp tools. Human hands.

Every mutation requires a fresh keystroke confirmation — whether you
triggered it directly, the embedded agent proposed it after reading the
evidence, or an MCP client submitted a request when MCP is enabled. MCP
write proposals are opt-in and never executed automatically. Whoever
initiates it, the write converges on the same in-TUI confirmation and the
same fail-closed audit path. Secret values are masked before model calls,
and `--readonly` removes the write path entirely.

[Read the safety model](ops.md){ .md-button .korvid-button } [Inspect the threat model](threat-model.md){ .md-button .korvid-button }

## Find your flight path

- **Operating a cluster?** Start with the [five-minute guide](getting-started.md), then keep the [key reference](keybindings.md) nearby.
- **Adding AI?** Configure the [embedded agent](agent.md) or connect an [external MCP client](mcp.md).
- **Evaluating production use?** Read [performance and scale](performance.md), [air-gapped operation](airgap.md), and the [threat model](threat-model.md).
- **Contributing?** Begin with the [architecture](dev/specs/2026-08-12-korvid-architecture.md) and [quality gates](dev/quality-gates.md).
```

- [ ] **Step 2: Create the install guide**

Create `docs/getting-started.md` with supported Python/OS requirements, Homebrew and `uv tool` install commands, the base/agent/MCP/all extras table, the first `korvid` invocation, a ten-key quick reference, and links to `tui.md`, `agent.md`, `mcp.md`, and `airgap.md`. State that the current published release is `0.3.0`, pin intentionally versioned install and upgrade commands to `0.3.0`, present the live `brew install hellices/korvid/korvid` route, and retain the Git fallback install command from README for readers wanting unreleased `main`.

- [ ] **Step 3: Add the template, mark, and responsive visual system**

Create `docs/overrides/home.html`:

```jinja2
{% extends "main.html" %}

{% block tabs %}{% endblock %}

{% block content %}
  {{ super() }}
{% endblock %}
```

Create an original `docs/assets/korvid-mark.svg` using geometric wing and eye
shapes with `currentColor`; do not use K9s artwork. Create
`docs/stylesheets/extra.css` with CSS custom properties, an amber-on-charcoal
hero, responsive two-column demo layout above 960px, a three-card feature grid,
visible keyboard focus, reduced-motion handling, and standard readable
documentation widths.

- [ ] **Step 4: Run the green build**

Run: `make docs-build`

Expected: PASS with `Documentation built in ... seconds`; `site/index.html`,
`site/getting-started/index.html`, and `site/search/search_index.json` exist.

- [ ] **Step 5: Commit the landing experience**

```bash
git add docs/index.md docs/getting-started.md docs/overrides/home.html \
  docs/assets/korvid-mark.svg docs/stylesheets/extra.css
git commit -m "docs: launch product-led korvid site"
```

---

### Task 3: Canonical entry points and Pages deployment

**Files:**
- Create: `.github/workflows/docs.yml`
- Modify: `README.md`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `make docs-build` and the static `site/` output from Tasks 1–2.
- Produces: Pull-request build validation, `main` deployment, and canonical links to the official site.

- [ ] **Step 1: Point users to the official documentation**

Set `[project.urls].Documentation` in `pyproject.toml` to
`https://hellices.github.io/korvid/`. Add a `Documentation` link near the top
of `README.md`, and replace user-guide GitHub blob links in the README feature
index with their `https://hellices.github.io/korvid/<slug>/` equivalents.
Keep source, security-reporting, demo-source, and development-plan links on
GitHub.

- [ ] **Step 2: Add the Pages workflow**

Create `.github/workflows/docs.yml` with `pull_request` and `push` on `main`,
path filters for `docs/**`, `mkdocs.yml`, `pyproject.toml`, `uv.lock`,
`Makefile`, and the workflow itself. Use:

- `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1`
- `astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9`
- `actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b`
- `actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b`
- `actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e`

The build job runs on `ubuntu-latest` with `contents: read`, uses uv version
`0.10.9`, runs `uv sync --locked --group docs`, then
`uv run --frozen --group docs mkdocs build --strict`, and uploads `site/` only
on pushes to `main`. The deploy job also runs only on pushes to `main`, needs the build job, has
`pages: write`/`id-token: write`, uses the `github-pages` environment, and sets
its URL from the deploy step output. `configure-pages` is the deploy job's first
step under `pages: write`; `deploy-pages` follows it.

Before the first deployment can succeed, a repository admin must enable Pages
once, out-of-band from this workflow: **Settings -> Pages -> Build and
deployment -> Source: GitHub Actions.** This one-time setting has no code
representation; record it here so a clean run of this plan does not end with
a deploy job that fails because Pages is still on its default source. No
server or hosting infrastructure is otherwise required — GitHub Pages serves
the built static site.

- [ ] **Step 3: Validate links, metadata, and workflow**

Run:

```bash
make docs-build
uv run pytest -p no:tach tests/test_homebrew_formula.py::test_every_resource_carries_a_pypi_url_and_a_sha256 -q
uvx zizmor --min-severity medium .github/workflows/docs.yml
```

Expected: all three commands pass; no private mirror URL remains in `uv.lock`.

- [ ] **Step 4: Commit deployment and entry points**

```bash
git add .github/workflows/docs.yml README.md pyproject.toml
git commit -m "ci: publish official documentation site"
```

---

### Task 4: Rendered-site acceptance

**Files:**
- Modify only files from Tasks 1–3 if browser verification exposes a defect.
- Create: `docs/overrides/partials/copyright.html` (footer defect)
- Create: `tests/test_docs_landing_design.py`
- Create: `tests/test_wheel_packaging.py`

**Interfaces:**
- Consumes: Locally rendered `site/` output.
- Produces: Evidence that the implementation meets the visual, responsive, search, navigation, and content requirements.

- [x] **Step 1: Start a local server**

Run `uv run --frozen --group docs mkdocs serve --dev-addr 127.0.0.1:8765` as a detached
background server and verify `curl --fail http://127.0.0.1:8765/`.

- [x] **Step 2: Verify desktop rendering**

Open `http://127.0.0.1:8765/` at 1440×1000. Confirm the hero, install command,
demo, feature cards, safety statement, and flight-path links are visible;
confirm no horizontal overflow and no broken image.

- [x] **Step 3: Verify narrow rendering and search**

Resize to 390×844. Confirm actions and cards stack, navigation opens, body text
remains readable, and keyboard focus is visible. Search for `fail-closed` and
confirm a safety-related result appears.

- [x] **Step 4: Run final repository checks**

Run:

```bash
make docs-build
uv run pre-commit run --all-files --show-diff-on-failure
uv run pytest -x -q
git diff --check
```

Expected: all commands pass. Stop the local server and remove the generated
`site/` directory if MkDocs did not clean it.

**Behind a mirror, prefix every command with `UV_FROZEN=1`.** The `mypy`
pre-commit hook runs bare `uv run mypy`, which re-resolves and rewrites
`uv.lock` to private-index URLs; `UV_FROZEN=1` keeps the lock untouched
and the `no-private-index-in-lock` hook green.

- [x] **Step 5: Prove the wheel ships code only, not the docs site**

The site added `docs/`, `mkdocs.yml`, `docs/stylesheets/`, `docs/overrides/`
and a `docs` dependency group. None of it may reach the published artifact.
Reproducible proof:

```bash
UV_FROZEN=1 uv build --wheel --out-dir .wheel-proof
uv run --frozen python - <<'PY'
import pathlib, zipfile
wheel = next(pathlib.Path(".wheel-proof").glob("*.whl"))
with zipfile.ZipFile(wheel) as zf:
    names = zf.namelist()
    print("stray:", [n for n in names if not n.startswith(("korvid/", "korvid-"))])
    meta = zf.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
print("mkdocs:", [line for line in meta.splitlines() if "mkdocs" in line.lower()])
PY
rm -rf .wheel-proof
```

Expected: `stray: []` and `mkdocs: []`. `UV_FROZEN=1` is mandatory — a bare
`uv build` re-locks. The always-on source invariants behind this proof live
in `tests/test_wheel_packaging.py`.

- [x] **Step 6: Commit any acceptance fixes**

If browser verification required changes, commit only those files:

```bash
git add docs/index.md docs/stylesheets/extra.css \
  docs/overrides/partials/copyright.html \
  tests/test_docs_landing_design.py tests/test_wheel_packaging.py \
  tests/test_docs_build_config.py \
  docs/superpowers/plans/2026-08-21-official-documentation-site.md
git commit -m "fix: polish documentation site rendering"
```
