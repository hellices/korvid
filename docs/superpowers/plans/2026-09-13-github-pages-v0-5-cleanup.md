# GitHub Pages v0.5 Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the public GitHub Pages release path clearly distinguish stable installs from `main`, and keep internal engineering pages out of public search and sitemap results without deleting them from the repository.

**Architecture:** Keep the current MkDocs Material site and landing page. Use Material's supported `meta` plugin to apply `search.exclude: true` to `docs/dev/` by default, explicitly allowlist the contributor index and current architecture page, and override MkDocs' sitemap template so the same metadata controls sitemap publication. Pin the user-facing copy and metadata policy with dependency-free tests.

**Tech Stack:** MkDocs 1.6.1, Material for MkDocs 9.7.7, YAML front matter, Jinja sitemap override, pytest.

## Global Constraints

- Do not redesign the home page or add a frontend dependency.
- Preserve the Direct, Agent, and MCP media assets and controller behavior.
- Stable Homebrew, `uv tool`, and `pipx` installation must appear before development-source installation.
- Keep repository-only engineering documents available through repository links; do not duplicate their content.
- Keep the current architecture page, contributor index, release notes, and evaluation evidence searchable.
- Do not claim Pulse or Action Palette is shipped before its implementation merges.
- Do not change any runtime dependency or `uv.lock`.

---

### Task 1: Clarify the stable installation and unreleased paths

**Files:**
- Create: `tests/test_docs_public_site.py`
- Modify: `docs/getting-started.md`
- Modify: `docs/release-notes/unreleased.md`
- Modify: `mkdocs.yml`

**Interfaces:**
- Consumes: existing Markdown headings and MkDocs `nav` entries.
- Produces: stable-install ordering and an explicit `Unreleased (main)` contract used by later validation.

- [ ] **Step 1: Write failing release-path tests**

```python
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent


def test_stable_install_precedes_development_source() -> None:
    source = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    stable = source.index("## Install")
    development = source.index("## Development build")
    git_source = source.index("git+https://github.com/hellices/korvid")
    assert stable < development < git_source


def test_unreleased_page_identifies_main_and_stable_release() -> None:
    source = (ROOT / "docs" / "release-notes" / "unreleased.md").read_text(encoding="utf-8")
    opening = source.split("## ", 1)[0]
    assert "`main`" in opening
    assert "https://github.com/hellices/korvid/releases/latest" in opening
    assert "https://github.com/hellices/korvid/milestone/6" in opening


def test_release_navigation_labels_unreleased_main() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    release_notes = config["nav"][-1]["Project"][-1]["Release notes"]
    assert release_notes[0] == {
        "Unreleased (main)": "release-notes/unreleased.md"
    }
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
uv run pytest -p no:tach tests/test_docs_public_site.py -q
```

Expected: three failures because the development heading, opening disclosure, and new nav label do not exist.

- [ ] **Step 3: Reorder the Getting Started source install**

Keep `## Current release` as prose pointing at the latest GitHub Release. Move the Git source command below `## Choose your extras`:

````markdown
## Development build

The guides describe `main`, which can include changes not yet published.
To test the reviewed source instead of the latest PyPI release:

```sh
uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'
```
````

- [ ] **Step 4: Add the Unreleased disclosure and nav label**

Immediately after `# Unreleased`, add:

```markdown
This page tracks changes merged to `main` that are not part of the
[latest published release](https://github.com/hellices/korvid/releases/latest).
The [v0.5.0 milestone](https://github.com/hellices/korvid/milestone/6)
tracks the current release scope.
```

Change the first release-note nav entry to:

```yaml
- Unreleased (main): release-notes/unreleased.md
```

- [ ] **Step 5: Run the release-path tests**

Run:

```bash
uv run pytest -p no:tach tests/test_docs_public_site.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/getting-started.md docs/release-notes/unreleased.md mkdocs.yml \
  tests/test_docs_public_site.py
git commit -m "docs: clarify stable and unreleased site paths"
```

---

### Task 2: Bound public search and sitemap scope

**Files:**
- Modify: `tests/test_docs_public_site.py`
- Modify: `mkdocs.yml`
- Create: `docs/dev/.meta.yml`
- Modify: `docs/dev/README.md`
- Modify: `docs/dev/specs/2026-08-12-korvid-architecture.md`
- Modify: `docs/release.md`
- Create: `docs/overrides/sitemap.xml`

**Interfaces:**
- Consumes: Material's `meta` plugin and `search.exclude` page metadata.
- Produces: one inherited internal-doc policy shared by search and sitemap generation.

- [ ] **Step 1: Write failing publication-scope tests**

Append tests that require:

```python
def test_internal_docs_use_one_inherited_search_policy() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    plugins = [
        item if isinstance(item, str) else next(iter(item))
        for item in config["plugins"]
    ]
    assert plugins.index("meta") < plugins.index("search")

    defaults = yaml.safe_load(
        (ROOT / "docs" / "dev" / ".meta.yml").read_text(encoding="utf-8")
    )
    assert defaults == {"search": {"exclude": True}}


def test_public_dev_entrypoints_override_the_internal_default() -> None:
    for relative in (
        "docs/dev/README.md",
        "docs/dev/specs/2026-08-12-korvid-architecture.md",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert source.startswith("---\nsearch:\n  exclude: false\n---\n")


def test_maintainer_release_runbook_is_not_search_indexed() -> None:
    source = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    assert source.startswith("---\nsearch:\n  exclude: true\n---\n")


def test_sitemap_uses_the_search_exclusion_policy() -> None:
    source = (ROOT / "docs" / "overrides" / "sitemap.xml").read_text(
        encoding="utf-8"
    )
    assert 'file.page.meta.get("search", {})' in source
    assert 'search.get("exclude", false)' in source
```

