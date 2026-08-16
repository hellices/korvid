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

If PyPI is unreachable from your machine, do not work around it here — and
you do not have to. `uv lock` fetches wheel *metadata* from
`files.pythonhosted.org`, so a TLS-intercepted machine cannot produce a
usable lock at all: locking through the mirror names the mirror, and this
repository rejects that. Three paths out, in order of preference:

1. **Dependabot** (`.github/dependabot.yml`) — routine bumps, on a schedule,
   resolved against PyPI. Nothing to do.
2. **The `Relock` workflow** (`.github/workflows/relock.yml`) — a manual
   dispatch for the deliberate changes Dependabot will not make, such as
   taking a major version that needs source edits in the same pull request.
   It runs as two jobs on purpose:

   - `relock` is **read-only**. It locks on a runner with direct access,
     asserts every `url`/`registry`/`index` value names a PyPI origin
     (`scripts/check_lock_hosts.py`, which parses the TOML so unreadable or
     empty input fails rather than passing silently), records the lock's
     SHA-256 **before** anything else runs, and then runs the whole gate —
     `uv sync --locked`, ruff, the format check, mypy, `tach`, the suite. It
     installs and executes the dependencies it is updating, so it holds no
     token that could write anything, and it re-checks the digest afterwards
     in case that code edited the lock underneath it.
   - `propose` takes only `uv.lock` across, checks out **`main`** (not the
     base branch — a branch must not supply the validator that clears it),
     re-runs the host check with that trusted copy, and matches the digest
     before the token is exposed. Nothing that ran during verification can
     reach its credential, plant a `pre-push` hook, or substitute a
     different lock that merely looks acceptable.

   One repository setting is a prerequisite: **Settings → Actions → "Allow
   GitHub Actions to create and approve pull requests"**. With it off,
   `gh pr create` is refused and the API cannot even read the flag. The
   workflow still pushes and verifies the branch, then prints the setting
   and the one command that opens the pull request by hand — which has the
   side effect of starting CI, so nothing is lost either way.

   It runs the gate itself because it has to: GitHub suppresses the
   workflow events raised by `GITHUB_TOKEN`, so the pull request it opens
   **does not start CI**. Push an empty commit to that branch to get the
   full matrix — Windows, the other interpreters, the security audit —
   before merging; the pull request body says so too. CodeQL is triggered
   only for pull requests targeting `main`
   (`.github/workflows/codeql.yml`), so a relock onto another base does not
   get it even then.
3. A machine with direct PyPI access.

A lock is a supply-chain artifact; a convenient one that points somewhere
else is worse than none.

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
## 5. Merging — who decides a change lands

The layers above establish that a change is *correct*. They do not decide that
it should land. That decision is the maintainer's, and a branch ruleset is what
makes it stick.

A `Protect default branch` ruleset covers `~DEFAULT_BRANCH` in both
`hellices/korvid` and the `hellices/homebrew-korvid` tap:

| rule | effect |
|---|---|
| pull request required | nothing reaches `main` outside a pull request |
| all review threads resolved | no open objection is merged over |
| required status checks | `pre-commit`, `test (3.11)`, `test (3.12)`, `test (3.13)`; the tap requires `test (ubuntu-latest)` and `test (macos-latest)` |
| Copilot code review | requested automatically, including on drafts |
| no deletion, no force push | `main`'s history is append-only |
| **no bypass actors** | the rules above hold for everyone, administrators included |

Check names are matched exactly, and a matrix job reports one check per leg -
there is no plain `test` context, and requiring one would block every merge
forever. The required few are the ones that carry the rest: `test` is where
ruff, the format check, mypy, `tach`, `deptry` and the 80% coverage floor run
(layer 3), and `pre-commit` is the layer-1 hooks over every file. The remaining
CI jobs report but do not block.

Write access is the outer wall - only the maintainer has it, so no one else can
merge whatever the ruleset says. The ruleset's job is the maintainer's own
accidents: a stray push to `main`, a force push, a merge over a red check or an
unanswered review thread.

### Why zero approving reviews are required

This looks like the opposite of a maintainer gate. It is not, and the reason is
worth recording, because the obvious alternative is worse than it looks. Two
GitHub behaviours cancel each other out:

- an author cannot approve their own pull request, so requiring one approval
  permanently deadlocks a maintainer who writes their own changes;
- the administrator bypass that lifts that deadlock lifts *every* other rule
  with it. A bypassing merge is not "approval waived" - it sails past required
  checks and unresolved threads too, and the REST merge endpoint takes it
  silently. Confirmed on the tap: a pull request reporting
  `BLOCKED / REVIEW_REQUIRED` merged on the first `gh api -X PUT .../merge`.

So "required review plus admin bypass" is not a weaker gate than no bypass; for
anyone holding the maintainer's credentials it is no gate at all. Zero required
reviews with **no bypass actors** keeps every other rule genuinely enforced,
which is the part with teeth. Add the approval rule the day a second person
gets write access - then it costs nothing and buys real review.

There is deliberately no `CODEOWNERS` file either. It narrows *which* of the
accounts with write access may approve; with one maintainer that set already
has one member, so the rule would constrain nothing while implying a review
structure the project does not have.

### The limit, stated plainly

No repository setting stops an agent holding the maintainer's credentials -
GitHub cannot tell the two apart. That boundary is drawn in `AGENTS.md`
instead: **agents never merge and never open a pull request unless asked.**
Their job ends at "the pull request is ready". Making it a technical wall would
mean giving the agent a different token (a fine-grained PAT with pull requests
scoped to read), which is a credential decision, not a repository one.

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
