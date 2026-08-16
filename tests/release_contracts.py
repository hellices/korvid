from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


def markdown_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    before, separator, after = text.partition(marker)
    del before
    if not separator:
        raise AssertionError(f"missing markdown section: {heading}")
    return after.split("\n## ", 1)[0].strip()


def workflow_jobs(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text())
    jobs = document.get("jobs")
    if not isinstance(jobs, dict):
        raise AssertionError(f"{path} has no jobs mapping")
    return jobs


def run_scripts(job: Mapping[str, Any]) -> tuple[str, ...]:
    steps = job.get("steps", ())
    return tuple(str(step["run"]) for step in steps if isinstance(step, dict) and "run" in step)
