from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from tests.release_contracts import UPGRADE_SOURCE_VERSION, markdown_section

_ROOT = Path(__file__).parents[1]
_AGENTS = _ROOT / "AGENTS.md"
_README = _ROOT / "README.md"
_AGENT_DOC = _ROOT / "docs" / "agent.md"
_GETTING_STARTED = _ROOT / "docs" / "getting-started.md"
_HOMEPAGE = _ROOT / "docs" / "index.md"
_OBSERVABILITY = _ROOT / "docs" / "observability.md"
_RUNBOOK = _ROOT / "docs" / "release.md"
_SECURITY = _ROOT / "SECURITY.md"
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


def _assert_release_runbook_contracts(runbook: str) -> None:
    headings = (
        "## One-time repository and publisher bindings",
        "## Irreversible boundaries",
        "## Dry run on `main` before tagging",
        "## Required cross-version upgrade gate",
        "## Publish `$TAG`",
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
    publish = markdown_section(runbook, "Publish `$TAG`")
    recovery = markdown_section(runbook, "Safe recovery boundaries")
    verify = markdown_section(runbook, "Verify the published artifacts")
    tap = markdown_section(runbook, "Publish and verify the Homebrew tap")
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
        "LOCAL_HEAD=$(git rev-parse HEAD)",
        "git update-index -q --refresh",
        "git diff --quiet --ignore-submodules --",
        "git diff --cached --quiet --ignore-submodules --",
        "VERSION=$(python scripts/release/release_config.py version)",
        "UPGRADE_SOURCE=$(python scripts/release/release_config.py upgrade-source)",
        'TAG="v$VERSION"',
        ': "${VERSION:?release version is required}"',
        ': "${UPGRADE_SOURCE:?release upgrade source is required}"',
        ': "${TAG:?release tag is required}"',
        "gh workflow run Release --ref main",
        "gh run list --workflow Release --limit 1",
        "RUN_ID=$(gh run list --workflow Release --limit 1 --json databaseId "
        "--jq '.[0].databaseId // empty')",
        'gh run watch "$RUN_ID" --exit-status',
        'gh run view "$RUN_ID"',
    ):
        assert command in dry_run
    assert "check out the reviewed commit before reading release metadata" in dry_run
    assert "tracked working tree has local modifications" in dry_run
    assert "index has staged tracked changes" in dry_run
    assert dry_run.index("LOCAL_HEAD=$(git rev-parse HEAD)") < dry_run.index(
        "VERSION=$(python scripts/release/release_config.py version)"
    )
    assert dry_run.index("git diff --cached --quiet --ignore-submodules --") < dry_run.index(
        "VERSION=$(python scripts/release/release_config.py version)"
    )
    assert dry_run.index(
        "VERSION=$(python scripts/release/release_config.py version)"
    ) < dry_run.index("gh workflow run Release --ref main")
    assert "The gh run list command retrieves" in dry_run
    assert "The second command retrieves" not in dry_run

    for command in (
        ': "${RUN_ID:?set RUN_ID to the confirmed dry-run workflow ID}"',
        ': "${COMMIT:?set COMMIT to the reviewed origin/main SHA}"',
        ': "${VERSION:?set VERSION via scripts/release/release_config.py version}"',
        ': "${UPGRADE_SOURCE:?set UPGRADE_SOURCE via scripts/release/release_config.py upgrade-source}"',
        "DRY_RUN_COMMIT=$(gh run view \"$RUN_ID\" --json headSha --jq '.headSha') || exit 1",
        '[ "$DRY_RUN_COMMIT" != "$COMMIT" ]',
        'gh run download "$RUN_ID" --name dist --dir "$candidate_dir"',
        'CANDIDATE="$PWD/$candidate_dir/korvid-${VERSION}-py3-none-any.whl"',
        'uv pip install --python "$upgrade_python" "korvid[all]==${UPGRADE_SOURCE}"',
        '"$upgrade_korvid" --version | grep -Fx "korvid ${UPGRADE_SOURCE}"',
        '"$upgrade_korvid" --version | grep -Fx "korvid ${VERSION}"',
        'test ! -e "$runtime_root"',
    ):
        assert command in upgrade

    for command in (
        ': "${COMMIT:?set COMMIT to the reviewed origin/main SHA}"',
        ': "${VERSION:?set VERSION via scripts/release/release_config.py version}"',
        ': "${TAG:?set TAG to v$VERSION}"',
        '[ "$TAG" != "v$VERSION" ]',
        'echo "TAG $TAG does not match expected release tag v$VERSION; refusing to publish" >&2',
        'git show "$COMMIT:pyproject.toml" >"$metadata" 2>/dev/null',
        'python scripts/release/release_config.py version --pyproject "$metadata"',
        "reviewed commit $COMMIT declares version $REVIEWED_VERSION, not requested VERSION $VERSION; refusing to publish",
        'git tag -a "$TAG" "$COMMIT" -m "korvid $TAG"',
        'test "$(git rev-list -n 1 "refs/tags/$TAG")" = "$COMMIT"',
        'git push origin "refs/tags/$TAG"',
        "TAG_RUN_ID=$(gh run list --workflow Release --event push \\",
        '--branch "$TAG" --commit "$COMMIT" --limit 1 \\',
        "TAG_RUN_COMMIT=$(gh run view \"$TAG_RUN_ID\" --json headSha --jq '.headSha')",
        'test "$TAG_RUN_COMMIT" = "$COMMIT"',
        'gh run watch "$TAG_RUN_ID" --exit-status',
    ):
        assert command in publish
    assert publish.index('[ "$TAG" != "v$VERSION" ]') < publish.index(
        'if git rev-parse --quiet --verify "refs/tags/$TAG" >/dev/null; then'
    )
    assert publish.index(
        'if git rev-parse --quiet --verify "refs/tags/$TAG" >/dev/null; then'
    ) < publish.index('git show "$COMMIT:pyproject.toml" >"$metadata" 2>/dev/null')
    assert publish.index(
        'git show "$COMMIT:pyproject.toml" >"$metadata" 2>/dev/null'
    ) < publish.index('git tag -a "$TAG" "$COMMIT" -m "korvid $TAG"')

    _assert_safe_recovery_contracts(recovery)

    assert "```sh\n" in verify
    assert "set -eu" in verify
    for command in (
        ': "${VERSION:?set VERSION via scripts/release/release_config.py version}"',
        ': "${TAG:?set TAG to v$VERSION}"',
        'gh release download "$TAG" --dir "dist/$TAG"',
        'gh attestation verify "dist/$TAG/korvid-${VERSION}-py3-none-any.whl" --repo hellices/korvid',
        'gh attestation verify "dist/$TAG/SHA256SUMS" --repo hellices/korvid',
        '(cd "dist/$TAG" && shasum --algorithm 256 --check SHA256SUMS)',
    ):
        assert command in verify

    for command in (
        ': "${VERSION:?set VERSION via scripts/release/release_config.py version}"',
        ': "${TAG:?set TAG to v$VERSION}"',
        "gh pr list --repo hellices/homebrew-korvid \\",
        "korvid ${VERSION}",
        "bump-korvid-${VERSION}",
        'echo "trusted bump-korvid-${VERSION} tap PR not found; use the manual path below" >&2',
        'formula_path="$PWD/dist/$TAG/korvid.rb"',
        'gh release download "$TAG" --pattern korvid.rb --dir "dist/$TAG"',
        'branch="bump-korvid-${VERSION}"',
        'git commit -m "korvid ${VERSION}"',
        '--title "korvid ${VERSION}" \\',
        '--body "Generated by the korvid ${TAG} release workflow from its tag-revalidated uv.lock."',
        'korvid --version | grep -Fx "korvid ${VERSION}"',
    ):
        assert command in tap


