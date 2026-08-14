"""Tests for metadata-only Kubernetes relationship fact extraction (issue #281)."""

from korvid.k8s.relationship_facts import (
    FactConfidence,
    RelationKind,
    extract_relationship_facts,
)


def test_pod_facts_include_metadata_only_references() -> None:
    manifest = {
        "metadata": {
            "name": "api-0",
            "namespace": "prod",
            "uid": "pod-1",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "api-abc",
                    "uid": "rs-1",
                }
            ],
        },
        "spec": {
            "nodeName": "node-a",
            "volumes": [
                {"name": "data", "persistentVolumeClaim": {"claimName": "api-data"}},
                {"name": "cfg", "configMap": {"name": "api-config"}},
                {"name": "tls", "secret": {"secretName": "api-tls"}},
            ],
            "containers": [
                {
                    "name": "api",
                    "env": [
                        {
                            "name": "TOKEN",
                            "valueFrom": {"secretKeyRef": {"name": "api-creds", "key": "token"}},
                        },
                        {"name": "LITERAL", "value": "must-not-be-retained"},
                    ],
                    "command": ["must-not-be-retained"],
                }
            ],
        },
    }
    facts = extract_relationship_facts("Pod", "", "v1", manifest)
    pairs = {(fact.relation, fact.target.kind, fact.target.name) for fact in facts.references}
    assert (RelationKind.OWNED_BY, "ReplicaSet", "api-abc") in pairs
    assert (RelationKind.USES_VOLUME, "PersistentVolumeClaim", "api-data") in pairs
    assert (RelationKind.USES_CONFIG, "ConfigMap", "api-config") in pairs
    assert (RelationKind.USES_CONFIG, "Secret", "api-tls") in pairs
    assert (RelationKind.USES_CONFIG, "Secret", "api-creds") in pairs
    assert (RelationKind.SCHEDULED_ON, "Node", "node-a") in pairs
    assert all(fact.confidence is not FactConfidence.INFERRED for fact in facts.references)
    assert "must-not-be-retained" not in repr(facts)
    assert "token" not in repr(facts)


def test_service_absent_and_empty_selectors_emit_no_fact() -> None:
    absent = extract_relationship_facts(
        "Service", "", "v1", {"metadata": {"name": "external", "namespace": "prod"}}
    )
    empty = extract_relationship_facts(
        "Service",
        "",
        "v1",
        {"metadata": {"name": "external", "namespace": "prod"}, "spec": {"selector": {}}},
    )
    assert absent.selectors == ()
    assert empty.selectors == ()


def test_policy_v1_empty_pdb_selector_matches_all() -> None:
    facts = extract_relationship_facts(
        "PodDisruptionBudget",
        "policy",
        "v1",
        {"metadata": {"name": "all", "namespace": "prod"}, "spec": {"selector": {}}},
    )
    assert facts.selectors[0].target_kind == "Pod"
    assert facts.selectors[0].relation is RelationKind.PROTECTED_BY
    assert facts.selectors[0].empty_matches is True
    assert facts.selectors[0].match_is_subject is True
    assert facts.selectors[0].field == "spec.selector"


def test_policy_v1beta1_empty_pdb_selector_emits_no_fact() -> None:
    facts = extract_relationship_facts(
        "PodDisruptionBudget",
        "policy",
        "v1beta1",
        {"metadata": {"name": "all", "namespace": "prod"}, "spec": {"selector": {}}},
    )
    assert facts.selectors == ()


def test_endpoint_slice_target_ref_is_observed() -> None:
    facts = extract_relationship_facts(
        "EndpointSlice",
        "discovery.k8s.io",
        "v1",
        {
            "metadata": {"name": "api-abc", "namespace": "prod"},
            "endpoints": [
                {
                    "targetRef": {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "namespace": "prod",
                        "name": "api-0",
                        "uid": "pod-1",
                    }
                }
            ],
        },
    )
    target = facts.references[0]
    assert target.target.kind == "Pod"
    assert target.target.uid == "pod-1"
    assert target.confidence is FactConfidence.OBSERVED
    assert target.field == "endpoints[0].targetRef"


