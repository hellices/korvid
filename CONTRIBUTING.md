# Contributing to korvid

Thanks for helping. korvid is a keyboard-first Kubernetes TUI with an
embedded agent. It holds cluster credentials and performs writes, so a few
rules here are stricter than usual. This page is the short version.

## Ways to help

- **Try it and report what breaks.** The
  [throwaway-cluster guide](https://hellices.github.io/korvid/getting-started/#practice-on-a-throwaway-cluster)
  is the quickest way in. Report problems with the
  [bug report form](https://github.com/hellices/korvid/issues/new?template=bug_report.yml).
- **Pick up a small issue.**
  [`good first issue`](https://github.com/hellices/korvid/labels/good%20first%20issue)
  issues are scoped, name the files involved, and say how to verify the change.
  Ask for one first, as described below.
- **Improve the docs.** The user guide lives in `docs/` and is published at
  <https://hellices.github.io/korvid/>. Doc fixes follow the same steps; a
  short issue is enough.
- **Propose a feature.** Open a
  [feature request](https://github.com/hellices/korvid/issues/new?template=feature_request.yml)
  and wait for a maintainer to accept it before you write the change.

Report security vulnerabilities privately, never in an issue: see
[SECURITY.md](SECURITY.md).

## From issue to pull request

Every change starts as an issue. If you do not have write access to this
repository, open a pull request only for an issue that a maintainer has
assigned to you. Review time goes to changes the project has agreed to make,
and two people do not build the same thing.

1. **Find or open an issue.** New issues from the forms get the
   `status: needs-triage` label until a maintainer accepts or closes them.
   Accepted work that is open to contributors is labelled `help wanted` or
   `good first issue`.
2. **Ask for it.** Comment on the issue with a short plan.
3. **Wait to be assigned.** A maintainer assigns the issue to you, one at a
   time until your first pull request is merged. If an assigned issue sees no
   pull request or update for 14 days, a maintainer may release it to someone
   else.
4. **Open the pull request.** Put `Closes #N` at the top of the description,
   where `N` is the assigned issue.

A pull request from someone without write access that does not close an
issue assigned to its author is closed without review, however good the
change. Ask for the issue first; once a maintainer assigns it, the pull
request can be reopened. Security fixes go through [SECURITY.md](SECURITY.md)
instead of an issue.

## Set up

You need Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and `git`.
A Kubernetes cluster is only needed to try the app, not to run the tests.

```sh
git clone https://github.com/hellices/korvid && cd korvid
uv sync --frozen --dev --all-extras
uv run korvid --version
```

Keep `--frozen`, and do not commit a re-locked `uv.lock` unless the change is
about dependencies. A lock resolved through a private package mirror pins
hundreds of URLs that nobody else can reach, and a pre-commit hook rejects it.
[Working behind a package mirror](docs/dev/quality-gates.md#working-behind-a-corporate-package-mirror)
explains how to re-lock safely.

On Windows, read the
[Windows contributor notes](docs/windows.md) first.

## Check your change

```sh
uv run ruff check --fix src/ tests/   # lint and import order
uv run ruff format src/ tests/        # format
uv run mypy                           # strict type check of src/ and tests/
uv run tach check                     # layer boundaries
uv run pytest -x -q                   # tests
make check                            # size limits, lint, types, tests, layers
```

While iterating, run one test file:
`uv run pytest -p no:tach tests/path/to/test_file.py`. For documentation
changes, also run `make docs-build`. `uv run pre-commit install` runs the
repository's hooks on every commit.

## Ground rules

Tooling or review enforces each of these. The
[architecture document](docs/dev/specs/2026-08-12-korvid-architecture.md)
explains the reasons.

- **Layers.** `src/korvid/` is split into layers, and `tach` checks their
  imports. Only `ui/` may import Textual. Dependencies are passed through
  constructors and wired once, in `src/korvid/__main__.py`.
- **Security invariants.** Never weaken these, in code or in tests:
  - every cluster write, whether it comes from a key, the agent, or an MCP
    proposal, goes through an approval dialog that only a user keystroke
    confirms;
  - the agent has no shell or free-form `kubectl` tool; its cluster surface is
    the structured tool registry;
  - sensitive reads go through the masking pipeline;
  - audit logging is fail-closed: if the audit entry cannot be written, the
    write is blocked.
- **Types and style.** Public functions are fully annotated for
  `mypy --strict`. A `# type: ignore` names its error code and a reason. No
  bare `except:`. Docstrings use Google style. Ruff keeps function
  complexity at 10 or below.
- **Size limits.** `scripts/check_source_size.py` caps module length at 1,200
  lines, with explicit caps for a few older modules. Extract a module rather
  than raise a cap.
- **Tests.** Write the failing test first. Every test asserts something,
  `pytest.raises` always passes `match=`, and tests must pass in any order
  (pytest-randomly shuffles them). Warnings are errors. UI tests wait on a
  condition with `tests/ui/waits.py::until()` instead of sleeping.
- **Docs are tested too.** Tests in `tests/test_docs_*.py` pin the README and
  many guide pages. The README is also the PyPI page, so its links must be
  absolute URLs.
- **Optional extras stay optional.** The agent, MCP, and observability stacks
  are extras. The base TUI must keep working without them, which
  `tests/test_optional_extras.py` checks.

## Pull requests

- Work on a branch; `main` accepts changes only through pull requests.
- Keep one concern per pull request, and close the issue you were assigned
  (`Closes #N`). Describe what changed and how you verified it; the pull
  request template has a short checklist.
- CI must be green: lint, types, tests on Python 3.11 to 3.13 and on Windows,
  coverage of at least 80%, and the layer and dependency checks.
- The maintainer reviews and merges. Expect questions about safety and
  evidence: korvid prefers refusing an action to guessing.

## Using AI coding assistants

AI-assisted contributions are welcome, and you are responsible for every line.
Run the checks yourself and review the diff before you open a pull request.
[AGENTS.md](AGENTS.md) holds the instructions that coding agents in this
repository follow, and doubles as a dense summary of the rules above.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE), the license of this project.