def _assert_no_pinned_korvid_requirement(text: str, *, label: str) -> None:
    match = re.search(r"korvid(?:\[[^\]]+\])?==([^\s'\"`)\],]+)", text)
    assert match is None, (
        f"{label} pins korvid requirement {match.group(0)!r}; latest-install docs must be unpinned"
    )


def _assert_evergreen_installation_contracts(
    readme: str, agent: str, getting_started: str, homepage: str, observability: str
) -> None:
    quick_start = markdown_section(readme, "Quick start")
    installation = markdown_section(readme, "Installation")
    agent_install = markdown_section(agent, "Installing the agent")
    current_release = markdown_section(getting_started, "Current release")
    install = markdown_section(getting_started, "Install")
    observability_install = markdown_section(observability, "Install")

    for text, label in (
        (quick_start, "README quick start"),
        (installation, "README installation"),
        (agent_install, "docs/agent.md installing the agent"),
        (current_release, "docs/getting-started.md current release"),
        (install, "docs/getting-started.md install"),
        (homepage, "docs/index.md"),
        (observability_install, "docs/observability.md install"),
    ):
        _assert_no_pinned_korvid_requirement(text, label=label)

    assert "uv tool install 'korvid[all]'" in quick_start
    assert "pipx install 'korvid[all]'" in quick_start
    assert "python -m pip install 'korvid[all]'" in quick_start
    assert "uv tool install 'korvid[all]'" in installation
    assert "uv tool install --force 'korvid[all]'" in installation
    assert "pipx install --force 'korvid[all]'" in installation
    assert 'uv tool install "korvid[agent]"' in agent_install
    assert 'pipx install "korvid[agent]"' in agent_install
    assert 'uv tool install "korvid[all]"' in agent_install
    assert "https://github.com/hellices/korvid/releases/latest" in current_release
    assert "uv tool install 'korvid[all]'" in install
    assert "pipx install 'korvid[all]'" in install
    assert "uv tool install 'korvid[all]'" in homepage
    assert "uv tool install 'korvid[agent,observability]'" in observability_install
    assert "uv tool install 'korvid[mcp,observability]'" in observability_install
    assert "pipx install" in observability_install


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


