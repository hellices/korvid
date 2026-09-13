"""Structural invariants for the least-privilege GitHub Pages docs workflow.

`.github/workflows/docs.yml` builds the MkDocs site on every pull request and
push to `main`, but only *deploys* to GitHub Pages from `main`. Deployment
needs `pages: write` and `id-token: write`; the build step that just runs
`make docs-build` needs neither, so the two must live in separate jobs with
separate, minimal permission blocks (`zizmor` flags a job holding
Pages-deploy permissions it never uses as excessive).
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"
GETTING_STARTED_HOMEBREW_COMMAND = "brew install hellices/korvid/korvid"
GETTING_STARTED_HOMEBREW_TOKEN = "hellices/korvid/korvid"
CANONICAL_SITE_URL = "https://hellices.github.io/korvid/"
PUBLIC_CONTRIBUTOR_PATH = "dev/"
PUBLIC_ARCHITECTURE_PATH = "dev/specs/2026-08-12-korvid-architecture/"
PUBLIC_EVAL_PATH = "evals/methodology/"
INTERNAL_CONTRACT_TESTS_PATH = "dev/contract-tests/"
INTERNAL_RELEASE_PATH = "release/"
CLEANUP_PLAN_DOC = (
    ROOT / "docs" / "superpowers" / "plans" / "2026-09-13-github-pages-v0-5-cleanup.md"
)

PATH_FILTERS = {
    "docs/**",
    "mkdocs.yml",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "scripts/check_docs_site.py",
    ".github/workflows/docs.yml",
}

PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "20cfd1bf945f4377ade1205e4dbc17946fc9a30d",
    "actions/configure-pages": "45bfe0192ca1faeb007ade9deae92b16b8254a0d",
    "actions/upload-pages-artifact": "fc324d3547104276b827a68afc52ff2a11cc49c9",
    "actions/deploy-pages": "368f82528645a54fb793d4d04e342629a3f51346",
}


def _load() -> dict[str, Any]:
    assert WORKFLOW.exists(), f"{WORKFLOW} must exist"
    text = WORKFLOW.read_text(encoding="utf-8")
    config: dict[str, Any] = yaml.safe_load(text)
    return config


def _on_section(config: dict[str, Any]) -> dict[str, Any]:
    """Return the `on:` trigger mapping.

    PyYAML's default (non-1.2) resolver treats the unquoted scalar `on` as
    the boolean `True`, so a bare `on:` key in the workflow parses to the key
    `True`, not `"on"`. `cast` sidesteps the invariant-dict mismatch between
    the declared `dict[str, Any]` and a lookup keyed by a bool.
    """
    raw = cast("dict[object, Any]", config)
    value = raw.get("on", raw.get(True))
    assert isinstance(value, dict), "workflow must define an 'on' trigger section"
    return value


def _all_step_uses(config: dict[str, Any]) -> list[str]:
    uses: list[str] = []
    for job in config["jobs"].values():
        for step in job.get("steps", []):
            if "uses" in step:
                uses.append(step["uses"])
    return uses


def test_triggers_on_pull_request_and_push_to_main_with_path_filters() -> None:
    config = _load()
    on = _on_section(config)
    assert "pull_request" in on
    assert set(on["pull_request"].get("paths", [])) == PATH_FILTERS

    assert "push" in on
    assert on["push"].get("branches") == ["main"]
    assert set(on["push"].get("paths", [])) == PATH_FILTERS


def test_every_action_is_pinned_to_the_expected_full_commit_sha() -> None:
    config = _load()
    uses = _all_step_uses(config)
    assert uses, "workflow must have at least one 'uses' step"
    for ref in uses:
        repo, _, pinned = ref.partition("@")
        assert repo in PINNED_ACTIONS, f"unexpected action {repo!r}; update PINNED_ACTIONS"
        assert pinned == PINNED_ACTIONS[repo], (
            f"{repo} must be pinned to {PINNED_ACTIONS[repo]}, found {pinned}"
        )
        assert len(pinned) == 40, f"{repo} must be pinned to a full 40-character commit SHA"

    # Every action in the expected set must actually be used somewhere.
    used_repos = {ref.partition("@")[0] for ref in uses}
    assert used_repos == set(PINNED_ACTIONS), "workflow must use exactly the expected actions"


def test_build_job_has_only_read_permissions_and_runs_the_docs_build() -> None:
    config = _load()
    build = config["jobs"]["build"]
    assert build["runs-on"] == "ubuntu-latest", (
        "pull-request-controlled dependencies and MkDocs plugins must run on an "
        "ephemeral GitHub-hosted runner, not persistent shared infrastructure"
    )
    assert build["timeout-minutes"] == 10
    assert build["permissions"] == {"contents": "read"}

    run_steps = [step["run"] for step in build["steps"] if "run" in step]
    assert any("uv sync --locked" in run and "--group docs" in run for run in run_steps), (
        "build job must run 'uv sync --locked --group docs'"
    )
    assert any(
        run.strip() == "uv run --frozen --group docs mkdocs build --strict" for run in run_steps
    ), (
        "the runner image does not guarantee make; build docs directly with the "
        "same frozen strict MkDocs command as the Makefile target"
    )
    assert any(
        run.strip() == "uv run --frozen python scripts/check_docs_site.py" for run in run_steps
    ), "build job must validate the generated publication artifacts before upload"
    assert all("make " not in run for run in run_steps), (
        "the docs workflow must not depend on make being installed on the runner"
    )


def test_docs_workflow_pins_the_uv_tool_version() -> None:
    config = _load()
    build = config["jobs"]["build"]
    setup_steps = [
        step for step in build["steps"] if step.get("uses", "").startswith("astral-sh/setup-uv@")
    ]
    assert len(setup_steps) == 1
    assert setup_steps[0].get("with", {}).get("version") == "0.10.9", (
        "the docs build must use the repository's release-workflow uv version "
        "instead of downloading an unbounded latest release"
    )


def test_configure_pages_runs_in_deploy_where_pages_permission_exists() -> None:
    """`actions/configure-pages` must run where `pages: write` is granted.

    The action calls the Pages `GET /repos/{owner}/{repo}/pages` endpoint,
    which needs `pages: write` (or at least page metadata read) on the
    workflow's token. The build job only has `contents: read`, so a
    `configure-pages` step placed there fails there whenever Pages has not
    yet been switched to the GitHub Actions source — contradicting
    docs/dev/README.md's claim that "the workflow's build job still succeeds
    but the deploy job fails". Moving the step to the top of `deploy` (which
    already carries `pages: write` for `deploy-pages`) makes that claim true
    and keeps `build`'s permissions minimal.
    """
    config = _load()
    build = config["jobs"]["build"]
    deploy = config["jobs"]["deploy"]

    build_uses = {step["uses"].partition("@")[0] for step in build["steps"] if "uses" in step}
    assert "actions/configure-pages" not in build_uses, (
        "actions/configure-pages must not run in the build job: build only has "
        "contents:read, and the action needs pages:write to call GetPages"
    )

    all_uses = _all_step_uses(config)
    configure_pages_uses = [ref for ref in all_uses if ref.startswith("actions/configure-pages@")]
    assert len(configure_pages_uses) == 1, "configure-pages must run exactly once in the workflow"

    deploy_uses = [step["uses"] for step in deploy["steps"] if "uses" in step]
    assert deploy_uses, "deploy job must have at least one 'uses' step"
    assert deploy_uses[0].startswith("actions/configure-pages@"), (
        "configure-pages must be the first step in deploy, before deploy-pages, "
        "so Pages is configured in the job that actually holds pages:write"
    )


def test_site_upload_only_happens_on_push_to_main() -> None:
    config = _load()
    build = config["jobs"]["build"]
    upload_steps = [
        step
        for step in build["steps"]
        if step.get("uses", "").startswith("actions/upload-pages-artifact@")
    ]
    assert len(upload_steps) == 1, "build job must upload the site exactly once"
    condition = upload_steps[0].get("if", "")
    assert "github.ref == 'refs/heads/main'" in condition
    assert "github.event_name == 'push'" in condition


def test_deploy_job_is_main_only_needs_build_and_is_least_privilege() -> None:
    config = _load()
    deploy = config["jobs"]["deploy"]

    condition = deploy.get("if", "")
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/main'" in condition

    needs = deploy["needs"]
    assert needs == "build" or needs == ["build"]

    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}

    assert deploy["environment"]["name"] == "github-pages"
    assert deploy["environment"]["url"] == "${{ steps.deployment.outputs.page_url }}"

    deploy_steps = deploy["steps"]
    assert len(deploy_steps) == 2, (
        "deploy must have exactly two steps: configure-pages, then deploy-pages"
    )
    assert deploy_steps[0]["uses"].startswith("actions/configure-pages@"), (
        "configure-pages must run first, in the job that holds pages:write"
    )
    assert deploy_steps[1]["uses"].startswith("actions/deploy-pages@")
    assert deploy_steps[1]["id"] == "deployment"


def test_deploy_job_exposes_the_page_url_for_post_deploy_smoke_checks() -> None:
    """The deployed Pages URL must be available to a later smoke job."""

    config = _load()
    deploy = config["jobs"]["deploy"]
    assert deploy.get("outputs") == {"page_url": "${{ steps.deployment.outputs.page_url }}"}


def _smoke_script(config: dict[str, Any]) -> str:
    steps = _smoke_steps(config)
    assert len(steps) == 1, "smoke job must keep its post-deploy probe in one bounded step"
    script = steps[0].get("run")
    assert isinstance(script, str), "smoke job must execute a shell probe"
    return script


def _smoke_steps(config: dict[str, Any]) -> list[dict[str, Any]]:
    smoke = config["jobs"]["smoke"]
    steps = smoke["steps"]
    assert isinstance(steps, list), "smoke job must declare its steps as a list"
    return steps


def _smoke_step(config: dict[str, Any]) -> dict[str, Any]:
    steps = _smoke_steps(config)
    assert len(steps) == 1, "smoke job must keep its post-deploy probe in one bounded step"
    return steps[0]


def _shell_assignment(script: str, name: str) -> int:
    match = re.search(rf"^{name}=(\d+)$", script, flags=re.MULTILINE)
    assert match is not None, f"smoke script must assign {name}"
    return int(match.group(1))


def _site_page_source(site_path: str) -> Path:
    normalized = site_path.removesuffix("/")
    file_candidate = ROOT / "docs" / f"{normalized}.md"
    if file_candidate.exists():
        return file_candidate
    return ROOT / "docs" / normalized / "README.md"


def _frontmatter(text: str) -> str:
    match = re.match(r"\A---\n(?P<frontmatter>.*?)\n---\n", text, flags=re.DOTALL)
    return match.group("frontmatter") if match is not None else ""


def _smoke_function(script: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\)\s*\{{.*?^\}}",
        script,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"smoke job must define {name}()"
    return match.group(0)


def _bash_executable() -> str:
    if sys.platform != "win32":
        bash = shutil.which("bash")
        assert bash is not None
        return bash

    git = shutil.which("git")
    assert git is not None
    git_bash = Path(git).parent.parent / "bin" / "bash.exe"
    assert git_bash.is_file()
    return str(git_bash)


def _run_smoke_checker(checker: str, path: str, description: str, body: str) -> int:
    script = _smoke_script(_load())
    functions = "\n\n".join(
        _smoke_function(script, name)
        for name in (
            "url_for",
            "canonical_url_for",
            "assert_body_contains",
            "assert_body_not_contains",
            "assert_body_matches",
            "search_locations",
            "sitemap_urls",
            "lines_with_prefix",
            "assert_exact_line_set",
            "assert_line_present",
            "assert_line_absent",
            "check_home_page",
            "check_release_notes_nav",
            "check_search_index",
            "check_sitemap",
            "retry_until_body_checks",
        )
    )
    probe = textwrap.dedent(
        f"""\
        set -euo pipefail

        base_url="https://example.invalid"
        CANONICAL_SITE_URL={shlex.quote(CANONICAL_SITE_URL)}
        PUBLIC_CONTRIBUTOR_PATH={shlex.quote(PUBLIC_CONTRIBUTOR_PATH)}
        PUBLIC_ARCHITECTURE_PATH={shlex.quote(PUBLIC_ARCHITECTURE_PATH)}
        PUBLIC_EVAL_PATH={shlex.quote(PUBLIC_EVAL_PATH)}
        INTERNAL_RELEASE_PATH={shlex.quote(INTERNAL_RELEASE_PATH)}
        CONTENT_ATTEMPTS=1
        CONTENT_CURL_MAX_TIME=1
        CONTENT_SLEEP_SECONDS=0

        {functions}

        fetch_body() {{
          printf '%s' "$STUB_BODY"
        }}

        STUB_BODY={shlex.quote(body)}
        retry_until_body_checks {shlex.quote(path)} {shlex.quote(description)} {shlex.quote(checker)}
        """
    )
    result = subprocess.run(
        [_bash_executable(), "--noprofile", "--norc", "-c", probe],
        check=False,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return result.returncode


def test_run_smoke_checker_uses_a_portable_noninteractive_bash_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    git_root = tmp_path / "Git"
    git = git_root / "cmd" / "git.exe"
    git.parent.mkdir(parents=True)
    git.write_text("", encoding="utf-8")
    git_bash = git_root / "bin" / "bash.exe"
    git_bash.parent.mkdir(parents=True)
    git_bash.write_text("", encoding="utf-8")

    captured: dict[str, list[str]] = {}

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda name: str(git) if name == "git" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)

    body = (
        '{"docs":['
        '{"location":"dev/specs/2026-08-12-korvid-architecture/"},'
        '{"location":"evals/methodology/"}'
        "]}"
    )

    assert (
        _run_smoke_checker(
            "check_search_index", "search/search_index.json", "search index scope", body
        )
        == 0
    )
    assert captured["command"][:4] == [str(git_bash), "--noprofile", "--norc", "-c"]


def _smoke_invocation_count(script: str, helper_name: str) -> int:
    return sum(1 for line in script.splitlines() if line.lstrip().startswith(f"{helper_name} "))


def test_smoke_home_page_checker_succeeds_when_markup_matches_contract() -> None:
    body = """
    <section data-scene-switcher>
      <p>AI-NATIVE KUBERNETES TUI</p>
      <button id="scene-tab-direct" aria-controls="scene-direct">Direct</button>
      <button id="scene-tab-agent" aria-controls="scene-agent">Agent</button>
      <button id="scene-tab-mcp" aria-controls="scene-mcp">MCP</button>
      <article id="scene-direct" aria-labelledby="scene-tab-direct">
        <video src="assets/demo.mp4"></video>
        <img class="scene-panel__fallback" src="assets/scenes/cockpit-poster.png">
      </article>
      <article id="scene-agent" aria-labelledby="scene-tab-agent">
        <video src="assets/scenes/agent-demo.mp4" data-poster="assets/scenes/agent-poster.png"></video>
        <img class="scene-panel__fallback" src="assets/scenes/agent-poster.png">
      </article>
      <article id="scene-mcp" aria-labelledby="scene-tab-mcp">
        <video src="assets/scenes/mcp-follow-demo.mp4" data-poster="assets/scenes/mcp-poster.png"></video>
        <img class="scene-panel__fallback" src="assets/scenes/mcp-poster.png">
      </article>
    </section>
    """
    assert _run_smoke_checker("check_home_page", "", "home page structure", body) == 0


@pytest.mark.parametrize(
    "body",
    [
        "<section data-scene-switcher><p>AI-NATIVE KUBERNETES TUI</p></section>",
        """
        <section data-scene-switcher>
          <button id="scene-tab-direct" aria-controls="scene-direct">Direct</button>
          <button id="scene-tab-agent" aria-controls="scene-agent">Agent</button>
          <article id="scene-direct" aria-labelledby="scene-tab-direct">
            <video src="assets/demo.mp4"></video>
            <img class="scene-panel__fallback" src="assets/scenes/cockpit-poster.png">
          </article>
          <article id="scene-agent" aria-labelledby="scene-tab-agent">
            <video src="assets/scenes/agent-demo.mp4" data-poster="assets/scenes/agent-poster.png"></video>
            <img class="scene-panel__fallback" src="assets/scenes/agent-poster.png">
          </article>
        </section>
        """,
    ],
)
def test_smoke_home_page_checker_fails_when_expected_markup_is_missing(body: str) -> None:
    assert _run_smoke_checker("check_home_page", "", "home page structure", body) != 0


def test_smoke_release_notes_nav_checker_matches_any_versioned_release_link() -> None:
    body = """
    <nav aria-label="Release notes">
      <a href="../v0.5.0/">v0.5.0</a>
      <a href="../v0.4.1/">v0.4.1</a>
    </nav>
    """
    assert (
        _run_smoke_checker(
            "check_release_notes_nav",
            "release-notes/unreleased/",
            "release-notes navigation",
            body,
        )
        == 0
    )


def test_smoke_release_notes_nav_checker_fails_without_a_versioned_release_link() -> None:
    body = '<nav aria-label="Release notes"><a href="../latest/">Latest</a></nav>'
    assert (
        _run_smoke_checker(
            "check_release_notes_nav",
            "release-notes/unreleased/",
            "release-notes navigation",
            body,
        )
        != 0
    )


def test_smoke_scope_checker_succeeds_when_search_index_matches_contract() -> None:
    assert (
        _run_smoke_checker(
            "check_search_index",
            "search/search_index.json",
            "search index scope",
            (
                '{"docs":['
                '{"location":"dev/"},'
                '{"location":"dev/specs/2026-08-12-korvid-architecture/"},'
                '{"location":"evals/methodology/"}'
                "]}"
            ),
        )
        == 0
    )


@pytest.mark.parametrize(
    ("checker", "path", "description", "body"),
    [
        (
            "check_search_index",
            "search/search_index.json",
            "search index scope",
            '{"docs":[{"location":"evals/methodology/"}]}',
        ),
        (
            "check_search_index",
            "search/search_index.json",
            "search index scope",
            '{"docs":[{"location":"dev/specs/2026-08-12-korvid-architecture/"},'
            '{"location":"evals/methodology/"}]}',
        ),
        (
            "check_search_index",
            "search/search_index.json",
            "search index scope",
            '{"docs":[{"location":"dev/"},'
            '{"location":"dev/specs/2026-08-12-korvid-architecture/"},'
            '{"location":"evals/methodology/"},{"location":"dev/contract-tests/"}]}',
        ),
        (
            "check_search_index",
            "search/search_index.json",
            "search index scope",
            '{"docs":[{"location":"dev/specs/2026-08-12-korvid-architecture/"},'
            '{"location":"evals/methodology/"},{"location":"dev/ui-controllers/"}]}',
        ),
        (
            "check_sitemap",
            "sitemap.xml",
            "sitemap scope",
            (f"<urlset><url><loc>{CANONICAL_SITE_URL}{PUBLIC_EVAL_PATH}</loc></url></urlset>"),
        ),
        (
            "check_sitemap",
            "sitemap.xml",
            "sitemap scope",
            (
                "<urlset>"
                f"<url><loc>{CANONICAL_SITE_URL}{PUBLIC_ARCHITECTURE_PATH}</loc></url>"
                f"<url><loc>{CANONICAL_SITE_URL}{PUBLIC_EVAL_PATH}</loc></url>"
                "</urlset>"
            ),
        ),
        (
            "check_sitemap",
            "sitemap.xml",
            "sitemap scope",
            (
                "<urlset>"
                f"<url><loc>{CANONICAL_SITE_URL}{PUBLIC_CONTRIBUTOR_PATH}</loc></url>"
                f"<url><loc>{CANONICAL_SITE_URL}{PUBLIC_ARCHITECTURE_PATH}</loc></url>"
                f"<url><loc>{CANONICAL_SITE_URL}{PUBLIC_EVAL_PATH}</loc></url>"
                f"<url><loc>{CANONICAL_SITE_URL}{INTERNAL_CONTRACT_TESTS_PATH}</loc></url>"
                "</urlset>"
            ),
        ),
        (
            "check_sitemap",
            "sitemap.xml",
            "sitemap scope",
            (
                "<urlset>"
                f"<url><loc>{CANONICAL_SITE_URL}{PUBLIC_ARCHITECTURE_PATH}</loc></url>"
                f"<url><loc>{CANONICAL_SITE_URL}{PUBLIC_EVAL_PATH}</loc></url>"
                f"<url><loc>{CANONICAL_SITE_URL}dev/ui-controllers/</loc></url>"
                "</urlset>"
            ),
        ),
    ],
)
def test_smoke_scope_checkers_fail_when_a_required_or_forbidden_entry_is_wrong(
    checker: str,
    path: str,
    description: str,
    body: str,
) -> None:
    assert _run_smoke_checker(checker, path, description, body) != 0


def test_smoke_job_is_main_only_after_deploy_and_least_privilege() -> None:
    """The post-deploy smoke job must stay isolated from build/deploy privileges."""

    config = _load()
    smoke = config["jobs"]["smoke"]

    condition = smoke.get("if", "")
    assert "github.event_name == 'push'" in condition
    assert "github.ref == 'refs/heads/main'" in condition

    needs = smoke["needs"]
    assert needs == "deploy" or needs == ["deploy"]

    assert smoke["runs-on"] == "ubuntu-latest"
    assert smoke["timeout-minutes"] >= 8
    assert smoke["permissions"] == {}

    step = _smoke_step(config)
    assert step.get("env") == {"SITE_URL": "${{ needs.deploy.outputs.page_url }}"}

    script = _smoke_script(config)
    assert "${{ needs.deploy.outputs.page_url }}" not in script
    assert 'if [ -z "${SITE_URL:-}" ]; then' in script
    assert 'base_url="${SITE_URL%/}"' in script
    assert "retry_until_contains()" in script
    assert "retry_until_body_checks()" in script
    assert "retry_until_ok()" in script
    assert 'if [ "$attempt" -lt "$CONTENT_ATTEMPTS" ]; then' in script
    assert 'if [ "$attempt" -lt "$MEDIA_ATTEMPTS" ]; then' in script
    assert 'sleep "$CONTENT_SLEEP_SECONDS"' in script
    assert 'sleep "$MEDIA_SLEEP_SECONDS"' in script
    assert "--connect-timeout 10" in script
    assert "CONTENT_CURL_MAX_TIME=10" in script
    assert '--max-time "$MEDIA_CURL_MAX_TIME"' in script

    content_budget = _shell_assignment(script, "CONTENT_ATTEMPTS") * _shell_assignment(
        script, "CONTENT_CURL_MAX_TIME"
    ) + (_shell_assignment(script, "CONTENT_ATTEMPTS") - 1) * _shell_assignment(
        script, "CONTENT_SLEEP_SECONDS"
    )
    media_budget = _shell_assignment(script, "MEDIA_ATTEMPTS") * _shell_assignment(
        script, "MEDIA_CURL_MAX_TIME"
    ) + (_shell_assignment(script, "MEDIA_ATTEMPTS") - 1) * _shell_assignment(
        script, "MEDIA_SLEEP_SECONDS"
    )
    content_probes = _smoke_invocation_count(
        script, "retry_until_contains"
    ) + _smoke_invocation_count(script, "retry_until_body_checks")
    media_probes = _smoke_invocation_count(script, "retry_until_ok")
    worst_case_seconds = content_probes * content_budget + media_probes * media_budget
    assert smoke["timeout-minutes"] * 60 > worst_case_seconds, (
        "smoke timeout must exceed the probe's bounded worst case"
    )


def test_smoke_job_anchors_the_getting_started_probe_to_a_build_safe_token() -> None:
    """The workflow must probe the HTML-safe token, while the docs keep the full command."""

    source = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert GETTING_STARTED_HOMEBREW_COMMAND in source
    assert GETTING_STARTED_HOMEBREW_TOKEN in source

    script = _smoke_script(_load())
    assert f'retry_until_contains "getting-started/" "{GETTING_STARTED_HOMEBREW_TOKEN}"' in script
    assert GETTING_STARTED_HOMEBREW_COMMAND not in script


def test_smoke_retry_helpers_retry_the_content_predicate_not_only_transport() -> None:
    """A stale CDN response must be retried until the expected body state appears."""

    script = _smoke_script(_load())
    contains_helper = re.search(
        r"retry_until_contains\(\)\s*\{(?P<body>.*?)^\}",
        script,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert contains_helper is not None, "smoke job must define retry_until_contains()"
    helper_body = contains_helper.group("body")
    assert 'body="$(fetch_body "$path" "$CONTENT_CURL_MAX_TIME")"' in helper_body
    assert 'grep -Fq "$expected" <<<"$body"' in helper_body
    assert 'if [ "$attempt" -lt "$CONTENT_ATTEMPTS" ]; then' in helper_body
    assert 'sleep "$CONTENT_SLEEP_SECONDS"' in helper_body

    body_checks_helper = re.search(
        r"retry_until_body_checks\(\)\s*\{(?P<body>.*?)^\}",
        script,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert body_checks_helper is not None, "smoke job must define retry_until_body_checks()"
    helper_body = body_checks_helper.group("body")
    assert '"$checker" "$body"' in helper_body
    assert 'if [ "$attempt" -lt "$CONTENT_ATTEMPTS" ]; then' in helper_body
    assert 'sleep "$CONTENT_SLEEP_SECONDS"' in helper_body


def test_smoke_job_checks_public_pages_search_and_media_entrypoints() -> None:
    """The smoke probe must cover release-facing pages, artifacts, and hero media URLs."""

    script = _smoke_script(_load())
    for path in (
        'retry_until_body_checks "" "home page structure" check_home_page',
        f'retry_until_contains "getting-started/" "{GETTING_STARTED_HOMEBREW_TOKEN}"',
        'retry_until_contains "release-notes/unreleased/" "Unreleased (main)"',
        'retry_until_body_checks "release-notes/unreleased/" "release-notes navigation" check_release_notes_nav',
        'retry_until_body_checks "search/search_index.json" "search index scope" check_search_index',
        'retry_until_body_checks "sitemap.xml" "sitemap scope" check_sitemap',
        'retry_until_ok "assets/demo.mp4"',
        'retry_until_ok "assets/scenes/agent-demo.mp4"',
        'retry_until_ok "assets/scenes/mcp-follow-demo.mp4"',
    ):
        assert path in script

    for assignment in (
        f'CANONICAL_SITE_URL="{CANONICAL_SITE_URL}"',
        f'PUBLIC_CONTRIBUTOR_PATH="{PUBLIC_CONTRIBUTOR_PATH}"',
        f'PUBLIC_ARCHITECTURE_PATH="{PUBLIC_ARCHITECTURE_PATH}"',
        f'PUBLIC_EVAL_PATH="{PUBLIC_EVAL_PATH}"',
        f'INTERNAL_RELEASE_PATH="{INTERNAL_RELEASE_PATH}"',
    ):
        assert assignment in script

    assert "search_locations()" in script
    assert "sitemap_urls()" in script
    assert "canonical_url_for()" in script
    assert "assert_exact_line_set()" in script
    assert "playwright" not in script.lower()
    assert "npm " not in script.lower()
    assert "node " not in script.lower()


def test_smoke_scope_paths_map_to_repository_sources() -> None:
    """Each smoke-checked public or internal route must still point at a real source page."""

    assert _site_page_source(PUBLIC_CONTRIBUTOR_PATH).exists()
    assert _site_page_source(PUBLIC_ARCHITECTURE_PATH).exists()
    assert _site_page_source(PUBLIC_EVAL_PATH).exists()


def test_workflow_level_permissions_are_read_only() -> None:
    config = _load()
    assert config.get("permissions") == {"contents": "read"}


def test_concurrency_never_lets_a_pull_request_discard_a_main_deployment() -> None:
    config = _load()
    concurrency = config.get("concurrency")
    assert isinstance(concurrency, dict), "docs workflow must configure concurrency"
    assert concurrency.get("group") == (
        "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.ref || 'pages' }}"
    ), (
        "pull requests need their own ref-scoped groups while pushes share the "
        "Pages deployment group"
    )
    assert concurrency.get("cancel-in-progress") == (
        "${{ github.event_name == 'pull_request' }}"
    ), "only superseded runs of the same pull request should be cancelled"


DEV_README = ROOT / "docs" / "dev" / "README.md"
DESIGN_DOC = ROOT / "docs" / "superpowers" / "specs" / "2026-08-21-documentation-site-design.md"
PLAN_DOC = ROOT / "docs" / "superpowers" / "plans" / "2026-08-21-official-documentation-site.md"
WORKFLOW_LINK = "https://github.com/hellices/korvid/blob/main/.github/workflows/docs.yml"
SITE_URL = "https://hellices.github.io/korvid/"


def _publishing_section() -> str:
    """Return the contributor docs' publishing section, or fail loudly."""
    text = DEV_README.read_text(encoding="utf-8")
    heading = "## Publishing the documentation site"
    assert heading in text, (
        "docs/dev/README.md must explain how the site reaches "
        f"{SITE_URL} — the workflow alone does not tell a maintainer that "
        "Pages must be switched to the GitHub Actions source once"
    )
    section = text.split(heading, 1)[1]
    return section.split("\n## ", 1)[0]


