# PEP 668-safe Installation Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every active end-user installation path safe on PEP 668 systems by leading with isolated `uv tool`/`pipx` environments and reserving pip for explicitly isolated contexts.

**Architecture:** Active user docs and runtime missing-extra messages share the
same isolated-install boundary. A pure agent-layer helper renders
version-aware `uv tool`/`pipx` reinstall commands for the composition root, UI,
and provider layer; release tests pin docs and smoke-test descriptions. Package
functionality, metadata, dependencies, workflows, and lockfile behavior do not
change.

**Tech Stack:** Markdown, pytest, Python `pathlib`, existing `markdown_section` release-test helper, uv tool/pipx packaging commands

## Global Constraints

- Work only in `/Users/hwang-inhwan/workspace/kube/.worktrees/fix-302-pep668-install` on branch `fix/302-pep668-install`.
- Public application installation uses `uv tool install` first and names `pipx install` as the equivalent alternative.
- `python -m pip` remains only in an explicitly activated virtualenv, controlled container, air-gap bundle, or maintainer-only release procedure.
- Never recommend or execute `--break-system-packages`; active guidance must
  explicitly tell readers not to use it.
- Do not change `pyproject.toml`, dependency declarations, build-system pins, release workflows, or `uv.lock`.
- Do not rewrite historical implementation plans or immutable historical release notes.
- Derive release-version assertions from `pyproject.toml`; active commands must name `0.2.0` on this branch.
- Behind the corporate mirror, use the shared environment with `UV_NO_SYNC=1`, `UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv`, and `PYTHONPATH="$PWD/src:$PWD"`.
- All manual edits use `apply_patch`; never regenerate the lockfile.

## File map

- `README.md`
  - Primary PyPI/GitHub landing page and active public installation instructions.
  - Owns the extras matrix, reinstall/upgrade, source install, uninstall, release install, and PEP 668 recovery copy.
- `docs/agent.md`
  - Optional Entra installation hint for users and the separate development command.
- `docs/observability.md`
  - Optional observability extra combinations for embedded-agent and MCP installs.
- `docs/release.md`
  - Active release runbook section for isolated PyPI/source install, reinstall, and uninstall.
  - Existing maintainer smoke/upgrade commands that explicitly create a venv remain unchanged.
- `tests/test_release_scripts.py`
  - User-document contract tests for isolated installation, PEP 668 recovery, optional extras, and runbook commands.
- `tests/test_release_policy.py`
  - Current-version contract for README and runbook installation commands.
- `src/korvid/agent/install_hint.py`
  - Pure version-aware formatter for isolated missing-extra recovery.
- `src/korvid/__main__.py`, `src/korvid/ui/app.py`, `src/korvid/providers/entra.py`
  - Existing startup/UI/provider missing-extra messages that consume the
    formatter.
- `scripts/release/smoke_install.py`
  - CI-only disposable-venv expansion descriptions; execution stays unchanged.
- `tests/agent/test_install_hint.py`
  - Exact standard and Entra helper output contracts.

---

### Task 1: Make the README installation contract PEP 668-safe

**Files:**
- Modify: `tests/test_release_scripts.py:1493-1512`
- Modify: `tests/test_release_policy.py:246-265`
- Modify: `README.md:179-234`
- Modify: `README.md:257-259`

**Interfaces:**
- Consumes: `_readme() -> str`, `_project_version() -> str`, and `markdown_section(markdown: str, heading: str) -> str`.
- Produces: a README whose public `Installation` section contains isolated base/extras/source/reinstall/uninstall paths and explicit PEP 668 recovery.
- Preserves: exact release-version checking and the existing quick-start preference for uv tool/pipx.

- [ ] **Step 1: Strengthen the README contract test**

Replace `test_readme_recommends_an_isolated_install_for_an_application` in
`tests/test_release_scripts.py` with:

