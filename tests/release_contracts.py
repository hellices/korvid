from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def _markdown_fence(line: str) -> tuple[str, int] | None:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped:
        return None
    marker = stripped[0]
    if marker not in {"`", "~"}:
        return None
    width = len(stripped) - len(stripped.lstrip(marker))
    if width < 3:
        return None
    return marker, width


def _level_two_heading(line: str) -> str | None:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped.startswith("## "):
        return None
    title = stripped[3:].strip()
    if not title:
        return None
    return title.rstrip("#").rstrip()


def _is_closing_markdown_fence(line: str, fence: tuple[str, int]) -> bool:
    fence_marker = _markdown_fence(line)
    if fence_marker is None or fence_marker[0] != fence[0]:
        return False
    if fence_marker[1] < fence[1]:
        return False
    stripped = line.lstrip(" ")
    suffix = stripped[fence_marker[1] :]
    return not suffix.strip()


def _markdown_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    fence: tuple[str, int] | None = None
    current_heading: str | None = None
    current_lines: list[str] = []

    for line in text.splitlines():
        if fence is not None:
            if current_heading is not None:
                current_lines.append(line)
            if _is_closing_markdown_fence(line, fence):
                fence = None
            continue

        title = _level_two_heading(line)
        if title is not None:
            if current_heading is not None:
                sections.append((current_heading, current_lines))
            current_heading = title
            current_lines = []
            continue

        fence = _markdown_fence(line)
        if current_heading is not None:
            current_lines.append(line)

    if current_heading is not None:
        sections.append((current_heading, current_lines))
    return sections


def markdown_section(text: str, heading: str) -> str:
    for title, section_lines in _markdown_sections(text):
        if title == heading:
            return "\n".join(section_lines).strip()
    raise AssertionError(f"missing markdown section: {heading}")


def workflow_jobs(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    if not isinstance(document, dict):
        raise AssertionError(f"{path} must contain a YAML mapping at the document root")
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        raise AssertionError(f"{path} has no jobs mapping")
    return jobs


def run_scripts(job: Mapping[str, Any]) -> tuple[str, ...]:
    steps = job.get("steps", ())
    return tuple(str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step)
