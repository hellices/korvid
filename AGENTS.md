# korvid Development Guidelines

korvid is an AI-native Kubernetes TUI (Python 3.11+, Textual). Design doc: `docs/dev/specs/2026-07-23-korvid-tui-design.md`. Engineering standards: `docs/dev/specs/2026-07-24-korvid-engineering-standards.md`.

## Quick Setup

```bash
uv sync --dev --all-extras
```

**Behind a corporate mirror**, use `uv sync --frozen --dev --all-extras`
and never re-lock. `uv lock` rewrites every artefact URL in `uv.lock` to
whichever index resolved it, so re-locking behind a mirror pins ~1,700
URLs to a host only that network can reach — breaking CI and every
outside contributor. A pre-commit hook rejects such a lock; if the lock
genuinely must change, re-lock with `UV_INDEX_URL= uv lock --no-config`.
`--no-config` is not optional: without it uv still reads
`~/.config/uv/uv.toml` and produces the same mirrored lock.

## Development Commands

```bash
uv run ruff check --fix src/ tests/   # lint (+ import sorting)
uv run ruff format src/ tests/        # format
uv run mypy src/                      # typecheck (strict)
uv run tach check                     # layer-boundary check
uv run pytest -x -q                   # fast test run
uv run pytest --cov                   # tests with coverage (gate: 80%)
```

## Workflow Economics

- While iterating, run only **targeted** checks on files you changed (single test file, ruff on the touched file).
- Do **not** run the full test suite or mypy between every edit — pre-commit and CI cover them at the right time.
- Run `uv run tach check` whenever you add or change imports across packages.
- Commit frequently. pre-commit will reject bad commits; fix and retry rather than bypassing.
- Never use `git commit --no-verify` or edit gate files to make a failure pass.

## Architecture — Layer Rules (enforced by tach)

```
src/korvid/
├── __main__.py     # composition root — ALL wiring (constructor injection) happens here
├── ui/             # Textual App/Screens/Widgets + ui/messages.py (UI Bus messages)
├── core/           # pure Python: ResourceStore, WatchManager, ActionExecutor, AuditLog
├── tools/          # pure Python: tool schemas, ToolExecutor, UIBridge, diagnose
├── agent/          # pure Python: agentic loop, ToolRegistry, LLMProvider ABC
├── mcp/            # MCP adapter (optional extra: korvid[mcp])
├── k8s/            # pure Python: kubernetes.aio wrapper
└── providers/      # concrete LLMProvider implementations (optional extra: korvid[agent])
```

| Layer | May import | Textual imports allowed? |
|---|---|---|
| `ui/` | core, agent, k8s, tools | **Yes — only here** |
| `core/` | k8s | No |
| `tools/` | core, k8s | No |
| `agent/` | core, k8s, tools | No |
| `mcp/` | core, tools | No |
| `k8s/` | (stdlib + kubernetes client) | No |
| `providers/` | agent | No |

- Interfaces at layer boundaries are `abc.ABC` (e.g., `agent/provider.py: LLMProvider`).
- No DI containers, no service locators. Dependencies are injected via constructors, wired once in `__main__.py`.
- The UI Bus is Textual `Message` subclasses defined in `ui/messages.py`. `core/`/`agent/` expose plain async functions; `ui/` workers translate results into Messages.
- Plugins/providers register via `importlib.metadata.entry_points` groups: `korvid.provider`, `korvid.panel`, `korvid.tool`.
- **Optional extras**: `mcp/`'s stack (mcp/anyio/starlette/uvicorn) ships in the `[mcp]` extra; `providers/`'s stack (httpx/keyring) in `[agent]`. `__main__.py` imports both lazily — a missing extra degrades to a None wiring unless the feature was explicitly requested, in which case startup fails with an install hint. Import-graph tests in `tests/test_optional_extras.py` pin this boundary.

## Style Rules

