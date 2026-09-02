"""Behavior checks for the landing page's JavaScript controllers."""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
JS_TESTS = ROOT / "tests" / "js"


class _SceneMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.switchers = 0
        self.tabs: dict[str, str] = {}
        self.panels: dict[str, str] = {}
        self.video_panels: set[str] = set()
        self.fallback_panels: set[str] = set()
        self._inside_switcher = False
        self._current_panel: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "section" and "data-scene-switcher" in attributes:
            self.switchers += 1
            self._inside_switcher = True
        if not self._inside_switcher:
            return
        if attributes.get("role") == "tab":
            self.tabs[attributes["aria-controls"]] = attributes["id"]
        if attributes.get("role") == "tabpanel":
            self._current_panel = attributes["id"]
            self.panels[attributes["id"]] = attributes["aria-labelledby"]
        if self._current_panel is None:
            return
        if tag == "video" and "controls" in attributes:
            self.video_panels.add(self._current_panel)
        if (
            tag == "img"
            and "scene-panel__fallback" in (attributes.get("class") or "").split()
            and (attributes.get("alt") or "").strip()
        ):
            self.fallback_panels.add(self._current_panel)

    def handle_endtag(self, tag: str) -> None:
        if tag == "article":
            self._current_panel = None
        if tag == "section" and self._inside_switcher:
            self._inside_switcher = False


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
    parser = _SceneMarkupParser()
    parser.feed((ROOT / "docs" / "index.md").read_text())

    assert parser.switchers == 1
    assert parser.tabs
    assert parser.tabs == parser.panels
    assert set(parser.panels) == parser.video_panels == parser.fallback_panels


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_fallback_behavior() -> None:
    result = _run_harness("scene_fallback_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout
