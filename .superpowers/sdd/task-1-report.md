# Task 1 Report

Status: DONE

Files changed:
- docs/index.md
- tests/test_docs_landing_design.py

Commits:
- ee4be7f

RED:
- Command: `.venv/bin/python -m pytest -p no:tach tests/test_docs_landing_design.py -q`
- Result: 9 failures, including missing `ONE WORKSPACE` / `CHECKABLE EVIDENCE` / `HUMAN AUTHORITY` highlights and stale `_highlight("GROUND")` / `_highlight("CONTROL")` lookups.

GREEN:
- Command: `.venv/bin/python -m pytest -p no:tach tests/test_docs_landing_design.py -q`
- Result: 107 passed.

Validation:
- `.venv/bin/ruff check tests/test_docs_landing_design.py` — passed
- `.venv/bin/ruff format --check tests/test_docs_landing_design.py` — passed after formatting
- `.venv/bin/mkdocs build --strict` — passed with existing MkDocs nav warnings only
- `git diff --check` — passed

Rendered inspection:
- Local preview at `http://127.0.0.1:8981/korvid/` was available.
- Desktop: heading and all three labels rendered correctly; each card had one paragraph and three working internal links; no overflow observed.
- Mobile 390px: same heading and labels remained visible; labels fit within cards; no overflow observed.

Self-review:
- Verified the homepage highlights now match the new shared-workspace / checkable-evidence / human-authority framing.
- Verified the focused contract tests, lint, formatting, strict MkDocs build, and diff check all passed.

Concerns:
- MkDocs still reports unrelated pages outside nav, but the build is strict-clean and successful.

## Fix 2026-08-28

Changed files:
- `tests/test_docs_landing_design.py`

Commit:
- `e59e095` — `test: pin docs landing highlight contract`

Exact commands/results:
- `.venv/bin/python -m pytest -p no:tach tests/test_docs_landing_design.py -q` — `108 passed in 0.34s`
- `.venv/bin/ruff check tests/test_docs_landing_design.py` — `All checks passed!`
- `.venv/bin/ruff format --check tests/test_docs_landing_design.py` — `1 file already formatted`
- `git diff --check` — passed with no output

Self-review:
- Added the dedicated `test_one_workspace_highlight_keeps_every_driver_visible` contract for the ONE WORKSPACE card.
- Pinned the card's keyboard, embedded Agent, external MCP, same visible cockpit, optional MCP follow, supported reads, notification, and three required links.
- Tightened the linked-promises test to require `2 <= len(links) <= 3` for each highlight card.
- Left homepage copy unchanged.