def _assert_allowed_release_doc_versions(
    name: str, text: str, *, version: str, allow_current: bool
) -> set[str]:
    found = _named_versions(text)
    if allow_current:
        assert version in found, f"{name} never names the version being shipped ({version})"
    else:
        assert version not in found, f"{name} hardcodes the version being shipped ({version})"
    allowed = _ALLOWED_RELEASE_DOC_HISTORY | ({version} if allow_current else set())
    stale = found - allowed
    assert not stale, (
        f"{name} names {sorted(stale)}; the only other versions release docs may name are "
        f"the explicit historical set {sorted(_ALLOWED_RELEASE_DOC_HISTORY)}"
    )
    return found


def _assert_release_versions_contracts(version: str, runbook: str, notes: str) -> None:
    runbook_versions = _named_versions(runbook)
    assert version not in runbook_versions, (
        f"docs/release.md hardcodes the version being shipped ({version})"
    )
    assert UPGRADE_SOURCE_VERSION not in runbook_versions, (
        f"docs/release.md hardcodes the upgrade source ({UPGRADE_SOURCE_VERSION})"
    )
    _assert_allowed_release_doc_versions(
        "docs/release.md", runbook, version=version, allow_current=False
    )

    notes_versions = _named_versions(notes)
    assert version in notes_versions, (
        f"docs/release-notes must name the version being shipped ({version})"
    )
    assert notes_versions == {version}, (
        "docs/release-notes may only name the version being shipped "
        f"({version}); found {sorted(notes_versions)}"
    )

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


def test_release_runbook_preserves_release_order_and_variable_source_binding() -> None:
    _assert_release_runbook_contracts(_RUNBOOK.read_text(encoding="utf-8"))


