"""The published wheel must ship code only — never the documentation site.

Task 3 added a full MkDocs site (`docs/`, `mkdocs.yml`, `docs/stylesheets/`,
`docs/overrides/`, `docs/assets/`) and a `docs` dependency group. None of it
belongs in the artifact a user installs with `pip install korvid`: shipping
it would inflate the wheel, and declaring `mkdocs-material` as a runtime
requirement would drag a static-site generator into every cluster operator's
environment.

The invariants below are asserted against `pyproject.toml`, which is what a
future edit would actually regress — hatchling derives both the wheel's
member list and its `METADATA` from it. They run offline and in
milliseconds, unlike a real build.

The end-to-end proof (run once per acceptance pass, and reproducible by
anyone) is:

```bash
# UV_FROZEN=1 is mandatory: a bare `uv build` re-resolves and rewrites
# uv.lock, which behind a corporate mirror pins every artefact URL to a
# private index (see AGENTS.md).
UV_FROZEN=1 uv build --wheel --out-dir .wheel-proof
uv run --frozen python - <<'PY'
import pathlib, zipfile
wheel = next(pathlib.Path(".wheel-proof").glob("*.whl"))
with zipfile.ZipFile(wheel) as zf:
    names = zf.namelist()
    print("stray:", [n for n in names if not n.startswith(("korvid/", "korvid-"))])
    meta = zf.read(next(n for n in names if n.endswith(".dist-info/METADATA"))).decode()
print("mkdocs:", [line for line in meta.splitlines() if "mkdocs" in line.lower()])
PY
rm -rf .wheel-proof
```

Expected: `stray: []` and `mkdocs: []`.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Anything whose presence in the wheel would mean the docs site leaked in.
DOCS_ONLY_TOKENS = ("docs", "site", "mkdocs", "stylesheets", "overrides", ".css", ".svg", ".gif")

# Hatch keys that can add files to a wheel beyond `packages`.
FILE_INJECTING_KEYS = ("force-include", "include", "only-include", "artifacts", "shared-data")


def _pyproject() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def _wheel_target() -> dict[str, object]:
    hatch = _pyproject().get("tool", {})
    assert isinstance(hatch, dict)
    build = hatch.get("hatch", {})
    assert isinstance(build, dict)
    targets = build.get("build", {})
    assert isinstance(targets, dict)
    wheel = targets.get("targets", {})
    assert isinstance(wheel, dict)
    target = wheel.get("wheel", {})
    assert isinstance(target, dict)
    return target


def test_wheel_ships_only_the_korvid_package() -> None:
    """`packages` must be exactly the import package — no docs directory."""
    assert _wheel_target().get("packages") == ["src/korvid"], (
        "the wheel must contain only src/korvid; adding another path here would "
        "publish the documentation site inside the installable artifact"
    )


def test_wheel_target_does_not_force_extra_files_into_the_artifact() -> None:
    """No hatch key may inject docs, site output, CSS, SVG, or mkdocs.yml."""
    target = _wheel_target()
    for key in FILE_INJECTING_KEYS:
        value = target.get(key)
        if value is None:
            continue
        rendered = str(value).lower()
        for token in DOCS_ONLY_TOKENS:
            assert token not in rendered, (
                f"[tool.hatch.build.targets.wheel].{key} = {value!r} would pull "
                f"{token!r} into the wheel; the wheel ships code only"
            )


def test_mkdocs_is_never_a_runtime_or_extra_requirement() -> None:
    """`METADATA` must carry no MkDocs `Requires-Dist`.

    hatchling writes `project.dependencies` and every
    `project.optional-dependencies` entry into the wheel's `METADATA` as
    `Requires-Dist`. `[dependency-groups]` are PEP 735 development groups and
    are *not* written there — which is exactly why the docs toolchain lives
    in one.
    """
    project = _pyproject()["project"]
    assert isinstance(project, dict)

    runtime = list(project.get("dependencies", []))
    extras = project.get("optional-dependencies", {})
    assert isinstance(extras, dict)
    for extra_requirements in extras.values():
        runtime.extend(extra_requirements)

    for requirement in runtime:
        assert "mkdocs" not in requirement.lower(), (
            f"{requirement!r} would appear in the wheel's METADATA as a Requires-Dist; "
            "the documentation toolchain belongs in [dependency-groups].docs"
        )


def test_docs_toolchain_lives_in_a_dependency_group() -> None:
    """The docs group must exist, so nothing is tempted to move it to an extra."""
    groups = _pyproject().get("dependency-groups", {})
    assert isinstance(groups, dict)
    docs = groups.get("docs")
    assert docs, "[dependency-groups].docs must hold the MkDocs toolchain"
    assert any("mkdocs" in requirement.lower() for requirement in docs)
