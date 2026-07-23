# korvid — Engineering Standards

- **Date**: 2026-07-24
- **Status**: Draft (awaiting review)
- **Scope**: toolchain, code architecture, quality gates, and agentic-development harness for all korvid code. Complements the design document (`2026-07-23-korvid-tui-design.md`).

All choices below were validated against what fast-rising Python projects actually practice as of mid-2026 (Textual, posting, harlequin, FastAPI, pydantic, pydantic-ai, marimo, litellm, aider, uv/ruff) rather than textbook doctrine. Citations are noted inline where a choice follows a specific project.

---

## 1. Toolchain

**Principle: minimize tool count — one tool per concern, all configured in one file.**

| Concern | Tool | Rationale |
|---|---|---|
| Package/env management | **uv** (`uv sync`, `uv run`, `uv.lock`) | Universal standard in examined projects; matches our PyPI-first distribution (design doc §12-3) |
| Build backend | **hatchling** | Used by pydantic, pydantic-ai, posting; uv-native |
| Lint + import sorting | **Ruff** (`ruff check`) | Replaces flake8/isort/pyupgrade/bugbear in a single binary; no project examined starts new tooling with black/isort anymore |
| Formatting | **Ruff format** | Black-compatible, built in — Black is not installed |
| Type checking (primary) | **mypy --strict** | Mature, best ecosystem compat. pydantic-ai/pydantic use pyright as primary; we stay with mypy for stub breadth and revisit in Phase 2 |
| Type checking (secondary) | **ty** (astral) — non-blocking CI job | FastAPI pattern: run ty in CI with `continue-on-error` while it matures (beta, 0.0.x). Positions us to switch when stable |
| Testing | **pytest + pytest-asyncio + pytest-textual-snapshot + pytest-xdist + pytest-randomly** | Textual's official test harness (`Pilot`) + snapshot testing; randomly catches hidden test-order coupling |
| Coverage | **pytest-cov**, gate `fail_under = 80`, branch coverage on | Global floor; `diff-cover` is a Phase-2 option if drift appears |
| Layer boundaries | **tach** (`tach check`) | Enforces the import rules in §3 structurally — especially important when agents write code |
| Dependency hygiene | **deptry** (CI) | Catches undeclared/unused imports |
| Spell check | **typos** (pre-commit) | Rust-based, near-zero noise; catches typos in AI-generated comments/docstrings |
| Security | **zizmor** (GitHub Actions audit, pre-commit) + **pip-audit** (scheduled CI) | Workflow security + dependency CVEs |
| Hooks | **pre-commit** | See §4 gate layering |
| Task runner | **Makefile** with `uv run` targets | pydantic-ai pattern; nox/tox rejected (uv handles env isolation) |

**Explicitly rejected**: black/isort (Ruff covers), Poetry (uv covers), DI containers (§3), pluggy (§3), commitizen/conventional-commits enforcement (agents get stuck in pattern-mismatch loops; no examined project enforces it), interrogate (docstring presence ≠ quality), radon/xenon (Ruff `C90` covers), mutation testing (cost/noise), pyrefly (too early).

### Single-file configuration

Every tool is configured in **`pyproject.toml`** — no `setup.cfg`, `.flake8`, `mypy.ini`, or `pytest.ini`. The only exceptions are formats that cannot live in pyproject: `.pre-commit-config.yaml`, `tach.toml`, and `.github/workflows/`. This mirrors the product's own single-config philosophy (design doc §5-7).

```toml
[project]
name = "korvid"
requires-python = ">=3.11"
license = "Apache-2.0"

[project.scripts]
korvid = "korvid.__main__:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[dependency-groups]  # PEP 735
dev = [
  "pytest>=8", "pytest-asyncio", "pytest-cov", "pytest-xdist",
  "pytest-randomly", "pytest-textual-snapshot",
  "mypy>=1.14", "ruff>=0.15", "tach", "deptry", "pre-commit",
]
typecheck-ty = ["ty>=0.0.63"]  # experimental, non-blocking CI only

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = [
  "E", "W", "F", "I",   # pycodestyle, pyflakes, isort
  "B",                  # bugbear (incl. B017 bad pytest.raises)
  "C4", "SIM", "UP",    # comprehensions, simplify, pyupgrade
  "PT",                 # flake8-pytest-style — catches weak AI-written tests
  "C90",                # mccabe complexity
  "TID",                # tidy imports
  "RUF",                # ruff-specific
  "S101",               # assert in production code
]
ignore = ["E501"]  # line length is the formatter's job

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.flake8-pytest-style]
mark-parentheses = false
fixture-parentheses = false

[tool.ruff.lint.per-file-ignores]
"tests/**/*.py" = ["S101"]  # assert is the point of tests

[tool.mypy]
strict = true
warn_unused_ignores = true

[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "--strict-markers --tb=short --randomly-seed=last"
testpaths = ["tests"]
filterwarnings = ["error"]  # deprecations fail immediately

[tool.coverage.run]
branch = true
source = ["src/korvid"]

[tool.coverage.report]
fail_under = 80
show_missing = true

[tool.typos.files]
extend-exclude = ["*.lock", "*.snap"]
```

