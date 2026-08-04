"""Fail-closed real-cluster adapter for conversational journey evaluation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from korvid.evals.journey import ConversationJourney, JourneyTurn
from korvid.evals.journey_runner import RecordingUI
from korvid.evals.scenario import Evidence
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta, build_alias_map
from korvid.tools.executor import ToolExecutor

EXPECTED_CONTEXT = "aks-korvid-contract-test"
NAMESPACE_PREFIX = "korvid-agent-eval-"


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
) -> ConversationJourney:
    """Copy a journey's conversational targets into one live run namespace."""
    source = _source_namespace(journey)
    turns: list[JourneyTurn] = []
    for turn in journey.turns:
        turns.append(
            replace(
                turn,
                user=turn.user.replace(source, namespace),
                screen=turn.screen.replace(source, namespace),
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
    return replace(journey, turns=tuple(turns))


def build_live_aliases(resources: list[ResourceMeta]) -> dict[str, ResourceMeta]:
    """Match application discovery semantics: first collision wins."""
    return build_alias_map(resources)


class LiveJourneyEnvironment:
    """Connected read executor for the guarded contract-test context."""

    def __init__(self, client: KubeClient, aliases: dict[str, ResourceMeta]) -> None:
        self._client = client
        self._aliases = aliases

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
            resources = await client.discover_resources()
        except Exception:
            await client.close()
            raise
        return cls(client, build_live_aliases(resources))

    def executor_factory(self, _fixture: Any) -> ToolExecutor:
        """Read-only tool executor; profile construction omits every write."""
        return ToolExecutor(
            self._client,
            self._aliases,
            ui=RecordingUI(),
        )

    async def close(self) -> None:
        await self._client.close()
