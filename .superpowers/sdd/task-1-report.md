# Task 1 Report — PEP 668-safe README installation contract

## Summary
Updated the public README installation contract to prefer isolated installs via `uv tool`/`pipx`, added explicit PEP 668 recovery guidance, and aligned release-policy tests with the new contract.

## Files changed
- `README.md`
- `tests/test_release_scripts.py`
- `tests/test_release_policy.py`

## RED
### Command
```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/fix-302-pep668-install && UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run pytest -p no:tach -q tests/test_release_scripts.py::test_readme_recommends_an_isolated_install_for_an_application tests/test_release_policy.py::test_current_release_docs_only_name_allowed_versions
```

### Output
```text
F.                                                                       [100%]
=================================== FAILURES ===================================
________ test_readme_recommends_an_isolated_install_for_an_application _________
tests/test_release_scripts.py:1501: in test_readme_recommends_an_isolated_install_for_an_application
    assert f"uv tool install 'korvid[all]=={version}'" in install
E   assert "uv tool install 'korvid[all]==0.2.0'" in 'The protected `v0.1.0` workflow failed before publication, so that tag remains\nimmutable, unpublished audit history....s\n```\n\nContributor docs: [Windows contributor notes](https://github.com/hellices/korvid/blob/main/docs/windows.md).'
=========================== short test summary info ============================
FAILED tests/test_release_scripts.py::test_readme_recommends_an_isolated_install_for_an_application
1 failed, 1 passed in 0.17s
```

## GREEN
### Command
```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/fix-302-pep668-install && UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run pytest -p no:tach -q tests/test_release_scripts.py::test_readme_recommends_an_isolated_install_for_an_application tests/test_release_policy.py::test_current_release_docs_only_name_allowed_versions
```

### Output
```text
..                                                                       [100%]
2 passed in 0.06s
```

## Focused checks
### Command
```bash
cd /Users/hwang-inhwan/workspace/kube/.worktrees/fix-302-pep668-install && UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run pytest -p no:tach -q tests/test_release_scripts.py::test_every_absolute_repository_link_resolves_to_a_real_path tests/test_release_scripts.py::test_release_readme_discloses_the_retained_os_keyring_credential tests/test_release_policy.py
```

### Output
```text
......                                                                   [100%]
6 passed in 0.05s
```

## Commit
- `bee38db` — `docs: make public installs PEP 668-safe`

## Self-review
- README now uses `uv tool install`/`pipx` for all public install paths and replaces raw pip install/uninstall guidance in the public Installation section.
- The install section now includes explicit PEP 668 recovery text and preserves the retained-state/uninstall disclosure.
- The release-policy test now expects the README to name the isolated install path.
- No changes were made to `pyproject.toml`, dependencies, workflows, or `uv.lock`.

## Concerns
- The brief referenced `tests/test_release_scripts.py::test_readme_links_are_valid_from_pypi`, but that exact test does not exist in this tree; I ran the equivalent available README-link check (`test_every_absolute_repository_link_resolves_to_a_real_path`) instead.

## Review fix

### Files
- `README.md`
- `docs/release.md`
- `tests/test_release_policy.py`
- `tests/test_release_scripts.py`

### Command outputs
- `uv run pytest -p no:tach -q tests/test_release_scripts.py::test_readme_recommends_an_isolated_install_for_an_application tests/test_release_policy.py::test_current_release_docs_only_name_allowed_versions` → `2 passed in 0.09s`
- `uv run ruff check tests/test_release_scripts.py && uv run ruff format --check tests/test_release_scripts.py` → `All checks passed!` / `1 file already formatted`
- `uv run pytest -p no:tach -q tests/test_release_scripts.py::test_readme_has_no_relative_links_because_pypi_cannot_follow_them tests/test_release_scripts.py::test_release_readme_discloses_the_retained_os_keyring_credential tests/test_release_policy.py` → `6 passed in 0.06s`

### Commit SHA
- `8ff1c22`

### Concerns
- None.

## Review fix 2

### Files
- `README.md`
- `docs/release.md`
- `tests/test_release_policy.py`

### Command outputs
- `UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run pytest -p no:tach -q tests/test_release_scripts.py::test_readme_recommends_an_isolated_install_for_an_application tests/test_release_policy.py::test_current_release_docs_only_name_allowed_versions` → `2 passed in 0.09s`
- `UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run pytest -p no:tach -q tests/test_release_scripts.py::test_every_absolute_repository_link_resolves_to_a_real_path tests/test_release_scripts.py::test_release_readme_discloses_the_retained_os_keyring_credential tests/test_release_policy.py` → `6 passed in 0.05s`
- `UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run ruff check tests/test_release_scripts.py tests/test_release_policy.py && UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run ruff format --check tests/test_release_scripts.py tests/test_release_policy.py` → `All checks passed!` / `2 files already formatted`

### Commit SHA
- `5a45f81`

### Concerns
- None.

## Review fix 3

### Files
- `tests/test_release_scripts.py`

### Command outputs
- `UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run pytest -p no:tach -q tests/test_release_scripts.py::test_readme_recommends_an_isolated_install_for_an_application tests/test_release_policy.py::test_current_release_docs_only_name_allowed_versions` → `..                                                                       [100%]` / `2 passed in 0.15s`
- `UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run pytest -p no:tach -q tests/test_release_scripts.py::test_every_absolute_repository_link_resolves_to_a_real_path tests/test_release_scripts.py::test_release_readme_discloses_the_retained_os_keyring_credential tests/test_release_policy.py` → `......                                                                   [100%]` / `6 passed in 0.10s`
- `UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run ruff check tests/test_release_scripts.py && UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv PYTHONPATH="$PWD/src:$PWD" uv run ruff format --check tests/test_release_scripts.py` → `All checks passed!` / `1 file already formatted`
- `git diff --check` → no output

### Concerns
- None.
