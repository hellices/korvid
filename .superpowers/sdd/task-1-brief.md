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

theme:
  name: material
  custom_dir: docs/overrides
  language: en
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

