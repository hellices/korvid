"""Guarded live-cluster journey adapter tests."""

from __future__ import annotations

import pytest

from korvid.evals.journey import bundled_journeys_dir, load_journeys
from korvid.evals.live_journey import (
    build_live_aliases,
    guard_live_target,
    retarget_journey_namespace,
)
from korvid.k8s.discovery import PODS_META, ResourceMeta


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
