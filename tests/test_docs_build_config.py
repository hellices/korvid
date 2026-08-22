"""Docs build configuration invariants.

Ensures that `make docs-build` and `make docs-serve` use `--frozen` so that
running them behind a corporate mirror cannot silently rewrite uv.lock to
private-index URLs.

Also ensures /site/ is listed in .gitignore so generated MkDocs output cannot
be accidentally committed.

Finally, ensures `mkdocs.yml` never downgrades `validation.links.not_found`
below its strict-mode-visible default (which would let genuinely broken
internal doc links pass `mkdocs build --strict` silently), and that
`docs/getting-started.md` follows the repository's current release and
Homebrew guidance without reaching out to PyPI or the tap during tests.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parent.parent
MATERIAL_BUNDLE = ROOT / "docs" / "assets" / "javascripts" / "bundle.d7400e89.min.js"


def test_makefile_docs_build_uses_frozen() -> None:
    """docs-build target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text()
    # Find the docs-build recipe line
    assert "uv run --frozen --group docs mkdocs build" in makefile, (
        "docs-build must use 'uv run --frozen --group docs mkdocs build --strict'"
    )


def test_makefile_docs_serve_uses_frozen() -> None:
    """docs-serve target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text()
    assert "uv run --frozen --group docs mkdocs serve" in makefile, (
        "docs-serve must use 'uv run --frozen --group docs mkdocs serve'"
    )


def test_gitignore_excludes_site_dir() -> None:
    """/site/ must be in .gitignore to prevent committing generated output."""
    gitignore = (ROOT / ".gitignore").read_text()
    assert "/site/" in gitignore, (
        "/site/ must be listed in .gitignore so 'make docs-build' output cannot be committed"
    )


def _load_mkdocs_config() -> dict[str, Any]:
    """Parse `mkdocs.yml`, tolerating its `!!python/name:...` custom tag.

    `SafeLoader` refuses arbitrary Python-object tags. This config only uses
    one (`pymdownx.superfences`'s code-format callable), which the tests
    below don't need to resolve, so map it to an inert placeholder string.

    Returns:
        The parsed mapping.
    """

    class _TolerantLoader(yaml.SafeLoader):
        pass

    # PyYAML's stubs leave `add_multi_constructor` unannotated, so mypy
    # --strict rejects the call rather than the code.
    _TolerantLoader.add_multi_constructor(  # type: ignore[no-untyped-call]  # untyped in types-PyYAML
        "tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix
    )
    config = yaml.load((ROOT / "mkdocs.yml").read_text(), Loader=_TolerantLoader)
    assert isinstance(config, dict), "mkdocs.yml must parse to a mapping"
    return config


def test_mkdocs_config_does_not_downgrade_link_validation() -> None:
    """`validation.links.not_found` must not be lowered below the strict-mode default.

    MkDocs's own default for `not_found` is `warn`, which `strict: true`
    escalates to a build failure. Downgrading it (e.g. to `info`) would let
    genuinely broken internal doc links pass `make docs-build` silently.
    """
    config = _load_mkdocs_config()
    not_found = (config.get("validation") or {}).get("links", {}).get("not_found")
    assert not_found in (None, "warn", "warning", "error"), (
        "mkdocs.yml must not downgrade validation.links.not_found below its "
        f"strict-mode-visible default; found {not_found!r}"
    )


def test_getting_started_matches_current_project_and_readme_release() -> None:
    """The offline release sources must agree on every intentionally pinned install."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project_version = pyproject["project"]["version"]
    readme = (ROOT / "README.md").read_text()
    getting_started = (ROOT / "docs" / "getting-started.md").read_text()

    readme_install = re.search(
        r"uv tool install 'korvid\[all\]==([^']+)'",
        readme,
    )
    assert readme_install is not None, "README must expose a pinned full install"
    assert readme_install.group(1) == project_version, (
        "README's primary pinned install must match pyproject.toml's project.version"
    )

    published_match = re.search(
        r"## Current release\s*\n\s*\*\*`([^`]+)`\*\* is the current published release",
        getting_started,
    )
    assert published_match is not None, "getting-started.md must name the current published release"
    assert published_match.group(1) == project_version, (
        f"getting-started.md advertises {published_match.group(1)!r} as current, "
        f"but pyproject.toml's project.version is {project_version!r}"
    )

    pinned_versions = set(re.findall(r"\bkorvid(?:\[[^\]]+\])?==(\d+\.\d+\.\d+)", getting_started))
    assert pinned_versions == {project_version}, (
        "every intentionally pinned getting-started command must use the current "
        f"project release {project_version}; found {sorted(pinned_versions)}"
    )
    assert f"release-notes/v{project_version}.md" in getting_started
    assert "awaiting publication" not in getting_started.lower()


def test_getting_started_cannot_call_homebrew_unpublished_when_readme_installs_it() -> None:
    """README's live brew route and the guide's Homebrew section cannot contradict."""
    readme = (ROOT / "README.md").read_text()
    getting_started = (ROOT / "docs" / "getting-started.md").read_text()
    command = "brew install hellices/korvid/korvid"
    assert command in readme, "README is the offline repository source for the brew route"
    assert command in getting_started

    section = getting_started.split("### Homebrew (macOS and Linux)", 1)[1]
    section = section.split("\n### ", 1)[0]
    normalized = " ".join(section.lower().split())
    for stale_claim in (
        "tap is unpublished",
        "tap is unavailable",
        "once the homebrew tap is published",
        "awaiting publication",
    ):
        assert stale_claim not in normalized, (
            f"getting-started.md exposes the README's working brew command but still says "
            f"{stale_claim!r}"
        )


def test_current_release_is_primary_when_the_homebrew_formula_lags() -> None:
    """The site must not present an older tap formula as the current release."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project_version = pyproject["project"]["version"]
    index = (ROOT / "docs" / "index.md").read_text()
    assert f"uv tool install 'korvid[all]=={project_version}'" in index

    getting_started = (ROOT / "docs" / "getting-started.md").read_text()
    section = getting_started.split("### Homebrew (macOS and Linux)", 1)[1]
    section = section.split("\n### ", 1)[0]
    normalized = " ".join(section.lower().replace("`", "").replace("*", "").split())
    packaged = re.search(r"tap currently packages \**(\d+\.\d+\.\d+)\**", normalized)
    assert packaged is not None, "the guide must state which release the external tap packages"
    if packaged.group(1) != project_version:
        assert f"current {project_version}" in normalized
        assert "uv tool" in normalized
        assert "pipx" in normalized


def test_getting_started_describes_the_all_extra_completely() -> None:
    """The canonical install table must include every component in the `all` extra."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    all_extra = pyproject["project"]["optional-dependencies"]["all"]
    assert all_extra == ["korvid[agent,mcp,observability]"]

    project_version = pyproject["project"]["version"]
    needle = f"`korvid[all]=={project_version}`"
    getting_started = (ROOT / "docs" / "getting-started.md").read_text()
    row = next((line for line in getting_started.splitlines() if needle in line), None)
    assert row is not None, f"getting-started.md must contain an install-table row for {needle}"
    for component in ("agent", "mcp", "observability"):
        assert component in row.lower(), (
            f"the `all` install row must name its {component} component"
        )


def test_getting_started_limits_the_helm_cli_requirement_to_writes() -> None:
    """Cluster-backed Helm browsing works without the local Helm executable."""
    getting_started = (ROOT / "docs" / "getting-started.md").read_text()
    requirements = getting_started.split("## Requirements", 1)[1].split("\n## ", 1)[0]
    requirements = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", requirements)
    normalized = " ".join(requirements.lower().split())
    assert "helm and operator views do not require the helm cli" in normalized
    assert "helm write actions require" in normalized


def _plugin_options(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return a configured plugin's options, using `{}` for its short form."""
    plugins = config.get("plugins")
    assert isinstance(plugins, list), "mkdocs.yml must declare its plugins as a list"
    for plugin in plugins:
        if plugin == name:
            return {}
        if isinstance(plugin, dict) and name in plugin:
            options = plugin[name]
            assert isinstance(options, dict), f"{name} plugin options must be a mapping"
            return options
    return None


def test_mkdocs_disables_remote_fonts_and_localizes_external_assets() -> None:
    """Visitors must not fetch Google fonts or Mermaid from third-party hosts."""
    config = _load_mkdocs_config()
    theme = config.get("theme")
    assert isinstance(theme, dict), "mkdocs.yml must configure a theme"
    assert theme.get("font") is False, (
        "theme.font must be false so Material does not emit fonts.googleapis.com "
        "or fonts.gstatic.com runtime requests"
    )
    privacy = _plugin_options(config, "privacy")
    assert privacy is not None, (
        "Material's privacy plugin must download external Mermaid assets at build "
        "time and serve them locally"
    )
    assert privacy.get("assets", True) is True
    assert privacy.get("assets_fetch", True) is True
    expressions = privacy.get("assets_expr_map")
    assert isinstance(expressions, dict), (
        "privacy.assets_expr_map must cover Material's extensionless unpkg "
        "ResizeObserver fallback as well as its .js Mermaid URL"
    )
    javascript_expression = expressions.get(".js")
    assert isinstance(javascript_expression, str)
    resize_url = "https://unpkg.com/resize-observer-polyfill"
    match = re.search(javascript_expression, f'"{resize_url}"')
    assert match is not None, (
        "the privacy plugin's JavaScript expression must localize Material's "
        "extensionless ResizeObserver fallback instead of leaving an executable "
        "unpkg runtime URL in the built bundle"
    )
    assert match.group("url") == resize_url


def test_material_bundle_pins_the_resize_observer_fallback() -> None:
    """The localized fallback must be immutable before the privacy plugin fetches it."""
    assert MATERIAL_BUNDLE.is_file(), (
        "docs must override Material's bundle with the reviewed URL-pinned copy"
    )
    bundle = MATERIAL_BUNDLE.read_bytes()
    assert b"https://unpkg.com/resize-observer-polyfill@1.5.1/dist/ResizeObserver.js" in bundle
    assert b"https://unpkg.com/mermaid@11.17.0/dist/mermaid.min.js" in bundle
    assert b'"https://unpkg.com/resize-observer-polyfill"' not in bundle
    assert b"https://unpkg.com/mermaid@11/dist/mermaid.min.js" not in bundle
    assert hashlib.sha256(bundle).hexdigest() == (
        "72f6ab94668b5cebcf2dfaf0517d9e412ee3c117ce180db9b44bb77a7504eb9c"
    ), "the reviewed Material bundle override must not drift without an explicit update"


def test_mkdocs_excludes_override_sources_but_keeps_theme_customization() -> None:
    """Jinja sources stay out of site/ while remaining available to Material."""
    config = _load_mkdocs_config()
    excluded = config.get("exclude_docs")
    assert isinstance(excluded, str), "mkdocs.yml must exclude docs/overrides from output"
    excluded_paths = {line.strip().rstrip("/") for line in excluded.splitlines() if line.strip()}
    assert "overrides" in excluded_paths, (
        "docs/overrides must be excluded or MkDocs copies the raw Jinja sources to site/overrides/"
    )

    theme = config.get("theme")
    assert isinstance(theme, dict)
    assert theme.get("custom_dir") == "docs/overrides"
    assert (ROOT / "docs" / "overrides" / "home.html").is_file()
    assert (ROOT / "docs" / "overrides" / "partials" / "copyright.html").is_file()


def test_current_release_notes_are_in_navigation() -> None:
    """The current project release must be discoverable in the site navigation."""
    config = _load_mkdocs_config()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    nav_text = repr(config.get("nav"))
    expected = f"release-notes/v{version}.md"
    assert expected in nav_text, f"mkdocs nav must include the current release notes: {expected}"


def test_theme_custom_dir_resolves_to_the_overrides_directory() -> None:
    """`theme.custom_dir` must point at the directory that holds korvid's partials.

    The korvid footer is a `partials/copyright.html` override. MkDocs only
    picks it up when `theme.custom_dir` resolves — relative to `mkdocs.yml` —
    to the directory containing it. A renamed or mistyped `custom_dir` builds
    cleanly and silently restores the bare Material footer, so the wiring is
    pinned here rather than left to a human noticing the footer changed.
    """
    config = _load_mkdocs_config()
    theme = config.get("theme")
    assert isinstance(theme, dict), "mkdocs.yml must configure a theme"
    custom_dir = theme.get("custom_dir")
    assert isinstance(custom_dir, str), (
        "theme.custom_dir must be set, or the korvid template overrides are ignored"
    )
    assert custom_dir, "theme.custom_dir must be set, or the korvid template overrides are ignored"

    # MkDocs resolves `custom_dir` relative to the config file's directory.
    resolved = (ROOT / custom_dir).resolve()
    assert resolved.is_dir(), f"theme.custom_dir points at {custom_dir!r}, which does not exist"
    assert (resolved / "partials" / "copyright.html").is_file(), (
        f"{custom_dir}/partials/copyright.html must exist: it is the override that "
        "replaces Material's default footer with korvid's"
    )
    assert (resolved / "home.html").is_file(), (
        f"{custom_dir}/home.html must exist: docs/index.md selects it via `template:`"
    )