def test_contributor_docs_explain_how_the_site_is_published() -> None:
    """Publishing is a merge, not a deploy script — and it needs one repo setting."""
    section = _publishing_section()
    lowered = section.lower()
    # Markdown hard-wraps prose across source lines, so a substring check on
    # the raw text is one reflow away from splitting a phrase across a
    # newline (e.g. "do\nnot deploy"). Collapsing all whitespace runs
    # (including newlines) to a single space makes phrase checks robust to
    # how the paragraph happens to wrap.
    normalized = " ".join(lowered.split())
    assert "no server" in normalized or "no hosting" in normalized, (
        "the section must say that no server has to be run or provisioned"
    )
    assert "settings" in normalized, (
        "the one-time enablement path (Settings -> Pages) must be spelled out"
    )
    assert "pages" in normalized, (
        "the one-time enablement path (Settings -> Pages) must be spelled out"
    )
    assert "github actions" in normalized, (
        "Pages must be switched from the default branch source to the GitHub "
        "Actions source, or the workflow's deploy job fails"
    )
    assert SITE_URL in section, "the section must name the published URL"
    # "main" alone would trivially match the workflow link's "/blob/main/"
    # segment (checked elsewhere in this file) without the prose ever
    # stating that *merging* to main is the publishing trigger, so assert
    # the two appear together as a clause rather than as two independent
    # substrings.
    assert re.search(r"merg\w*[^.]*\bto\s+`?main`?\b", normalized), (
        "the section must state, in prose, that merging to `main` is what "
        "publishes the site — not merely contain the word 'main' via the "
        "workflow link"
    )
    assert "release-surface smoke" in normalized, (
        "the section must explain that a main push runs a bounded release-surface smoke"
    )
    assert re.search(r"push[^.]*\bmain\b[^.]*build[^.]*deploy[^.]*smoke", normalized), (
        "the section must say that a push to main builds, deploys, then runs the smoke check"
    )
    assert re.search(r"smoke fail\w*[^.]*after[^.]*pages[^.]*deploy", normalized), (
        "the section must say a smoke failure can happen after Pages was already deployed"
    )
    assert "pull request" in normalized, (
        "the section must state that pull-request builds validate but never deploy"
    )
    assert "not deploy" in normalized, (
        "the section must state that pull-request builds validate but never deploy"
    )
    assert "custom domain" in normalized, (
        "a custom domain is deliberately deferred; say so instead of leaving it open"
    )
    assert "optional" in normalized, (
        "a custom domain is deliberately deferred; say so instead of leaving it open"
    )
    for entry_point in ("readme", "pyproject.toml", "entry-point tests"):
        assert entry_point in normalized, (
            f"custom-domain migration must include the canonical {entry_point} contract"
        )


def test_publishing_section_links_the_workflow_in_a_strict_build_safe_way() -> None:
    """A repo-relative `../../.github/...` link would fail `mkdocs build --strict`.

    `docs/dev/README.md` is a built page, and MkDocs validates internal links
    against files inside `docs/`. The workflow lives outside the docs tree, so
    it must be linked absolutely on GitHub.
    """
    section = _publishing_section()
    assert WORKFLOW_LINK in section, (
        f"link the workflow as {WORKFLOW_LINK}; a docs-relative path to "
        ".github/workflows/docs.yml is not a documentation file and would break "
        "the strict build"
    )
    assert "](../../.github" not in section, (
        "a relative link outside docs/ fails MkDocs' internal-link validation"
    )
    assert "](.github" not in section, (
        "a relative link outside docs/ fails MkDocs' internal-link validation"
    )


def test_design_document_records_the_one_time_pages_enablement() -> None:
    """The committed design doc must not imply the workflow is sufficient alone."""
    design = " ".join(DESIGN_DOC.read_text(encoding="utf-8").lower().split())
    enablement = re.search(
        r"before the workflow can deploy for the first time[^.]*"
        r"enable pages once:[^.]*source: github actions",
        design,
    )
    assert enablement is not None, (
        "the design document's rollout section must record the exact one-time "
        "repository setting (Settings -> Pages -> Build and deployment -> "
        "Source: GitHub Actions); without it the deploy job fails on first run"
    )


def test_plan_records_the_one_time_pages_enablement_step() -> None:
    """The implementation plan must carry the same one-time enablement step."""
    plan = PLAN_DOC.read_text(encoding="utf-8").lower()
    assert "source: github actions" in plan, (
        "the plan's deployment task must include the one-time repository setting "
        "(Settings -> Pages -> Build and deployment -> Source: GitHub Actions), "
        "otherwise a clean run of the plan produces a workflow that cannot deploy"
    )


def test_plan_places_configure_pages_first_in_the_privileged_deploy_job() -> None:
    """The executable plan must reproduce the workflow's least-privilege ordering."""
    plan = " ".join(PLAN_DOC.read_text(encoding="utf-8").lower().replace("`", "").split())
    assert "configure-pages is the deploy job's first step" in plan, (
        "Task 3 must say configure-pages runs first in deploy, where pages: write "
        "exists; an action pin alone does not preserve that ordering"
    )


def test_cleanup_plan_uses_findall_for_sitemap_locations() -> None:
    """The committed cleanup plan must use the descendant loc lookup."""

    plan = CLEANUP_PLAN_DOC.read_text(encoding="utf-8")
    assert 'root.findall(".//{*}loc")' in plan
    assert 'root.iter("{*}loc")' not in plan


def test_cleanup_plan_task3_names_the_workflow_test_and_local_smoke_replay() -> None:
    task = CLEANUP_PLAN_DOC.read_text(encoding="utf-8").split(
        "### Task 3: Run the full documentation gate and prepare the pull request", 1
    )[1]
    normalized = " ".join(task.lower().replace("`", "").split())
    assert ".github/workflows/docs.yml" in task
    assert "tests/test_docs_workflow.py" in task
    assert "extract the smoke step" in normalized
    assert "site_url" in normalized


