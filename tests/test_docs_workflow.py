"""Structural invariants for the least-privilege GitHub Pages docs workflow.

`.github/workflows/docs.yml` builds the MkDocs site on every pull request and
push to `main`, but only *deploys* to GitHub Pages from `main`. Deployment
needs `pages: write` and `id-token: write`; the build step that just runs
`make docs-build` needs neither, so the two must live in separate jobs with
separate, minimal permission blocks (`zizmor` flags a job holding
Pages-deploy permissions it never uses as excessive).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "docs.yml"

PATH_FILTERS = {
    "docs/**",
    "mkdocs.yml",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    ".github/workflows/docs.yml",
}

PINNED_ACTIONS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "c771a70e6277c0a99b617c7a806ffedaca235ff9",
    "actions/configure-pages": "983d7736d9b0ae728b81ab479565c72886d7745b",
    "actions/upload-pages-artifact": "7b1f4a764d45c48632c6b24a0339c27f5614fb0b",
    "actions/deploy-pages": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
}


def _load() -> dict[str, Any]:
    assert WORKFLOW.exists(), f"{WORKFLOW} must exist"
    text = WORKFLOW.read_text()
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
    assert build["permissions"] == {"contents": "read"}

    run_steps = [step["run"] for step in build["steps"] if "run" in step]
    assert any("uv sync --locked" in run and "--group docs" in run for run in run_steps), (
        "build job must run 'uv sync --locked --group docs'"
    )
    assert any(run.strip() == "make docs-build" for run in run_steps), (
        "build job must run 'make docs-build'"
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
    assert len(deploy_steps) == 1
    assert deploy_steps[0]["uses"].startswith("actions/deploy-pages@")
    assert deploy_steps[0]["id"] == "deployment"


def test_workflow_level_permissions_are_read_only() -> None:
    config = _load()
    assert config.get("permissions") == {"contents": "read"}


DEV_README = ROOT / "docs" / "dev" / "README.md"
DESIGN_DOC = ROOT / "docs" / "superpowers" / "specs" / "2026-08-21-documentation-site-design.md"
PLAN_DOC = ROOT / "docs" / "superpowers" / "plans" / "2026-08-21-official-documentation-site.md"
WORKFLOW_LINK = "https://github.com/hellices/korvid/blob/main/.github/workflows/docs.yml"
SITE_URL = "https://hellices.github.io/korvid/"


def _publishing_section() -> str:
    """Return the contributor docs' publishing section, or fail loudly."""
    text = DEV_README.read_text()
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
    assert "no server" in lowered or "no hosting" in lowered, (
        "the section must say that no server has to be run or provisioned"
    )
    assert "settings" in lowered, (
        "the one-time enablement path (Settings -> Pages) must be spelled out"
    )
    assert "pages" in lowered, (
        "the one-time enablement path (Settings -> Pages) must be spelled out"
    )
    assert "github actions" in lowered, (
        "Pages must be switched from the default branch source to the GitHub "
        "Actions source, or the workflow's deploy job fails"
    )
    assert SITE_URL in section, "the section must name the published URL"
    assert "main" in lowered, "the section must state that merging to main is what publishes"
    assert "merg" in lowered, "the section must state that merging to main is what publishes"
    assert "pull request" in lowered, (
        "the section must state that pull-request builds validate but never deploy"
    )
    assert "not deploy" in lowered, (
        "the section must state that pull-request builds validate but never deploy"
    )
    assert "custom domain" in lowered, (
        "a custom domain is deliberately deferred; say so instead of leaving it open"
    )
    assert "optional" in lowered, (
        "a custom domain is deliberately deferred; say so instead of leaving it open"
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
    design = DESIGN_DOC.read_text().lower()
    assert "source: github actions" in design, (
        "the design document's rollout section must record the exact one-time "
        "repository setting (Settings -> Pages -> Build and deployment -> "
        "Source: GitHub Actions); without it the deploy job fails on first run"
    )
    assert "once" in design, "the setting is a one-time admin action; say so"


def test_plan_records_the_one_time_pages_enablement_step() -> None:
    """The implementation plan must carry the same one-time enablement step."""
    plan = PLAN_DOC.read_text().lower()
    assert "source: github actions" in plan, (
        "the plan's deployment task must include the one-time repository setting "
        "(Settings -> Pages -> Build and deployment -> Source: GitHub Actions), "
        "otherwise a clean run of the plan produces a workflow that cannot deploy"
    )
