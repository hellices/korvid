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
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parent.parent
MATERIAL_BUNDLE = ROOT / "docs" / "assets" / "javascripts" / "bundle.d7400e89.min.js"
MERMAID_VENDOR = ROOT / "docs" / "assets" / "javascripts" / "vendor" / "mermaid-11.17.0.min.js"
RESIZE_OBSERVER_VENDOR = (
    ROOT / "docs" / "assets" / "javascripts" / "vendor" / "resize-observer-polyfill-1.5.1.js"
)
VISUAL_STORYTELLING = ROOT / "docs" / "assets" / "javascripts" / "visual-storytelling.js"


def test_makefile_docs_build_uses_frozen() -> None:
    """docs-build target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    # Find the docs-build recipe line
    assert "uv run --frozen --group docs mkdocs build" in makefile, (
        "docs-build must use 'uv run --frozen --group docs mkdocs build --strict'"
    )


def test_makefile_docs_serve_uses_frozen() -> None:
    """docs-serve target must pass --frozen to prevent lock rewrites."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "uv run --frozen --group docs mkdocs serve" in makefile, (
        "docs-serve must use 'uv run --frozen --group docs mkdocs serve'"
    )


def test_docs_dependency_matches_the_vendored_material_bundle() -> None:
    """The hashed bundle override is specific to Material 9.7.7."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["dependency-groups"]["docs"] == ["mkdocs-material==9.7.7"]


def test_gitignore_excludes_site_dir() -> None:
    """/site/ must be in .gitignore to prevent committing generated output."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/site/" in gitignore, (
        "/site/ must be listed in .gitignore so 'make docs-build' output cannot be committed"
    )


def test_gitignore_excludes_material_privacy_cache() -> None:
    """Build-localized third-party assets must never become untracked changes."""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/.cache/plugin/privacy/" in gitignore


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

    add_multi_constructor = cast(
        "Callable[[str, Callable[[Any, str, Any], object]], None]",
        _TolerantLoader.add_multi_constructor,
    )
    add_multi_constructor("tag:yaml.org,2002:python/name:", lambda loader, suffix, node: suffix)
    config = yaml.load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"), Loader=_TolerantLoader)
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


def test_core_concept_pages_each_have_their_selected_visual_evidence() -> None:
    expected = {
        "overview.md": ("```mermaid", "KORVID — product boundary"),
        "tui.md": ('class="docs-visual docs-visual--annotated"', "cockpit-poster.png"),
        "agent.md": ('class="docs-storyboard"', "agent-poster.png"),
        "mcp.md": ("```mermaid", "External MCP client"),
        "ops.md": ("```mermaid", "Audit append"),
        "resource-relationships.md": (
            'class="docs-visual"',
            "relationship-graph.png",
        ),
    }
    for relative, markers in expected.items():
        source = (ROOT / "docs" / relative).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in source, f"{relative} must contain {marker!r}"


def test_tui_annotation_pins_match_the_poster_layout() -> None:
    """`tui.md`'s pins must land on what their captions claim.

    Measured against the 1280x720 `cockpit-poster.png` this branch ships
    (a settled, populated post-navigation frame):

    * the effective key-hint row occupies y 8-25px (~1-3%);
    * the selected `CrashLoopBackOff` row occupies y 119-137px (~17-19%);
    * the `ctx:(current)  ns:shop` status row occupies y 695-710px (~97%),
      with `ns:shop` starting near x 157px (~12%).

    The pins are ordered 1, 2, 3 in the source, matching the ordered list in
    the figcaption. An earlier revision pointed pin 3 at 8% (the first pod
    row) and pin 1 at 92% (~40px above the status row) and asserted those
    offsets as fact, so the test actively defended the mismatch.
    """
    source = (ROOT / "docs" / "tui.md").read_text(encoding="utf-8")
    expected = {
        1: ("12%", "97%", "the context/namespace status row"),
        2: ("50%", "18%", "the selected, populated resource row"),
        3: ("50%", "3%", "the effective key-hint row"),
    }
    for number, (x, y, target) in expected.items():
        pin = (
            f'<span class="docs-visual__pin" style="--x: {x}; --y: {y};" '
            f'aria-hidden="true">{number}</span>'
        )
        assert pin in source, f"pin {number} must point at {target} (--x: {x}; --y: {y})"
    positions = [source.index(f'aria-hidden="true">{number}</span>') for number in (1, 2, 3)]
    assert positions == sorted(positions), (
        "the pins must stay in the order their figcaption list explains them"
    )


