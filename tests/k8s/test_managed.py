"""Who manages this object? Pure detection from manifest metadata (issue #119).

Every fact comes from labels / annotations / ownerReferences on the object
itself — no hardcoded product knowledge, and malformed metadata yields None
rather than an error (the banner is best-effort display, fail-open).
"""

from __future__ import annotations

from typing import Any

from korvid.k8s.managed import ManagedBy, manager_of


def _manifest(
    *,
    labels: dict[str, Any] | None = None,
    annotations: dict[str, Any] | None = None,
    owners: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": "web", "namespace": "default"}
    if labels is not None:
        meta["labels"] = labels
    if annotations is not None:
        meta["annotations"] = annotations
    if owners is not None:
        meta["ownerReferences"] = owners
    return {"metadata": meta}


# ---------------------------------------------------------------------------
# helm
# ---------------------------------------------------------------------------


def test_helm_release_detected_from_labels_and_annotations() -> None:
    found = manager_of(
        _manifest(
            labels={"app.kubernetes.io/managed-by": "Helm"},
            annotations={
                "meta.helm.sh/release-name": "nginx",
                "meta.helm.sh/release-namespace": "web",
            },
        )
    )
    assert found is not None
    assert found.manager == "helm"
    assert "helm release web/nginx" in found.note
    assert "helm upgrade" in found.note


def test_helm_annotations_alone_are_enough() -> None:
    """Charts that don't set the managed-by label still stamp the release
    annotations via `helm install` — the annotation is the ground truth."""
    found = manager_of(_manifest(annotations={"meta.helm.sh/release-name": "nginx"}))
    assert found is not None
    assert found.manager == "helm"
    assert "helm release nginx" in found.note


def test_helm_managed_by_label_without_release_annotation_still_warns() -> None:
    found = manager_of(_manifest(labels={"app.kubernetes.io/managed-by": "Helm"}))
    assert found is not None
    assert found.manager == "helm"


def test_managed_by_label_for_another_tool_is_not_helm() -> None:
    assert manager_of(_manifest(labels={"app.kubernetes.io/managed-by": "kustomize"})) is None


# ---------------------------------------------------------------------------
# OLM operators
# ---------------------------------------------------------------------------


def test_olm_csv_owner_reference_detected() -> None:
    found = manager_of(
        _manifest(
            owners=[
                {
                    "apiVersion": "operators.coreos.com/v1alpha1",
                    "kind": "ClusterServiceVersion",
                    "name": "kafka-operator.v0.38.0",
                }
            ]
        )
    )
    assert found is not None
    assert found.manager == "olm"
    assert "operator kafka-operator.v0.38.0" in found.note
    assert "revert" in found.note


def test_olm_owner_labels_detected() -> None:
    found = manager_of(
        _manifest(
            labels={
                "olm.owner": "kafka-operator.v0.38.0",
                "olm.owner.kind": "ClusterServiceVersion",
            }
        )
    )
    assert found is not None
    assert found.manager == "olm"
    assert "kafka-operator.v0.38.0" in found.note


def test_olm_managed_label_alone_detected() -> None:
    found = manager_of(_manifest(labels={"olm.managed": "true"}))
    assert found is not None
    assert found.manager == "olm"


# ---------------------------------------------------------------------------
# generic controller CRs (non-OLM operators: Strimzi, cert-manager, ...)
# ---------------------------------------------------------------------------


def test_custom_controller_owner_detected() -> None:
    found = manager_of(
        _manifest(
            owners=[
                {
                    "apiVersion": "kafka.strimzi.io/v1beta2",
                    "kind": "Kafka",
                    "name": "my-cluster",
                    "controller": True,
                }
            ]
        )
    )
    assert found is not None
    assert found.manager == "controller"
    assert "Kafka/my-cluster" in found.note
    assert "edit the Kafka" in found.note


def test_builtin_controller_owner_is_not_reported() -> None:
    """A pod owned by a ReplicaSet is normal Kubernetes, not operator
    management — the banner would be pure noise on every pod."""
    found = manager_of(
        _manifest(
            owners=[
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "web-abc123",
                    "controller": True,
                }
            ]
        )
    )
    assert found is None


def test_k8s_io_group_controller_owner_is_not_reported() -> None:
    found = manager_of(
        _manifest(
            owners=[
                {
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "VolumeAttachment",
                    "name": "va-1",
                    "controller": True,
                }
            ]
        )
    )
    assert found is None


def test_non_controller_custom_owner_is_not_reported() -> None:
    """Only the controller reference manages the object; plain owner refs
    (garbage-collection edges) don't revert anything."""
    found = manager_of(
        _manifest(
            owners=[
                {
                    "apiVersion": "kafka.strimzi.io/v1beta2",
                    "kind": "Kafka",
                    "name": "my-cluster",
                }
            ]
        )
    )
    assert found is None


# ---------------------------------------------------------------------------
# precedence and robustness
# ---------------------------------------------------------------------------


def test_helm_wins_over_olm_and_controller() -> None:
    found = manager_of(
        _manifest(
            labels={"app.kubernetes.io/managed-by": "Helm", "olm.managed": "true"},
            annotations={"meta.helm.sh/release-name": "nginx"},
            owners=[
                {
                    "apiVersion": "kafka.strimzi.io/v1beta2",
                    "kind": "Kafka",
                    "name": "my-cluster",
                    "controller": True,
                }
            ],
        )
    )
    assert found is not None
    assert found.manager == "helm"


def test_olm_wins_over_generic_controller() -> None:
    found = manager_of(
        _manifest(
            labels={"olm.managed": "true"},
            owners=[
                {
                    "apiVersion": "kafka.strimzi.io/v1beta2",
                    "kind": "Kafka",
                    "name": "my-cluster",
                    "controller": True,
                }
            ],
        )
    )
    assert found is not None
    assert found.manager == "olm"


def test_unmanaged_object_yields_none() -> None:
    assert manager_of(_manifest()) is None


def test_malformed_metadata_yields_none() -> None:
    assert manager_of({}) is None
    assert manager_of({"metadata": "oops"}) is None
    assert manager_of(_manifest(labels={"app.kubernetes.io/managed-by": 7})) is None
    assert manager_of({"metadata": {"ownerReferences": ["oops", 3]}}) is None
    assert manager_of({"metadata": {"ownerReferences": [{"controller": True, "kind": 5}]}}) is None


def test_managed_by_is_frozen() -> None:
    """The dataclass is a value passed across an await gap into a dialog —
    freezing it pins that nothing mutates it in flight."""
    found = manager_of(_manifest(labels={"olm.managed": "true"}))
    assert isinstance(found, ManagedBy)
    assert found.__dataclass_params__.frozen  # type: ignore[attr-defined]  # dataclass introspection


def test_string_controller_flag_is_not_controlling() -> None:
    """ownerReferences.controller is a boolean in the API; a malformed
    string value like "false" must not be treated as truthy — that would
    produce a false ownership warning."""
    manifest = {
        "metadata": {
            "ownerReferences": [
                {
                    "apiVersion": "kafka.strimzi.io/v1beta2",
                    "kind": "Kafka",
                    "name": "prod",
                    "controller": "false",
                }
            ]
        }
    }
    assert manager_of(manifest) is None
