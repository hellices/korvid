from __future__ import annotations

import re
import tomllib
from pathlib import Path

from tests.release_contracts import markdown_section

_ROOT = Path(__file__).parents[1]
_AGENTS = _ROOT / "AGENTS.md"
_README = _ROOT / "README.md"
_RUNBOOK = _ROOT / "docs" / "release.md"
_ALLOWED_README_HISTORY = {"0.1.0", "0.1.1", "0.1.2"}


def _project_version() -> str:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    version = pyproject["project"]["version"]
    assert isinstance(version, str)
    assert version
    return version


def _release_notes(version: str) -> str:
    path = _ROOT / "docs" / "release-notes" / f"v{version}.md"
    assert path.is_file(), f"{path.name} is missing; the release stages notes from this file"
    return path.read_text()


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _named_versions(text: str) -> set[str]:
    return set(re.findall(r"\b\d+\.\d+\.\d+\b", text))


def _assert_agent_policy_contracts(agents: str) -> None:
    pull_requests = markdown_section(agents, "Pull Requests")
    review_loop = markdown_section(agents, "Review Loop")
    policy = f"{pull_requests}\n{review_loop}"
    normalized = _normalized(policy).lower()
    for blocked_route in ("gh pr merge", "auto-merge", "REST/GraphQL merge endpoints"):
        assert blocked_route in policy
    assert "maintainer merges" in normalized
    assert "no merge automation, in any form." in normalized
    assert "do not add a workflow, action, script or scheduled job that merges" in normalized
    assert "the release pr the release workflow opens there" in normalized
    assert "Never approve your own work" in policy
    assert "This loop ends in a report, never in a merge" in review_loop
    assert "gh pr merge" not in review_loop
    assert "toward merge" not in review_loop
    assert "Testing Gotchas" not in review_loop


def _assert_release_runbook_contracts(runbook: str, version: str) -> None:
    headings = (
        "## One-time repository and publisher bindings",
        "## Irreversible boundaries",
        "## Dry run on `main` before tagging",
        "## Required cross-version upgrade gate",
        f"## Publish `v{version}`",
        "## Safe recovery boundaries",
        "## Verify the published artifacts",
        "## Publish and verify the Homebrew tap",
    )
    offsets = [runbook.index(heading) for heading in headings]
    assert offsets == sorted(offsets)

    bindings = markdown_section(runbook, "One-time repository and publisher bindings")
    dry_run = markdown_section(runbook, "Dry run on `main` before tagging")
    upgrade = markdown_section(runbook, "Required cross-version upgrade gate")
    publish = markdown_section(runbook, f"Publish `v{version}`")
    recovery = markdown_section(runbook, "Safe recovery boundaries")
    verify = markdown_section(runbook, "Verify the published artifacts")
    dry_run_normalized = _normalized(dry_run)

    for binding in (
        "refs/tags/v*",
        "`release`",
        "`.github/workflows/release.yml`",
        "`hellices/korvid`",
    ):
        assert binding in bindings

    assert "```sh\nset -eu" in dry_run
    for command in (
        "git fetch origin main",
        "COMMIT=$(git rev-parse origin/main)",
        "gh workflow run Release --ref main",
        "gh run list --workflow Release --limit 1",
        'gh run watch "$RUN_ID" --exit-status',
        'gh run view "$RUN_ID"',
    ):
        assert command in dry_run
    assert "Do not tag anything until that dry run succeeds" in dry_run_normalized

    for command in (
        ': "${RUN_ID:?set RUN_ID to the confirmed dry-run workflow ID}"',
        ': "${COMMIT:?set COMMIT to the reviewed origin/main SHA}"',
        "DRY_RUN_COMMIT=$(gh run view \"$RUN_ID\" --json headSha --jq '.headSha') || exit 1",
        '[ "$DRY_RUN_COMMIT" != "$COMMIT" ]',
        'gh run download "$RUN_ID" --name dist --dir "$candidate_dir"',
        f'CANDIDATE="$PWD/$candidate_dir/korvid-{version}-py3-none-any.whl"',
        "uv pip install --python \"$upgrade_python\" 'korvid[all]==0.1.2'",
        f"\"$upgrade_korvid\" --version | grep -Fx 'korvid {version}'",
    ):
        assert command in upgrade

    for command in (
        f'git tag -a v{version} "$COMMIT" -m "korvid v{version}"',
        f'test "$(git rev-list -n 1 refs/tags/v{version})" = "$COMMIT"',
        f"git push origin refs/tags/v{version}",
        "TAG_RUN_ID=$(gh run list --workflow Release --event push \\",
        f'--branch v{version} --commit "$COMMIT" --limit 1 \\',
        "TAG_RUN_COMMIT=$(gh run view \"$TAG_RUN_ID\" --json headSha --jq '.headSha')",
        'test "$TAG_RUN_COMMIT" = "$COMMIT"',
        'gh run watch "$TAG_RUN_ID" --exit-status',
    ):
        assert command in publish

    assert "Do **not** attempt recovery by deleting or moving a published tag/version." in recovery

    assert "```sh\nset -eu" in verify
    for command in (
        f"gh release download v{version} --dir dist/v{version}",
        f"gh attestation verify dist/v{version}/korvid-{version}-py3-none-any.whl --repo hellices/korvid",
        f"gh attestation verify dist/v{version}/SHA256SUMS --repo hellices/korvid",
        f"(cd dist/v{version} && shasum --algorithm 256 --check SHA256SUMS)",
    ):
        assert command in verify


