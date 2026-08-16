# Release Policy Cleanup Task 2 Report

## Reviewer feedback resolution

1. **Restored release-order / tag-source binding / dry-run gating coverage**
   - Reworked `test_release_runbook_preserves_release_order_and_exact_source_binding` to assert:
     - heading order across bindings → dry run → upgrade gate → publish → recovery → verify → tap;
     - exact dry-run commands (`git fetch origin main`, `COMMIT=$(git rev-parse origin/main)`, `gh workflow run Release --ref main`, `gh run list --workflow Release --limit 1`, `gh run watch "$RUN_ID" --exit-status`);
     - exact dry-run upgrade gate commands (`DRY_RUN_COMMIT=...`, `gh run download "$RUN_ID" --name dist`, exact wheel path, exact `korvid[all]==0.1.2` upgrade source);
     - exact tag/source binding commands (`git tag -a vX.Y.Z "$COMMIT"`, `git rev-list`, `--branch vX.Y.Z --commit "$COMMIT"`, `TAG_RUN_COMMIT=...`, `test "$TAG_RUN_COMMIT" = "$COMMIT"`);
     - verify commands for wheel + `SHA256SUMS` attestation and checksum validation;
     - recovery prohibition against deleting or moving a published tag/version.

2. **Strengthened cleanup contract**
   - Reworked `test_release_docs_preserve_retained_state_and_explicit_cleanup_controls` to assert:
     - retained-state heading precedes cleanup heading;
     - retained state explicitly includes `audit.jsonl.lock` and `mcp-endpoint.json.lock`;
     - cleanup keeps the keyring-delete step before file removal;
     - cleanup removes `audit.jsonl*`, `mcp-endpoint.json*`, logs, and agent payloads;
     - cleanup contains no `--force` flag.

3. **Restored broad stale-version coverage**
   - Reworked `test_current_release_docs_only_name_allowed_versions` to scan every semantic version in `README.md`, `docs/release.md`, and `docs/release-notes/v0.2.0.md`.
   - `_ALLOWED_RELEASE_DOC_HISTORY` now names the explicit historical set `{0.1.0, 0.1.1, 0.1.2}` for all release docs, matching the old `_UNPUBLISHED_TAGS | _PUBLISHED_PREDECESSORS` guard.
   - README and `docs/release.md` may name only the current version plus that explicit historical set.
   - Current release notes must still name only the current version.
   - The test still checks current install/verify commands and rejects `uv tool install --upgrade` in notes.

4. **Removed remaining prose-phrase assertions from the focused policy module**
   - Replaced the remaining brittle full-sentence safety checks with section-scoped semantic contracts.
   - `Irreversible boundaries` now requires independent bullet semantics for:
     - annotated tag publication being irreversible;
     - PyPI publication being irreversible;
     - provenance attestation being irreversible, with `Sigstore` and `Rekor` markers present.
   - `Safe recovery boundaries` now requires:
     - conditional staged-asset identity semantics (`draft release` + `byte-identical` + `staged assets` + `resume`);
     - an explicit `stop and diagnose` path for missing draft / staged-asset mismatch states;
     - a prohibition on deleting or moving a published tag/version.
   - Existing heading-order, command, and exact source-binding assertions stayed intact.

## Old-to-new invariant mapping against `cd4f318`

- `test_agent_instructions_forbid_merging_and_merge_automation`
  - now represented by `test_agent_policy_forbids_agent_controlled_merge_paths`
  - restored deleted invariants: no merge automation, no merge scripts/workflows, tap-release PR mention, review loop ends in a report, no merge command in the review-loop slice, slice stops before `Testing Gotchas`

- `test_release_docs_runbook_names_bindings_commands_and_irreversible_steps`
  - now represented by `test_release_runbook_preserves_release_order_and_exact_source_binding`
  - plus unchanged focused coverage in:
    - `test_release_docs_runbook_requires_protected_tags_and_maintainer_approval`
    - `test_release_docs_runbook_gives_the_five_trusted_publisher_claims`
    - `test_release_docs_call_provenance_attestation_irreversible`
    - `test_release_docs_show_how_to_find_the_run_id_and_the_dispatch_precondition`

- `test_release_docs_runbook_marks_recovery_boundaries_and_upgrade_source`
  - now represented by `test_release_runbook_preserves_release_order_and_exact_source_binding`
  - plus unchanged source-install fallback coverage in `test_release_docs_keep_a_source_install_fallback_before_publication`

