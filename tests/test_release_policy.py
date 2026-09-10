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
_MKDOCS = _ROOT / "mkdocs.yml"
_RUNBOOK = _ROOT / "docs" / "release.md"
_SECURITY = _ROOT / "SECURITY.md"
_UNPUBLISHED_RELEASE_NOTE = _ROOT / "docs" / "release-notes" / "v0.4.0.md"
_FIRST_PUBLISHED_0_4_VERSION = "0.4.1"
_FIRST_PUBLISHED_0_4_NOTE = _ROOT / "docs" / "release-notes" / f"v{_FIRST_PUBLISHED_0_4_VERSION}.md"
_ALLOWED_RELEASE_DOC_HISTORY = frozenset({"0.1.0", "0.1.1", "0.1.2"})


def _project_version() -> str:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = pyproject["project"]["version"]
    assert isinstance(version, str)
    assert version
    return version


def _optional_dependencies(extra: str) -> list[str]:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["optional-dependencies"][extra]
    assert isinstance(dependencies, list)
    assert all(isinstance(dependency, str) for dependency in dependencies)
    return dependencies


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


def _other_release_version(version: str) -> str:
    return "0.4.0" if version != "0.4.0" else "0.3.0"


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
    _assert_section_has_bullet(pull_requests, "ordinary", "source", "tap", "manual")
    _assert_section_has_bullet(
        pull_requests,
        "trusted",
        "post-release",
        "homebrew-korvid",
        "validator",
        "workflow_run",
        "match-head-commit",
    )
    _assert_section_has_bullet(
        pull_requests,
        "opened by the app bot",
        "HOMEBREW_APP_SLUG",
    )
    _assert_section_has_bullet(
        pull_requests,
        "workflow_run",
        "head sha",
        "match-head-commit",
    )
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
    assert "repository variable: `HOMEBREW_APP_ID`" in bindings
    assert "repository secret: `HOMEBREW_APP_PRIVATE_KEY`" in bindings
    assert "HOMEBREW_APP_SLUG" not in bindings

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
        ': "${HOMEBREW_APP_SLUG:?set to the GitHub App slug bound to the tap}"',
        'case "$HOMEBREW_APP_SLUG" in',
        'EXPECTED_BOT_LOGIN="${HOMEBREW_APP_SLUG}[bot]"',
        'gh api "repos/hellices/homebrew-korvid/pulls?state=open&head=hellices:bump-korvid-${VERSION}&base=main" \\',
        "bump-korvid-${VERSION}",
        'AUTHOR_LOGIN=$(gh api "repos/hellices/homebrew-korvid/pulls?state=open&head=hellices:bump-korvid-${VERSION}&base=main" \\',
        ".user.login // empty",
        "does not match expected",
        'gh pr checks "$TAP_PR" --repo hellices/homebrew-korvid --watch || exit 1',
        'gh pr view "$TAP_PR" --repo hellices/homebrew-korvid --json',
        'korvid --version | grep -Fx "korvid ${VERSION}"',
    ):
        assert command in tap
    assert "HOMEBREW_APP_ID" in bindings
    assert "HOMEBREW_APP_ID" in tap
    assert "HOMEBREW_APP_PRIVATE_KEY" in bindings
    assert "HOMEBREW_APP_PRIVATE_KEY" in tap
    assert "HOMEBREW_APP_SLUG" in tap
    for contract in (
        "default-branch validator",
        "safe to retry",
        "same version",
        "downgrade",
        "fails visibly",
    ):
        assert contract in tap
    assert (
        "source workflow itself can keep using `actions/create-github-app-token`'s `app-slug` output"
        in _normalized(tap)
    )
    lowered_tap = _normalized_lower(tap)
    assert "ordinary human-authored pull" in lowered_tap
    assert "requests remain manual" in lowered_tap
    assert "gh pr merge --squash --match-head-commit" in tap
    assert "merge enrollment can stay disabled" in lowered_tap
    assert 'gh release download "$TAG" --pattern korvid.rb --dir "dist/$TAG"' not in tap
    assert 'git commit -m "korvid ${VERSION}"' not in tap


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