def test_getting_started_matches_current_project_and_readme_release() -> None:
    """The offline release sources must agree on every intentionally pinned install."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")

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


def test_getting_started_matches_the_python_installation_contract() -> None:
    """The guide must not invent an upper bound absent from Requires-Python."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["requires-python"] == ">=3.11"
    getting_started = " ".join(
        (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8").lower().split()
    )
    assert "python 3.11 or newer" in getting_started
    assert "ci currently qualifies 3.11, 3.12, and 3.13" in getting_started


def test_getting_started_cannot_call_homebrew_unpublished_when_readme_installs_it() -> None:
    """README's live brew route and the guide's Homebrew section cannot contradict."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
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
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]
    index = (ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    assert f"uv tool install 'korvid[all]=={project_version}'" in index

    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
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
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    all_extra = pyproject["project"]["optional-dependencies"]["all"]
    assert all_extra == ["korvid[agent,mcp,observability]"]

    project_version = pyproject["project"]["version"]
    needle = f"`korvid[all]=={project_version}`"
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    row = next((line for line in getting_started.splitlines() if needle in line), None)
    assert row is not None, f"getting-started.md must contain an install-table row for {needle}"
    for component in ("agent", "mcp", "observability"):
        assert component in row.lower(), (
            f"the `all` install row must name its {component} component"
        )


def test_getting_started_lists_observability_surface_combinations() -> None:
    """Observability is installed with the agent or MCP surface that exposes it."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = pyproject["project"]["version"]
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    for extras in ("agent,observability", "mcp,observability"):
        assert f"`korvid[{extras}]=={project_version}`" in getting_started


def test_getting_started_limits_the_helm_cli_requirement_to_writes() -> None:
    """Cluster-backed Helm browsing works without the local Helm executable."""
    getting_started = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
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


def test_mkdocs_disables_remote_fonts_and_external_asset_fetches() -> None:
    """Builds and visitors must not fetch executable assets from third parties."""
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
    assert privacy.get("assets_fetch", True) is False


def test_material_bundle_uses_checksum_pinned_local_vendor_assets() -> None:
    """The Material bundle and both dynamically loaded scripts are immutable."""
    assert MATERIAL_BUNDLE.is_file(), (
        "docs must override Material's bundle with the reviewed URL-pinned copy"
    )
    bundle = MATERIAL_BUNDLE.read_bytes()
    assert b'new URL("assets/javascripts/vendor/mermaid-11.17.0.min.js",__md_scope).href' in bundle
    assert (
        b'new URL("assets/javascripts/vendor/resize-observer-polyfill-1.5.1.js",'
        b"__md_scope).href" in bundle
    )
    assert b"/korvid/assets/javascripts/vendor/" not in bundle
    assert b"https://unpkg.com/" not in bundle
    assert hashlib.sha256(bundle).hexdigest() == (
        "1c6de1ec928cbec390682d3ba17e617828eb391d8e0f9a5b718e31617b824b2c"
    ), "the reviewed Material bundle override must not drift without an explicit update"
    assert hashlib.sha256(MERMAID_VENDOR.read_bytes()).hexdigest() == (
        "8d8e0eec56d3a83b4b3c87f42050845546dee93ebe1875d2117c12e6947c0cb3"
    )
    assert hashlib.sha256(RESIZE_OBSERVER_VENDOR.read_bytes()).hexdigest() == (
        "2290b5c60e0cdc62851fb687800237273ac53797595b1b133860c4f1386de378"
    )


def test_material_bundle_checkout_preserves_reviewed_bytes() -> None:
    """Git must not rewrite the checksum-pinned JavaScript to CRLF on Windows."""
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "docs/assets/javascripts/bundle.d7400e89.min.js -text" in attributes
    assert "docs/assets/javascripts/vendor/*.js -text" in attributes


def test_mkdocs_loads_only_the_reviewed_local_storytelling_script() -> None:
    config = _load_mkdocs_config()
    assert config.get("extra_javascript") == ["assets/javascripts/visual-storytelling.js"]
    assert VISUAL_STORYTELLING.is_file()
    script = VISUAL_STORYTELLING.read_bytes()
    assert hashlib.sha256(script).hexdigest() == (
        "524d71a64240a5aa167e7c99f2a7bfea307d79017ed079c69aabd40746946480"
    )
    assert b"\r" not in script, (
        "the reviewed bytes are LF-only; a CRLF checkout would break the pin above"
    )


def test_storytelling_script_checkout_preserves_reviewed_bytes() -> None:
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "docs/assets/javascripts/visual-storytelling.js text eol=lf" in attributes


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
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    nav_text = repr(config.get("nav"))
    expected = f"release-notes/v{version}.md"
    assert expected in nav_text, f"mkdocs nav must include the current release notes: {expected}"


def test_release_notes_navigation_matches_every_release_file() -> None:
    """A new release note must never be silently omitted from site navigation."""
    config = _load_mkdocs_config()
    nav = config.get("nav")
    assert isinstance(nav, list)
    project = next(item["Project"] for item in nav if isinstance(item, dict) and "Project" in item)
    release_notes = next(
        item["Release notes"]
        for item in project
        if isinstance(item, dict) and "Release notes" in item
    )
    configured = {
        path for item in release_notes if isinstance(item, dict) for path in item.values()
    }
    sources = {
        path.relative_to(ROOT / "docs").as_posix()
        for path in (ROOT / "docs" / "release-notes").glob("*.md")
    }
    assert configured == sources


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


def test_mkdocs_excludes_repository_only_history_from_site_search() -> None:
    """Historical executable plans stay in Git, not in public product search."""
    config = _load_mkdocs_config()
    excluded = config.get("exclude_docs")
    assert isinstance(excluded, str)
    excluded_paths = {line.strip().rstrip("/") for line in excluded.splitlines() if line.strip()}
    assert "dev/plans" in excluded_paths
    assert "superpowers" in excluded_paths
