# Task 5 — final-review fix report

**Status:** complete. Every finding in the final-review set (C1, I2, I3, I4,
M1–M8, spec gap) is resolved, with tests written RED before implementation.
Nothing was pushed; issue #176 was never inspected or modified.

- **Base:** `b4417210ae75a5e74dad1029e744908324251659` (`b441721`, "docs: add the
  first-release runbook")
- **Head:** `7f92f47` — four new commits, no existing commit amended.

## Commits

| Commit | Subject |
|---|---|
| `2d22b47` | `fix: keep the korvid --version fast path faithful to the real parser` |
| `78fc32a` | `ci: fetch the live trusted head, scope the dry run, and isolate smoke installs` |
| `39edcd7` | `docs: correct the v0.1.0 release claims, retained state, and dry-run limits` |
| `7f92f47` | `docs: show how to resolve RUN_ID in both release watch commands` |

Every commit carries `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
(verified: 4/4).

## Files changed

```
.github/workflows/release.yml        workflow graph, fetch step, env, timeout, attest gate
README.md                            install claims, source fallback, retained state
docs/release.md                      runbook: irreversibility, dry-run scope, XDG, cleanup
scripts/release/check_dry_run.py     X.Y.Z gate, live-remote docstring
scripts/release/check_version.py     shared X.Y.Z gate
scripts/release/version_format.py    new — shared release-version format gate
scripts/release/smoke_install.py     fresh/expansion install phases, forbidden modules
src/korvid/cli.py                    version-only fast path
tests/test_cli.py                    delegation + fast-path tests
tests/test_release_scripts.py        workflow, script, and documentation invariants
```

## RED evidence

Tests were added first. Baseline before any test was added:
`60 passed` (`tests/test_release_scripts.py tests/test_cli.py`).

After adding the new tests and **before** any implementation change:

```
26 failed, 66 passed in 4.28s
FAILED tests/test_cli.py::test_console_entrypoint_does_not_shortcut_version_used_as_a_flag_value
FAILED tests/test_release_scripts.py::test_attestation_is_gated_to_tag_pushes_and_never_runs_on_a_dry_run
FAILED tests/test_release_scripts.py::test_dry_run_rejects_versions_outside_the_supported_release_format[$(id)]
FAILED tests/test_release_scripts.py::test_dry_run_rejects_versions_outside_the_supported_release_format[0.1.0.dev1]
FAILED tests/test_release_scripts.py::test_dry_run_rejects_versions_outside_the_supported_release_format[0.1.0rc1]
FAILED tests/test_release_scripts.py::test_dry_run_rejects_versions_outside_the_supported_release_format[1.0]
FAILED tests/test_release_scripts.py::test_dry_run_rejects_versions_outside_the_supported_release_format[1.2.3-x]
FAILED tests/test_release_scripts.py::test_dry_run_rejects_versions_outside_the_supported_release_format[]
FAILED tests/test_release_scripts.py::test_release_docs_call_provenance_attestation_irreversible
FAILED tests/test_release_scripts.py::test_release_docs_correct_the_xdg_config_claim
FAILED tests/test_release_scripts.py::test_release_docs_describe_fresh_installs_and_extra_expansion_separately
FAILED tests/test_release_scripts.py::test_release_docs_keep_a_source_install_fallback_before_publication
FAILED tests/test_release_scripts.py::test_release_docs_list_and_clean_the_mcp_endpoint_state
FAILED tests/test_release_scripts.py::test_release_docs_show_how_to_find_the_run_id_and_the_dispatch_precondition
FAILED tests/test_release_scripts.py::test_release_docs_state_the_dry_run_skips_attestation_and_publication
FAILED tests/test_release_scripts.py::test_release_workflow_fetches_the_live_trusted_branch_before_the_source_policy
FAILED tests/test_release_scripts.py::test_release_workflow_keeps_github_expressions_out_of_shell_bodies
FAILED tests/test_release_scripts.py::test_release_workflow_smoke_job_declares_a_timeout
FAILED tests/test_release_scripts.py::test_release_workflow_smoke_step_passes_matrix_values_through_env
FAILED tests/test_release_scripts.py::test_smoke_install_forbids_optional_feature_packages_outside_their_variant
FAILED tests/test_release_scripts.py::test_smoke_install_plan_for_base_is_a_single_fresh_environment
FAILED tests/test_release_scripts.py::test_smoke_install_plan_installs_the_variant_directly_in_a_fresh_environment
FAILED tests/test_release_scripts.py::test_smoke_install_plan_keeps_a_separate_base_to_extra_expansion_check
FAILED tests/test_release_scripts.py::test_smoke_install_rejects_unknown_variants
FAILED tests/test_release_scripts.py::test_smoke_install_required_modules_follow_the_selected_variant
FAILED tests/test_release_scripts.py::test_smoke_install_variant_matrix_excludes_entra
```

Three later tests were also written RED individually:

- `test_smoke_install_resolves_a_relative_workspace` —
  `AssertionError: PosixPath('relative-workspace') != PosixPath('/private/.../relative-workspace')`
- `test_smoke_install_runs_a_fresh_install_then_a_separate_expansion` —
  first RED for the missing phase runner, then
  `RuntimeError: korvid console launcher still exists after uninstall`
  (proving the real assertion fires against the fake).
- `test_release_version_format_helper_is_shared_by_both_gates` —
  `ModuleNotFoundError: No module named 'version_format'`.

Two new tests passed on first run by design and are regression pins, not RED
cases: `test_dry_run_refuses_a_stale_dispatch_sha_against_the_live_remote_ref`
(the script side of C1 was already correct — the defect was the workflow not
refreshing `origin/main`) and `test_smoke_install_reports_workspace_cleanup_failures`
(pins the existing fail-closed cleanup so M5 cannot regress).

An additional RED was produced by running the real script:

```
$ python3 scripts/release/smoke_install.py --wheel dist/... --workspace ./.smoke-base
[Errno 2] No such file or directory: '.smoke-base/venv-fresh/bin/python'
```

A relative `--workspace` broke every command, because commands run with
`cwd=workspace`. Fixed by resolving the workspace in `main()`.

## GREEN — commands and results

| Command | Result |
|---|---|
| `uv run pytest -p no:tach tests/test_release_scripts.py tests/test_cli.py -q` | `99 passed` |
| `uv run pytest -p no:tach tests/test_main_wiring.py tests/test_optional_extras.py -q` | `73 passed` |
| `uv run pytest -q` (full suite, at HEAD `7f92f47`) | `4054 passed, 21 skipped in 640s` |
| `uv run ruff check src/ tests/ scripts/` | `All checks passed!` |
| `uv run ruff format --check src/ tests/ scripts/` | `319 files already formatted` |
| `uv run mypy` | `Success: no issues found in 307 source files` |
| `uv run tach check` | `✅ All modules validated!` |
| `uvx zizmor --min-severity medium .github/workflows/release.yml` | `No findings to report. (15 suppressed)` |
| `git diff --check b4417210..HEAD` | clean |
| `uv run --no-project python scripts/release/check_version.py v0.1.0` | prints `0.1.0`, exit 0 |
| `uv run --no-project python scripts/release/check_dry_run.py origin/main` | correctly rejects: `checked-out HEAD must match the trusted branch head` |

pre-commit (ruff, ruff-format, typos, mypy, no-bare-type-ignore) passed on all
four commits. No `--no-verify`, no `--amend`.

## Finding-by-finding resolution

### C1 (critical) — `origin/main` could be the dispatch SHA

`actions/checkout` with `fetch-depth: 0` can leave `refs/remotes/origin/main`
pointing at the ref the run was dispatched from, making `check_dry_run.py`'s
`HEAD == origin/main` comparison vacuous.

- `verify` now has an explicit step, after checkout and before *any* source
  policy step, running
  `git fetch --force --no-tags origin "refs/heads/main:refs/remotes/origin/main"`.
  It runs on both events, so `check_source.py`'s `origin/main` reachability
  check on the publication path is also refreshed.
- `check_dry_run.py`'s module docstring records the requirement.
- `test_release_workflow_fetches_the_live_trusted_branch_before_the_source_policy`
  pins the exact fetch refspec **and** its ordering before both
  `check_dry_run.py origin/main` and `check_source.py`.
- `test_dry_run_refuses_a_stale_dispatch_sha_against_the_live_remote_ref` builds
  a repo whose `refs/remotes/origin/main` is ahead of a detached `HEAD` and
  asserts rejection. `test_dry_run_accepts_the_live_remote_tracking_head` pins
  the positive case.
- Commit hashes stay out of policy errors: both tests assert that neither the
  stale nor the live commit appears in stderr.

### I2 (important) — dry-run attestation is irreversible

`attest` now carries `if: github.event_name == 'push'`. A dry run executes
`verify → build → smoke → sbom → offline → collect` and stops. `collect` is
deliberately **not** gated, so the dry run still exercises full asset
collection and checksum generation. `stage-github-release`, `publish-pypi`, and
`finalize-github-release` were already push-gated and additionally depend on
`attest`, so a skipped `attest` keeps them skipped.

`test_attestation_is_gated_to_tag_pushes_and_never_runs_on_a_dry_run` asserts
the job order, that `collect` has no push gate, and that `attest` does.
Documentation now calls attestation irreversible (public Sigstore signing,
public Rekor transparency log) and states that the dry run does not exercise
attestation or publication.

### I3 (important) — "fresh install" claim was false

`smoke_install.py` previously always installed base and then widened with
`--upgrade`, so no variant was ever installed fresh.

New `install_plan(wheel, variant)` returns `InstallPhase` records:

- `fresh` (`venv-fresh`): one direct install of the variant's own requirement
  (e.g. `korvid[mcp] @ file://…`) from the single downloaded wheel, in a
  brand-new virtual environment.
- `expansion` (`venv-expansion`, non-base variants only): a *separate* clean
  environment that installs base and then runs the documented
  `--upgrade 'korvid[extra]'` command. It covers **every** non-base variant
  (`agent`, `mcp`, `all`).

Both phases run the full contract: version assertion, required module imports,
forbidden module absence, launcher `--help`/`--version`, uninstall, and
launcher/`find_spec` removal checks. `_assert_no_user_state` runs once over the
shared fake HOME/XDG/APPDATA roots after both phases, so uninstall/no-state
assertions are preserved.

Tests distinguishing fresh from expansion:
`test_smoke_install_plan_installs_the_variant_directly_in_a_fresh_environment`,
`test_smoke_install_plan_keeps_a_separate_base_to_extra_expansion_check`,
`test_smoke_install_plan_for_base_is_a_single_fresh_environment`, and
`test_smoke_install_runs_a_fresh_install_then_a_separate_expansion` (drives the
real phase runner offline with fakes and asserts the fresh install never passes
through the base requirement, and that the two venvs are distinct).

### I4 (important) — XDG_CONFIG_HOME claim was wrong

Verified in source: `core/config.py:DEFAULT_CONFIG_PATH` and
`providers/token_store.py:DEFAULT_CREDENTIALS_PATH` are both
`Path.home() / ".config" / "korvid" / …` — no environment override.
`core/audit.py` and `mcp/server.py` honor `XDG_STATE_HOME`;
`core/logexport.py` and `core/private_export.py` honor `XDG_DATA_HOME`.

The runbook now states plainly that `XDG_CONFIG_HOME` is not honored and that
`config.yaml` and the `credentials.json` fallback are always under
`~/.config/korvid`, with an explicit warning not to rewrite those two paths
when cleaning up (the old guidance would have left credentials behind). The
cleanup block annotates which lines honor which variable. README carries the
same correction. Pinned by `test_release_docs_correct_the_xdg_config_claim`.

### M1 — MCP endpoint state

`~/.local/state/korvid/mcp-endpoint.json` and its `.lock` sibling are now in
the retained-state list, the cleanup commands, and the README uninstall note,
with a warning to stop any running `korvid --mcp` server first. Pinned by
`test_release_docs_list_and_clean_the_mcp_endpoint_state`, which also asserts
that credentials removal is present in the cleanup section.

### M2 — optional packages must not leak into base

`required_modules` now checks `httpx` + `keyring` for `agent`/`all` and `mcp`
for `mcp`/`all`. New `forbidden_modules` asserts absence:
`base → {httpx, keyring, mcp}`, `agent → {mcp}`, `mcp → {keyring}`,
`all → {}`. `httpx` is deliberately *not* forbidden for `mcp`, because the
`mcp` distribution depends on httpx (confirmed in `uv.lock`); forbidding it
would be a guaranteed false failure. `entra` remains outside the matrix, pinned
by `test_smoke_install_variant_matrix_excludes_entra`, and `--variant` choices
now derive from `variants()`.

### M3 — expressions in shell bodies, unvalidated version

The smoke step's `run:` body no longer contains any `${{ … }}`; `VERSION`,
`VARIANT`, and `WORKSPACE` are passed via `env:` and referenced as shell
variables, with `shell: bash` for Windows. `test_release_workflow_keeps_github_expressions_out_of_shell_bodies`
parses the workflow with PyYAML and asserts **no** job has an expression in any
`run:` body, so this cannot regress anywhere in the file.

New `scripts/release/version_format.py` holds the single X.Y.Z gate, used by
both `check_dry_run.py` (dry run) and `check_version.py` (publication), so the
two paths cannot drift. The rejection message never echoes the rejected value,
and validation happens before the version is printed to stdout (and thus before
it can reach `$GITHUB_OUTPUT`, the shell, or artifact file names). `0.1.0`
passes; `0.1.0.dev1`, `1.0`, `0.1.0rc1`, `1.2.3-x`, `$(id)`, and `""` are
rejected.

### M4 — `-n --version` divergence

`korvid.cli.main` now takes the fast path only when `sys.argv[1:]` is exactly
`["--version"]`; everything else is delegated verbatim to
`korvid.__main__.main`. `korvid -n --version` therefore reaches the real parser,
which consumes `--version` as the namespace value, exactly as before the CLI
shim existed. Three in-process tests cover this:
`test_console_entrypoint_delegates_to_the_app_composition_root`,
`test_console_entrypoint_does_not_shortcut_version_used_as_a_flag_value`, and
`test_console_entrypoint_takes_the_fast_path_only_for_the_exact_version_call`.
The existing subprocess import-blocking test still passes, so the fast path
still avoids app startup imports.

### M5 — smoke job timeout, fail-closed cleanup

`timeout-minutes: 30` on the `smoke` job (36 cells × 2 venvs).
`test_release_workflow_smoke_job_declares_a_timeout` asserts a positive
integer ≤ 60. Cleanup remains fail-closed — `shutil.rmtree` failures are
reported and return exit 1, never swallowed — now pinned by
`test_smoke_install_reports_workspace_cleanup_failures`.

### M6 — source-install fallback

README and the runbook both document
`pip install 'korvid[all] @ git+https://github.com/hellices/korvid'` for the
window between the workflow landing on `main` and `v0.1.0` appearing on PyPI,
while stating that PyPI is the release path once published. The Quick start
points at it too. Pinned by
`test_release_docs_keep_a_source_install_fallback_before_publication`.

### M7 — RUN_ID and dispatch availability

Both `gh run watch` commands now resolve the id with
`gh run list --workflow Release --limit 1 --json databaseId --jq '.[0].databaseId'`
and quote `"$RUN_ID"`, with a note to confirm the resolved run is yours. The
runbook states `workflow_dispatch` is offered only after the workflow file
exists on the repository's **default branch**. Pinned by
`test_release_docs_show_how_to_find_the_run_id_and_the_dispatch_precondition`;
the stale `gh run watch RUN_ID` assertion was updated to the quoted form.

### M8 — version equality

`test_pyproject_version_matches_the_package_version` asserts
`pyproject["project"]["version"] == korvid.__version__`. Placed in
`tests/test_release_scripts.py` alongside the other release invariants (a
duplicate added to `tests/test_cli.py` during drafting was removed).

### Spec gap — dry-run limits

`docs/release.md` has a dedicated "What the dry run proves — and what it cannot"
section naming everything it cannot exercise: attestation,
`stage-github-release`, PyPI publication, `finalize-github-release`,
compare-assets recovery, and pre-publication tag revalidation. It states that a
green dry run **reduces but does not eliminate** first-publication risk, and
the first-release limitation section repeats it and recommends treating the
first release as a supervised operation. Pinned by
`test_release_docs_state_the_dry_run_skips_attestation_and_publication`.

## Self-review

**Job graph (dispatch).** `verify` (fetch + `check_dry_run`) → `build` →
{`smoke`, `sbom`, `offline`} → `collect`. `attest` is skipped by its `if`;
`stage-github-release` is skipped by both its own `if` and its skipped `attest`
dependency; `publish-pypi` and `finalize-github-release` follow. Nothing
publishes, nothing signs. `smoke` is not a dependency of `collect`, but its
failure still fails the run.

**Job graph (tag push).** Unchanged apart from the added fetch:
`verify` → `build` → {`smoke`, `sbom`, `offline`} → `collect` → `attest` →
`stage-github-release` → `publish-pypi` → `finalize-github-release`. `attest`'s
`if` is true for `push`, so the publication chain is intact. The three
revalidation jobs keep their own `git fetch … refs/heads/main` and their
`--expected-commit` binding.

**Fresh vs expansion.** For `base` the plan is one phase; nothing installs an
extra. For `agent`/`mcp`/`all` the first phase's only requirement is the
extra-bearing direct reference, asserted by test to not contain the base
requirement. The expansion phase uses a different venv directory, so a wheel
cached in the first venv cannot mask a broken direct install. `--upgrade` is
applied only to requirements after the first, so the fresh install never uses
it.

**Windows paths.** The smoke step uses `shell: bash` (Git Bash is present on
`windows-latest`), and `$WORKSPACE` expands without backslash mangling inside
double quotes. `Path(...).resolve()` normalizes the mixed `D:\a\_temp/…`
separators. `os.name == "nt"` still selects `Scripts/` and `python.exe`, since
the script runs under the native Windows interpreter from `setup-python`. The
workspace directory name was shortened (`smoke-` instead of `smoke-install-`),
which reduces path length even though each cell now nests two venvs.

**Cleanup paths.** `_assert_no_user_state` scans the fake `HOME`, `XDG_*`,
`APPDATA`, and `LOCALAPPDATA` roots after both phases, so state created by
either install is caught. `HOME`/`USERPROFILE` are redirected, which is what
actually covers the hardcoded `~/.config/korvid` paths — that stays correct
under the I4 finding. Workspace removal remains fail-closed.

**Release claims.** Cross-checked every claim that survived: the smoke matrix
description now matches what the script does; the retained-state list matches
`core/config.py`, `providers/token_store.py`, `core/audit.py`,
`core/logexport.py`, `core/private_export.py`, and `mcp/server.py`; the
irreversibility list now includes attestation; and the recovery boundaries and
first-release upgrade limitation are unchanged and still accurate.

## Concerns

1. **Anonymous fetch on `korvid-runners`.** The new `verify` fetch runs with
   `persist-credentials: false`, so it relies on anonymous access to the
   repository, exactly like the three existing revalidation jobs. If the
   repository is ever made private, all four fetches break together. Worth
   confirming once on the first real run.
2. **No end-to-end smoke execution here.** `pip install` of the built wheel
   cannot complete in this environment: `pyproject.toml` requires
   `regex>=2026.7.19`, which is not on PyPI (latest available is `2026.1.15`).
   The phase runner is therefore covered by an offline test with faked
   `venv.create`/`_run` plus a real invocation that got as far as pip
   resolution. The first real proof of the two-phase install will be CI.
3. **Smoke wall-clock roughly doubles.** Each non-base cell now builds two
   virtual environments. 30 minutes should be ample, but if Windows `all` cells
   run close to the limit, raise `timeout-minutes` rather than dropping the
   expansion phase.
4. **`forbidden_modules` is a hand-maintained map.** If a future base
   dependency ever pulls `httpx` transitively, the base cell fails loudly
   (which is the intent) but the map must then be corrected deliberately, not
   relaxed by reflex.
5. **`workflow_dispatch` is still unproven.** By construction it cannot exist
   until this workflow is on the default branch, so the first dry run is itself
   the first execution of the dry-run path.

## Advisory-fix append (2026-08-06)

- **Commit:** `b6fd741` (`fix: harden release attestation and version format`)
- **Evidence:**
  - `uv run pytest -p no:tach tests/test_release_scripts.py -q` → `95 passed`
  - `uv run ruff check scripts/release/version_format.py tests/test_release_scripts.py`
    and `uv run ruff format --check scripts/release/version_format.py tests/test_release_scripts.py`
    → clean
  - `uvx zizmor .github/workflows/release.yml` → no findings
  - `git diff --check` → clean
