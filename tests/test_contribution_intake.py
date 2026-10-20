"""Contracts for how contributions are accepted.

Without a written intake rule, a pull request can arrive for work nobody
agreed to: #446 was opened for #445 before anyone assigned it, and it did not
link the issue. The rule is that every change starts as an issue, and every
pull request closes an issue in this repository that is assigned to its
author, maintainers included: maintainers accept and assign issues
themselves, and everyone else asks. These tests keep the issue forms, the
pull request template, and CONTRIBUTING.md saying the same thing.
"""

from __future__ import annotations

import hashlib
import re
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
#: SHA-256 of every file in .github/ISSUE_TEMPLATE, read with universal
#: newlines. A form shows more than its markdown items: labels, descriptions,
#: and options are text a reader sees too, and so is the chooser's
#: config.yml. Like the reviewed practice section, any edit or new file is a
#: deliberate change: re-check that each form still applies the triage label
#: and that nothing a reader sees qualifies the intake rule, then update this.
ISSUE_TEMPLATE_SHA256 = {
    "bug_report.yml": "7e9c3cacb7f80dec9756f6e507badae25c872d8a704e4282d2a7e6c90db43713",
    "config.yml": "8407efa53b745f1f8589006ac0b82391929294859bef5bb50c6c3254125f3a59",
    "feature_request.yml": "9246e136f5f45436e9bab5aae1e474c73b992c46afcd1ef00f740c40d38b3d60",
}
FORM_LABELS = {
    "bug_report.yml": ["bug", TRIAGE_LABEL],
    "feature_request.yml": ["enhancement", TRIAGE_LABEL],
}


#: The intake section of CONTRIBUTING.md as a reader sees it, block by block
#: (inline code renders without its backticks). It is pinned whole: a check
#: for single words, or even whole sentences, would accept a section that
#: keeps them but adds a clause or a block that retracts the rule.
INTAKE_HEADING = "From issue to pull request"
INTAKE_SECTION = (
    "Every change starts as an issue, and every pull request closes an issue in this "
    "repository that is assigned to its author. The rule is the same for maintainers: they "
    "accept and assign issues themselves, and everyone else asks. Review time goes to "
    "changes the project has agreed to make, and two people do not build the same thing.",
    "Find or open an issue. New issues from the forms get the status: needs-triage label "
    "until a maintainer accepts or closes them. Accepted work that is open to contributors "
    "is labelled help wanted or good first issue.",
    "Ask for it. Comment on the issue with a short plan.",
    "Wait to be assigned. A maintainer assigns the issue to you, one at a time until your "
    "first pull request is merged. If an assigned issue sees no pull request or update for "
    "14 days, a maintainer may release it to someone else.",
    "Open the pull request. Put Closes #N at the top of the description, where N is the "
    "assigned issue. Work that needs more than one pull request is split into sub-issues, "
    "one for each.",
    "A pull request that does not close an issue assigned to its author is closed without "
    "review, however good the change. Ask for the issue first; once a maintainer assigns it, "
    "the pull request can be reopened. The only exceptions are pull requests opened by "
    "Dependabot or the Relock workflow, and security fixes, which go through SECURITY.md "
    "instead of an issue.",
)
#: The feature form's only markdown item, pinned whole for the same reason.
FEATURE_FORM_NOTE = (
    "Start with the problem: the best design may differ from the first idea. A maintainer "
    "reviews each request. Wait until one accepts it and assigns it to you before opening "
    "a pull request."
)
#: The pull request template as a reader sees it, also pinned whole: the
#: issue it closes comes first, and every box starts unticked.
PR_TEMPLATE_HEAD = ("Closes #", "Summary", "Verification", "Checklist")
PR_TEMPLATE_TASKS = (
    "This closes an issue in this repository that is assigned to me.",
    "Tests cover the change, written to fail first where behavior changed.",
    "make check passes locally.",
    "User-facing docs and the README match the change, or nothing user-facing changed.",
    "No security invariant is weakened: writes still need a keystroke approval and a "
    "fail-closed audit entry, and sensitive reads are still masked.",
)

#: CommonMark with raw HTML on, as GitHub parses it, plus the GFM extensions
#: that change what a reader sees: tables, strikethrough, and task lists.
_MARKDOWN = MarkdownIt("commonmark").enable(["table", "strikethrough"]).use(tasklists_plugin)
_HTML = RendererHTML()
#: Raw HTML other than comments is refused. GitHub drops some of it with its
#: content (`<script>`), collapses some (`<details>`), and shows the rest, so
#: what a reader sees of it cannot be pinned. This parser leaves out the task
#: list plugin, whose checkboxes are HTML that markdown itself produces.
_PLAIN = MarkdownIt("commonmark").enable(["table", "strikethrough"])
#: Struck text reads as withdrawn, so it does not count as stating anything.
_STRUCK = frozenset({"del", "s", "strike"})
#: The elements that start a new block of text.
_BLOCKS = frozenset(
    {"blockquote", "li", "ol", "p", "pre", "td", "th", "tr", "ul"}
    | {"h1", "h2", "h3", "h4", "h5", "h6"}
)
_VOID = frozenset({"br", "hr", "img", "input"})