def _assert_cleanup_contracts(readme: str, runbook: str) -> None:
    installation = markdown_section(readme, "Installation")
    retained = markdown_section(runbook, "Retained local state after uninstall")
    cleanup = markdown_section(runbook, "opt-in cleanup")

    assert runbook.index("## Retained local state after uninstall") < runbook.index(
        "## opt-in cleanup"
    )
    assert (
        "[release runbook](https://github.com/hellices/korvid/blob/main/docs/release.md)"
        in installation
    )
    for retained_marker in (
        "~/.config/korvid/config.yaml",
        "~/.config/korvid/credentials.json",
        "~/.local/state/korvid/audit.jsonl",
        "~/.local/state/korvid/audit.jsonl.lock",
        "~/.local/state/korvid/mcp-endpoint.json",
        "~/.local/state/korvid/mcp-endpoint.json.lock",
        "~/.local/share/korvid/logs",
        "~/.local/share/korvid/agent-payloads",
    ):
        assert retained_marker in retained

    assert cleanup.index("Stop all korvid processes") < cleanup.index(
        "Then remove the retained files"
    )
    assert 'keyring.delete_password("korvid", "github-oauth")' in cleanup
    assert 'rm -f "$state_root/audit.jsonl"' in cleanup
    assert '"$state_root/audit.jsonl.lock"' in cleanup
    assert 'rm -f "$state_root/mcp-endpoint.json" "$state_root/mcp-endpoint.json.lock"' in cleanup
    assert 'rm -rf "$data_root/logs" "$data_root/agent-payloads"' in cleanup
    assert "--force" not in cleanup


def _assert_release_versions_contracts(version: str, readme: str, notes: str) -> None:
    readme_versions = _named_versions(readme)
    notes_versions = _named_versions(notes)
    assert version in readme_versions
    assert version in notes_versions
    assert readme_versions - (_ALLOWED_README_HISTORY | {version}) == set()
    assert notes_versions == {version}

    assert f"python -m pip install 'korvid[all]=={version}'" in readme
    assert "## Install or upgrade" in notes
    assert f"uv tool install 'korvid[all]=={version}'" in notes
    assert f"pipx install --force 'korvid[all]=={version}'" in notes
    assert "uv tool install --upgrade" not in notes
    verify = markdown_section(notes, "Verify")
    assert "```sh\nset -eu" in verify
    assert f"gh attestation verify dist/v{version}/korvid-{version}-py3-none-any.whl" in verify
    assert "--repo hellices/korvid" in verify


def test_agent_policy_forbids_agent_controlled_merge_paths() -> None:
    _assert_agent_policy_contracts(_AGENTS.read_text())


def test_release_runbook_preserves_release_order_and_exact_source_binding() -> None:
    _assert_release_runbook_contracts(_RUNBOOK.read_text(), _project_version())


def test_release_docs_preserve_retained_state_and_explicit_cleanup_controls() -> None:
    _assert_cleanup_contracts(_README.read_text(), _RUNBOOK.read_text())


def test_current_release_docs_only_name_allowed_versions() -> None:
    version = _project_version()
    _assert_release_versions_contracts(version, _README.read_text(), _release_notes(version))
