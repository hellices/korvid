# Distinctive Homepage Highlights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the generic homepage highlights with three concise promises that reveal korvid's shared-workspace, checkable-evidence, and human-authority model without competitor comparisons.

**Architecture:** Keep the existing three-card HTML and CSS component unchanged. Update its semantic contract in the landing-page tests first, then replace only the section heading, labels, paragraphs, and links in `docs/index.md`; all media, responsive, accessibility, and JavaScript behavior remain untouched.

**Tech Stack:** MkDocs Material, HTML embedded in Markdown, pytest, Python regular-expression contract tests

## Global Constraints

- The labels are exactly `ONE WORKSPACE`, `CHECKABLE EVIDENCE`, and `HUMAN AUTHORITY`, in that order.
- The section heading is exactly `One cockpit. Three ways in. You stay in command.`
- Do not mention competitors or use comparative words such as `better`, `unlike`, or `more than`.
- Keep each card to one paragraph of no more than 40 words and no more than three internal documentation links.
- MCP follow is optional and fire-and-forget; it does not alter or gate tool results.
- Do not imply that every Agent sentence is cited, that Direct, Agent, and MCP share one snapshot, or that every MCP result receives the embedded-provider masking policy.
- Do not imply that Agent or MCP can approve a write.
- Preview language remains `best-effort` and `operation-specific`.
- The homepage remains below 800 source words and retains exactly three major content blocks.
- Do not modify CSS, JavaScript, media, focused guides, or product code.

---

## File structure

- Modify `tests/test_docs_landing_design.py`: rename the highlight helper contract and pin the new labels, framing, links, capability claims, and safety limitations.
- Modify `docs/index.md`: replace only the `feature-highlights` heading and the contents of its three cards.

### Task 1: Reframe the homepage highlights around korvid's interaction model

**Files:**
- Modify: `tests/test_docs_landing_design.py:446`
- Modify: `tests/test_docs_landing_design.py:2366-2424`
- Modify: `tests/test_docs_landing_design.py:2507-2573`
- Modify: `tests/test_docs_landing_design.py:2708-2759`
- Modify: `tests/test_docs_landing_design.py:3125-3164`
- Modify: `tests/test_docs_landing_design.py:3216-3241`
- Modify: `docs/index.md:45-76`

**Interfaces:**
- Consumes: `_highlights() -> str`, `_highlight(label: str) -> str`, `_flatten(markup: str) -> str`, and the existing three-card `feature-highlights` markup.
- Produces: a landing-page contract whose labels, claims, and links match the selected design; no runtime or Python interface changes.

- [ ] **Step 1: Write the failing label, framing, and one-workspace tests**

Update the helper docstring and the three-card structural test, then add a focused shared-workspace test:

```python
def _highlight(label: str) -> str:
    """One workspace / evidence / authority card in the highlights section."""
    cards: list[str] = re.findall(r"<article>.*?</article>", _highlights(), re.DOTALL)
    matching = [card for card in cards if f">{label}<" in card]
    assert len(matching) == 1, f"exactly one highlight must carry the {label!r} promise"
    return matching[0]


def test_feature_highlights_are_three_linked_promises() -> None:
    cards = re.findall(r"<article>.*?</article>", _highlights(), re.DOTALL)
    assert len(cards) == 3, f"exactly three promises; found {len(cards)}"
    labels = [re.search(r"<span>([^<]+)</span>", card) for card in cards]
    assert [label.group(1) for label in labels if label is not None] == [
        "ONE WORKSPACE",
        "CHECKABLE EVIDENCE",
        "HUMAN AUTHORITY",
    ], "the promises stay in the order a visitor meets them"
    assert "One cockpit. Three ways in. You stay in command." in _highlights()
    for card in cards:
        paragraphs = re.findall(r"<p>(.*?)</p>", card, re.DOTALL)
        assert len(paragraphs) == 1, "one paragraph per promise keeps the page scannable"
        assert len(re.sub(r"<[^>]+>", " ", paragraphs[0]).split()) <= 40
        links = re.findall(r'<a href="([^"]+)"', card)
        assert 2 <= len(links) <= 3
        for href in links:
            assert re.fullmatch(r"[a-z0-9-]+/(?:#[a-z0-9-]+)?", href)


def test_one_workspace_highlight_keeps_every_driver_visible() -> None:
    workspace = _flatten(_highlight("ONE WORKSPACE"))
    for driver in ("keyboard", "embedded agent", "external mcp"):
        assert driver in workspace
    assert "same visible cockpit" in workspace
    assert "optional mcp follow" in workspace
    assert "supported reads" in workspace
    assert "notification" in workspace
    assert 'href="tui/"' in _highlight("ONE WORKSPACE")
    assert 'href="agent/#direct-control-and-the-conversation"' in _highlight(
        "ONE WORKSPACE"
    )
    assert 'href="mcp/#read-once-or-follow-activity"' in _highlight("ONE WORKSPACE")
```

- [ ] **Step 2: Rewrite the evidence tests for the new card**

Rename `test_ground_highlight_keeps_the_read_paths_truthful` to
`test_checkable_evidence_highlight_keeps_the_read_paths_truthful`. Select
`_highlight("CHECKABLE EVIDENCE")` and preserve these assertions:

```python
evidence = _highlight("CHECKABLE EVIDENCE")
lowered = _flatten(evidence)
for fact in (
    "watch-backed tables",
    "fresh describe and log reads",
    "bounded agent/mcp reads",
    "different moments",
    "snapshots can differ",
    "successful agent reads",
    "checkable citations",
    "evidence gaps",
):
    assert fact in lowered
for overclaim in ("same evidence", "same snapshot", "one snapshot", "every sentence"):
    assert overclaim not in lowered
for destination in ('href="agent/"', 'href="mcp/"', 'href="tui/#follow-one-signal"'):
    assert destination in evidence
```

In `test_no_landing_surface_reduces_the_tui_to_a_watch_backed_snapshot`, replace
the `GROUND` lookup with:

```python
evidence = _flatten(_highlight("CHECKABLE EVIDENCE"))
assert "snapshots can differ" in evidence
assert "identical" not in evidence
```

Update the Agent and MCP scene link assertions at the end of their existing
tests to select `CHECKABLE EVIDENCE`.

- [ ] **Step 3: Rewrite the write-boundary tests for the new card**

Rename `test_control_highlight_orders_confirmation_audit_and_execution` to
`test_human_authority_highlight_orders_confirmation_audit_and_execution`, keep
its ordering assertions, and select:

```python
authority = _highlight("HUMAN AUTHORITY")
lowered = _flatten(authority)
```

Adjust the unconditional approval phrases to match the new concise paragraph:

```python
ordered = ["every write", "fresh approval keystroke", "fail-closed audit", "blocks"]
positions = [lowered.find(stage) for stage in ordered]
assert all(position != -1 for position in positions)
assert positions == sorted(positions)
```

In `test_mcp_landing_copy_keeps_the_production_write_and_follow_limits`, select
`HUMAN AUTHORITY` and retain the `proposal` and `off by default` assertions.

In the provider/MCP disclosure test, select `HUMAN AUTHORITY` and assert the
paragraph contains:

```python
assert "provider payloads are masked" in prose
assert "mcp disclosure remains tool-specific" in prose
```

Keep the `threat-model/` destination assertion and the ban on the broader
`secret values are masked before model calls` claim.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```bash
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
.venv/bin/python -m pytest -p no:tach tests/test_docs_landing_design.py -q
```

Expected: FAIL because `docs/index.md` still contains `SEE / GROUND / CONTROL`,
the old heading, and none of the new shared-workspace or checkable-evidence copy.

- [ ] **Step 5: Replace the homepage highlight copy**

Replace only `docs/index.md` lines 45-76 with:

```html
<section class="feature-highlights" aria-labelledby="highlights-title">
  <h2 id="highlights-title">One cockpit. Three ways in. You stay in command.</h2>
  <div class="feature-highlights__grid">
    <article>
      <span>ONE WORKSPACE</span>
      <p>Keyboard, the embedded Agent, and external MCP clients meet in the same visible cockpit. Agent navigation moves the panes you see; optional MCP follow mirrors supported reads, while follow-off activity still surfaces as a notification.</p>
      <ul>
        <li><a href="tui/">Resource cockpit</a></li>
        <li><a href="agent/#direct-control-and-the-conversation">Shared Agent workspace</a></li>
        <li><a href="mcp/#read-once-or-follow-activity">MCP follow</a></li>
      </ul>
    </article>
    <article>
      <span>CHECKABLE EVIDENCE</span>
      <p>Watch-backed tables, fresh describe and log reads, and bounded Agent/MCP reads land at different moments, so snapshots can differ. Successful Agent reads mint checkable citations; deterministic diagnoses expose evidence gaps.</p>
      <ul>
        <li><a href="agent/">Agent evidence</a></li>
        <li><a href="mcp/">MCP boundary</a></li>
        <li><a href="tui/#follow-one-signal">Diagnosis surfaces</a></li>
      </ul>
    </article>
    <article>
      <span>HUMAN AUTHORITY</span>
      <p>Best-effort, operation-specific previews may run first. Every write still waits for a fresh approval keystroke and a fail-closed audit; a failed append blocks the mutation. MCP proposals stay off by default; provider payloads are masked, while MCP disclosure remains tool-specific.</p>
      <ul>
        <li><a href="ops/">Approval and audit</a></li>
        <li><a href="threat-model/">Provider and MCP boundaries</a></li>
        <li><a href="overview/">Architecture</a></li>
      </ul>
    </article>
  </div>
</section>
```

- [ ] **Step 6: Run focused tests and verify GREEN**

Run:

```bash
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
.venv/bin/python -m pytest -p no:tach tests/test_docs_landing_design.py -q
```

Expected: all tests in `test_docs_landing_design.py` pass.

- [ ] **Step 7: Run formatting and strict documentation validation**

Run:

```bash
repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"
.venv/bin/ruff check tests/test_docs_landing_design.py
.venv/bin/ruff format --check tests/test_docs_landing_design.py
.venv/bin/mkdocs build --strict
git diff --check
```

Expected: Ruff reports no errors, formatting is unchanged, MkDocs completes
without warnings, and `git diff --check` exits successfully.

- [ ] **Step 8: Inspect the rendered page**

Open the existing local preview at `http://127.0.0.1:8981/korvid/` and verify at
desktop and 390px mobile widths:

- the new heading and three labels are visible;
- the labels do not wrap awkwardly or overflow their cards;
- the hero and Direct / Agent / MCP switcher are unchanged;
- the page still has only the hero, highlights, and destination-navigation
  blocks;
- each highlight has one paragraph and three working internal links.

- [ ] **Step 9: Commit the implementation**

```bash
git add docs/index.md tests/test_docs_landing_design.py
git commit -m "docs: highlight korvid's shared operating model" \
  -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Expected: one commit containing only the homepage copy and its exact contract
tests.
