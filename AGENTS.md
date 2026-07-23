# korvid Development Guidelines

korvid is an AI-native Kubernetes TUI (Python 3.11+, Textual). Design doc: `docs/specs/2026-07-23-korvid-tui-design.md`. Engineering standards: `docs/specs/2026-07-24-korvid-engineering-standards.md`.

## Quick Setup

```bash
uv sync --dev
```

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
├── agent/          # pure Python: agentic loop, ToolRegistry, LLMProvider ABC
├── k8s/            # pure Python: kubernetes.aio wrapper
└── providers/      # concrete LLMProvider implementations
```

| Layer | May import | Textual imports allowed? |
|---|---|---|
| `ui/` | core, agent, k8s | **Yes — only here** |
| `core/` | k8s | No |
| `agent/` | core, k8s | No |
| `k8s/` | (stdlib + kubernetes client) | No |
| `providers/` | agent | No |

- Interfaces at layer boundaries are `abc.ABC` (e.g., `agent/provider.py: LLMProvider`).
- No DI containers, no service locators. Dependencies are injected via constructors, wired once in `__main__.py`.
- The UI Bus is Textual `Message` subclasses defined in `ui/messages.py`. `core/`/`agent/` expose plain async functions; `ui/` workers translate results into Messages.
- Plugins/providers register via `importlib.metadata.entry_points` groups: `korvid.provider`, `korvid.panel`, `korvid.tool`.

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

## Security Invariants (from the design doc — never weaken these in code)

- Agent write tools always pass the approval gate; approval dialogs are confirmed only by user keystrokes.
- `run_kubectl` validates (verb × resource × flags); sensitive reads go through the masking pipeline.
- Audit logging is fail-closed: if the audit entry cannot be written, the write action is blocked.
