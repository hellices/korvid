"""Drill-down relation registry: parent kind -> child kind + ownership matching."""

from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relations import drill_child, owned_by


class TestDrillChild:
    def test_deployments_drill_to_replicasets(self) -> None:
        assert drill_child("deployments") == "replicasets"

    def test_replicasets_drill_to_pods(self) -> None:
        assert drill_child("replicasets") == "pods"

    def test_unrelated_kind_has_no_child(self) -> None:
        assert drill_child("configmaps") is None

    def test_pods_have_no_child(self) -> None:
        # Pods drill into containers, which is a separate screen, not a kind.
        assert drill_child("pods") is None


class TestOwnedBy:
    def test_generic_summary_owned(self) -> None:
        rs = GenericSummary(
            name="web-1", namespace="d", kind="ReplicaSet", created="", owner_uids=("dep-1",)
        )
        assert owned_by(rs, "dep-1")
        assert not owned_by(rs, "dep-2")

    def test_pod_summary_owned(self) -> None:
        pod = PodSummary(
            name="p",
            namespace="d",
            phase="Running",
            ready="1/1",
            restarts=0,
            node=None,
            owner_uids=("rs-1",),
        )
        assert owned_by(pod, "rs-1")

    def test_object_without_owner_uids_never_matches(self) -> None:
        class Bare:
            name = "x"
            namespace = "d"

        assert not owned_by(Bare(), "any")