def _assert_current_release_note_contracts(version: str, notes: str) -> None:
    assert notes.startswith(f"# korvid v{version}\n"), (
        f"docs/release-notes heading must start with '# korvid v{version}'"
    )
    notes_versions = _named_versions(notes)
    assert version in notes_versions, (
        f"docs/release-notes must name the version being shipped ({version})"
    )

    assert "## Install or upgrade" in notes
    assert f"uv tool install 'korvid[all]=={version}'" in notes, (
        "docs/release-notes installs the current korvid version"
    )
    assert f"uv tool install --force 'korvid[all]=={version}'" in notes, (
        "docs/release-notes reinstalls the current korvid version"
    )
    assert f"pipx install --force 'korvid[all]=={version}'" in notes, (
        "docs/release-notes shows the current pipx reinstall version"
    )
    assert "uv tool install --upgrade" not in notes

    pinned_requirements = set(re.findall(r"korvid(?:\[[^\]]+\])?==(\d+\.\d+\.\d+)", notes))
    assert pinned_requirements == {version}, (
        "docs/release-notes pins korvid installable versions "
        f"{sorted(pinned_requirements)} instead of only {version}"
    )

    verify = markdown_section(notes, "Verify")
    assert "```sh\nset -eu" in verify
    assert f"gh release download v{version} --dir dist/v{version}" in verify, (
        "docs/release-notes downloads the current tagged artifacts"
    )
    assert f"gh attestation verify dist/v{version}/korvid-{version}-py3-none-any.whl" in verify, (
        "docs/release-notes verifies the current wheel filename"
    )
    assert f"gh attestation verify dist/v{version}/SHA256SUMS --repo hellices/korvid" in verify, (
        "docs/release-notes verifies the current checksum file"
    )
    assert f"(cd dist/v{version} && shasum --algorithm 256 --check SHA256SUMS)" in verify, (
        "docs/release-notes checks the current checksum directory"
    )
    assert "--repo hellices/korvid" in verify

    attested_wheels = set(
        re.findall(
            r"gh attestation verify dist/v(\d+\.\d+\.\d+)/korvid-(\d+\.\d+\.\d+)-py3-none-any\.whl",
            verify,
        )
    )
    assert attested_wheels == {(version, version)}, (
        "docs/release-notes verifies wheel artifacts "
        f"{sorted(attested_wheels)} instead of only {(version, version)}"
    )


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
    _assert_current_release_note_contracts(version, notes)


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


def test_first_published_0_4_release_note_records_the_security_remediation() -> None:
    notes = _FIRST_PUBLISHED_0_4_NOTE.read_text(encoding="utf-8")
    security = markdown_section(notes, "Security fixes")
    mcp_dependencies = _optional_dependencies("mcp")
    agent_dependencies = _optional_dependencies("agent")

    assert "httpx2>=2.12.0,<3" in mcp_dependencies
    assert not any(dependency.startswith("httpx2") for dependency in agent_dependencies)
    assert "[mcp]" in security
    assert "[all]" in security
    assert "`httpx2>=2.12.0,<3`" in security
    normalized_security = _normalized(security.replace("`", ""))
    assert "Locked httpx2 2.12.0 requires httpcore2 2.12.0 transitively." in normalized_security
    assert "optional `[agent]` extra" not in security
    assert re.search(r"httpx2[^.]{0,120}2\.11[^.]{0,120}CVE-2026-84379", security, re.S)
    assert re.search(r"httpx2[^.]{0,120}2\.11[^.]{0,120}CVE-2026-84380", security, re.S)
    assert re.search(r"httpx2[^.]{0,120}2\.12[^.]{0,120}CVE-2026-84382", security, re.S)
    assert not re.search(r"httpcore2[^.]{0,120}CVE-2026-84382", security, re.S)
    assert "v0.4.0 tag was blocked before publication" in notes
    assert "immutable audit record" in notes
    assert "not an installable release" in notes
    assert "https://hellices.github.io/korvid/release-notes/v0.4.0/" in notes


