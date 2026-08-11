"""Where a citation opens (issue #192).

A reference is only worth having if selecting it puts the evidence on
screen. Deciding *which* screen is a pure mapping from what was read, so
it lives here rather than in the app: the UI asks one question and gets
one answer, and the answer is testable without a running Textual app.

Reads that name no single object return None. That is deliberate - a
citation that opens the wrong resource is worse than one that reports it
cannot be followed.
"""

from __future__ import annotations

import dataclasses

from korvid.agent.evidence import Evidence


@dataclasses.dataclass(frozen=True)
class _Route:
    """How one read's citation is opened."""

    view: str
    #: Kind for reads that take no `kind` argument, so the locator cannot
    #: supply one. Without this those entries were unreachable: the table
    #: said "list" and the runtime always returned None (#192 review).
    fixed_kind: str | None = None
    #: The read renders events, which the target view must fetch.
    events: bool = False


#: Which view each cluster read is best shown in.
#:
#: "describe" for reads about one object, because korvid's describe view
#: renders the manifest and - for pods - the object's events.
#:
#: Stated as a table rather than inferred from the name, so a new read
#: tool is classified deliberately; `test_every_registered_read_is_classified`
#: fails until it is.
NAVIGABLE_TOOLS: dict[str, _Route | None] = {
    # Compound diagnostics gather more than any one view shows: owner
    # chains, container states, node and PVC context, log excerpts, and -
    # for the workload case - whole child-pod diagnoses. Opening describe
    # and reporting success would tell the user the cited evidence is on
    # screen when most of it is not (#192 review). None until there is a
    # view that can show a diagnosis.
    "diagnose_pod": None,
    "diagnose_pvc": None,
    "diagnose_service": None,
    "diagnose_workload": None,
    "get_events": _Route("describe", events=True),
    "get_logs": _Route("logs"),
    "get_resource": _Route("describe"),
    "helm_list_releases": _Route("list", fixed_kind="helmreleases"),
    # `operators` is not a stable alias: discovery leaves it bound to a
    # real Operator kind when one claims it, so the citation could open an
    # unrelated table. The read also merges installed subscriptions with
    # the package catalog, which no single list shows.
    "list_operators": None,
    "list_resources": _Route("list"),
}


@dataclasses.dataclass(frozen=True)
class EvidenceTarget:
    """The view a citation opens, and what it opens there."""

    view: str
    kind: str | None
    name: str | None
    namespace: str | None
    container: str | None
    #: The read left the container to the executor, which picks the pod's
    #: *first* one. Opening every container would show streams that were
    #: not the evidence, so the caller resolves the same default.
    needs_container_resolution: bool = False
    #: The cited evidence includes events, so the target view has to fetch
    #: them - describe does so for pods only, and a citation that promises
    #: events must not open a manifest without them.
    expects_events: bool = False
    #: The read was not namespace-scoped, which for a listing means every
    #: namespace. `None` alone cannot say this: the app reads a missing
    #: namespace as "keep the current scope".
    all_namespaces: bool = False

    def __post_init__(self) -> None:
        if not self.view:
            raise ValueError("an evidence target needs a view to open")


def target_for(evidence: Evidence) -> EvidenceTarget | None:
    """Where selecting this citation should go, or None if nowhere.

    None for a tool this mapping does not know (a plugin read that has not
    been classified), for a read deliberately marked unnavigable because
    no view can show what it gathered, and for a single-object view with
    no object to show. All three are better reported than guessed.
    """
    route = NAVIGABLE_TOOLS.get(evidence.tool)
    if route is None:
        return None
    kind = route.fixed_kind or evidence.kind
    if kind is None:
        return None
    is_list = route.view == "list"
    if not is_list and evidence.name is None:
        return None
    return EvidenceTarget(
        view=route.view,
        kind=kind,
        name=None if is_list else evidence.name,
        namespace=evidence.namespace,
        container=evidence.container,
        needs_container_resolution=route.view == "logs" and evidence.container is None,
        expects_events=route.events,
        all_namespaces=is_list and evidence.namespace is None,
    )