- `test_release_docs_runbook_lists_retained_user_data_and_opt_in_cleanup`
- `test_release_docs_list_and_clean_the_mcp_endpoint_state`
  - now represented by `test_release_docs_preserve_retained_state_and_explicit_cleanup_controls`
  - plus unchanged focused coverage in:
    - `test_release_docs_runbook_lists_and_cleans_the_os_keyring_credential`
    - `test_release_readme_discloses_the_retained_os_keyring_credential`
    - `test_release_docs_correct_the_xdg_config_claim`

- `test_release_notes_exist_for_the_version_being_shipped`
- `test_release_docs_readme_pins_current_install_and_links_the_runbook`
- `test_release_docs_never_name_a_version_other_than_the_project_version`
  - now represented by `test_current_release_docs_only_name_allowed_versions`
  - restored broad doc scanning across `README.md`, `docs/release.md`, and the current release notes

Preserved unchanged rendered-package/link tests:
- `test_every_sidebar_link_to_a_repository_file_points_at_a_real_file`
- `test_every_absolute_repository_link_resolves_to_a_real_path`
- `test_readme_has_no_relative_links_because_pypi_cannot_follow_them`

## Mutation RED evidence

All mutations used helper-input copies only; repository docs were not edited.

### Remaining semantic-class review issue

Command:

```sh
uv run --no-sync python - <<'PY'
from tests.release_contracts import markdown_section
from tests.test_release_policy import (
   _RUNBOOK,
   _assert_irreversible_boundary_contracts,
   _assert_safe_recovery_contracts,
)

runbook = _RUNBOOK.read_text()

cases = [
   (
       "annotated-tag-irreversible",
       runbook.replace(
           "annotated tag publication is irreversible",
           "annotated tag publication is reversible",
           1,
       ),
       lambda text: _assert_irreversible_boundary_contracts(
           markdown_section(text, "Irreversible boundaries")
       ),
   ),
   (
       "pypi-publication-irreversible",
       runbook.replace(
           "PyPI publication is irreversible",
           "PyPI publication is reversible",
           1,
       ),
       lambda text: _assert_irreversible_boundary_contracts(
           markdown_section(text, "Irreversible boundaries")
       ),
   ),
   (
       "attestation-sigstore-rekor",
       runbook.replace(
           "Sigstore infrastructure and records the entry in the\n"
           "  public Rekor transparency log",
           "internal CI notes only",
           1,
       ),
       lambda text: _assert_irreversible_boundary_contracts(
           markdown_section(text, "Irreversible boundaries")
       ),
   ),
   (
       "staged-asset-identity-match",
       runbook.replace("byte-identical", "comparable", 1),
       lambda text: _assert_safe_recovery_contracts(
           markdown_section(text, "Safe recovery boundaries")
       ),
   ),
   (
       "stop-and-diagnose",
       runbook.replace("stop and diagnose", "continue and retry", 1),
       lambda text: _assert_safe_recovery_contracts(
           markdown_section(text, "Safe recovery boundaries")
       ),
   ),
   (
       "published-tag-version-prohibition",
       runbook.replace(
           "Do **not** attempt recovery by deleting or moving a published tag/version.",
           "Attempt recovery by deleting or moving a published tag/version.",
           1,
       ),
       lambda text: _assert_safe_recovery_contracts(
           markdown_section(text, "Safe recovery boundaries")
       ),
   ),
]

for name, mutated, check in cases:
   try:
       check(mutated)
   except AssertionError as exc:
       print(f"{name}: FAIL as expected -> {str(exc) or 'AssertionError'}")
   else:
       raise SystemExit(f"{name}: unexpectedly passed")
PY
```

Result:

- `annotated-tag-irreversible`: `FAIL as expected -> missing bullet containing terms ('annotated tag', 'irreversible')`
- `pypi-publication-irreversible`: `FAIL as expected -> missing bullet containing terms ('pypi', 'irreversible')`
- `attestation-sigstore-rekor`: `FAIL as expected -> missing bullet containing terms ('attestation', 'irreversible', 'sigstore', 'rekor')`
- `staged-asset-identity-match`: `FAIL as expected -> missing bullet containing terms ('draft release', 'byte-identical', 'staged assets', 'resume')`
- `stop-and-diagnose`: `FAIL as expected -> AssertionError`
- `published-tag-version-prohibition`: `FAIL as expected -> AssertionError`

