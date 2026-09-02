"""Behavior checks for the landing page's JavaScript controllers."""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
JS_TESTS = ROOT / "tests" / "js"


class _StartTagCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.tags.append((tag, dict(attrs)))


def _run_harness(name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["node", str(JS_TESTS / name)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_switcher_behavior() -> None:
    result = _run_harness("scene_switcher_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout


def test_landing_markup_connects_scene_controls_to_fallback_content() -> None:
    parser = _StartTagCollector()
    parser.feed((ROOT / "docs" / "index.md").read_text())

    switchers = [attrs for _, attrs in parser.tags if "data-scene-switcher" in attrs]
    tabs = [attrs for _, attrs in parser.tags if attrs.get("role") == "tab"]
    panels = [attrs for _, attrs in parser.tags if attrs.get("role") == "tabpanel"]
    controlled_panels = {tab["aria-controls"]: tab["id"] for tab in tabs}
    labelled_panels = {panel["id"]: panel["aria-labelledby"] for panel in panels}
    videos = [attrs for tag, attrs in parser.tags if tag == "video"]
    fallbacks = [
        attrs
        for tag, attrs in parser.tags
        if tag == "img" and "scene-panel__fallback" in (attrs.get("class") or "").split()
    ]

    assert len(switchers) == 1
    assert controlled_panels == labelled_panels
    assert len(videos) == len(panels) == len(fallbacks)
    assert all("controls" in video for video in videos)
    assert all((fallback.get("alt") or "").strip() for fallback in fallbacks)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_fallback_behavior() -> None:
    result = _run_harness("scene_fallback_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout
