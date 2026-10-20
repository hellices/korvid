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

from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
import yaml
from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from mdit_py_plugins.tasklists import tasklists_plugin

ROOT = Path(__file__).parents[1]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"
TRIAGE_LABEL = "status: needs-triage"


#: The intake section of CONTRIBUTING.md as a reader sees it, block by block
#: (inline code renders without its backticks). It is pinned whole: a check
#: for single words, or even whole sentences, would accept a section that
#: keeps them but adds a clause or a block that retracts the rule.
INTAKE_HEADING = "From issue to pull request"
INTAKE_SECTION = (
    "Every change starts as an issue. If you do not have write access to this repository, "
    "open a pull request only for an issue that a maintainer has assigned to you. Review "
    "time goes to changes the project has agreed to make, and two people do not build the "
    "same thing.",
    "Find or open an issue. New issues from the forms get the status: needs-triage label "
    "until a maintainer accepts or closes them. Accepted work that is open to contributors "
    "is labelled help wanted or good first issue.",
    "Ask for it. Comment on the issue with a short plan.",
    "Wait to be assigned. A maintainer assigns the issue to you, one at a time until your "
    "first pull request is merged. If an assigned issue sees no pull request or update for "
    "14 days, a maintainer may release it to someone else.",
    "Open the pull request. Put Closes #N at the top of the description, where N is the "
    "assigned issue.",
    "A pull request from someone without write access that does not close an issue "
    "assigned to its author is closed without review, however good the change. Ask for the "
    "issue first; once a maintainer assigns it, the pull request can be reopened. Security "
    "fixes go through SECURITY.md instead of an issue.",
)
#: The feature form's note, pinned whole for the same reason.
FEATURE_FORM_NOTE = (
    "Start with the problem: the best design may differ from the first idea. A maintainer "
    "reviews each request. Wait until one accepts it and assigns it to you before opening "
    "a pull request."
)
PR_TEMPLATE_TASK = "This closes an issue that a maintainer assigned to me, or I have write access."

#: CommonMark with raw HTML on, as GitHub parses it, plus the GFM extensions
#: that change what a reader sees: tables, strikethrough, and task lists.
_MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"]).use(tasklists_plugin)
_HTML = RendererHTML()
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
#: The elements markdown syntax renders text into. Only text nodes count, so
#: comments, link titles, and unused reference definitions never do. Neither
#: does text inside any other element: raw HTML can collapse it (`<details>`)
#: or have GitHub drop it (`<script>`), and ~~struck~~ text reads as withdrawn.
_SHOWN = frozenset(
    {"a", "blockquote", "code", "em", "li", "ol", "p", "pre", "strong", "ul"}
    | _HEADINGS
    | {"table", "tbody", "td", "th", "thead", "tr"}
)
#: The elements that start a new block of text.
_BLOCKS = frozenset({"blockquote", "li", "ol", "p", "pre", "td", "th", "tr", "ul"} | _HEADINGS)
_VOID = frozenset({"br", "hr", "img", "input"})


