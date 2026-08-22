# Task 6 report

## Status

Complete. Added one selected visual explanation to each of the six core concept pages, reusing existing Task 1-2 assets only and preserving the product/security boundaries in the brief.

## Files

- `tests/test_docs_build_config.py`
- `docs/overview.md`
- `docs/tui.md`
- `docs/agent.md`
- `docs/mcp.md`
- `docs/ops.md`
- `docs/resource-relationships.md`
- `docs/stylesheets/extra.css`

## RED evidence

Command:

```bash
uv run --frozen pytest -p no:tach -q tests/test_docs_build_config.py::test_core_concept_pages_each_have_their_selected_visual_evidence
```

Result: failed as expected before docs changes.

```text
FAILED tests/test_docs_build_config.py::test_core_concept_pages_each_have_their_selected_visual_evidence
AssertionError: tui.md must contain 'class="docs-visual docs-visual--annotated"'
```

## GREEN results

Commands:

```bash
uv run --frozen pytest -p no:tach -q tests/test_docs_build_config.py tests/test_docs_landing_design.py
uv run --frozen --group docs mkdocs build --strict
```

Results:

```text
55 passed in 1.98s
Documentation built in 1.45 seconds
```

## Commit

`6aba29a docs: add visual guides to core concepts`

## Self-review

- Confirmed the Overview cue describes the product boundary as a contract, not a shared snapshot/cache/package.
- Confirmed the TUI annotation states evidence is watch-backed.
- Confirmed the Agent storyboard keeps context, reads, citations, and UI drive distinct and says writes stop at confirmation.
- Confirmed the MCP flow says follow is UI navigation only and proposals remain opt-in until human confirmation plus audit success.
- Confirmed the Operations flow blocks execution on audit failure.
- Confirmed images are local, lazy-loaded, 1280×720, and use meaningful alt text/captions tied to synthetic evidence.
- Confirmed no new JavaScript, runtime code, remote assets, dependencies, broad reference rewrite, or media regeneration.

## Concerns

None.

## Review fixes

### Files

- `docs/tui.md`
- `docs/resource-relationships.md`
- `tests/test_docs_build_config.py`

### RED evidence

Command:

```bash
uv run pytest -p no:tach tests/test_docs_build_config.py -q
```

Result: failed before the pin positions were corrected.

```text
AssertionError: pin 1 must point to the bottom context/status row at --y: 92%
```

### GREEN results

Commands:

```bash
uv run pytest -p no:tach tests/test_docs_build_config.py -q
uv run --frozen --group docs mkdocs build --strict
```

Results:

```text
25 passed in 0.10s
Documentation built in 0.90 seconds
```

### Commit

`docs: align visual annotations with product screens`

### Self-review

- Confirmed `tui.md` keeps the existing list order and labels while swapping only the pin Y positions.
- Confirmed the new test ties pin 1 to `--y: 92%` and pin 3 to `--y: 8%`.
- Confirmed the resource-relationships caption now describes one relationship table with grouped sections.
- Confirmed no assets, CSS, diagrams, other pages, or the ignored ledger were changed.