class _Reader(HTMLParser):
    """Collects the text a reader sees, block by block, with each block's element.

    Comments, link titles, and unused reference definitions never become text
    nodes, so they never count.
    """

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
        if not _STRUCK.intersection(self._open):
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


def _raw_html(markdown: str) -> list[str]:
    """The raw HTML in `markdown`, with comments taken out."""
    tokens = _PLAIN.parse(markdown)
    snippets = [token.content for token in tokens if token.type == "html_block"]
    snippets += [
        child.content
        for token in tokens
        for child in token.children or []
        if child.type == "html_inline"
    ]
    bare = (re.sub(r"<!--.*?-->", "", snippet, flags=re.DOTALL).strip() for snippet in snippets)
    return [snippet for snippet in bare if snippet]


def _visible(html: str) -> list[tuple[str, str]]:
    reader = _Reader()
    reader.feed(html)
    reader.close()
    return [(element, text) for element, parts in reader.blocks if (text := _flat("".join(parts)))]


def _render(markdown: str) -> list[tuple[str, str]]:
    raw = _raw_html(markdown)
    assert not raw, f"raw HTML other than comments: {raw}"
    return _visible(_MARKDOWN.render(markdown))


def _blocks(markdown: str) -> list[str]:
    """The blocks of text GitHub shows a reader of `markdown`, each on one line."""
    return [text for _, text in _render(markdown)]


def _section(markdown: str, heading: str) -> list[str]:
    """The blocks a reader sees under the h2 `heading`, up to the next h1 or h2."""
    blocks = _render(markdown)
    # Exactly once: a second section with the same heading would never be read.
    assert blocks.count(("h2", heading)) == 1, f"expected exactly one visible h2 {heading!r}"
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
        "~~the rule~~",
    ],
    ids=["comment", "long-comment", "reference-definition", "link-title", "struck"],
)
def test_rendered_text_drops_what_a_reader_does_not_see(hidden: str) -> None:
    assert _blocks(f"before\n\n{hidden}\n\nafter") == ["before", "after"]


@pytest.mark.parametrize(
    "raw",
    [
        "<details>\n\nthe rule\n\n</details>",
        "<script>the rule</script>",
        "<div>the rule</div>",
        "text <span>the rule</span>",
        "<!-- a comment --><div>the rule</div>",
    ],
    ids=["collapsed", "dropped", "shown-block", "shown-inline", "after-a-comment"],
)
def test_raw_html_other_than_comments_is_refused(raw: str) -> None:
    with pytest.raises(AssertionError, match="raw HTML other than comments"):
        _blocks(f"before\n\n{raw}\n\nafter")


def test_sections_and_tasks_come_from_markdown_a_reader_sees() -> None:
    markdown = (
        "## Shown <!-- note -->\n\n"
        "body\n\n"
        "<!--\n## Hidden\n-->\n\n"
        "### Lower\n\n"
        "- [ ] open <!-- note -->\n"
        "- [x] ticked\n"
        "- not a task\n\n"
        "## Next\n\n"
        "after\n"
    )

    assert _section(markdown, "Shown") == ["body", "Lower", "open", "ticked", "not a task"]
    assert _section(markdown, "Next") == ["after"]
    with pytest.raises(AssertionError, match="exactly one visible h2"):
        _section(markdown, "Hidden")
    with pytest.raises(AssertionError, match="exactly one visible h2"):
        _section(markdown + "\n## Shown\n\nagain\n", "Shown")
    assert _tasks(markdown) == [(False, "open"), (True, "ticked")]


def test_every_issue_form_waits_for_triage() -> None:
    # Pinned exactly, for every form: a form that also applied `help wanted`
    # or `good first issue` would mark work nobody has accepted as open to
    # contributors, and a new form would skip triage unless it is listed here.
    forms = sorted(path.name for path in FORMS.glob("*.yml") if path.name != "config.yml")

    assert forms == sorted(FORM_LABELS), "every issue form needs its labels pinned"
    for name, labels in FORM_LABELS.items():
        assert _form(name)["labels"] == labels, name


def test_the_issue_templates_are_the_reviewed_text() -> None:
    digests = {
        path.name: hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        for path in FORMS.iterdir()
    }

    assert digests == ISSUE_TEMPLATE_SHA256, (
        "an issue template changed or was added: re-check that every form applies the "
        "triage label and that nothing a reader sees qualifies the intake rule, then "
        "update ISSUE_TEMPLATE_SHA256"
    )


def test_the_feature_form_says_work_waits_for_an_assignment() -> None:
    # Every markdown item is pinned, in order: the note has to be one of them,
    # whole and visible, and no other item may qualify it.
    notes = [
        _blocks(item["attributes"]["value"])
        for item in _form("feature_request.yml")["body"]
        if item["type"] == "markdown"
    ]

    assert notes == [[FEATURE_FORM_NOTE]]


def test_the_pull_request_template_starts_with_the_assigned_issue() -> None:
    template = _text(PR_TEMPLATE)

    assert _blocks(template) == [*PR_TEMPLATE_HEAD, *PR_TEMPLATE_TASKS]
    assert _tasks(template) == [(False, task) for task in PR_TEMPLATE_TASKS]


def test_contributing_states_the_intake_rule() -> None:
    assert _section(_text(CONTRIBUTING), INTAKE_HEADING) == list(INTAKE_SECTION)
