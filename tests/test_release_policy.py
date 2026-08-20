from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.release_contracts import UPGRADE_SOURCE_VERSION, markdown_section

_ROOT = Path(__file__).parents[1]
_AGENTS = _ROOT / "AGENTS.md"
_README = _ROOT / "README.md"
_RUNBOOK = _ROOT / "docs" / "release.md"
_ALLOWED_RELEASE_DOC_HISTORY = frozenset({"0.1.0", "0.1.1", "0.1.2"})


def _project_version() -> str:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    assert isinstance(version, str)
    assert version
    return version


def _release_notes(version: str) -> str:
    path = _ROOT / "docs" / "release-notes" / f"v{version}.md"
    assert path.is_file(), f"{path.name} is missing; the release stages notes from this file"
    return path.read_text(encoding="utf-8")


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _normalized_lower(text: str) -> str:
    return _normalized(text).lower()


def _named_versions(text: str) -> set[str]:
    return set(re.findall(r"\b\d+\.\d+\.\d+\b", text))


def _section_bullets(section: str) -> list[str]:
    bullets: list[str] = []
    current: list[str] = []
    for raw_line in section.splitlines():
        candidate = raw_line.lstrip()
        if candidate.startswith("- "):
            if current:
                bullets.append(_normalized(" ".join(current)))
            current = [candidate[2:]]
            continue
        if current and candidate:
            current.append(candidate)
            continue
        if current:
            bullets.append(_normalized(" ".join(current)))
            current = []
    if current:
        bullets.append(_normalized(" ".join(current)))
    return bullets


def _assert_section_has_bullet(section: str, *terms: str) -> None:
    lowered_terms = tuple(term.lower() for term in terms)
    for bullet in _section_bullets(section):
        lowered_bullet = bullet.lower()
        if all(term in lowered_bullet for term in lowered_terms):
            return
    raise AssertionError(f"missing bullet containing terms {terms!r}")


def _numbered_step(section: str, number: int) -> str:
    match = re.search(
        rf"(?ms)^{number}\.\s+(.*?)(?=^\d+\.\s+|\Z)",
        section,
    )
    assert match is not None, f"missing numbered step {number}"
    return _normalized_lower(match.group(1))


def _assert_irreversible_boundary_contracts(section: str) -> None:
    _assert_section_has_bullet(section, "annotated tag", "irreversible")
    _assert_section_has_bullet(section, "pypi", "irreversible")
    _assert_section_has_bullet(section, "attestation", "irreversible", "sigstore", "rekor")


def _assert_safe_recovery_contracts(section: str) -> None:
    _assert_section_has_bullet(
        section, "draft release", "byte-identical", "staged assets", "resume"
    )
    _assert_section_has_bullet(
        section, "pypi", "draft release", "missing", "staged assets", "stop", "diagnose"
    )
    _assert_section_has_bullet(section, "not", "deleting", "moving", "published tag", "version")


