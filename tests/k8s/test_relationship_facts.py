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
                "template": {
                    "spec": {
                        "nodeName": "node-a",
                        "volumes": [{"name": "cfg", "configMap": {"name": "api-config"}}],
                    }
                },
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
    # spec.template.spec is also extracted through the shared _pod_spec helper,
    # minus node placement: a template's nodeName is not an observed
    # placement of the Deployment itself (see the SCHEDULED_ON tests below).
    pairs = {(fact.relation, fact.target.kind, fact.target.name) for fact in facts.references}
    assert (RelationKind.USES_CONFIG, "ConfigMap", "api-config") in pairs
    assert (RelationKind.SCHEDULED_ON, "Node", "node-a") not in pairs


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


def test_reference_grant_retains_to_name_when_present() -> None:
    """`spec.to[].name` narrows a grant to one object; dropping it would
    silently widen the grant to every object of that group/kind."""
    facts = extract_relationship_facts(
        "ReferenceGrant",
        "gateway.networking.k8s.io",
        "v1beta1",
        {
            "metadata": {"name": "edge-to-payments", "namespace": "prod"},
            "spec": {
                "from": [
                    {
                        "group": "gateway.networking.k8s.io",
                        "kind": "HTTPRoute",
                        "namespace": "edge",
                    }
                ],
                "to": [{"group": "", "kind": "Service", "name": "payments"}],
            },
        },
    )
    assert len(facts.grants) == 1
    assert facts.grants[0].to_name == "payments"


def test_reference_grant_without_to_name_grants_every_name() -> None:
    """An omitted (or blank/non-string) `spec.to[].name` means "all objects
    of this group/kind" and must be recorded as `None`, not `""`."""
    facts = extract_relationship_facts(
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
                "to": [
                    {"group": "", "kind": "Service"},
                    {"group": "", "kind": "Secret", "name": 3},
                ],
            },
        },
    )
    assert [grant.to_name for grant in facts.grants] == [None, None]


def test_grpc_route_backend_refs_are_extracted() -> None:
    """Gateway GRPCRoute shares HTTPRoute's `spec.rules[].backendRefs[]`
    shape; the loader lists every discovered `*Route`, so extraction must
    cover it too rather than reporting complete coverage of nothing."""
    facts = extract_relationship_facts(
        "GRPCRoute",
        "gateway.networking.k8s.io",
        "v1",
        {
            "metadata": {"name": "grpc", "namespace": "edge"},
            "spec": {"rules": [{"backendRefs": [{"kind": "Service", "name": "api"}]}]},
        },
    )
    assert len(facts.references) == 1
    fact = facts.references[0]
    assert fact.relation is RelationKind.ROUTES_TO
    assert fact.target.kind == "Service"
    assert fact.target.name == "api"
    assert fact.target.namespace == "edge"
    assert fact.confidence is FactConfidence.DECLARED
    assert fact.field == "spec.rules[0].backendRefs[0]"


def test_stream_route_backend_refs_are_extracted() -> None:
    """TLSRoute/TCPRoute/UDPRoute use the same `backendRefs` shape."""
    for kind in ("TLSRoute", "TCPRoute", "UDPRoute"):
        facts = extract_relationship_facts(
            kind,
            "gateway.networking.k8s.io",
            "v1alpha2",
            {
                "metadata": {"name": "stream", "namespace": "edge"},
                "spec": {
                    "rules": [{"backendRefs": [{"name": "db", "namespace": "data", "port": 5432}]}]
                },
            },
        )
        assert len(facts.references) == 1, kind
        fact = facts.references[0]
        assert fact.relation is RelationKind.ROUTES_TO, kind
        assert fact.target.kind == "Service", kind
        assert fact.target.namespace == "data", kind
        assert fact.target.name == "db", kind


