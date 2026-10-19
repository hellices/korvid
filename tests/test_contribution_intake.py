"""Contracts for how outside contributions are accepted.

Without a written intake rule, a pull request can arrive for work nobody
agreed to: #446 was opened for #445 before anyone assigned it, and it did not
link the issue. The rule is that every change starts as an issue, a
maintainer accepts it, and someone without write access opens a pull request
only for an issue a maintainer assigned to them, linked with `Closes #N`.
These tests keep the issue forms, the pull request template, and
CONTRIBUTING.md saying the same thing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
TRIAGE_LABEL = "status: needs-triage"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(path: Path) -> str:
    return " ".join(_text(path).split())


def _form(name: str) -> dict[str, Any]:
    form = yaml.safe_load(_text(FORMS / name))
    assert isinstance(form, dict)
    return form


def test_every_issue_form_waits_for_triage() -> None:
    for name in ("bug_report.yml", "feature_request.yml"):
        assert TRIAGE_LABEL in _form(name)["labels"], name


def test_the_feature_form_says_work_waits_for_an_assignment() -> None:
    notes = " ".join(
        " ".join(item["attributes"]["value"].split())
        for item in _form("feature_request.yml")["body"]
        if item["type"] == "markdown"
    )

    assert "assigns" in notes
    assert "before opening a pull request" in notes


def test_the_pull_request_template_starts_with_the_assigned_issue() -> None:
    lines = [
        line
        for line in _text(PR_TEMPLATE).splitlines()
        if line.strip() and not line.lstrip().startswith("<!--")
    ]

    assert lines[0] == "Closes #", "the template must ask for the issue before anything else"
    assert "a maintainer assigned to me" in _flat(PR_TEMPLATE)


def test_contributing_states_the_intake_rule() -> None:
    assert "\n## From issue to pull request\n" in _text(CONTRIBUTING)
    text = _flat(CONTRIBUTING)
    for phrase in (
        TRIAGE_LABEL,
        "without write access",
        "assigns",
        "Closes #",
        "closed without review",
        "14 days",
    ):
        assert phrase in text, f"CONTRIBUTING.md must state the intake rule: {phrase!r}"
