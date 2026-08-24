"""Fail-closed real-cluster adapter for conversational journey evaluation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from korvid.agent.interaction import InteractionContext, PaneContext
from korvid.evals.journey import ConversationJourney, JourneyTurn
from korvid.evals.journey_runner import LIVE_BOUNDARY_ERROR
from korvid.evals.scenario import Evidence
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta, build_alias_map
from korvid.k8s.helm import HelmReleaseSummary
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary
from korvid.k8s.reads import ReadOps
from korvid.tools.executor import ToolExecutor

EXPECTED_CONTEXT = "aks-korvid-contract-test"
NAMESPACE_PREFIX = "korvid-agent-eval-"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "korvid-agent-eval"
RUN_LABEL = "korvid.dev/eval-run"
NAMESPACE_META = ResourceMeta(
    kind="Namespace",
    plural="namespaces",
    group="",
    version="v1",
    namespaced=False,
)


def guard_live_target(context: str, namespace: str) -> None:
    """Refuse live evaluation outside the dedicated, owned target."""
    if context != EXPECTED_CONTEXT:
        raise ValueError(
            f"live journeys require dedicated context {EXPECTED_CONTEXT!r}, got {context!r}"
        )
    if not namespace.startswith(NAMESPACE_PREFIX):
        raise ValueError(
            f"live journey namespace prefix must be {NAMESPACE_PREFIX!r}, got {namespace!r}"
        )
    if not namespace.removeprefix(NAMESPACE_PREFIX):
        raise ValueError("live journey namespace requires a non-empty run suffix")


def guard_namespace_ownership(
    namespace: str,
    manifest: dict[str, Any],
) -> None:
    """Verify the trusted Namespace object belongs to this exact eval run."""
    labels = (manifest.get("metadata") or {}).get("labels") or {}
    if labels.get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
        raise ValueError(f"live journey namespace needs {MANAGED_BY_LABEL}={MANAGED_BY_VALUE}")
    expected_run = namespace.removeprefix(NAMESPACE_PREFIX)
    if labels.get(RUN_LABEL) != expected_run:
        raise ValueError(f"live journey namespace {RUN_LABEL} must be {expected_run!r}")


def _retarget_evidence(
    groups: tuple[tuple[Evidence, ...], ...],
    source: str,
    target: str,
) -> tuple[tuple[Evidence, ...], ...]:
    return tuple(
        tuple(
            replace(
                evidence,
                args={
                    key: target if key == "namespace" and value == source else value
                    for key, value in evidence.args.items()
                },
            )
            for evidence in group
        )
        for group in groups
    )


def _retarget_pane(pane: PaneContext, source: str, namespace: str) -> PaneContext:
    selected = pane.selected
    return replace(
        pane,
        scope=namespace if pane.scope == source else pane.scope,
        selected=(
            None
            if selected is None
            else replace(
                selected,
                namespace=namespace if selected.namespace == source else selected.namespace,
            )
        ),
    )


def _retarget_interaction(
    interaction: InteractionContext,
    source: str,
    namespace: str,
    context: str | None,
) -> InteractionContext:
    """Point an authored workspace at the live run's own target.

    The guarded live adapter refuses every read outside the run namespace,
    so a starting pane still scoped to the fixture namespace would tell
    the model to look somewhere it is not allowed to look. The fixture's
    `kube_context` is a fake for the same reason: the published row has to
    name the context the run actually connected to, and `None` only when
    the run truly used the kubeconfig's current context.
    """
    secondary = interaction.secondary_pane
    return replace(
        interaction,
        kube_context=context,
        focused_pane=_retarget_pane(interaction.focused_pane, source, namespace),
        secondary_pane=(
            None if secondary is None else _retarget_pane(secondary, source, namespace)
        ),
    )


def _retarget_optional(
    interaction: InteractionContext | None,
    source: str,
    namespace: str,
    context: str | None,
) -> InteractionContext | None:
    """`_retarget_interaction` for a turn that may not restate the screen."""
    if interaction is None:
        return None
    return _retarget_interaction(interaction, source, namespace, context)


def _source_namespace(journey: ConversationJourney) -> str:
    for turn in journey.turns:
        for group in turn.expected_evidence:
            for evidence in group:
                namespace = evidence.args.get("namespace")
                if isinstance(namespace, str) and namespace:
                    return namespace
    raise ValueError(f"journey {journey.id!r} has no namespaced evidence target")


def retarget_journey_namespace(
    journey: ConversationJourney,
    namespace: str,
    *,
    context: str | None = None,
) -> ConversationJourney:
    """Copy a journey's conversational targets into one live run namespace.

    Args:
        journey: The authored journey, written against a fixture namespace.
        namespace: The live run's own namespace.
        context: The kube context this run connected to, as the CLI's
            `--context` supplied it. An empty string or `None` means the
            kubeconfig's current context, and is published as `None` —
            the authored fixture context is never carried into a live row.

    Returns:
        The journey with every namespace, evidence target, forbidden
        target and workspace pointed at the live run.
    """
    source = _source_namespace(journey)
    run_context = (context or "").strip() or None
    turns: list[JourneyTurn] = []
    for turn in journey.turns:
        turns.append(
            replace(
                turn,
                user=turn.user.replace(source, namespace),
                interaction=_retarget_optional(turn.interaction, source, namespace, run_context),
                expected_evidence=_retarget_evidence(turn.expected_evidence, source, namespace),
                forbidden_targets=tuple(
                    {
                        key: namespace if key == "namespace" and value == source else value
                        for key, value in target.items()
                    }
                    for target in turn.forbidden_targets
                ),
            )
        )
    return replace(
        journey,
        turns=tuple(turns),
        interaction=_retarget_interaction(journey.interaction, source, namespace, run_context),
    )


def build_live_aliases(resources: list[ResourceMeta]) -> dict[str, ResourceMeta]:
    """Match application discovery semantics: first collision wins."""
    return build_alias_map(resources)


class NamespaceBoundReadOps(ReadOps):
    """Fail-closed view of a client confined to one namespaced fixture."""

    def __init__(self, delegate: ReadOps, namespace: str) -> None:
        self._delegate = delegate
        self._namespace = namespace

    def _guard(self, meta: ResourceMeta, namespace: str | None) -> None:
        if not meta.namespaced:
            raise ValueError(f"{LIVE_BOUNDARY_ERROR} rejects cluster-scoped read of {meta.plural}")
        if namespace is None:
            raise ValueError(f"{LIVE_BOUNDARY_ERROR} reads require an explicit namespace")
        if namespace != self._namespace:
            raise ValueError(
                f"{LIVE_BOUNDARY_ERROR} read outside live journey namespace: {namespace!r}"
            )

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        self._guard(meta, namespace)
        return await self._delegate.list_objects(meta, namespace)

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        self._guard(meta, namespace)
        return await self._delegate.get_object(meta, namespace, name)

    async def list_helm_releases(self, namespace: str | None) -> list[HelmReleaseSummary]:
        if namespace is None:
            raise ValueError(f"{LIVE_BOUNDARY_ERROR} reads require an explicit namespace")
        if namespace != self._namespace:
            raise ValueError(
                f"{LIVE_BOUNDARY_ERROR} read outside live journey namespace: {namespace!r}"
            )
        return await self._delegate.list_helm_releases(namespace)

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        if namespace != self._namespace:
            raise ValueError(
                f"{LIVE_BOUNDARY_ERROR} read outside live journey namespace: {namespace!r}"
            )
        return await self._delegate.list_events_for(namespace, name, kind=kind, uid=uid)

    async def stream_logs(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        follow: bool = True,
        tail_lines: int = 200,
    ) -> AsyncIterator[LogLine]:
        if namespace != self._namespace:
            raise ValueError(
                f"{LIVE_BOUNDARY_ERROR} read outside live journey namespace: {namespace!r}"
            )
        async for line in self._delegate.stream_logs(
            namespace,
            pod,
            container,
            previous=previous,
            follow=follow,
            tail_lines=tail_lines,
        ):
            yield line


class LiveJourneyEnvironment:
    """Connected read executor for the guarded contract-test context."""

    def __init__(
        self,
        client: KubeClient,
        aliases: dict[str, ResourceMeta],
        namespace: str,
    ) -> None:
        self._client = client
        self._aliases = aliases
        self._reads = NamespaceBoundReadOps(client, namespace)

    @classmethod
    async def connect(
        cls,
        context: str,
        namespace: str,
    ) -> LiveJourneyEnvironment:
        guard_live_target(context, namespace)
        client = KubeClient()
        await client.connect(context=context)
        try:
            manifest = await client.get_object(NAMESPACE_META, None, namespace)
            guard_namespace_ownership(namespace, manifest)
            resources = await client.discover_resources()
        except Exception:
            await client.close()
            raise
        return cls(client, build_live_aliases(resources), namespace)

    def executor_factory(self, _fixture: Any) -> ToolExecutor:
        """Read-only tool executor; the eval policy arms no write at all.

        No UI bridge either: screen actions are typed `UiAction` values
        applied to the eval workspace bridge, never routed through the
        tool executor.
        """
        return ToolExecutor(self._reads, self._aliases)

    async def close(self) -> None:
        await self._client.close()