class _Reader(HTMLParser):
    """Collects the text a reader sees, block by block, with each block's element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple[str, list[str]]] = [("", [])]
        self._open: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._break(tag, tag)
        if tag not in _VOID:
            self._open.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            del self._open[len(self._open) - 1 - self._open[::-1].index(tag) :]
        self._break(tag, "")

    def handle_data(self, data: str) -> None:
        if all(tag in _SHOWN for tag in self._open):
            self.blocks[-1][1].append(data)

    def _break(self, tag: str, element: str) -> None:
        if tag not in _BLOCKS:
            return
        if "".join(self.blocks[-1][1]).strip():
            self.blocks.append((element, []))
        else:
            self.blocks[-1] = (element, [])


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _visible(html: str) -> list[tuple[str, str]]:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return [(element, text) for element, parts in reader.blocks if (text := _flat("".join(parts)))]


def _blocks(markdown: str) -> list[str]:
    """The blocks of text GitHub shows a reader of `markdown`, each on one line."""
    return [text for _, text in _visible(_MARKDOWN.render(markdown))]


def _section(markdown: str, heading: str) -> list[str]:
    """The blocks a reader sees under the h2 `heading`, up to the next h1 or h2."""
    blocks = _visible(_MARKDOWN.render(markdown))
    assert ("h2", heading) in blocks, f"no visible h2 {heading!r}"
    start = blocks.index(("h2", heading)) + 1
    ends = [index for index, (element, _) in enumerate(blocks) if element in {"h1", "h2"}]
    end = next((index for index in ends if index >= start), len(blocks))
    return [text for _, text in blocks[start:end]]


def _tasks(markdown: str) -> list[tuple[bool, str]]:
    """Each markdown task list item: whether its box is ticked, and its text."""
    tokens = _MARKDOWN.parse(markdown)
    tasks = []
    for item, inline in zip(tokens, tokens[2:], strict=False):
        if item.type == "list_item_open" and "task-list-item" in str(item.attrGet("class")):
            box, *label = inline.children or []
            html = _HTML.renderInline(label, _MARKDOWN.options, {})
            tasks.append(
                ('checked="checked"' in box.content, " ".join(t for _, t in _visible(html)))
            )
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

    assert _blocks(shown) == [
        "A heading",
        "a task with code and a link",
        "quoted & escaped",
        "a",
        "table",
        "x",
        "y",
    ]


@pytest.mark.parametrize(
    "hidden",
    [
        "<!-- the rule -->",
        "<!--\nthe rule\n-->",
        '[unused]: https://example.invalid "the rule"',
        '[](https://example.invalid "the rule")',
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
    assert _blocks(f"before\n\n{hidden}\n\nafter") == ["before", "after"]


def test_sections_and_tasks_come_from_markdown_a_reader_sees() -> None:
    markdown = (
        "## Shown <!-- note -->\n\n"
        "body\n\n"
        "<!--\n## Hidden\n-->\n\n"
        "<details>\n\n## Collapsed\n\n</details>\n\n"
        "### Lower\n\n"
        "- [ ] open <!-- note -->\n"
        "- [x] ticked\n"
        "- not a task\n\n"
        "<ul><li>[ ] raw HTML</li></ul>\n\n"
        "## Next\n\n"
        "after\n"
    )

    assert _section(markdown, "Shown") == [
        "body",
        "Lower",
        "open",
        "ticked",
        "not a task",
        "[ ] raw HTML",
    ]
    assert _section(markdown, "Next") == ["after"]
    for hidden in ("Hidden", "Collapsed"):
        with pytest.raises(AssertionError, match="no visible h2"):
            _section(markdown, hidden)
    assert _tasks(markdown) == [(False, "open"), (True, "ticked")]


def test_every_issue_form_waits_for_triage() -> None:
    for name in ("bug_report.yml", "feature_request.yml"):
        labels = _form(name)["labels"]
        assert isinstance(labels, list), f"{name}: issue forms take a list of labels"
        assert TRIAGE_LABEL in labels, name


def test_the_feature_form_says_work_waits_for_an_assignment() -> None:
    # Each markdown item renders on its own, so the note has to be one of
    # them, whole and visible, not straddle a field between two.
    notes = [
        _blocks(item["attributes"]["value"])
        for item in _form("feature_request.yml")["body"]
        if item["type"] == "markdown"
    ]

    assert [FEATURE_FORM_NOTE] in notes


def test_the_pull_request_template_starts_with_the_assigned_issue() -> None:
    template = _text(PR_TEMPLATE)

    assert _blocks(template)[0] == "Closes #", (
        "the template must ask for the issue before anything else"
    )
    assert (False, PR_TEMPLATE_TASK) in _tasks(template), (
        "an unticked box must ask the author to confirm the assignment"
    )


def test_contributing_states_the_intake_rule() -> None:
    assert _section(_text(CONTRIBUTING), INTAKE_HEADING) == list(INTAKE_SECTION)