def test_route_kinds_outside_the_gateway_group_are_not_extracted() -> None:
    """A CRD that merely ends in `Route` (e.g. an OpenShift `Route`, or a
    vendor `gateway.networking.x-k8s.io` kind) is neither discovered by the
    loader nor safe to interpret with the Gateway API's backendRef shape."""
    for group in ("route.openshift.io", "gateway.networking.x-k8s.io", ""):
        facts = extract_relationship_facts(
            "Route",
            group,
            "v1",
            {
                "metadata": {"name": "legacy", "namespace": "edge"},
                "spec": {"rules": [{"backendRefs": [{"name": "api"}]}]},
            },
        )
        assert facts.references == (), group


def test_gateway_kind_itself_emits_no_backend_refs() -> None:
    """`Gateway` is discovered and listed, but it declares listeners, not
    backends — the generic route path must not fire for it."""
    facts = extract_relationship_facts(
        "Gateway",
        "gateway.networking.k8s.io",
        "v1",
        {
            "metadata": {"name": "public", "namespace": "edge"},
            "spec": {"rules": [{"backendRefs": [{"name": "api"}]}]},
        },
    )
    assert facts.references == ()


def test_pv_claim_ref_retains_the_bound_claim_uid() -> None:
    """`spec.claimRef` is an ObjectReference carrying the bound PVC's UID.

    Dropping it would let a deleted-and-recreated PVC of the same name be
    reconnected to a stale PV binding, so the UID must survive extraction
    and feed the graph's UID-first resolution.
    """
    facts = extract_relationship_facts(
        "PersistentVolume",
        "",
        "v1",
        {
            "metadata": {"name": "pv-1"},
            "spec": {"claimRef": {"namespace": "prod", "name": "api-data", "uid": "pvc-old"}},
        },
    )
    assert len(facts.references) == 1
    assert facts.references[0].target.uid == "pvc-old"


def test_pv_claim_ref_without_uid_stays_name_resolved() -> None:
    """A `claimRef` with no (or a blank) UID keeps name-based resolution."""
    for claim_ref in ({"namespace": "prod", "name": "api-data"}, {"name": "api-data", "uid": ""}):
        facts = extract_relationship_facts(
            "PersistentVolume",
            "",
            "v1",
            {"metadata": {"name": "pv-1"}, "spec": {"claimRef": claim_ref}},
        )
        assert facts.references[0].target.uid is None, claim_ref