1. **Release order contract**
   - Command:
     ```sh
     uv run --no-sync python - <<'PY'
     from tests.test_release_policy import _RUNBOOK, _project_version, _assert_release_runbook_contracts

     runbook = _RUNBOOK.read_text()
     recovery = '## Safe recovery boundaries'
     verify = '## Verify the published artifacts'
     runbook = runbook.replace(recovery, '__RECOVERY__', 1)
     runbook = runbook.replace(verify, recovery, 1)
     runbook = runbook.replace('__RECOVERY__', verify, 1)
     _assert_release_runbook_contracts(runbook, _project_version())
     PY
     ```
   - Result: failed on the heading-order assertion (`offsets == sorted(offsets)`).

2. **Dry-run gating command contract**
   - Command:
     ```sh
     uv run --no-sync python - <<'PY'
     from tests.test_release_policy import _RUNBOOK, _project_version, _assert_release_runbook_contracts

     runbook = _RUNBOOK.read_text().replace(
         'gh workflow run Release --ref main',
         'gh workflow run Release --ref release-branch',
         1,
     )
     _assert_release_runbook_contracts(runbook, _project_version())
     PY
     ```
   - Result: failed on the dry-run command assertion (`assert command in dry_run`).

3. **Tag/source binding contract**
   - Command:
     ```sh
     uv run --no-sync python - <<'PY'
     from tests.test_release_policy import _RUNBOOK, _project_version, _assert_release_runbook_contracts

     version = _project_version()
     runbook = _RUNBOOK.read_text().replace(
         f'test "$(git rev-list -n 1 refs/tags/v{version})" = "$COMMIT"',
         'test "$(git rev-list -n 1 refs/tags/v0.0.0)" = "$COMMIT"',
         1,
     )
     _assert_release_runbook_contracts(runbook, version)
     PY
     ```
   - Result: failed on the publish source-binding assertion (`assert command in publish`).

4. **Cleanup retained-lock contract**
   - Command:
     ```sh
     uv run --no-sync python - <<'PY'
     from tests.test_release_policy import _README, _RUNBOOK, _assert_cleanup_contracts

     runbook = _RUNBOOK.read_text().replace('audit.jsonl.lock', 'audit.jsonl.lck', 1)
     _assert_cleanup_contracts(_README.read_text(), runbook)
     PY
     ```
   - Result: failed on the retained-state marker assertion (`assert retained_marker in retained`).

5. **Cleanup no-`--force` contract**
   - Command:
     ```sh
     uv run --no-sync python - <<'PY'
     from tests.test_release_policy import _README, _RUNBOOK, _assert_cleanup_contracts

     runbook = _RUNBOOK.read_text().replace(
         'rm -f ~/.config/korvid/config.yaml ~/.config/korvid/credentials.json',
         'rm --force ~/.config/korvid/config.yaml ~/.config/korvid/credentials.json',
         1,
     )
     _assert_cleanup_contracts(_README.read_text(), runbook)
     PY
     ```
   - Result: failed on the cleanup safety assertion (`assert "--force" not in cleanup`).

6. **Broad stale-version contract**
   - Command:
     ```sh
     uv run --no-sync python - <<'PY'
     from tests.test_release_policy import _README, _RUNBOOK, _project_version, _release_notes, _assert_release_versions_contracts

     version = _project_version()
     runbook = _RUNBOOK.read_text().replace(
         f'korvid-{version}-py3-none-any.whl',
         'korvid-9.9.9-py3-none-any.whl',
         1,
     )
     _assert_release_versions_contracts(
         version,
         _README.read_text(),
         runbook,
         _release_notes(version),
     )
     PY
     ```
   - Result: failed on the new runbook stale-version assertion (`docs/release.md names ['9.9.9']`).

## GREEN verification evidence

Commands run:

```sh
uv run --no-sync pytest -p no:tach tests/test_release_policy.py -q
uv run --no-sync pytest -p no:tach tests/test_release_policy.py tests/test_release_scripts.py tests/test_lockfile.py -q
uv run --no-sync ruff check tests/test_release_policy.py
uv run --no-sync ruff format --check tests/test_release_policy.py
```

Results:
- `tests/test_release_policy.py`: `4 passed in 0.05s`
- Task 2 pytest selection: `153 passed in 3.38s`
- `ruff check`: `All checks passed!`
- `ruff format --check`: `1 file already formatted`

## Final commit

- Commit: `PENDING`
- Message: `test: scan release runbook for stale versions`
- Trailer: `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`

## Self-review

- Only `tests/test_release_policy.py` changed under `tests/`; the report was updated under `.superpowers/`.
- No workflow files, product docs, or `uv.lock` were modified.
- Recovered contracts are structural: commands, headings, ordering, URLs, section-scoped semantic bullets, and prohibitions.
- Every deleted invariant class called out by the reviewer now has explicit RED evidence and focused GREEN verification, including the release runbook stale-version guard.