def _assert_agent_policy_contracts(agents: str) -> None:
    pull_requests = markdown_section(agents, "Pull Requests")
    review_loop = markdown_section(agents, "Review Loop")
    policy = f"{pull_requests}\n{review_loop}"
    for blocked_route in ("gh pr merge", "auto-merge", "REST/GraphQL merge endpoints"):
        assert blocked_route in policy
    _assert_section_has_bullet(pull_requests, "maintainer", "merge", "human decision")
    _assert_section_has_bullet(
        pull_requests, "merge automation", "workflow", "script", "rest/graphql"
    )
    _assert_section_has_bullet(pull_requests, "homebrew-korvid", "release pr", "maintainer")
    _assert_section_has_bullet(pull_requests, "approve", "own work")
    handoff_step = _numbered_step(review_loop, 10)
    for term in ("required check", "report", "stop", "merge"):
        assert term in handoff_step
    assert re.search(r"\b(?:never|not)\b[^.]{0,40}\bmerge\b", handoff_step)
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
    irreversible = markdown_section(runbook, "Irreversible boundaries")
    dry_run = markdown_section(runbook, "Dry run on `main` before tagging")
    upgrade = markdown_section(runbook, "Required cross-version upgrade gate")
    publish = markdown_section(runbook, f"Publish `v{version}`")
    recovery = markdown_section(runbook, "Safe recovery boundaries")
    verify = markdown_section(runbook, "Verify the published artifacts")
    for binding in (
        "refs/tags/v*",
        "`release`",
        "`.github/workflows/release.yml`",
        "`hellices/korvid`",
    ):
        assert binding in bindings

    _assert_irreversible_boundary_contracts(irreversible)

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

    for command in (
        ': "${RUN_ID:?set RUN_ID to the confirmed dry-run workflow ID}"',
        ': "${COMMIT:?set COMMIT to the reviewed origin/main SHA}"',
        "DRY_RUN_COMMIT=$(gh run view \"$RUN_ID\" --json headSha --jq '.headSha') || exit 1",
        '[ "$DRY_RUN_COMMIT" != "$COMMIT" ]',
        'gh run download "$RUN_ID" --name dist --dir "$candidate_dir"',
        f'CANDIDATE="$PWD/$candidate_dir/korvid-{version}-py3-none-any.whl"',
        f"uv pip install --python \"$upgrade_python\" 'korvid[all]=={UPGRADE_SOURCE_VERSION}'",
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

    _assert_safe_recovery_contracts(recovery)

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
    assert "rm -f ~/.config/korvid/config.yaml ~/.config/korvid/credentials.json" in cleanup
    assert 'state_root="${XDG_STATE_HOME:-$HOME/.local/state}/korvid"' in cleanup
    assert 'data_root="${XDG_DATA_HOME:-$HOME/.local/share}/korvid"' in cleanup
    assert 'rm -f "$state_root/audit.jsonl"' in cleanup
    assert '"$state_root/audit.jsonl.lock"' in cleanup
    assert 'rm -f "$state_root/mcp-endpoint.json" "$state_root/mcp-endpoint.json.lock"' in cleanup
    assert 'rm -rf "$data_root/logs" "$data_root/agent-payloads"' in cleanup
    assert "--force" not in cleanup


def _assert_allowed_release_doc_versions(name: str, text: str, version: str) -> set[str]:
    found = _named_versions(text)
    stale = found - (_ALLOWED_RELEASE_DOC_HISTORY | {version})
    assert not stale, (
        f"{name} names {sorted(stale)}; the project version is {version} and the only "
        "other versions release docs may name are the explicit historical set "
        f"{sorted(_ALLOWED_RELEASE_DOC_HISTORY)}"
    )
    assert version in found, f"{name} never names the version being shipped ({version})"
    return found


def _strip_scoped_upgrade_source(text: str, approved_contexts: tuple[str, ...]) -> str:
    for context in approved_contexts:
        assert text.count(context) == 1, f"upgrade-source context drifted: {context!r}"
        sanitized = context.replace(UPGRADE_SOURCE_VERSION, "upgrade-source")
        text = text.replace(context, sanitized)
    return text


def test_scoped_upgrade_source_stripping_keeps_unapproved_mentions() -> None:
    approved = f"published `{UPGRADE_SOURCE_VERSION}` installation to the candidate wheel"
    stale = f"uv tool install 'korvid[all]=={UPGRADE_SOURCE_VERSION}'"
    cleaned = _strip_scoped_upgrade_source(f"{approved}\n{stale}", (approved,))
    assert UPGRADE_SOURCE_VERSION in cleaned
    assert stale in cleaned


def _assert_release_versions_contracts(version: str, readme: str, runbook: str, notes: str) -> None:
    readme_upgrade = f"published `{UPGRADE_SOURCE_VERSION}` installation to the candidate wheel"
    upgrade_section = markdown_section(runbook, "Required cross-version upgrade gate")
    runbook_upgrade_intro = f"`v{UPGRADE_SOURCE_VERSION}` is the supported upgrade source"
    _assert_allowed_release_doc_versions(
        "README.md",
        _strip_scoped_upgrade_source(readme, (readme_upgrade,)),
        version,
    )
    _assert_allowed_release_doc_versions(
        "docs/release.md",
        _strip_scoped_upgrade_source(runbook, (runbook_upgrade_intro, upgrade_section)),
        version,
    )
    notes_versions = _named_versions(notes)
    assert version in notes_versions, (
        f"docs/release-notes must name the version being shipped ({version})"
    )
    assert notes_versions == {version}, (
        "docs/release-notes may only name the version being shipped "
        f"({version}); found {sorted(notes_versions)}"
    )

    assert f"uv tool install 'korvid[all]=={version}'" in readme
    install = markdown_section(runbook, "Install, reinstall, and uninstall from PyPI")
    assert f"uv tool install 'korvid[all]=={version}'" in install
    assert "## Install or upgrade" in notes
    assert f"uv tool install 'korvid[all]=={version}'" in notes
    assert f"pipx install --force 'korvid[all]=={version}'" in notes
    assert "uv tool install --upgrade" not in notes
    verify = markdown_section(notes, "Verify")
    assert "```sh\nset -eu" in verify
    assert f"gh attestation verify dist/v{version}/korvid-{version}-py3-none-any.whl" in verify
    assert "--repo hellices/korvid" in verify


def test_agent_policy_forbids_agent_controlled_merge_paths() -> None:
    _assert_agent_policy_contracts(_AGENTS.read_text(encoding="utf-8"))


def test_release_runbook_preserves_release_order_and_exact_source_binding() -> None:
    _assert_release_runbook_contracts(_RUNBOOK.read_text(encoding="utf-8"), _project_version())


def test_release_docs_preserve_retained_state_and_explicit_cleanup_controls() -> None:
    _assert_cleanup_contracts(
        _README.read_text(encoding="utf-8"), _RUNBOOK.read_text(encoding="utf-8")
    )


def test_current_release_docs_only_name_allowed_versions() -> None:
    version = _project_version()
    _assert_release_versions_contracts(
        version,
        _README.read_text(encoding="utf-8"),
        _RUNBOOK.read_text(encoding="utf-8"),
        _release_notes(version),
    )


def test_upgrade_source_version_is_rejected_outside_its_documented_context() -> None:
    version = _project_version()
    stale_install = f"\nuv tool install 'korvid[all]=={UPGRADE_SOURCE_VERSION}'\n"
    with pytest.raises(AssertionError, match=r"README\.md names"):
        _assert_release_versions_contracts(
            version,
            _README.read_text(encoding="utf-8") + stale_install,
            _RUNBOOK.read_text(encoding="utf-8"),
            _release_notes(version),
        )