def test_release_docs_preserve_retained_state_and_explicit_cleanup_controls() -> None:
    _assert_cleanup_contracts(
        _README.read_text(encoding="utf-8"), _RUNBOOK.read_text(encoding="utf-8")
    )


def test_current_release_docs_only_name_allowed_versions() -> None:
    version = _project_version()
    _assert_release_versions_contracts(
        version,
        _RUNBOOK.read_text(encoding="utf-8"),
        _release_notes(version),
    )


def test_installation_docs_use_evergreen_latest_release_guidance() -> None:
    _assert_evergreen_installation_contracts(
        _README.read_text(encoding="utf-8"),
        _AGENT_DOC.read_text(encoding="utf-8"),
        _GETTING_STARTED.read_text(encoding="utf-8"),
        _HOMEPAGE.read_text(encoding="utf-8"),
        _OBSERVABILITY.read_text(encoding="utf-8"),
    )


def test_security_policy_uses_evergreen_supported_version_language() -> None:
    security = _SECURITY.read_text(encoding="utf-8")
    assert _named_versions(security) == set()
    assert "https://github.com/hellices/korvid/releases/latest" in security
    assert "current published minor line" in security
    assert "publishing a new minor line supersedes the previous minor line" in security


def test_pinned_requirement_is_rejected_in_evergreen_install_docs() -> None:
    with pytest.raises(AssertionError, match=r"README quick start pins korvid requirement"):
        _assert_no_pinned_korvid_requirement(
            "uv tool install 'korvid[all]==0.4.0'", label="README quick start"
        )


def test_hardcoded_upgrade_source_is_rejected_in_runbook() -> None:
    version = _project_version()
    mutated = _RUNBOOK.read_text(encoding="utf-8") + (
        f"\nuv tool install 'korvid[all]=={UPGRADE_SOURCE_VERSION}'\n"
    )
    with pytest.raises(AssertionError, match=r"docs/release\.md hardcodes the upgrade source"):
        _assert_release_versions_contracts(version, mutated, _release_notes(version))


def test_hardcoded_project_version_is_rejected_in_runbook() -> None:
    version = _project_version()
    mutated = _RUNBOOK.read_text(encoding="utf-8") + f"\nkorvid {version}\n"
    with pytest.raises(
        AssertionError, match=r"docs/release\.md hardcodes the version being shipped"
    ):
        _assert_release_versions_contracts(version, mutated, _release_notes(version))


def test_publish_step_requires_tag_to_match_version() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    publish = markdown_section(runbook, "Publish `$TAG`")
    mutated = publish.replace(
        '[ "$TAG" != "v$VERSION" ]',
        '[ "$TAG" != "$TAG" ]',
    )
    with pytest.raises(AssertionError, match=r'\[ "\$TAG" != "v\$VERSION" \]'):
        _assert_release_runbook_contracts(runbook.replace(publish, mutated))


def test_publish_step_requires_reviewed_commit_version_guard() -> None:
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    publish = markdown_section(runbook, "Publish `$TAG`")
    mutated = publish.replace(
        'git show "$COMMIT:pyproject.toml" >"$metadata" 2>/dev/null',
        'cat pyproject.toml >"$metadata"',
    )
    with pytest.raises(
        AssertionError, match=r'git show "\$COMMIT:pyproject\.toml" >"\$metadata" 2>/dev/null'
    ):
        _assert_release_runbook_contracts(runbook.replace(publish, mutated))


def test_upgrade_source_is_the_previous_minor_release() -> None:
    current = tuple(int(part) for part in _project_version().split("."))
    source = tuple(int(part) for part in UPGRADE_SOURCE_VERSION.split("."))
    assert source < current
    if source[0] == current[0]:
        assert source[1] == current[1] - 1
    else:
        pytest.fail("major bump requires an explicit UPGRADE_SOURCE_VERSION contract review")
    source_notes = _ROOT / "docs" / "release-notes" / f"v{UPGRADE_SOURCE_VERSION}.md"
    assert source_notes.is_file(), f"upgrade source has no release notes: {source_notes.name}"