- [ ] **Step 2: Run the publication-scope tests and verify they fail**

Run:

```bash
uv run pytest -p no:tach tests/test_docs_public_site.py -q
```

Expected: failures for missing `meta`, `.meta.yml`, front matter, and sitemap override.

- [ ] **Step 3: Enable inherited metadata**

Configure plugins in this order:

```yaml
plugins:
  - meta
  - search
  - privacy:
      assets: true
      assets_fetch: false
```

Create `docs/dev/.meta.yml`:

```yaml
search:
  exclude: true
```

Add this front matter to `docs/dev/README.md` and the current architecture page:

```yaml
---
search:
  exclude: false
---
```

Add this front matter to the maintainer release runbook:

```yaml
---
search:
  exclude: true
---
```

- [ ] **Step 4: Add the sitemap override**

Copy MkDocs' sitemap template into `docs/overrides/sitemap.xml` and add the same metadata condition:

```jinja
{%- for file in pages -%}
    {% set search = file.page.meta.get("search", {}) %}
    {% if not file.page.is_link
          and (file.page.abs_url or file.page.canonical_url)
          and not search.get("exclude", false) %}
```

Preserve the canonical URL and `lastmod` rendering from the upstream template.

- [ ] **Step 5: Run publication-scope tests**

Run:

```bash
uv run pytest -p no:tach tests/test_docs_public_site.py -q
```

Expected: PASS.

- [ ] **Step 6: Build and inspect generated publication artifacts**

Run:

```bash
uv run --frozen --group docs mkdocs build --strict
python - <<'PY'
import json
import xml.etree.ElementTree as ET
from pathlib import Path

index = json.loads(Path("site/search/search_index.json").read_text())
dev_pages = {
    item["location"].split("#", 1)[0]
    for item in index["docs"]
    if item["location"].startswith("dev/")
}
assert dev_pages == {"dev/", "dev/specs/2026-08-12-korvid-architecture/"}

root = ET.parse("site/sitemap.xml").getroot()
locations = {element.text for element in root.iter("{*}loc")}
assert "https://hellices.github.io/korvid/dev/" in locations
assert (
    "https://hellices.github.io/korvid/dev/specs/"
    "2026-08-12-korvid-architecture/"
) in locations
assert "https://hellices.github.io/korvid/dev/ui-controllers/" not in locations
assert "https://hellices.github.io/korvid/release/" not in locations
PY
```

Expected: strict build passes; only the two allowlisted `dev/` pages remain in search and sitemap.

- [ ] **Step 7: Commit**

```bash
git add mkdocs.yml docs/dev/.meta.yml docs/dev/README.md \
  docs/dev/specs/2026-08-12-korvid-architecture.md docs/release.md \
  docs/overrides/sitemap.xml tests/test_docs_public_site.py
git commit -m "docs: bound public search and sitemap scope"
```

---

### Task 3: Run the full documentation gate and prepare the pull request

**Files:**
- Modify only if validation exposes a defect in the files above.

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces: review evidence for issue #391.

- [ ] **Step 1: Run focused lint**

```bash
uv run ruff check tests/test_docs_public_site.py
uv run ruff format --check tests/test_docs_public_site.py
```

Expected: PASS.

- [ ] **Step 2: Run all documentation tests**

```bash
uv run pytest -p no:tach \
  tests/test_docs_agent_contracts.py \
  tests/test_docs_build_config.py \
  tests/test_docs_landing_behavior.py \
  tests/test_docs_links.py \
  tests/test_docs_media_assets.py \
  tests/test_docs_public_site.py \
  tests/test_docs_readability.py \
  tests/test_docs_site_entrypoints.py \
  tests/test_docs_workflow.py -q
```

Expected: PASS.

- [ ] **Step 3: Run strict site build**

```bash
uv run --frozen --group docs mkdocs build --strict
```

Expected: PASS.

- [ ] **Step 4: Validate rendered behavior**

Serve or open the built site and verify:

- 1440x900 and 390x844 have no horizontal overflow;
- Direct, Agent, and MCP tabs activate and load their video or fallback;
- Getting Started shows stable install before Development build;
- Unreleased identifies `main` and links the stable release and milestone;
- public search excludes internal implementation specs.

- [ ] **Step 5: Commit any validation-only corrections**

```bash
git add -A
git commit -m "test: verify public documentation scope"
```

Skip this commit when validation required no corrections.

- [ ] **Step 6: Request review and open the PR**

Run the repository's review workflow, publish the branch, and open a pull request with:

- a summary of the stable-install and release-state cleanup;
- the before/after search-index scope;
- exact test and strict-build evidence;
- desktop/mobile browser validation;
- `Closes #391`.