def test_pod_ephemeral_containers_config_references_are_extracted() -> None:
    """Ephemeral (debug) containers declare `envFrom`/`env[].valueFrom` too.

    Skipping them hides a real ConfigMap/Secret dependency while Pod
    coverage still reports `complete`; the same metadata-only extractor
    must read them, and still never retain a literal value or key.
    """
    manifest = {
        "metadata": {"name": "api-0", "namespace": "prod"},
        "spec": {
            "containers": [],
            "ephemeralContainers": [
                {
                    "name": "debugger",
                    "envFrom": [
                        {"secretRef": {"name": "debug-creds"}},
                        {"configMapRef": {"name": "debug-config"}},
                    ],
                    "env": [
                        {
                            "name": "TOKEN",
                            "valueFrom": {"secretKeyRef": {"name": "api-creds", "key": "token"}},
                        },
                        {
                            "name": "TUNE",
                            "valueFrom": {"configMapKeyRef": {"name": "api-tune", "key": "level"}},
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
    assert (RelationKind.USES_CONFIG, "Secret", "debug-creds") in pairs
    assert (RelationKind.USES_CONFIG, "ConfigMap", "debug-config") in pairs
    assert (RelationKind.USES_CONFIG, "Secret", "api-creds") in pairs
    assert (RelationKind.USES_CONFIG, "ConfigMap", "api-tune") in pairs
    fields = {fact.field for fact in facts.references}
    assert "spec.ephemeralContainers[0].envFrom[0].secretRef" in fields
    assert "spec.ephemeralContainers[0].env[0].valueFrom.secretKeyRef" in fields
    assert "must-not-be-retained" not in repr(facts)
    assert "token" not in repr(facts)
    assert "level" not in repr(facts)


def test_live_pod_node_name_is_observed_placement() -> None:
    """A live Pod's `spec.nodeName` stays an OBSERVED SCHEDULED_ON edge."""
    facts = extract_relationship_facts(
        "Pod",
        "",
        "v1",
        {"metadata": {"name": "api-0", "namespace": "prod"}, "spec": {"nodeName": "node-a"}},
    )
    scheduled = [fact for fact in facts.references if fact.relation is RelationKind.SCHEDULED_ON]
    assert len(scheduled) == 1
    assert scheduled[0].target.kind == "Node"
    assert scheduled[0].target.name == "node-a"
    assert scheduled[0].confidence is FactConfidence.OBSERVED
    assert scheduled[0].field == "spec.nodeName"


def test_workload_template_node_name_is_not_observed_placement() -> None:
    """`spec.template.spec.nodeName` is declarative template configuration.

    Emitting SCHEDULED_ON for it would claim the workload object itself is
    running on a node (an OBSERVED placement the workload never has), so
    every pod-template kind must skip it while keeping every other
    template-derived reference.
    """
    cases = [
        ("Deployment", "apps", {"template": {"spec": _placement_template_spec()}}),
        ("ReplicaSet", "apps", {"template": {"spec": _placement_template_spec()}}),
        ("StatefulSet", "apps", {"template": {"spec": _placement_template_spec()}}),
        ("DaemonSet", "apps", {"template": {"spec": _placement_template_spec()}}),
        ("Job", "batch", {"template": {"spec": _placement_template_spec()}}),
        (
            "CronJob",
            "batch",
            {"jobTemplate": {"spec": {"template": {"spec": _placement_template_spec()}}}},
        ),
    ]
    for kind, group, spec in cases:
        facts = extract_relationship_facts(
            kind, group, "v1", {"metadata": {"name": "wl", "namespace": "prod"}, "spec": spec}
        )
        relations = {fact.relation for fact in facts.references}
        assert RelationKind.SCHEDULED_ON not in relations, kind
        names = {(fact.relation, fact.target.kind, fact.target.name) for fact in facts.references}
        assert (RelationKind.USES_VOLUME, "PersistentVolumeClaim", "wl-data") in names, kind
        assert (RelationKind.USES_CONFIG, "Secret", "wl-pull") in names, kind
        assert (RelationKind.USES_CONFIG, "Secret", "wl-creds") in names, kind


def _placement_template_spec() -> dict[str, object]:
    """A pod template spec carrying `nodeName` plus real references."""
    return {
        "nodeName": "node-a",
        "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "wl-data"}}],
        "imagePullSecrets": [{"name": "wl-pull"}],
        "containers": [{"name": "app", "envFrom": [{"secretRef": {"name": "wl-creds"}}]}],
    }


def test_endpoint_target_ref_without_namespace_defaults_to_the_slice_namespace() -> None:
    """An omitted `targetRef.namespace` means the EndpointSlice's own
    namespace (Kubernetes ObjectReference semantics), not cluster scope —
    leaving it blank makes a same-namespace Pod unresolvable by name."""
    facts = extract_relationship_facts(
        "EndpointSlice",
        "discovery.k8s.io",
        "v1",
        {
            "metadata": {"name": "api-abc", "namespace": "prod"},
            "endpoints": [{"targetRef": {"apiVersion": "v1", "kind": "Pod", "name": "api-0"}}],
        },
    )
    assert len(facts.references) == 1
    assert facts.references[0].target.namespace == "prod"
    assert facts.references[0].relation is RelationKind.ROUTES_TO


def test_endpoint_target_ref_namespace_stays_authoritative_when_present() -> None:
    """An explicit namespace wins, including a genuinely cross-namespace
    one — which the graph then judges under its own cross-namespace rules."""
    facts = extract_relationship_facts(
        "EndpointSlice",
        "discovery.k8s.io",
        "v1",
        {
            "metadata": {"name": "api-abc", "namespace": "prod"},
            "endpoints": [
                {"targetRef": {"kind": "Pod", "namespace": "staging", "name": "api-0"}},
                {"targetRef": {"kind": "Pod", "namespace": "", "name": "api-1"}},
            ],
        },
    )
    namespaces = [fact.target.namespace for fact in facts.references]
    assert namespaces == ["staging", "prod"]