def test_first_published_0_4_release_note_distinguishes_development_security_fixes() -> None:
    notes = _FIRST_PUBLISHED_0_4_NOTE.read_text(encoding="utf-8")
    security = markdown_section(notes, "Security fixes")
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "GitPython>=3.1.59" in project["dependency-groups"]["dev"]
    assert "GitPython" in security
    assert "3.1.59" in security
    assert "development-only" in security
    for cve in ("CVE-2026-78675", "CVE-2026-78676", "CVE-2026-78677", "CVE-2026-78678"):
        assert cve in security


def test_release_history_marks_the_unpublished_audit_note() -> None:
    unpublished = _UNPUBLISHED_RELEASE_NOTE.read_text(encoding="utf-8")

    assert "Unpublished audit tag" in unpublished
    assert "blocked before publication" in unpublished
    assert "not an installable release" in unpublished
    assert (
        f"[korvid v{_FIRST_PUBLISHED_0_4_VERSION}](v{_FIRST_PUBLISHED_0_4_VERSION}.md)"
        in unpublished
    )


def test_unpublished_release_commands_are_disabled_historical_examples() -> None:
    unpublished = _UNPUBLISHED_RELEASE_NOTE.read_text(encoding="utf-8")
    commands = re.findall(r"^```sh\n(.*?)^```", unpublished, re.MULTILINE | re.DOTALL)
    assert commands, "the audit record must preserve the historical examples"
    for block in commands:
        assert all(
            not line.strip() or line.lstrip().startswith("#") for line in block.splitlines()
        ), "unpublished release commands must remain commented, not executable instructions"


def test_release_history_navigation_keeps_the_current_release_note() -> None:
    version = _project_version()
    mkdocs = _MKDOCS.read_text(encoding="utf-8")

    assert f"- v{version}: release-notes/v{version}.md" in mkdocs


def test_release_history_navigation_keeps_the_first_published_0_4_and_audit_entries() -> None:
    mkdocs = _MKDOCS.read_text(encoding="utf-8")

    assert (
        f"- v{_FIRST_PUBLISHED_0_4_VERSION}: release-notes/v{_FIRST_PUBLISHED_0_4_VERSION}.md"
        in mkdocs
    )
    assert "- v0.4.0 (unpublished): release-notes/v0.4.0.md" in mkdocs


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


def test_dependency_versions_are_allowed_in_current_release_notes() -> None:
    version = _project_version()
    augmented = _release_notes(version) + (
        "\nSecurity fixes ship with httpx2>=2.12.0 and httpcore2==2.12.0.\n"
    )
    assert "2.12.0" in augmented
    assert version in augmented
    _assert_current_release_note_contracts(version, augmented)


def test_stale_korvid_pin_is_rejected_in_current_release_notes() -> None:
    version = _project_version()
    stale = _other_release_version(version)
    mutated = _release_notes(version).replace(
        f"uv tool install 'korvid[all]=={version}'",
        f"uv tool install 'korvid[all]=={stale}'",
        1,
    )
    with pytest.raises(
        AssertionError, match=r"docs/release-notes installs the current korvid version"
    ):
        _assert_current_release_note_contracts(version, mutated)


def test_stale_artifact_filename_is_rejected_in_current_release_notes() -> None:
    version = _project_version()
    stale = _other_release_version(version)
    mutated = _release_notes(version).replace(
        f"gh attestation verify dist/v{version}/korvid-{version}-py3-none-any.whl",
        f"gh attestation verify dist/v{version}/korvid-{stale}-py3-none-any.whl",
        1,
    )
    with pytest.raises(
        AssertionError, match=r"docs/release-notes verifies the current wheel filename"
    ):
        _assert_current_release_note_contracts(version, mutated)


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
