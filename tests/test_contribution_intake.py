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

from collections.abc import Sequence
from html.parser import HTMLParser
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from mdit_py_plugins.tasklists import tasklists_plugin

ROOT = Path(__file__).parents[1]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
TRIAGE_LABEL = "status: needs-triage"


#: The rule's load-bearing sentences, pinned whole and as a reader sees them
#: (inline code renders without its backticks): checking single words such
#: as "assigns" would accept a sentence that means something else ("a
#: maintainer assigns a reviewer").
CONTRIBUTING_RULE = (
    "Every change starts as an issue. If you do not have write access to this repository, "
    "open a pull request only for an issue that a maintainer has assigned to you.",
    "New issues from the forms get the status: needs-triage label until a maintainer "
    "accepts or closes them.",
    "A maintainer assigns the issue to you, one at a time until your first pull request is merged.",
    "If an assigned issue sees no pull request or update for 14 days, a maintainer may "
    "release it to someone else.",
    "Put Closes #N at the top of the description, where N is the assigned issue.",
    "A pull request from someone without write access that does not close an issue "
    "assigned to its author is closed without review, however good the change.",
)
FEATURE_FORM_RULE = (
    "A maintainer reviews each request. Wait until one accepts it and assigns it to you "
    "before opening a pull request."
)
PR_TEMPLATE_TASK = "This closes an issue that a maintainer assigned to me, or I have write access."

#: CommonMark with raw HTML on, as GitHub parses it, plus the GFM extensions
#: that change what a reader sees: tables, strikethrough, and task lists.
_MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"]).use(tasklists_plugin)
_HTML = RendererHTML()
#: The elements markdown syntax renders text into. Only text nodes count, so
#: comments, link titles, and unused reference definitions never do. Neither
#: does text inside any other element: raw HTML can collapse it (`<details>`)
#: or have GitHub drop it (`<script>`), and ~~struck~~ text reads as withdrawn.
_SHOWN = frozenset(
    {"a", "blockquote", "code", "em", "li", "ol", "p", "pre", "strong", "ul"}
    | {"h1", "h2", "h3", "h4", "h5", "h6"}
    | {"table", "tbody", "td", "th", "thead", "tr"}
)
_VOID = frozenset({"br", "hr", "img", "input"})


class _Reader(HTMLParser):
    """Collects the text nodes a reader sees in rendered markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in _VOID:
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if all(tag in _SHOWN for tag in self._open):
            self.parts.append(data)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _shown(html: str) -> str:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return "".join(reader.parts)


def _rendered(markdown: str) -> str:
    """The text GitHub shows a reader of `markdown`."""
    return _shown(_MARKDOWN.render(markdown))


def _inline(tokens: Sequence[Token]) -> str:
    return _flat(_shown(_HTML.renderInline(tokens, _MARKDOWN.options, {})))


def _headings(markdown: str, tag: str) -> list[str]:
    """The text a reader sees in each `tag` heading."""
    return [
        _inline(inline.children or [])
        for heading, inline in pairwise(_MARKDOWN.parse(markdown))
        if heading.type == "heading_open" and heading.tag == tag
    ]


def _tasks(markdown: str) -> list[tuple[bool, str]]:
    """Each markdown task list item: whether its box is ticked, and its text."""
    tokens = _MARKDOWN.parse(markdown)
    tasks = []
    for item, inline in zip(tokens, tokens[2:], strict=False):
        if item.type == "list_item_open" and "task-list-item" in str(item.attrGet("class")):
            box, *label = inline.children or []
            tasks.append(('checked="checked"' in box.content, _inline(label)))
    return tasks


def _flat(text: str) -> str:
    return " ".join(text.split())


def _form(name: str) -> dict[str, Any]:
    form = yaml.safe_load(_text(FORMS / name))
    assert isinstance(form, dict)
    return form


def test_rendered_text_keeps_what_a_reader_sees() -> None:
    shown = (
        "## A *heading*\n\n"
        "- [ ] a **task** with `code` and [a link](https://example.invalid)\n\n"
        "> quoted &amp; escaped\n\n"
        "| a | table |\n| --- | --- |\n| x | y |\n"
    )

    assert _flat(_rendered(shown)) == (
        "A heading a task with code and a link quoted & escaped a table x y"
    )


@pytest.mark.parametrize(
    "hidden",
    [
        "<!-- the rule -->",
        "<!--\nthe rule\n-->",
        '[unused]: https://example.invalid "the rule"',
        'a [link](https://example.invalid "the rule")',
        "<details>\n\nthe rule\n\n</details>",
        "<script>the rule</script>",
        "~~the rule~~",
    ],
    ids=[
        "comment",
        "long-comment",
        "reference-definition",
        "link-title",
        "collapsed",
        "script",
        "struck",
    ],
)
def test_rendered_text_drops_what_a_reader_does_not_see(hidden: str) -> None:
    text = _flat(_rendered(f"before\n\n{hidden}\n\nafter"))

    assert "the rule" not in text
    assert text.startswith("before ")
    assert text.endswith(" after")


def test_headings_and_tasks_come_from_markdown_a_reader_sees() -> None:
    markdown = (
        "## Shown <!-- note -->\n\n"
        "<!--\n## Hidden\n-->\n\n"
        "### Lower\n\n"
        "- [ ] open <!-- note -->\n"
        "- [x] ticked\n"
        "- not a task\n\n"
        "<ul><li>[ ] raw HTML</li></ul>\n"
    )

    assert _headings(markdown, "h2") == ["Shown"]
    assert _tasks(markdown) == [(False, "open"), (True, "ticked")]


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
    template = _text(PR_TEMPLATE)
    lines = [line for line in _rendered(template).splitlines() if line.strip()]

    assert lines[0] == "Closes #", "the template must ask for the issue before anything else"
    assert (False, PR_TEMPLATE_TASK) in _tasks(template), (
        "an unticked box must ask the author to confirm the assignment"
    )


def test_contributing_states_the_intake_rule() -> None:
    contributing = _text(CONTRIBUTING)
    assert "From issue to pull request" in _headings(contributing, "h2")
    text = _flat(_rendered(contributing))
    for sentence in CONTRIBUTING_RULE:
        assert sentence in text, f"CONTRIBUTING.md must state the intake rule: {sentence!r}"