```python
def test_readme_recommends_an_isolated_install_for_an_application() -> None:
    """Every active public path isolates this CLI from system Python."""
    version = _project_version()
    readme = _readme()
    quick_start = markdown_section(readme, "Quick start")
    install = markdown_section(readme, "Installation")

    assert f"uv tool install 'korvid[all]=={version}'" in quick_start
    assert f"pipx install 'korvid[all]=={version}'" in quick_start
    assert f"uv tool install 'korvid[all]=={version}'" in install
    assert f"pipx install 'korvid[all]=={version}'" in install
    assert f"uv tool install --force 'korvid[all]=={version}'" in install
    assert "uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'" in install
    assert "uv tool uninstall korvid" in install
    assert "python -m pip install" not in install
    assert "python -m pip uninstall" not in install
    assert "externally-managed-environment" in install
    assert "virtual environment" in install
    assert "Do not use `--break-system-packages`" in install
    assert "3.11" in readme
```

In `_assert_release_versions_contracts` in `tests/test_release_policy.py`,
replace the README command assertion:

```python
assert f"python -m pip install 'korvid[all]=={version}'" in readme
```

with:

```python
assert f"uv tool install 'korvid[all]=={version}'" in readme
```

- [ ] **Step 2: Run the README contract tests and verify RED**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach -q \
  tests/test_release_scripts.py::test_readme_recommends_an_isolated_install_for_an_application \
  tests/test_release_policy.py::test_current_release_docs_only_name_allowed_versions
```

Expected: FAIL because the README `Installation` section still contains raw
pip install/uninstall commands and no PEP 668 recovery text.

- [ ] **Step 3: Replace the README installation section**

In `README.md`, keep the release-history paragraphs at lines 181-189, then
replace the command matrix and upgrade/source/uninstall guidance through the
state-retention paragraph with:

````markdown
korvid is an application, so each install gets its own environment. Use `uv
tool` (recommended) or `pipx`; both put `korvid` on `PATH`. Choose the complete
requirement you want:

```sh
uv tool install 'korvid==0.2.0'             # base TUI only
uv tool install 'korvid[agent]==0.2.0'      # :ai / Ctrl-A
uv tool install 'korvid[mcp]==0.2.0'        # korvid --mcp
uv tool install 'korvid[agent,observability]==0.2.0'  # agent + Prometheus/Loki
uv tool install 'korvid[mcp,observability]==0.2.0'    # MCP + Prometheus/Loki
uv tool install 'korvid[all]==0.2.0'        # recommended first install
uv tool install 'korvid[all,entra]==0.2.0'  # add Entra auth too
```

`pipx install` accepts the same requirement strings. For example:

```sh
pipx install 'korvid[all]==0.2.0'
```

If you already installed a narrower extra set, reinstall the full desired
requirement instead of assuming extras expand in place:

```sh
uv tool install --force 'korvid[all]==0.2.0'
# or
pipx install --force 'korvid[all]==0.2.0'
```

For unreleased `main` development, keep the source install isolated too:

```sh
uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'
# or
pipx install 'korvid[all] @ git+https://github.com/hellices/korvid'
```

Tagged versions should be installed from PyPI; the source form is only a
fallback for unreleased code.

If pip reports `error: externally-managed-environment`, it is protecting a
Python installation owned by your operating system (PEP 668). Do not use
`--break-system-packages`; rerun the install with `uv tool` or `pipx`. If you
specifically need pip inside a container or development environment, create and
activate a virtual environment first.

Without the `[agent]` extra the agent surface is simply absent — no agent
panel, and `Ctrl-A` / `:ai` / `:model` are not registered. Without the
`[mcp]` extra the `:mcp` command reports the feature as unavailable with
an install hint. Explicitly enabling a feature whose extra is missing
(`--mcp`, `agent.provider` in config) fails at startup with an actionable
message. `[entra]` adds Entra ID auth for Azure OpenAI.

`uv tool uninstall korvid` (or `pipx uninstall korvid`) removes the package
only. It does **not** remove `~/.config/korvid/config.yaml`, the fallback
`~/.config/korvid/credentials.json`, the OS keyring credential
(`korvid` / `github-oauth`), `~/.local/state/korvid/audit.jsonl`,
`~/.local/state/korvid/mcp-endpoint.json` (and its `.lock` sibling),
`~/.local/share/korvid/logs`, or `~/.local/share/korvid/agent-payloads`;
cleanup is explicit and opt-in in the [release runbook](https://github.com/hellices/korvid/blob/main/docs/release.md).
Note that `XDG_CONFIG_HOME` does not relocate the two `~/.config/korvid`
paths — only `XDG_STATE_HOME` and `XDG_DATA_HOME` are honored, for the state
and data paths respectively.
````

At the release-install example near line 257, replace:

```sh
python -m pip install 'korvid[all]==0.2.0'
```

with:

```sh
uv tool install --force 'korvid[all]==0.2.0'
```

- [ ] **Step 4: Run the README contract tests and verify GREEN**

Run the same command as Step 2.

Expected: `2 passed`.

- [ ] **Step 5: Run README-focused lint and release-version checks**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach -q \
  tests/test_release_scripts.py::test_readme_links_are_valid_from_pypi \
  tests/test_release_scripts.py::test_release_readme_discloses_the_retained_os_keyring_credential \
  tests/test_release_policy.py
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add README.md tests/test_release_scripts.py tests/test_release_policy.py
git commit -m "docs: make public installs PEP 668-safe"
```