def test_cleanup_plan_uses_tolerant_mkdocs_loader_examples() -> None:
    plan = CLEANUP_PLAN_DOC.read_text(encoding="utf-8")
    assert 'yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))' not in plan
    assert plan.count("class _TolerantLoader(yaml.SafeLoader):") >= 2
    assert (
        plan.count(
            'yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_TolerantLoader)'
        )
        >= 2
    )


def test_plan_reproduces_the_ephemeral_direct_docs_build() -> None:
    """Following the executable plan must preserve the CI security and tool contract."""
    plan = PLAN_DOC.read_text(encoding="utf-8")
    step = plan.split("- [ ] **Step 2: Add the Pages workflow**", 1)[1]
    step = step.split("- [ ] **Step 3:", 1)[0]
    normalized = " ".join(step.lower().replace("`", "").split())
    assert "build job runs on ubuntu-latest" in normalized
    assert "uv version 0.10.9" in normalized
    assert "uv run --frozen --group docs mkdocs build --strict" in normalized
    assert "make docs-build" not in normalized


def test_design_describes_ephemeral_isolation_for_untrusted_pr_builds() -> None:
    design = " ".join(DESIGN_DOC.read_text(encoding="utf-8").lower().split())
    assert "ephemeral github-hosted runner" in design
    assert "pull-request-controlled" in design
    assert "never executes untrusted documentation code" not in design


def test_design_records_every_custom_domain_entry_point() -> None:
    design = " ".join(DESIGN_DOC.read_text(encoding="utf-8").lower().split())
    for entry_point in ("site_url", "readme", "pyproject.toml", "entry-point tests"):
        assert entry_point in design