def test_http_route_and_reference_grant_retain_cross_namespace_policy() -> None:
    route = extract_relationship_facts(
        "HTTPRoute",
        "gateway.networking.k8s.io",
        "v1",
        {
            "metadata": {"name": "public", "namespace": "edge"},
            "spec": {
                "rules": [
                    {
                        "backendRefs": [
                            {
                                "group": "",
                                "kind": "Service",
                                "namespace": "prod",
                                "name": "api",
                            }
                        ]
                    }
                ]
            },
        },
    )
    grant = extract_relationship_facts(
        "ReferenceGrant",
        "gateway.networking.k8s.io",
        "v1beta1",
        {
            "metadata": {"name": "edge-to-services", "namespace": "prod"},
            "spec": {
                "from": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "HTTPRoute",
                        "namespace": "edge",
                    }
                ],
                "to": [{"group": "", "kind": "Service"}],
            },
        },
    )
    assert route.references[0].target.namespace == "prod"
    assert grant.grants[0].from_namespace == "edge"
    assert grant.grants[0].to_kind == "Service"


def test_malformed_relationship_lists_are_ignored() -> None:
    facts = extract_relationship_facts(
        "Ingress",
        "networking.k8s.io",
        "v1",
        {"metadata": {"name": "bad", "namespace": "prod"}, "spec": {"rules": "bad"}},
    )
    assert facts.references == ()


def test_deployment_selector_targets_pods() -> None:
    facts = extract_relationship_facts(
        "Deployment",
        "apps",
        "v1",
        {
            "metadata": {"name": "api", "namespace": "prod"},
            "spec": {
                "selector": {"matchLabels": {"app": "api"}},
                "template": {"spec": {"nodeName": "node-a"}},
            },
        },
    )
    assert len(facts.selectors) == 1
    selector_fact = facts.selectors[0]
    assert selector_fact.relation is RelationKind.MANAGED_BY
    assert selector_fact.target_group == ""
    assert selector_fact.target_kind == "Pod"
    assert selector_fact.selector.match_labels == (("app", "api"),)
    assert selector_fact.confidence is FactConfidence.DECLARED
    assert selector_fact.field == "spec.selector"
    assert selector_fact.empty_matches is False
    assert selector_fact.match_is_subject is True
    # spec.template.spec is also extracted through the shared _pod_spec helper.
    pairs = {(fact.relation, fact.target.kind, fact.target.name) for fact in facts.references}
    assert (RelationKind.SCHEDULED_ON, "Node", "node-a") in pairs


def test_workload_selectors_use_managed_by_not_selects() -> None:
    """Deployment/ReplicaSet/StatefulSet/DaemonSet/Job own the pods their
    selector matches, so they must emit MANAGED_BY (match_is_subject=True),
    never SELECTS (which is reserved for Service traffic-routing)."""
    cases = [
        ("Deployment", "apps"),
        ("ReplicaSet", "apps"),
        ("StatefulSet", "apps"),
        ("DaemonSet", "apps"),
        ("Job", "batch"),
    ]
    for kind, group in cases:
        facts = extract_relationship_facts(
            kind,
            group,
            "v1",
            {
                "metadata": {"name": "wl", "namespace": "prod"},
                "spec": {"selector": {"matchLabels": {"app": "wl"}}, "template": {"spec": {}}},
            },
        )
        assert len(facts.selectors) == 1, kind
        selector_fact = facts.selectors[0]
        assert selector_fact.relation is RelationKind.MANAGED_BY, kind
        assert selector_fact.match_is_subject is True, kind


def test_job_without_selector_emits_no_selector_fact() -> None:
    """Job's spec.selector is optional (usually controller-managed); when
    absent, no MANAGED_BY fact should be fabricated."""
    facts = extract_relationship_facts(
        "Job",
        "batch",
        "v1",
        {"metadata": {"name": "backup", "namespace": "prod"}, "spec": {"template": {"spec": {}}}},
    )
    assert facts.selectors == ()


def test_replicaset_owner_reference_points_to_deployment() -> None:
    """ReplicaSet -> Deployment ownership is retained via the universal
    owner-reference extraction (not Pod-only)."""
    facts = extract_relationship_facts(
        "ReplicaSet",
        "apps",
        "v1",
        {
            "metadata": {
                "name": "api-abc",
                "namespace": "prod",
                "uid": "rs-1",
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": "api",
                        "uid": "dep-1",
                    }
                ],
            },
            "spec": {},
        },
    )
    pairs = {
        (fact.relation, fact.target.group, fact.target.kind, fact.target.name, fact.target.uid)
        for fact in facts.references
    }
    assert (RelationKind.OWNED_BY, "apps", "Deployment", "api", "dep-1") in pairs
    owner_fact = next(f for f in facts.references if f.relation is RelationKind.OWNED_BY)
    assert owner_fact.target.namespace == "prod"
    assert owner_fact.field == "metadata.ownerReferences[0]"