---

### Task 2: Normalize optional-feature docs and the release runbook

**Files:**
- Modify: `tests/test_release_scripts.py:1514`
- Modify: `tests/test_release_policy.py:246-265`
- Modify: `docs/agent.md:191-198`
- Modify: `docs/observability.md:19-30`
- Modify: `docs/release.md:318-351`

**Interfaces:**
- Consumes: `_project_version() -> str`, repository-relative docs under `Path(__file__).parents[1]`, and `markdown_section`.
- Produces: active optional-feature and release-runbook commands that never target system Python.
- Preserves: maintainer upgrade/smoke commands that create and name their dedicated venv before using pip.

- [ ] **Step 1: Add active-doc installation contracts**

Immediately after
`test_readme_recommends_an_isolated_install_for_an_application` in
`tests/test_release_scripts.py`, add:

```python
def test_optional_feature_docs_use_isolated_tool_installers() -> None:
    version = _project_version()
    root = Path(__file__).parents[1]
    agent = (root / "docs" / "agent.md").read_text()
    observability = (root / "docs" / "observability.md").read_text()

    assert f"uv tool install --force 'korvid[all,entra]=={version}'" in agent
    assert "pip install korvid[entra]" not in agent
    assert f"uv tool install 'korvid[agent,observability]=={version}'" in observability
    assert f"uv tool install 'korvid[mcp,observability]=={version}'" in observability
    assert "\npip install " not in observability


def test_release_runbook_user_install_section_is_isolated() -> None:
    version = _project_version()
    install = markdown_section(
        _release_runbook(), "Install, reinstall, and uninstall from PyPI"
    )

    assert f"uv tool install 'korvid[all]=={version}'" in install
    assert f"pipx install 'korvid[all]=={version}'" in install
    assert f"uv tool install --force 'korvid[all]=={version}'" in install
    assert "uv tool uninstall korvid" in install
    assert "python -m pip install" not in install
    assert "python -m pip uninstall" not in install
    assert "--break-system-packages" not in install
```

In `_assert_release_versions_contracts` in `tests/test_release_policy.py`,
replace:

```python
assert f"python -m pip install 'korvid[all]=={version}'" in install
```

with:

```python
assert f"uv tool install 'korvid[all]=={version}'" in install
```

- [ ] **Step 2: Run the secondary-doc contracts and verify RED**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach -q \
  tests/test_release_scripts.py::test_optional_feature_docs_use_isolated_tool_installers \
  tests/test_release_scripts.py::test_release_runbook_user_install_section_is_isolated \
  tests/test_release_policy.py::test_current_release_docs_only_name_allowed_versions