---

## 2. Clean-Code Rules

1. **src layout**: all code under `src/korvid/`; tests under `tests/` mirroring the package tree
2. **One responsibility per module**; when a file needs a scroll map, it is too big. (posting's 65 KB `app.py` is the counterexample we design against)
3. **Typed everything public**: mypy --strict enforces annotations; no bare `# type: ignore` — always `# type: ignore[error-code]  # reason`
4. **Docstrings**: Google style, markdown (no RST); required on public API, optional on internals
5. **No bare `except:`**; catch specific exceptions (design doc §8 error-surfacing principles depend on this)
6. **Complexity budget**: Ruff `C901` max 10 per function
7. **Tests**: TDD — the failing test precedes the implementation. Every test asserts something (`assert` or `pytest.raises(..., match=...)` — PT011 enforces `match=`)
8. **Warnings are errors** in tests (`filterwarnings = ["error"]`) so upstream deprecations surface at once

---

## 3. Software Architecture — Pragmatic Layered ("clean architecture, as far as the evidence supports")

We examined how comparable fast-rising projects structure themselves: posting (Textual author's own TUI), harlequin (Textual SQL IDE **with a pluggable adapter system** — the closest analogue to our pluggable providers), pydantic-ai (agent framework with the most disciplined layering), marimo, litellm, aider. Findings:

- **Zero** of the six use a DI container (dependency-injector/punq/svcs). Wiring is constructor injection at the entry point.
- Adapter/provider interfaces are **`abc.ABC`**, not `typing.Protocol`, in every project that has them (harlequin `HarlequinAdapter`, pydantic-ai `Model`/`Provider`, marimo `ChatModel`).
- Plugin systems use **`importlib.metadata.entry_points`** with group names `<app>.<resource>` (harlequin: `harlequin.adapter`, `harlequin.keymap`). pluggy is only warranted when plugins must intercept each other's hooks (pytest's case) — not ours.
- Nobody practices full hexagonal architecture. The discipline that *does* pay: keeping non-UI layers free of framework imports.
- litellm is the anti-pattern for us: a monolithic router with a giant if/elif provider dispatch — a consequence of 100+ providers, not a design to emulate for our ~5.

**Verdict: pragmatic layered.** Full hexagonal (ports/adapters/domain isolation/DI container) is rejected as ceremony none of our references need; "modules + conventions only" is rejected because agent-written code needs structural boundaries, not conventions. We take exactly these elements of clean architecture and no more:

1. **Dependency direction is law**, enforced by tach in CI (not by convention):

```text
src/korvid/
├── __main__.py     # composition root — the ONLY place wiring happens
├── ui/             # Textual: App, Screens, Widgets, ui/messages.py (UI Bus messages)
├── core/           # pure Python: ResourceStore, WatchManager, ActionExecutor, AuditLog
├── agent/          # pure Python: agentic loop, ToolRegistry, LLMProvider ABC
├── k8s/            # pure Python: kubernetes.aio wrapper
└── providers/      # concrete LLMProvider implementations (openai, anthropic, ...)
```

| Layer | May import | Textual allowed? |
|---|---|---|
| `ui/` | `core`, `agent`, `k8s` | **Yes** (only here) |
| `core/` | `k8s` | No |
| `agent/` | `core`, `k8s` | No |
| `k8s/` | — (stdlib + kubernetes client only) | No |
| `providers/` | `agent` (the ABC) | No |

   `core/`, `agent/`, `k8s/` being Textual-free means the agentic loop, resource cache, and client wrapper are all testable as plain asyncio code without a TUI harness.

2. **Interfaces are `abc.ABC` at layer boundaries** — the key one being the provider interface (modeled on pydantic-ai's `Model`, deliberately minimal like marimo's `ChatModel`):

```python
# src/korvid/agent/provider.py
class LLMProvider(ABC):
    """Pluggable provider. Activated by config injection; no default bundled.
    Discovered via entry_points group "korvid.provider" (built-ins pre-registered)."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def complete(
        self,
        messages: list[ModelMessage],
        tools: list[ToolSpec],
        *,
        stream: bool = True,
    ) -> AsyncIterator[CompletionEvent]: ...
```

3. **Constructor injection, wired once in `__main__.py`** (harlequin injects its adapter into `Harlequin(App)` the same way). No service locators, no module-level singletons for injectable dependencies. Config is the one pragmatic exception (posting's `ContextVar` pattern is acceptable for read-only settings).

4. **The UI Bus rides Textual's message pump.** harlequin's async DB pipeline (`QuerySubmitted → QueriesExecuted → ResultsFetched`) is the proven template: define typed `Message` subclasses in `ui/messages.py` for every bus command/event (`AgentDelta`, `AgentToolRequest`, `AgentUICommand`, `NavigateCommand`, …) and let Textual dispatch them. We do **not** build a framework-independent dispatcher — `core/` stays Textual-free by exposing plain async functions/AsyncIterators that `ui/` workers (`@work`) call and translate into Messages. Both user keystrokes and agent UI-control tools emit the same Message types, which is what the design doc's "single entry point" (§4.1) requires.

5. **Plugins (Phase 3) = entry_points + ABC**, groups `korvid.provider`, `korvid.panel`, `korvid.tool`. Built-in providers are registered through the same mechanism as external ones, so "built-in vs plugin" is a packaging detail, not an architectural one. This is exactly harlequin's adapter model.

---

## 4. Quality Gates — Three Layers

The enforcement stack separates *fast feedback* from *unbypassable gates*. This split matters more in agentic development: slow hooks make agents spin; missing gates let agents merge junk.

| Layer | When | What runs | Budget |
|---|---|---|---|
| **Agent harness hooks** (§5) | after each file edit | `ruff check --fix` + `ruff format` on the touched file | < 1 s |
| **pre-commit** | `git commit` | ruff check, ruff format --check, mypy (incremental), typos, validate-pyproject, actionlint, zizmor, bare-`type: ignore` blocker | **< 10 s total** — anything slower moves to CI |
| **CI (GitHub Actions)** | PR + push | full pytest matrix (3.11/3.12/3.13) with coverage gate, `tach check`, `deptry`, `ty` (non-blocking), pip-audit (scheduled) | unbounded |

- Tests do **not** run in pre-commit (too slow → agents thrash); they run in CI and optionally in the harness Stop hook (§5).
- CI uses `astral-sh/setup-uv` **pinned to a commit SHA** with `enable-cache: true`, and `uv sync --locked` so the lockfile is authoritative.
- pre-commit hook for bare ignores (pygrep): blocks `# type: ignore` not followed by `[`.

---

## 5. Agentic Development Harness

korvid is developed primarily with AI coding agents. Repo-managed assets keep any agent (and any human) inside the rails. The three-tier split of responsibilities:

### 5.1 Agent behavior contract — `AGENTS.md` (repo root, committed)

The cross-harness standard file (read natively by Copilot/Codex-family tools; Claude Code reads it via `CLAUDE.md` containing the single line `@AGENTS.md` — the uv/marimo pattern). Contents:

- Quick setup + the exact dev commands (`make lint / format / typecheck / test`)
- The layer table from §3 and "run `uv run tach check` if you touched imports"
- Style rules agents most often violate: no bare `type: ignore`, `pytest.raises` needs `match=`, every test asserts
- **Workflow economics** (pydantic-ai's insight): *do not* run the full test suite or mypy after every edit — targeted checks while iterating; pre-commit and CI cover the rest at the right time
- PR rules: no PR without explicit human instruction

Per-layer `AGENTS.md` files (e.g., `src/korvid/agent/AGENTS.md` describing tool-gate invariants) are a Phase-2 option once the codebase grows (pydantic-ai's hierarchical pattern).

### 5.2 Harness hooks — `.claude/settings.json` + `.claude/hooks/` (committed)

For contributors using Claude Code (hook-capable harness). Non-hook harnesses lose only convenience — §4's pre-commit/CI still gate everything.

| Hook | Trigger | Action |
|---|---|---|
| PostToolUse (Edit\|Write, `*.py`) | after each edit | `uv run ruff check --fix` + `ruff format` on the file — agent never sees lint noise in review |
| PreToolUse (Edit\|Write) | before each edit | **block** edits to protected paths: `uv.lock`, `.github/workflows/`, `tach.toml`, `.pre-commit-config.yaml` (exit 2). Agents must ask the human to change gate files |
| Stop | agent finishes a turn | `uv run pytest -x -q --tb=short \| tail -20` — failures land in the agent's context immediately |
| SessionStart (compact) | after context compaction | re-inject the one-paragraph project brief (layers, commands, gate rules) so post-compaction agents don't relearn by trial |

### 5.3 Repo gates — `pyproject.toml`, `.pre-commit-config.yaml`, `tach.toml`, `Makefile`, `.github/workflows/`

Everything in §1 and §4. This layer is harness-independent and cannot be bypassed by any agent: pre-commit rejects the commit, CI rejects the merge. tach deserves emphasis — layer-boundary violations are the most common structural mistake agents make, and `tach check` turns the §3 table from prose into a failing build.

**Not in the repo**: personal harness skills (e.g., superpowers), editor config beyond `.editorconfig`, and user-level `~/.claude` settings. Those are personal assets; the repo only carries what every contributor needs.

---

## 6. Adoption Order

1. This document + `AGENTS.md` + `CLAUDE.md` (this PR)
2. Scaffold PR: `pyproject.toml`, `src/korvid/` skeleton with layer dirs, `tach.toml`, `.pre-commit-config.yaml`, `Makefile`, CI workflow, `.claude/` hooks — the first implementation-plan task of Phase 1
3. Everything after runs inside the gates