- All public functions/methods have type annotations (mypy --strict enforces).
- No bare `# type: ignore` — always `# type: ignore[error-code]  # reason`.
- No bare `except:` — catch specific exceptions.
- Docstrings: Google style, markdown, no RST syntax.
- Max function complexity 10 (ruff C901).
- Every test contains at least one `assert` or `pytest.raises(...)` block.
- `pytest.raises` always takes `match=` (ruff PT011 enforces).
- Tests must not depend on execution order (pytest-randomly will expose this).
- Treat warnings as errors in tests — fix deprecations, don't filter them.

## Commits

- Run `uv run ruff check --fix && uv run ruff format` on touched files before committing (harness hooks may do this automatically).
- Write clear, descriptive commit messages. Conventional-commit prefixes (`feat:`, `fix:`, `docs:`) are welcome but not enforced.

## Pull Requests

- Do NOT open a pull request without explicit human instruction.
- CI must be green: ruff, mypy, pytest (3.11/3.12/3.13), coverage ≥ 80%, tach, deptry.
- `main` rejects direct pushes (branch ruleset) — always work on a branch and merge via PR.

## Review Loop

For each review round on a PR:

1. Read every comment, including suppressed low-confidence comments hidden in the
   review body's `<details>` block. Classify each finding by confidence and impact.
   Suppressed low-confidence findings are advisory, not automatically mandatory.
2. Always address credible correctness, security, data-loss, architecture-invariant,
   or required-check failures. Fix code findings with TDD: write the failing test
   first (RED), then the fix (GREEN).
3. Run the full gate (`make check`) before committing. If the pre-commit
   ruff-format hook rewrites files on the first attempt, `git add -A` and commit
   again — never `--amend`, never `--no-verify`.
4. Reply to each review comment individually, naming the commit and the test added:
   `gh api repos/OWNER/REPO/pulls/N/comments/{comment_id}/replies -f body=...`
5. Resolve each addressed thread:
   `gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"PRRT_..."}) { thread { isResolved } } }'`
6. Re-request review:
   `gh api -X POST repos/OWNER/REPO/pulls/N/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'`
7. Poll with GraphQL (`reviewRequests`, `reviews`, `reviewThreads(isResolved:false)`);
   reviews typically land within 5–10 minutes and may need two waits.
8. Track consecutive rounds that contain only suppressed low-confidence findings and
   no unresolved blocking findings. After **2 consecutive low-confidence-only
   rounds**, stop making speculative changes and do not request another Copilot
   review. Any new credible blocking finding resets this counter.
9. At the limit, resolve or document the remaining advisory findings and proceed
   toward merge. Never use the round limit to ignore a credible blocking finding or
   bypass a required check.
10. Before merging, verify **every** required check:
    `gh pr view N --json statusCheckRollup` must be all SUCCESS. Then
    `gh pr merge N --squash`.

## Testing Gotchas

- Run a single test file without the tach plugin: `uv run pytest -p no:tach <path>`.
- New `UIBridge` method or parameter? Update every fake in the same change:
  `tests/tools/test_executor.py::FakeBridge`, `tests/tools/test_write_tools.py` fakes,
  `tests/test_main_wiring.py::_FakeApp`, the in-app bridge adapter in `ui/app.py`,
  and `__main__.py`'s bridge proxy.
- `WriteOps` fakes: keyword-only params must match exactly (e.g. `*, uid: str | None = None`).
- Never assert on wall-clock timing; use `tests/ui/waits.py::until()` for
  condition polling in Textual pilots (raw `pilot.pause()` loops are flaky in CI).
- `ConfirmScreen`'s preview widget is selected by class, not id: `query_one(".confirm-preview")`.
- ruff C901 counts nested `def`s toward complexity; extract helpers instead of nesting.
- Textual modals: CSS uses class-name selectors; scrollable bodies are
  `VerticalScroll` with `height: auto; max-height: 80%`.

## Security Invariants (from the design doc — never weaken these in code)

- Agent write tools always pass the approval gate; approval dialogs are confirmed only by user keystrokes.
- `run_kubectl` validates (verb × resource × flags); sensitive reads go through the masking pipeline.
- Audit logging is fail-closed: if the audit entry cannot be written, the write action is blocked.
