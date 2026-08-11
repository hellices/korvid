"""Where a citation opens (issue #192).

A reference is only worth having if selecting it puts the evidence on
screen. Deciding *which* screen is a pure mapping from what was read, so
it lives here rather than in the app: the UI asks one question and gets
one answer, and the answer is testable without a running Textual app.

Reads that name no single object return None. That is deliberate — a
citation that opens the wrong resource is worse than one that reports it
cannot be followed.
"""

from __future__ import annotations

import dataclasses

from korvid.agent.evidence import Evidence

#: Which view each cluster read is best shown in.
#:
#: "describe" for reads about one object, because korvid's describe view
#: already renders both the manifest and the object's events - so a
#: `get_events` citation lands where those events are, without a second
#: view that shows only half the story.
#:
#: Stated as a table rather than inferred from the name, so a new read
#: tool is classified deliberately; `test_every_registered_read_is_classified`
#: fails until it is.
NAVIGABLE_TOOLS: dict[str, str] = {
    "diagnose_pod": "describe",
    "diagnose_pvc": "describe",
    "diagnose_service": "describe",
    "diagnose_workload": "describe",
    "get_events": "describe",
    "get_logs": "logs",
    "get_resource": "describe",
    "helm_list_releases": "list",
    "list_operators": "list",
    "list_resources": "list",
}


@dataclasses.dataclass(frozen=True)
class EvidenceTarget:
    """The view a citation opens, and what it opens there."""

    view: str
    kind: str | None
    name: str | None
    namespace: str | None
    container: str | None

    def __post_init__(self) -> None:
        if not self.view:
            raise ValueError("an evidence target needs a view to open")


def target_for(evidence: Evidence) -> EvidenceTarget | None:
    """Where selecting this citation should go, or None if nowhere.

    None for three cases, all of which are better reported than guessed:
    a tool this mapping does not know (a plugin read that has not been
    classified), a listing with no kind to list, and a single-object view
    with no object to show.
    """
    view = NAVIGABLE_TOOLS.get(evidence.tool)
    if view is None:
        return None
    if evidence.kind is None:
        return None
    if view != "list" and evidence.name is None:
        return None
    return EvidenceTarget(
        view=view,
        kind=evidence.kind,
        name=None if view == "list" else evidence.name,
        namespace=evidence.namespace,
        container=evidence.container,
    )