```

Expected: FAIL on the existing bare pip commands in all three docs.

- [ ] **Step 3: Update the Entra extra guidance**

Replace the Entra install paragraph in `docs/agent.md` with:

````markdown
Entra ID auth needs the optional extra. For a tool-managed application install,
reinstall the complete desired extra set:

```sh
uv tool install --force 'korvid[all,entra]==0.2.0'
# or
pipx install --force 'korvid[all,entra]==0.2.0'
```

Use `uv sync --extra entra` for development. Configs written before
`agent.auth` existed keep working: `api_key_env` implies
`auth: {method: api_key}`.
````

- [ ] **Step 4: Update the observability install guidance**

Replace the install block at the top of `docs/observability.md` with:

````markdown
## Install

Install the complete application variant in its own tool environment:

```bash
uv tool install 'korvid[agent,observability]==0.2.0'  # embedded agent tools
uv tool install 'korvid[mcp,observability]==0.2.0'    # external MCP tools
```

`pipx install` accepts the same requirement strings.
````

Keep the explanation of the connector boundary and the `httpx`/`httpx2`
distinction unchanged.

- [ ] **Step 5: Replace the runbook's end-user install section**

In `docs/release.md`, replace the body of
`## Install, reinstall, and uninstall from PyPI` through its uninstall command
with:

````markdown
The simplest install is the full feature set in its own tool environment:

```sh
uv tool install 'korvid[all]==0.2.0'
# or
pipx install 'korvid[all]==0.2.0'
```

During the brief window between this workflow landing on `main` and `v0.2.0`
appearing on PyPI, keep the source install isolated too:

```sh
uv tool install 'korvid[all] @ git+https://github.com/hellices/korvid'
# or
pipx install 'korvid[all] @ git+https://github.com/hellices/korvid'
```

Once `v0.2.0` is published, PyPI is the release path and the source install is
only a fallback for unreleased code.

If you already installed any narrower korvid requirement, reinstall the full
desired extra set:

```sh
uv tool install --force 'korvid[all]==0.2.0'
# or
pipx install --force 'korvid[all]==0.2.0'
```

To remove the package itself:

```sh
uv tool uninstall korvid
# or
pipx uninstall korvid
```
````

- [ ] **Step 6: Run the secondary-doc contracts and verify GREEN**

Run the same command as Step 2.

Expected: `3 passed`.

- [ ] **Step 7: Run all release documentation tests**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach -q tests/test_release_policy.py tests/test_release_scripts.py
```

Expected: all tests pass with no warnings.

- [ ] **Step 8: Commit Task 2**

```bash
git add \
  docs/agent.md \
  docs/observability.md \
  docs/release.md \
  tests/test_release_scripts.py \
  tests/test_release_policy.py
git commit -m "docs: isolate optional and release installs"
```

---

### Task 3: Verify the complete issue #302 fix

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes: Task 1 and Task 2 documentation contracts.
- Produces: a clean, review-ready branch with evidence that install guidance is version-consistent and no lockfile changed.

- [ ] **Step 1: Verify active docs contain no unsafe override**

Run:

```bash
rg -n -F 'Do not use `--break-system-packages`' README.md

if rg -n -- '--break-system-packages' docs/agent.md docs/observability.md docs/release.md; then
  echo "unsafe PEP 668 override found" >&2
  exit 1
fi
```

Expected: README contains the explicit prohibition, secondary docs contain no
unsafe override, and the command exits 0.

- [ ] **Step 2: Verify the issue's failed command has an isolated replacement**

Run:

```bash
rg -n "externally-managed-environment|uv tool install 'korvid\\[all\\]==0\\.2\\.0'|pipx install 'korvid\\[all\\]==0\\.2\\.0'" README.md
```

Expected: matches for the PEP 668 error name and both isolated install
commands.

- [ ] **Step 3: Run the full repository gate**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
make check
```

Expected:

- ruff passes;
- mypy passes;
- pytest passes on the supported suite;
- tach reports all modules validated.

If the local Python 3.14 interpreter triggers the known nondeterministic
`tests/obs/test_fail_closed.py::TestRoundTenFindings::test_a_deeply_nested_body_is_a_backend_error`,
rerun the full local proxy suite with exactly that test deselected, document
the supported-CI distinction, and do not modify unrelated observability code.

- [ ] **Step 4: Verify formatting, diff, lockfile, and worktree integrity**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run ruff format --check src/ tests/

