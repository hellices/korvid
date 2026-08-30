# Public Documentation Pruning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove internal quality-gate content from the official site and turn long public pages into concise, visual product guides.

**Architecture:** MkDocs remains the publishing layer. `exclude_docs` defines the public boundary, `nav` exposes only intentional destinations, and readability tests enforce route/length/content contracts. Each long page keeps its subject-specific visual structure and links to repository or dedicated reference pages instead of embedding implementation procedures.

**Tech Stack:** MkDocs Material, Markdown, pytest, existing documentation contract tests, local `curl`.

## Global Constraints

- Preserve the landing page and Quick Start experience.
- Preserve existing useful screenshots, MP4/GIF assets, SVGs, Mermaid diagrams, storyboards, and compact tables.
- Do not regenerate binary media.
- Exclude `dev/quality-gates.md` from both navigation and the generated site.
- Agent and Performance must each remain below 2,000 words.
- Other product guides must remain below 2,200 words unless explicitly presented as technical reference.
- Do not replace long pages with repetitive purpose/summary templates.
- Retained safety and product claims must match current behavior.

---

### Task 1: Remove quality gates from the public site

**Files:**
- Modify: `mkdocs.yml`
- Modify: `docs/dev/README.md`
- Modify: `tests/test_docs_build_config.py`
- Modify: `tests/test_docs_links.py`

**Interfaces:**
- Consumes: MkDocs `exclude_docs` and `nav`.
- Produces: no generated `/dev/quality-gates/` route and no internal link to it.

- [ ] **Step 1: Write the failing public-boundary tests**

Add contracts equivalent to:

```python
def test_quality_gates_are_not_part_of_the_public_site() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    excluded = str(config["exclude_docs"])
    nav_text = json.dumps(config["nav"])
    assert "dev/quality-gates.md" in excluded
    assert "quality-gates" not in nav_text.lower()


def test_contributor_page_links_to_quality_gates_in_the_repository() -> None:
    source = (ROOT / "docs/dev/README.md").read_text(encoding="utf-8")
    assert "https://github.com/hellices/korvid/blob/main/docs/dev/quality-gates.md" in source
    assert "](quality-gates.md)" not in source
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/pytest -p no:tach \
  tests/test_docs_build_config.py \
  tests/test_docs_links.py -q
```

Expected: failure because `Quality gates` is still in `nav`, not excluded, and contributor docs use a local link.

- [ ] **Step 3: Define the public boundary**

In `mkdocs.yml`:

```yaml
exclude_docs: |
  overrides/
  dev/plans/
  dev/quality-gates.md
  superpowers/
```

Remove:

```yaml
- Quality gates: dev/quality-gates.md
```

Change the contributor page link to:

```markdown
[`quality-gates.md`](https://github.com/hellices/korvid/blob/main/docs/dev/quality-gates.md)
```

- [ ] **Step 4: Verify GREEN and generated-route absence**

Run:

```bash
.venv/bin/pytest -p no:tach tests/test_docs_build_config.py tests/test_docs_links.py -q
.venv/bin/mkdocs build --strict
test ! -e site/dev/quality-gates/index.html
```

Expected: all pass and the generated file is absent.

- [ ] **Step 5: Commit**

```bash
git add mkdocs.yml docs/dev/README.md tests/test_docs_build_config.py tests/test_docs_links.py
git commit -m "docs: remove quality gates from the public site"
```

### Task 2: Restore the Agent page as a product guide

**Files:**
- Modify: `docs/agent.md`
- Modify: `tests/test_docs_readability.py`
- Modify: `tests/test_docs_visual_assets.py`
- Modify: `tests/test_docs_agent_contracts.py`

**Interfaces:**
- Consumes: current Agent screenshot, provider table, safety and evidence contracts.
- Produces: a sub-2,000-word Agent guide with no embedded eval or migration manual.

- [ ] **Step 1: Write failing Agent readability contracts**

Add:

```python
def test_agent_page_is_a_product_guide_not_an_internal_manual() -> None:
    source = _source("agent.md")
    assert len(source.split()) < 2_000
    assert source.count("\n## ") <= 9
    assert "agent-poster.png" in source
    assert "DefaultAgentSession" in source
    assert "NativeAgentEngine" in source
    assert "fresh user keystroke" in source
    assert "Agent eval harness" not in source
    assert "Upgrading from the profile-based agent" not in source
    assert "uv run python -m korvid.evals" not in source
```

Keep or add focused contracts for:

```python
assert ":ai payload" in source
assert ":ai follow off" in source
assert "provider-plugins.md" in source
assert "airgap.md" in source
assert "evals/methodology.md" in source
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest -p no:tach \
  tests/test_docs_readability.py \
  tests/test_docs_visual_assets.py \
  tests/test_docs_agent_contracts.py -q
```

Expected: current 4,418-word/14-section page violates the size and internal-manual assertions.

- [ ] **Step 3: Rebuild `docs/agent.md` around nine decisions**

Retain:

1. concise `[agent]` installation;
2. existing Agent poster/storyboard;
3. prompt → bounded reads → cited answer;
4. direct control and interrupt/follow;
5. write approval and audit boundary;
6. payload inspection and masking;
7. compact provider table;
8. compact low/high tier table;
9. recording provenance and links to air-gap, plugin, and eval references.

Delete embedded procedures for:

- eval CLI campaigns and journey commands;
- profile migration tables;
- full prompt-pack/tool-description inventories;
- repeated offline configuration already covered by `airgap.md`;
- repeated provider-plugin lifecycle text covered by `provider-plugins.md`.

- [ ] **Step 4: Update factual tests instead of weakening them**

Change assertions that require deleted internal prose into assertions on the linked authoritative destinations. Keep security invariants and recording-pipeline assertions.

- [ ] **Step 5: Verify Agent GREEN**

Run:

```bash
.venv/bin/pytest -p no:tach \
  tests/test_docs_readability.py \
  tests/test_docs_visual_assets.py \
  tests/test_docs_agent_contracts.py \
  tests/test_agent_replacement_guard.py -q
.venv/bin/mkdocs build --strict
```

Expected: pass; `wc -w docs/agent.md` reports less than 2,000.

- [ ] **Step 6: Commit**

```bash
git add docs/agent.md tests/test_docs_readability.py \
  tests/test_docs_visual_assets.py tests/test_docs_agent_contracts.py
git commit -m "docs: refocus the Agent guide on the product workflow"
```

### Task 3: Prune long operational and technical pages

**Files:**
- Modify: `docs/performance.md`
- Modify: `docs/threat-model.md`
- Modify: `docs/provider-plugins.md`
- Modify: `docs/ops.md`
- Modify: `docs/overview.md`
- Modify: `tests/test_docs_readability.py`
- Modify: `tests/test_docs_links.py`
- Modify: directly related factual contract tests only when their source prose is intentionally replaced

**Interfaces:**
- Consumes: existing diagrams, measured tables, safety invariants, public API 2 facts.
- Produces: page-specific concise guides with links to raw/reference material.

- [ ] **Step 1: Write failing page-budget and preservation tests**

Add:

```python
def test_public_product_guides_stay_bounded() -> None:
    limits = {
        "overview.md": 1_400,
        "ops.md": 1_600,
        "performance.md": 2_000,
        "threat-model.md": 2_000,
        "provider-plugins.md": 2_200,
    }
    for page, maximum in limits.items():
        words = len(_source(page).split())
        assert words <= maximum, f"{page} has {words} words; limit is {maximum}"
```

Preserve markers:

```python
assert "Supported envelope" in performance
assert "Known limits" in performance
assert "Raw artifacts" in performance
assert "```mermaid" in threat_model
assert "Residual risks" in threat_model
assert "API 2" in provider_plugins
assert "fresh user keystroke" in ops.lower()
assert "fail-closed" in ops.lower()
```

- [ ] **Step 2: Verify RED**

Run:

```bash
.venv/bin/pytest -p no:tach tests/test_docs_readability.py tests/test_docs_links.py -q
```

Expected: Performance, Threat model, Ops, and Overview exceed their bounds.

- [ ] **Step 3: Prune each page according to its own structure**

- `performance.md`: one supported-envelope table, one interpretation section,
  one known-limits section, one raw-artifact link section.
- `threat-model.md`: keep the Mermaid boundary, protected assets/controls,
  residual risks, and deployment choices; remove implementation inventory.
- `provider-plugins.md`: keep “when to use,” API 2 surface, one minimal adapter,
  trust warning, and lifecycle/compatibility checklist.
- `ops.md`: keep approval/audit diagram, readonly/protected context, operation
  evidence table, and long-lived sessions; remove repeated Agent-tier prose.
- `overview.md`: keep the product map and paths to the focused guides; remove
  repeated feature catalog prose.

- [ ] **Step 4: Verify page-specific facts and links**

Run:

```bash
.venv/bin/pytest -p no:tach \
  tests/test_docs_readability.py \
  tests/test_docs_links.py \
  tests/test_docs_visual_assets.py \
  tests/test_docs_agent_contracts.py -q
.venv/bin/mkdocs build --strict
```

Expected: pass with all page budgets met.

- [ ] **Step 5: Commit**

```bash
git add docs/{performance,threat-model,provider-plugins,ops,overview}.md \
  tests/test_docs_readability.py tests/test_docs_links.py
git commit -m "docs: prune public guides to their operator essentials"
```

### Task 4: Validate the public experience and update the PR

**Files:**
- Modify only if verification exposes a direct regression.

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: strict build, route checks, local preview, updated PR.

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
```

Expected: all pass.

- [ ] **Step 2: Verify measurable outcomes**

```bash
test ! -e site/dev/quality-gates/index.html
test "$(wc -w < docs/agent.md)" -lt 2000
test "$(wc -w < docs/performance.md)" -lt 2000
git diff --exit-code -- uv.lock docs/assets/scenes docs/assets/mcp-follow-demo.gif
```

Expected: all commands exit 0.

- [ ] **Step 3: Verify the live preview**

With the existing server on `127.0.0.1:8981`:

```bash
curl -fsS http://127.0.0.1:8981/korvid/ >/dev/null
curl -fsS http://127.0.0.1:8981/korvid/agent/ >/dev/null
curl -fsS http://127.0.0.1:8981/korvid/performance/ >/dev/null
test "$(curl -sS -o /dev/null -w '%{http_code}' \
  http://127.0.0.1:8981/korvid/dev/quality-gates/)" = 404
```

Expected: product routes return 200 and quality-gates returns 404.

- [ ] **Step 4: Review, commit any direct fixes, and push**

Run an independent code review of the change set, fix only credible findings,
then push without force.

- [ ] **Step 5: Update PR #315**

Update the PR body with the public-site pruning, tests, word counts, route
removal, and preview URL. Re-run the review/check loop until required checks
are successful and no credible unresolved findings remain.
