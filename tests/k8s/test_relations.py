"""Drill-down relation registry: parent kind -> child kind + ownership matching."""

from korvid.k8s.discovery import PODS_META, ResourceMeta
from korvid.k8s.helm import HELM_RELEASES_META
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relations import drill_child, owned_by


class TestDrillChild:
    def test_deployments_drill_to_replicasets(self) -> None:
        parent = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
        assert drill_child(parent) == ("apps", "replicasets", False)

    def test_replicasets_drill_to_pods(self) -> None:
        parent = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True)
        assert drill_child(parent) == ("", "pods", False)

    def test_unrelated_kind_has_no_child(self) -> None:
        assert drill_child(ResourceMeta("ConfigMap", "configmaps", "", "v1", True)) is None

    def test_pods_have_no_child(self) -> None:
        # Pods drill into containers, which is a separate screen, not a kind.
        assert drill_child(PODS_META) is None

    def test_foreign_deployment_does_not_inherit_native_chain(self) -> None:
        parent = ResourceMeta("Deployment", "deployments", "example.io", "v1", True)
        assert drill_child(parent) is None

    def test_helm_history_belongs_only_to_synthetic_releases(self) -> None:
        flux = ResourceMeta("HelmRelease", "helmreleases", "helm.toolkit.fluxcd.io", "v2", True)
        assert drill_child(HELM_RELEASES_META) == ("", "helmrevisions", True)
        assert drill_child(flux) is None


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
