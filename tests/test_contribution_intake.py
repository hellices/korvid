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

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
TRIAGE_LABEL = "status: needs-triage"


#: The rule's load-bearing sentences, pinned whole: checking single words such
#: as "assigns" would accept a sentence that means something else ("a
#: maintainer assigns a reviewer").
CONTRIBUTING_RULE = (
    "Every change starts as an issue. If you do not have write access to this repository, "
    "open a pull request only for an issue that a maintainer has assigned to you.",
    "New issues from the forms get the `status: needs-triage` label until a maintainer "
    "accepts or closes them.",
    "A maintainer assigns the issue to you, one at a time until your first pull request is merged.",
    "If an assigned issue sees no pull request or update for 14 days, a maintainer may "
    "release it to someone else.",
    "Put `Closes #N` at the top of the description, where `N` is the assigned issue.",
    "A pull request from someone without write access that does not close an issue "
    "assigned to its author is closed without review, however good the change.",
)
FEATURE_FORM_RULE = (
    "A maintainer reviews each request. Wait until one accepts it and assigns it to you "
    "before opening a pull request."
)
PR_TEMPLATE_CHECK = (
    "- [ ] This closes an issue that a maintainer assigned to me, or I have write access."
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _rendered(markdown: str) -> str:
    """The text GitHub shows a reader: HTML comments of any length are invisible."""
    return re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)


def _flat(text: str) -> str:
    return " ".join(text.split())


def _form(name: str) -> dict[str, Any]:
    form = yaml.safe_load(_text(FORMS / name))
    assert isinstance(form, dict)
    return form


def test_rendered_text_drops_single_and_multi_line_comments() -> None:
    hidden = "keep <!-- gone --> this\n<!--\nalso gone\n-->\nand this"

    assert _flat(_rendered(hidden)) == "keep this and this"


def test_every_issue_form_waits_for_triage() -> None:
    for name in ("bug_report.yml", "feature_request.yml"):
        labels = _form(name)["labels"]
        assert isinstance(labels, list), f"{name}: issue forms take a list of labels"
        assert TRIAGE_LABEL in labels, name


def test_the_feature_form_says_work_waits_for_an_assignment() -> None:
    # Each markdown item renders as its own block, so the rule has to sit
    # whole and visible inside one of them, not straddle a field between two.
    notes = [
        _flat(_rendered(item["attributes"]["value"]))
        for item in _form("feature_request.yml")["body"]
        if item["type"] == "markdown"
    ]

    assert any(FEATURE_FORM_RULE in note for note in notes)


def test_the_pull_request_template_starts_with_the_assigned_issue() -> None:
    lines = [line for line in _rendered(_text(PR_TEMPLATE)).splitlines() if line.strip()]

    assert lines[0] == "Closes #", "the template must ask for the issue before anything else"
    assert PR_TEMPLATE_CHECK in lines


def test_contributing_states_the_intake_rule() -> None:
    rendered = _rendered(_text(CONTRIBUTING))
    assert "\n## From issue to pull request\n" in rendered
    text = _flat(rendered)
    for sentence in CONTRIBUTING_RULE:
        assert sentence in text, f"CONTRIBUTING.md must state the intake rule: {sentence!r}"