git diff --check origin/main...HEAD
test "$(git hash-object uv.lock)" = "14c3957808ce1437f587bfc9a4f230e38f0892a5"
git status --short
```

Expected:

- formatting passes;
- `git diff --check` reports no whitespace errors;
- `uv.lock` retains hash `14c3957808ce1437f587bfc9a4f230e38f0892a5`;
- worktree is clean after all planned commits.

- [ ] **Step 5: Request whole-branch review**

Generate a merge-base-to-HEAD review package and request a high-capability
review against:

- issue #302;
- `docs/dev/specs/2026-08-19-pep-668-install-guidance-design.md`;
- this implementation plan;
- the rule that public application installs are isolated and
  `--break-system-packages` is never recommended.

Fix Critical and Important findings before creating the pull request. Evaluate
Minor findings under the repository review-round policy.

- [ ] **Step 6: Prepare issue response text**

Prepare, but do not post before the pull request merges:

````markdown
This is PEP 668 protecting the OS-managed Python environment, not a korvid
wheel failure. Install korvid as an isolated application instead:

```sh
uv tool install 'korvid[all]==0.2.0'
# or
pipx install 'korvid[all]==0.2.0'
```

Do not use `--break-system-packages`. If pip is required for a controlled
container or development workflow, create and activate a virtual environment
first.
````

After merge, post this response to #302 and close the issue.

---

### Task 4: Align runtime hints and release-smoke descriptions

**Files:**
- Create: `src/korvid/agent/install_hint.py`
- Create: `tests/agent/test_install_hint.py`
- Modify: `src/korvid/__main__.py:86-111`
- Modify: `src/korvid/ui/app.py:3708-3713`
- Modify: `src/korvid/ui/app.py:3882-3887`
- Modify: `src/korvid/ui/app.py:3994-3999`
- Modify: `src/korvid/providers/entra.py:35-39`
- Modify: `docs/release.md:357-373`
- Modify: `scripts/release/smoke_install.py:1-8`
- Modify: `scripts/release/smoke_install.py:112-119`
- Modify: `tests/test_release_scripts.py`
- Delete from Git tree: `.superpowers/sdd/round3-inline-fix-report.md`
- Delete from Git tree: `.superpowers/sdd/task-1-report.md`

**Interfaces:**
- Produces:
  `isolated_install_hint(*, feature: str) -> str`.
- Output names `feature` in prose and contains versioned
  `korvid[all,entra]` uv tool and pipx `--force` commands plus
  development-checkout/active-virtualenv guidance.
- Every caller gets the known-extras superset so `--force` cannot remove a
  previously installed standard or Entra extra.
- UI notifications containing the generated hint always pass `markup=False`.
- Consumes: `korvid.__version__`.

- [ ] **Step 1: Write failing helper and smoke-description tests**

Create `tests/agent/test_install_hint.py`:

```python
import pytest

from korvid import __version__
from korvid.agent.install_hint import isolated_install_hint


@pytest.mark.parametrize("feature", ["agent", "mcp", "observability"])
def test_install_hint_names_feature_and_preserves_cumulative_extras(feature: str) -> None:
    hint = isolated_install_hint(feature=feature)
    requirement = f"korvid[all,entra]=={__version__}"
    assert feature in hint
    assert f"uv tool install --force '{requirement}'" in hint
    assert f"pipx install --force '{requirement}'" in hint
    assert "pip install" not in hint
    assert "development checkout or active virtualenv" in hint


def test_entra_hint_keeps_entra_in_the_cumulative_requirement() -> None:
    hint = isolated_install_hint(feature="Entra")
    requirement = f"korvid[all,entra]=={__version__}"
    assert requirement in hint
    assert "pip install" not in hint
