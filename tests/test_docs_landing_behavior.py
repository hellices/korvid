"""Behavior checks for the landing page's JavaScript controllers."""

from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import NoReturn

import pytest

ROOT = Path(__file__).parent.parent
JS_TESTS = ROOT / "tests" / "js"
_DIAGNOSTIC_LIMIT = 4096


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
            panel_id = attributes.get("aria-controls")
            tab_id = attributes.get("id")
            if panel_id is not None and tab_id is not None:
                self.tabs[panel_id] = tab_id
        if attributes.get("role") == "tabpanel":
            self._current_panel = attributes.get("id")
            tab_id = attributes.get("aria-labelledby")
            if self._current_panel is not None and tab_id is not None:
                self.panels[self._current_panel] = tab_id
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


def _bounded_timeout_output(output: bytes | str | None) -> str:
    if output is None:
        return "<none>"
    text = output.decode(errors="replace") if isinstance(output, bytes) else output
    if len(text) <= _DIAGNOSTIC_LIMIT:
        return text
    head = _DIAGNOSTIC_LIMIT // 2
    tail = _DIAGNOSTIC_LIMIT - head
    omitted = len(text) - _DIAGNOSTIC_LIMIT
    return f"{text[:head]}\n... <{omitted} characters truncated from middle> ...\n{text[-tail:]}"


def _run_harness(name: str) -> subprocess.CompletedProcess[str]:
    found = shutil.which("node")
    if found is None:
        raise RuntimeError("node is not installed")
    node = str(Path(found).resolve())
    try:
        return subprocess.run(
            [node, str(JS_TESTS / name)],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        error.add_note(
            "\n".join(
                (
                    f"Node harness: {name}",
                    f"Node executable: {node}",
                    "Node stdin: subprocess.DEVNULL (noninteractive harness)",
                    f"Captured stdout:\n{_bounded_timeout_output(error.stdout)}",
                    f"Captured stderr:\n{_bounded_timeout_output(error.stderr)}",
                )
            )
        )
        raise


def test_harness_timeout_preserves_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved = str(ROOT / "node-diagnostic.exe")
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def timeout(*args: object, **kwargs: object) -> NoReturn:
        calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(
            [resolved, str(JS_TESTS / "scene_fallback_harness.mjs")],
            10,
            output=(
                b"stdout-start\n"
                + b"x" * 10_000
                + b"stdout-middle"
                + b"x" * 10_000
                + b"stdout-tail"
            ),
            stderr=(
                b"stderr-start\n"
                + b"y" * 10_000
                + b"stderr-middle"
                + b"y" * 10_000
                + b"stderr-tail"
            ),
        )

    monkeypatch.setattr(shutil, "which", lambda executable: resolved)
    monkeypatch.setattr(subprocess, "run", timeout)

    with pytest.raises(subprocess.TimeoutExpired, match="timed out") as raised:
        _run_harness("scene_fallback_harness.mjs")

    command = calls[0][0][0]
    options = calls[0][1]
    assert command == [resolved, str(JS_TESTS / "scene_fallback_harness.mjs")]
    assert options["timeout"] == 10
    assert options["stdin"] is subprocess.DEVNULL
    note = "\n".join(raised.value.__notes__)
    assert f"Node executable: {resolved}" in note
    assert "Node stdin: subprocess.DEVNULL" in note
    assert "stdout-start" in note
    assert "stderr-start" in note
    assert "stdout-tail" in note
    assert "stderr-tail" in note
    assert "stdout-middle" not in note
    assert "stderr-middle" not in note
    assert "truncated" in note
    assert _bounded_timeout_output(None) == "<none>"
    assert _bounded_timeout_output("complete string") == "complete string"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_scene_switcher_behavior() -> None:
    result = _run_harness("scene_switcher_harness.mjs")

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "not ok" not in result.stdout
    assert "scene-switcher stage=complete" in result.stderr


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
    assert "scene-fallback stage=complete" in result.stderr
