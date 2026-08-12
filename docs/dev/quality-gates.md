# Quality gates: what runs where

Four layers, each with a different job. The point of the split is cost and
timing: a check belongs at the earliest layer that can run it cheaply, and
is repeated later only where trusting the earlier layer would be unsound.

## 1. Every commit — local, `pre-commit`

`ruff`, `ruff-format`, `typos`, `validate-pyproject`, `mypy`, and two
repository hooks: `no-bare-type-ignore` and `no-private-index-in-lock`.

These are fast enough to run on staged files, so nothing slow lives here.
The same hooks run again in CI over **all** files (`pre-commit` job), which
is what makes `--no-verify` an unusable shortcut rather than a quiet one.

### Working behind a corporate package mirror

`uv` records both the artifact URL and the index that served it, so one
`uv lock` behind `UV_INDEX_URL`, a project `uv.toml`, or a user-level
`~/.config/uv/uv.toml` rewrites the whole lockfile to a host only that
network can reach. (`pip.conf` is not a trigger — uv ignores pip's
configuration entirely.)

The lock is only the symptom. Configuration redirects every resolution —
CI's included — while the lock stays byte-for-byte clean, so three surfaces
are guarded:

| surface | guard |
| --- | --- |
| `uv.lock` URLs and registries | `no-private-index-in-lock` hook, `test_lockfile_names_no_host_other_than_pypi` |
| `[tool.uv]` index pins in `pyproject.toml` | `test_pyproject_pins_no_alternate_package_index` |
| a repository-level `uv.toml` | `test_repository_declares_no_uv_configuration_file` |

Behind a mirror, work with `uv sync --frozen --dev --all-extras` and do not
re-lock. If a lock genuinely must change:

```sh
git checkout uv.lock          # discard the rewritten lock
env -u UV_INDEX -u UV_DEFAULT_INDEX -u UV_INDEX_URL \
    -u UV_EXTRA_INDEX_URL -u UV_FIND_LINKS \
    uv lock --no-config       # re-lock against PyPI directly
```

Both halves are needed, and neither is enough alone. `--no-config` stops uv
discovering `~/.config/uv/uv.toml`; clearing the variables stops
`UV_INDEX`/`UV_DEFAULT_INDEX` doing the same job through the environment.
Measured on a machine configured for a mirror:

| command | resulting lock host |
| --- | --- |
| `uv lock` | the mirror |
| `UV_INDEX_URL= uv lock` | the mirror |
| `UV_INDEX_URL= uv lock --no-config` | the mirror, if `UV_INDEX` is set |
| `uv lock --no-config --default-index https://pypi.org/simple` | **still the mirror** — `UV_INDEX` outranks the flag |
| the command above | `files.pythonhosted.org` |

Then confirm before committing: every `url` and `registry` in the lock must
name `files.pythonhosted.org` or `pypi.org`.

If PyPI is unreachable from your machine, do not work around it here. No CI
workflow regenerates the lock — every job consumes the committed one
(`uv sync --locked`, `uv export --frozen`) — so the options are a machine
with direct access, or **Dependabot**, which opens lock updates against
PyPI on its own schedule (`.github/dependabot.yml`). A lock is a
supply-chain artifact; a convenient one that points somewhere else is worse
than none.

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