def test_endpoint_slice_owner_reference_points_to_service() -> None:
    """EndpointSlice -> Service ownership is retained via the universal
    owner-reference extraction (not Pod-only)."""
    facts = extract_relationship_facts(
        "EndpointSlice",
        "discovery.k8s.io",
        "v1",
        {
            "metadata": {
                "name": "api-abc",
                "namespace": "prod",
                "uid": "eps-1",
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "Service",
                        "name": "api",
                        "uid": "svc-1",
                    }
                ],
            },
        },
    )
    pairs = {
        (fact.relation, fact.target.group, fact.target.kind, fact.target.name, fact.target.uid)
        for fact in facts.references
    }
    assert (RelationKind.OWNED_BY, "", "Service", "api", "svc-1") in pairs


def test_service_selector_targets_pods_with_match_is_subject_false() -> None:
    facts = extract_relationship_facts(
        "Service",
        "",
        "v1",
        {
            "metadata": {"name": "api", "namespace": "prod"},
            "spec": {"selector": {"app": "api"}},
        },
    )
    assert len(facts.selectors) == 1
    selector_fact = facts.selectors[0]
    assert selector_fact.relation is RelationKind.SELECTS
    assert selector_fact.target_kind == "Pod"
    assert selector_fact.selector.match_labels == (("app", "api"),)
    assert selector_fact.confidence is FactConfidence.DECLARED
    assert selector_fact.field == "spec.selector"
    assert selector_fact.match_is_subject is False


def test_ingress_default_and_path_backends_target_services() -> None:
    facts = extract_relationship_facts(
        "Ingress",
        "networking.k8s.io",
        "v1",
        {
            "metadata": {"name": "public", "namespace": "prod"},
            "spec": {
                "defaultBackend": {"service": {"name": "default-svc"}},
                "rules": [
                    {
                        "http": {
                            "paths": [{"path": "/api", "backend": {"service": {"name": "api-svc"}}}]
                        }
                    }
                ],
            },
        },
    )
    assert len(facts.references) == 2
    default_fact, path_fact = facts.references
    assert default_fact.relation is RelationKind.ROUTES_TO
    assert default_fact.target.kind == "Service"
    assert default_fact.target.namespace == "prod"
    assert default_fact.target.name == "default-svc"
    assert default_fact.confidence is FactConfidence.DECLARED
    assert default_fact.field == "spec.defaultBackend.service"
    assert path_fact.target.name == "api-svc"
    assert path_fact.field == "spec.rules[0].http.paths[0].backend.service"


def test_pvc_and_pv_binding_references() -> None:
    pvc_facts = extract_relationship_facts(
        "PersistentVolumeClaim",
        "",
        "v1",
        {
            "metadata": {"name": "api-data", "namespace": "prod"},
            "spec": {"volumeName": "pv-1"},
        },
    )
    assert len(pvc_facts.references) == 1
    pvc_fact = pvc_facts.references[0]
    assert pvc_fact.relation is RelationKind.BOUND_TO
    assert pvc_fact.target.kind == "PersistentVolume"
    assert pvc_fact.target.namespace == ""
    assert pvc_fact.target.name == "pv-1"
    assert pvc_fact.confidence is FactConfidence.DECLARED
    assert pvc_fact.field == "spec.volumeName"

    pv_facts = extract_relationship_facts(
        "PersistentVolume",
        "",
        "v1",
        {
            "metadata": {"name": "pv-1"},
            "spec": {"claimRef": {"namespace": "prod", "name": "api-data"}},
        },
    )
    assert len(pv_facts.references) == 1
    pv_fact = pv_facts.references[0]
    assert pv_fact.relation is RelationKind.BOUND_TO
    assert pv_fact.target.kind == "PersistentVolumeClaim"
    assert pv_fact.target.namespace == "prod"
    assert pv_fact.target.name == "api-data"
    assert pv_fact.confidence is FactConfidence.DECLARED
    assert pv_fact.field == "spec.claimRef"
