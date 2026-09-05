"""Guarded live-cluster journey adapter tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from korvid.evals.journey import bundled_journeys_dir, load_journeys
from korvid.evals.live_journey import (
    NamespaceBoundReadOps,
    guard_live_target,
    guard_namespace_ownership,
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
    with pytest.raises(ValueError, match="non-empty run suffix"):
        guard_live_target("aks-korvid-contract-test", "korvid-agent-eval-")


def test_namespace_ownership_requires_managed_and_matching_run_labels() -> None:
    namespace = "korvid-agent-eval-run-123"
    labels = {
        "app.kubernetes.io/managed-by": "korvid-agent-eval",
        "korvid.dev/eval-run": "run-123",
    }
    guard_namespace_ownership(namespace, {"metadata": {"labels": labels}})

    with pytest.raises(ValueError, match="managed-by"):
        guard_namespace_ownership(namespace, {"metadata": {"labels": {}}})
    with pytest.raises(ValueError, match="eval-run"):
        guard_namespace_ownership(
            namespace,
            {"metadata": {"labels": {**labels, "korvid.dev/eval-run": "other"}}},
        )


def test_retarget_journey_namespace_updates_live_identity_and_targets() -> None:
    journey = next(
        item for item in load_journeys(bundled_journeys_dir()) if item.id == "triage-and-correct"
    )
    namespace = "korvid-agent-eval-run-123"

    retargeted = retarget_journey_namespace(
        journey,
        namespace,
        context="aks-korvid-contract-test",
    )

    assert retargeted.interaction.kube_context == "aks-korvid-contract-test"
    assert namespace in retargeted.turns[0].user
    assert retargeted.turns[0].expected_evidence[0][0].args["namespace"] == namespace
    assert retargeted.turns[1].forbidden_targets[0]["namespace"] == namespace
    assert retargeted.turns[1].interaction is not None
    assert retargeted.turns[1].interaction.kube_context == "aks-korvid-contract-test"
    assert retargeted.turns[1].interaction.focused_pane.selected is not None
    assert retargeted.turns[1].interaction.focused_pane.selected.uid is None
    assert journey.turns[0].expected_evidence[0][0].args["namespace"] == "shop"

    default_context = retarget_journey_namespace(journey, namespace, context="")
    assert default_context.interaction.kube_context is None
    assert all(
        turn.interaction is None or turn.interaction.kube_context is None
        for turn in default_context.turns
    )


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


# --- the live run's own context ---------------------------------------------
#
# A live journey runs against a real cluster, and the workspace the model is
# shown must say which one. The authored fixture context is a fake, so
# retargeting replaces it with the context the run actually connected to.