```

Add to `tests/test_release_scripts.py`:

```python
def test_runtime_install_hint_consumers_use_the_shared_helper() -> None:
    root = Path(__file__).parents[1] / "src" / "korvid"
    for relative in ("__main__.py", "ui/app.py", "providers/entra.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "isolated_install_hint" in source


def test_release_smoke_docs_describe_a_ci_venv_pip_check() -> None:
    root = Path(__file__).parents[1]
    runbook = markdown_section(_release_runbook(), "What the smoke matrix proves")
    smoke = (root / "scripts" / "release" / "smoke_install.py").read_text()
    assert "disposable CI virtual environment" in runbook
    assert "disposable CI virtual environment" in smoke
    assert "the documented base-to-extra expansion command" not in smoke
    assert "run the documented" not in runbook
```

Run the four tests. Expected: import failure for the missing helper and
assertion failures for raw runtime pip strings and stale smoke descriptions.

Also strengthen existing behavioral tests:

- `tests/test_main_wiring.py`: MCP and agent startup failures assert their
  context prefix, their specific `korvid[mcp]`/`korvid[agent]` requirement,
  and both isolated tool commands.
- `tests/test_observability_wiring.py`: configured-backend failure asserts
  `korvid[observability]` and both isolated commands.
- `tests/ui/test_agent_wiring.py`: agent setup and MCP notifications assert
  their specific extra.
- `tests/providers/test_entra.py`: force the lazy Azure import to fail and
  assert `korvid[all,entra]`, both isolated commands, and no raw pip command.

- [ ] **Step 2: Implement the pure install-hint helper**

Create `src/korvid/agent/install_hint.py`:

```python
from __future__ import annotations

from korvid import __version__


def isolated_install_hint(*, feature: str) -> str:
    requirement = f"korvid[all,entra]=={__version__}"
    return (
        f"reinstall the complete extras you use (including {feature}) with: "
        f"uv tool install --force '{requirement}' "
        f"(or: pipx install --force '{requirement}'). "
        "For a development checkout or active virtualenv, reinstall the "
        "complete extras in that environment instead."
    )
```

Import and interpolate this helper in every named runtime hint. Standard
agent/MCP/observability/UI hints pass their feature name. The Entra provider
passes `feature="Entra"`.

For the three direct UI missing-feature notifications and the rebuild-exception
notification that may contain this hint, pass `markup=False`. UI tests assert
both the raw message contract and `notification.markup is False`; this pins
Textual's literal rendering of `[all,entra]`.

Do not repurpose
`test_apply_agent_settings_notifies_on_plugin_error`: it continues to raise
`ProviderPluginError`. Add a separate test whose rebuild callback raises a
hint-bearing `RuntimeError` and assert that notification has `markup is False`.

- [ ] **Step 3: Correct smoke-test prose without changing execution**

In `docs/release.md`, describe the expansion as:

```markdown
environment: install base `korvid`, then use pip's `--upgrade` inside that
disposable CI virtual environment and re-assert the same contract.
```

In both `scripts/release/smoke_install.py` docstrings, replace “documented
base-to-extra expansion command” with “pip base-to-extra expansion behavior
inside a disposable CI virtual environment”. Do not change executable code.

- [ ] **Step 4: Remove ignored SDD reports from the Git tree**

Run:

```bash
git rm --cached \
  .superpowers/sdd/round3-inline-fix-report.md \
  .superpowers/sdd/task-1-report.md
```

The ignored working copies may remain locally, but neither path may appear in
`git ls-tree -r HEAD .superpowers` after commit.

- [ ] **Step 5: Verify GREEN and boundaries**

Run:

```bash
UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run pytest -p no:tach -q \
  tests/agent/test_install_hint.py \
  tests/test_main_wiring.py \
  tests/test_observability_wiring.py \
  tests/providers/test_entra.py \
  tests/ui/test_agent_wiring.py \
  tests/test_release_policy.py \
  tests/test_release_scripts.py

UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run ruff check \
  src/korvid/agent/install_hint.py \
  src/korvid/__main__.py \
  src/korvid/ui/app.py \
  src/korvid/providers/entra.py \
  tests/agent/test_install_hint.py \
  tests/test_release_scripts.py

UV_NO_SYNC=1 \
UV_PROJECT_ENVIRONMENT=/Users/hwang-inhwan/workspace/kube/.venv \
PYTHONPATH="$PWD/src:$PWD" \
uv run tach check
```

Expected: all tests and checks pass.

- [ ] **Step 6: Commit**

```bash
git add \
  docs/dev/specs/2026-08-19-pep-668-install-guidance-design.md \
  docs/dev/plans/2026-08-19-pep-668-install-guidance.md \
  docs/release.md \
  scripts/release/smoke_install.py \
  src/korvid/agent/install_hint.py \
  src/korvid/__main__.py \
  src/korvid/ui/app.py \
  src/korvid/providers/entra.py \
  tests/agent/test_install_hint.py \
  tests/test_release_scripts.py
git commit -m "fix: isolate runtime install hints"
```
