from __future__ import annotations

import tomllib
from pathlib import Path

from tests.release_contracts import markdown_section

_ROOT = Path(__file__).parents[1]
_AGENTS = _ROOT / "AGENTS.md"
_README = _ROOT / "README.md"
_RUNBOOK = _ROOT / "docs" / "release.md"


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


def test_agent_policy_forbids_agent_controlled_merge_paths() -> None:
    pull_requests = markdown_section(_AGENTS.read_text(), "Pull Requests")
    review_loop = markdown_section(_AGENTS.read_text(), "Review Loop")
    policy = f"{pull_requests}\n{review_loop}"
    normalized = _normalized(policy).lower()
    for blocked_route in ("gh pr merge", "auto-merge", "REST/GraphQL merge endpoints"):
        assert blocked_route in policy
    assert "maintainer merges" in normalized
    assert "homebrew-korvid" in policy
    assert "Never approve your own work" in policy
    assert "gh pr merge" not in review_loop
    assert "gh pr view N --json statusCheckRollup" in review_loop


def test_release_runbook_covers_irreversible_boundaries_and_recovery() -> None:
    version = _project_version()
    runbook = _RUNBOOK.read_text()
    irreversible = markdown_section(runbook, "Irreversible boundaries")
    dry_run = markdown_section(runbook, "Dry run on `main` before tagging")
    recovery = markdown_section(runbook, "Safe recovery boundaries")
    verify = markdown_section(runbook, "Verify the published artifacts")
    for command in ("gh workflow run Release --ref main", 'gh run watch "$RUN_ID" --exit-status'):
        assert command in dry_run
    assert "PyPI publication is irreversible" in irreversible
    assert "attestation is irreversible" in irreversible
    assert "staged assets match" in recovery
    assert "stop and diagnose" in recovery
    assert f"gh release download v{version} --dir dist/v{version}" in verify
    assert (
        f"gh attestation verify dist/v{version}/korvid-{version}-py3-none-any.whl"
        " --repo hellices/korvid"
    ) in verify
    assert "shasum --algorithm 256 --check SHA256SUMS" in verify


def test_release_docs_cover_retained_state_and_opt_in_cleanup() -> None:
    installation = markdown_section(_README.read_text(), "Installation")
    runbook = _RUNBOOK.read_text()
    retained = markdown_section(runbook, "Retained local state after uninstall")
    retained_normalized = _normalized(retained)
    cleanup = markdown_section(runbook, "opt-in cleanup")
    for retained_marker in (
        "~/.config/korvid/config.yaml",
        "~/.local/state/korvid/audit.jsonl",
        "~/.local/state/korvid/mcp-endpoint.json",
        "~/.local/share/korvid/agent-payloads",
    ):
        assert retained_marker in retained
    assert "service `korvid`, account `github-oauth`" in retained_normalized
    assert "cleanup is explicit and opt-in" in installation
    assert "docs/release.md" in installation
    assert "rm -f ~/.config/korvid/config.yaml ~/.config/korvid/credentials.json" in cleanup
    assert 'rm -f "$state_root/mcp-endpoint.json" "$state_root/mcp-endpoint.json.lock"' in cleanup
    assert 'rm -rf "$data_root/logs" "$data_root/agent-payloads"' in cleanup


def test_current_release_install_commands_use_the_project_version() -> None:
    version = _project_version()
    readme = _README.read_text()
    notes = _release_notes(version)
    assert f"uv tool install 'korvid[all]=={version}'" in readme
    assert f"python -m pip install 'korvid[all]=={version}'" in readme
    assert f"# korvid v{version}" in notes
    assert f"uv tool install 'korvid[all]=={version}'" in notes
    assert f"pipx install --force 'korvid[all]=={version}'" in notes
