# Quality gates: what runs where

Four layers, each with a different job. The point of the split is cost and
timing: a check belongs at the earliest layer that can run it cheaply, and
is repeated later only where trusting the earlier layer would be unsound.

## 1. Every commit — local, `pre-commit`

`ruff`, `ruff-format`, `typos`, `validate-pyproject`, `mypy`, and the
repository's own `no-bare-type-ignore` hook.

These are fast enough to run on staged files, so nothing slow lives here.
The same hooks run again in CI over **all** files (`pre-commit` job), which
is what makes `--no-verify` an unusable shortcut rather than a quiet one.

## 2. Before pushing — local, `make check`

`ruff` → `mypy` → `pytest -x -q` → `tach check`.

The full test suite takes ~13 minutes locally. Run it before pushing, not
between edits: CI runs the same checks, and iterating against a 13-minute
gate wastes more time than it saves.

## 3. Every push and pull request — CI (`.github/workflows/ci.yml`)

| job | what it establishes |
|---|---|
| `changes` | classifies the diff so a docs-only change skips redundant test legs |
| `test` (3.11, 3.12, 3.13) | ruff, `ruff format --check`, mypy, `tach`, `deptry`, `pytest --cov --cov-fail-under=80` |
| `windows-test` (3.12) | OS-specific behaviour — path handling, terminal, keyring |
| `pre-commit` | the layer-1 hooks over every file |
| `security` | `pip-audit` on the locked runtime resolution **including every extra**, `zizmor` on the workflows |
| `dependency-review` | pull requests only; fails on a new high-severity dependency |
| CodeQL | static analysis |
| `ty-experimental` | a second type checker, advisory |

The `changes` classifier gates **pytest only**. Every matrix leg still syncs
and runs ruff, the format check, mypy, `tach` and `deptry`; a docs-only
change runs the suite once, on 3.12, and Windows starts and syncs before
skipping its pytest step. What is saved is three redundant suite runs, not
the matrix.

## 4. Release — `.github/workflows/release.yml`, on tag

`verify` → `build` → `smoke` → `sbom` → `offline` → `collect` → `attest` →
`stage-github-release` → `publish-pypi` → `finalize-github-release`.

`verify` re-runs the *correctness* half of layer 3 on one Linux/3.12
environment - ruff, mypy, `tach`, `deptry`, coverage, and a `pip-audit` over
all extras - rather than trusting a green CI run. A tag can point at any
commit, including one CI never saw, and the job also checks that the tag's
commit is reachable from `origin/main`.

It is **not** the whole gate: `ruff format --check`, the pre-commit hooks,
Windows, the other two interpreters, `zizmor`, dependency-review, CodeQL and
`ty` run in CI only. Release assurance therefore rests on the tagged commit
having passed CI *and* on `verify` re-establishing the checks that would
make a broken artifact.

The GitHub release is **staged as a draft**, published to PyPI, and only
then finalized — so a failed publish leaves no release claiming artifacts
that do not exist.

## Outside the four layers

- **Live-cluster contract tests** (`k8s-contract.yml`) run on `main` only,
  never on pull requests or forks: the Azure identity is scoped to a
  protected environment. Write-path behaviour against a real API server
  cannot be established by any of the layers above.
- **Dev-dependency vulnerabilities** are not gated. Dev packages do not
  ship in the wheel, so `security` audits the runtime resolution with
  `--no-dev`; Dependabot (`.github/dependabot.yml`) carries dev bumps
  instead. Gating on them would make CI red for a flaw no user can reach,
  with no fix available until upstream ships one.
- **Agent evaluations** need a model and a cluster. See
  [`docs/evals/methodology.md`](../evals/methodology.md).
