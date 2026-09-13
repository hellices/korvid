from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent

yaml.SafeLoader.add_multi_constructor(
    "tag:yaml.org,2002:python/name:",
    lambda loader, suffix, node: suffix,
)


def test_stable_install_precedes_development_source() -> None:
    source = (ROOT / "docs" / "getting-started.md").read_text(encoding="utf-8")
    stable = source.index("## Install")
    development = source.index("## Development build")
    git_source = source.index("git+https://github.com/hellices/korvid")
    assert stable < development < git_source


def test_unreleased_page_identifies_main_and_stable_release() -> None:
    source = (ROOT / "docs" / "release-notes" / "unreleased.md").read_text(encoding="utf-8")
    opening = source.split("## ", 1)[0]
    assert "`main`" in opening
    assert "https://github.com/hellices/korvid/releases/latest" in opening
    assert "https://github.com/hellices/korvid/milestone/6" in opening


def test_release_navigation_labels_unreleased_main() -> None:
    config = yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    release_notes = config["nav"][-1]["Project"][-1]["Release notes"]
    assert release_notes[0] == {
        "Unreleased (main)": "release-notes/unreleased.md"
    }
