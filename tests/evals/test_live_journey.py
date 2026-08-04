"""Guarded live-cluster journey adapter tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from korvid.evals.journey import bundled_journeys_dir, load_journeys
from korvid.evals.live_journey import (
    NamespaceBoundReadOps,
    build_live_aliases,
    guard_live_target,
    retarget_journey_namespace,
)
from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.helm import HelmReleaseSummary
from korvid.k8s.logs import LogLine
from korvid.k8s.models import GenericSummary
from korvid.k8s.reads import ReadOps


def test_guard_live_target_accepts_only_dedicated_cluster_and_owned_namespace() -> None:
    guard_live_target(
        "aks-korvid-contract-test",
        "korvid-agent-eval-run-123",
    )
    with pytest.raises(ValueError, match="dedicated context"):
        guard_live_target("aks-shared-runners", "korvid-agent-eval-run-123")
    with pytest.raises(ValueError, match="namespace prefix"):
        guard_live_target("aks-korvid-contract-test", "default")


def test_retarget_journey_namespace_updates_turns_evidence_and_forbidden_targets() -> None:
    journey = next(
        item for item in load_journeys(bundled_journeys_dir()) if item.id == "triage-and-correct"
    )
    namespace = "korvid-agent-eval-run-123"

    retargeted = retarget_journey_namespace(journey, namespace)

    assert namespace in retargeted.turns[0].user
    assert retargeted.turns[0].expected_evidence[0][0].args["namespace"] == namespace
    assert retargeted.turns[1].forbidden_targets[0]["namespace"] == namespace
    assert journey.turns[0].expected_evidence[0][0].args["namespace"] == "shop"


def test_live_aliases_keep_core_pods_ahead_of_pod_metrics_collision() -> None:
    metrics_pods = ResourceMeta(
        kind="PodMetrics",
        plural="pods",
        group="metrics.k8s.io",
        version="v1beta1",
        namespaced=True,
    )
    aliases = build_live_aliases([PODS_META, metrics_pods])
    assert aliases["pods"] == PODS_META


class _ReadSpy(ReadOps):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def list_objects(self, meta: ResourceMeta, namespace: str | None) -> list[GenericSummary]:
        self.calls.append((meta.plural, namespace))
        return []

    async def get_object(
        self, meta: ResourceMeta, namespace: str | None, name: str
    ) -> dict[str, Any]:
        return {}

    async def list_helm_releases(self, namespace: str | None) -> list[HelmReleaseSummary]:
        return []

    async def list_events_for(
        self,
        namespace: str,
        name: str,
        *,
        kind: str | None = None,
        uid: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

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
        if False:
            yield LogLine(pod=pod, container=container, text="")


async def test_namespace_bound_reads_reject_cross_namespace_and_cluster_scope() -> None:
    spy = _ReadSpy()
    reads = NamespaceBoundReadOps(spy, "korvid-agent-eval-run-123")
    await reads.list_objects(PODS_META, "korvid-agent-eval-run-123")
    assert spy.calls == [("pods", "korvid-agent-eval-run-123")]

    with pytest.raises(ValueError, match="outside live journey namespace"):
        await reads.list_objects(PODS_META, "kube-system")
    with pytest.raises(ValueError, match="explicit namespace"):
        await reads.list_objects(PODS_META, None)
    node_meta = ResourceMeta("Node", "nodes", "", "v1", False)
    with pytest.raises(ValueError, match="cluster-scoped"):
        await reads.list_objects(node_meta, None)
