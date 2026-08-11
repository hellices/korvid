"""Evidence references an answer may cite (issue #192).

A diagnostic answer is only checkable if its claims point at the reads that
produced them. The references are minted here, by korvid, for exactly that
reason: if the provider could invent `[E3]`, an unsupported claim could be
made to look sourced, and the citation would be decoration rather than a
handle on the evidence.

The ledger is deliberately small and pure. It knows which reads happened
this turn, what each one looked at, and a bounded excerpt of what came
back. It does not know how citations are rendered, and it never edits the
model's text - `check_citations` reports what is supported and what is not,
leaving the caller to show both.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

#: `[E12]` and nothing else. `[E1x]`, `[E01]` and `[E]` are not citations
#: at all, rather than citations that fail to resolve - a malformed
#: reference is a formatting bug, and reporting it as "unsupported" would
#: blame the claim for it. ASCII digits explicitly: `\d` also matches
#: other Unicode decimal digits, so `[E1٢]` would otherwise be reported as
#: an unknown reference rather than as malformed.
_CITATION = re.compile(r"\[E([1-9][0-9]*)\]")

#: How each read names its target, mapped to the kind it implies. The
#: built-in reads disagree - `get_logs` and `diagnose_pod` take `pod`,
#: `diagnose_pvc` takes `pvc`, `diagnose_service` takes `service` - and a
#: locator that only understood `name` pointed those citations at nothing.
#: Stated as a table rather than inferred, so adding a read with a new
#: target argument is a decision someone makes, not a silent omission;
#: `test_the_locator_covers_every_registered_cluster_read` fails until it
#: is made.
TARGET_ARGUMENTS: dict[str, str] = {
    "pod": "pods",
    "pvc": "persistentvolumeclaims",
    "service": "services",
}

#: Excerpts ride in the prompt on every later step of the turn, so they
#: are capped: the issue requires the small-profile budget to survive the
#: addition of citation metadata.
_DEFAULT_EXCERPT_LIMIT = 240


@dataclasses.dataclass(frozen=True)
class Evidence:
    """One cluster read a claim may cite.

    Carries what someone checking the claim needs: which tool produced it,
    what it looked at, and enough of the result to recognise it. The UI
    slice of #192 will navigate from these fields; nothing here depends on
    that having happened yet.

    The locator is normalised across tools that name the same thing
    differently - `get_logs` takes `pod` where `get_resource` takes
    `kind` + `name` - because a citation that cannot identify its exact
    source is not navigable, which is the point of having one.

    It identifies the object by name, not by incarnation. `get_events`
    scopes its read to the live object's UID, so a pod deleted and
    recreated under the same name between the read and the citation would
    be opened as though it were the cited evidence. Fixing that needs the
    UID to travel out of the executor on the tool result, which is a
    change to the `tools/` contract rather than something this layer can
    recover - tracked separately.
    """

    ref: str
    tool: str
    kind: str | None
    namespace: str | None
    name: str | None
    container: str | None
    excerpt: str


class EvidenceLedger:
    """The reads of one agent turn, addressable by reference.

    Scoped to a turn on purpose: a citation must resolve to evidence
    fetched now, not to a stale read from an earlier question whose
    resource may since have changed.
    """

    def __init__(self, *, excerpt_limit: int = _DEFAULT_EXCERPT_LIMIT) -> None:
        if excerpt_limit < 1:
            # A zero limit cannot hold even the truncation marker, so the
            # bound this class advertises would be false.
            raise ValueError("excerpt_limit must be at least 1")
        self._excerpt_limit = excerpt_limit
        self._items: dict[str, Evidence] = {}

    def start_turn(self) -> None:
        """Drop the previous turn's evidence and restart numbering."""
        self._items.clear()

    def record(
        self,
        tool: str,
        arguments: dict[str, Any],
        result: str,
        *,
        error: bool = False,
    ) -> str | None:
        """Mint a reference for a successful read, or None for a failure.

        A failed read is not evidence. Handing it a reference would let a
        gap be cited as support, which is the opposite of what the
        references are for - gaps are reported as gaps.

        Args:
            tool: the read that produced the result. Required: a reference
                with no source could not be navigated to.
            arguments: the call's arguments; namespace and name are kept
                so the citation identifies a target, not just a tool.
            result: the model-visible text.
            error: whether the producer classified this as a failure.

        Raises:
            ValueError: if `tool` is empty.
        """
        if not tool.strip():
            raise ValueError("evidence needs a tool name to be navigable")
        if error:
            return None
        ref = f"E{len(self._items) + 1}"
        kind, name = _locate(arguments)
        self._items[ref] = Evidence(
            ref=ref,
            tool=tool,
            kind=kind,
            namespace=_text_arg(arguments, "namespace"),
            name=name,
            container=_text_arg(arguments, "container"),
            excerpt=_excerpt(result, self._excerpt_limit),
        )
        return ref

    def resolve(self, ref: str) -> Evidence | None:
        """The evidence behind a reference, or None if it is not ours."""
        return self._items.get(ref)

    def references(self) -> tuple[str, ...]:
        """Every reference minted this turn, in the order they were read."""
        return tuple(self._items)

    def check_citations(
        self, text: str
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Split the citations in *text* into supported, unknown, repeated.

        Reports rather than rewrites: the caller shows all three, so an
        unsupported citation is visible instead of being silently deleted
        along with the claim it was attached to.

        Each reference appears once per list, in first-mention order, so
        the caller can point at where the problem is. Repeats are listed
        separately rather than dropped - citing the same read twice is not
        extra support, and the issue asks for duplicate references to
        degrade visibly rather than to look like a single clean citation.

        Only *supported* references can repeat. An unknown reference cited
        twice is unsupported, full stop; reporting it as repeated as well
        would put two conflicting notes about one reference on screen.
        """
        supported: list[str] = []
        unknown: list[str] = []
        repeated: list[str] = []
        for match in _CITATION.finditer(text):
            ref = f"E{match.group(1)}"
            if ref not in self._items:
                if ref not in unknown:
                    unknown.append(ref)
                continue
            if ref in supported:
                if ref not in repeated:
                    repeated.append(ref)
                continue
            supported.append(ref)
        return tuple(supported), tuple(unknown), tuple(repeated)


def _locate(arguments: dict[str, Any]) -> tuple[str | None, str | None]:
    """The (kind, name) a read looked at, however that read spells it.

    `kind`/`name` win when present: a tool that says both is explicit, and
    a target argument only implies its kind.
    """
    kind = _text_arg(arguments, "kind")
    name = _text_arg(arguments, "name")
    for argument, implied_kind in TARGET_ARGUMENTS.items():
        target = _text_arg(arguments, argument)
        if target is not None:
            return kind or implied_kind, name or target
    return kind, name


def _text_arg(arguments: dict[str, Any], key: str) -> str | None:
    """A string argument, or None when absent or not a string.

    Tool arguments arrive from the model, so the type is not guaranteed.
    """
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None


def _excerpt(result: str, limit: int) -> str:
    """A bounded, single-paragraph sample of a result.

    Truncation is marked so a reader can tell a short result from a
    trimmed one; an unmarked cut reads as the whole story.
    """
    collapsed = " ".join(result.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"
