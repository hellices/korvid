"""Metadata-only Kubernetes relationship fact extraction (issue #281).

This module is the safety boundary for the operational relationship graph:
it walks a raw manifest and extracts *only* the metadata needed to describe
relationships between resources (owner references, label selectors, volume
and config references, routing backends, node scheduling, and storage
bindings). It never retains secret ``data``/``stringData``, literal env
``value`` entries, container ``command``/``args``, annotations, or any other
manifest content unrelated to a relationship.

Every extractor is defensive: malformed lists/mappings are skipped rather
than raising, so a partially-invalid manifest still yields whatever facts
can be safely derived from it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from korvid.k8s.selectors import LabelSelector, parse_label_selector


class RelationKind(StrEnum):
    """The kind of relationship a fact describes."""

    OWNED_BY = "owned_by"
    SELECTS = "selects"
    ROUTES_TO = "routes_to"
    USES_VOLUME = "uses_volume"
    USES_CONFIG = "uses_config"
    MANAGED_BY = "managed_by"
    PROTECTED_BY = "protected_by"
    SCHEDULED_ON = "scheduled_on"
    BOUND_TO = "bound_to"


#: The one API group whose `*Route` kinds share the standard Gateway API
#: `spec.rules[].backendRefs[]` shape. A CRD in any other group (e.g.
#: OpenShift's `route.openshift.io/Route`, or an experimental
#: `gateway.networking.x-k8s.io` kind) is never interpreted with it.
GATEWAY_GROUP = "gateway.networking.k8s.io"


def is_gateway_route_kind(group: str, kind: str) -> bool:
    """True for a `gateway.networking.k8s.io` `*Route` kind.

    This is the single definition of "a Gateway Route" shared by the
    snapshot loader (which decides what to LIST) and the extractor below
    (which decides what `backendRefs` to read), so the loader can never
    report complete coverage of a kind no extractor understands.
    """
    return group == GATEWAY_GROUP and kind.endswith("Route")


class FactConfidence(StrEnum):
    """How a relationship fact was derived."""

    #: Read directly from a manifest's spec (owner refs, selectors, volumes).
    DECLARED = "declared"
    #: Read from live cluster state (status/targetRef), not authored config.
    OBSERVED = "observed"
    #: Derived by graph-building heuristics rather than read directly.
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class TargetReference:
    """An immutable, metadata-only pointer to another Kubernetes object."""

    group: str
    kind: str
    namespace: str
    name: str
    uid: str | None = None


@dataclass(frozen=True, slots=True)
class ReferenceFact:
    """A single directed relationship from the subject to a target object."""

    relation: RelationKind
    target: TargetReference
    confidence: FactConfidence
    field: str


@dataclass(frozen=True, slots=True)
class SelectorFact:
    """A label-selector-based relationship (e.g. Deployment/Service -> Pod)."""

    relation: RelationKind
    target_group: str
    target_kind: str
    selector: LabelSelector
    confidence: FactConfidence
    field: str
    #: What an *explicitly empty* selector means for this relation: pass to
    #: `korvid.k8s.selectors.matches_selector`'s `empty_matches` kwarg.
    empty_matches: bool = False
    #: True when the subject resource itself carries the selector (e.g.
    #: Deployment/PodDisruptionBudget); False when the subject is merely
    #: named by the selector's evaluation (e.g. Service).
    match_is_subject: bool = False


@dataclass(frozen=True, slots=True)
class ReferenceGrantFact:
    """A `ReferenceGrant`-style cross-namespace reference policy.

    `to_name` mirrors `spec.to[].name`, which is optional: `None` means the
    grant covers every object of `to_group`/`to_kind` in `namespace`, while
    a name narrows it to exactly that one object. It is never `""` — a
    blank or non-string name in the manifest is normalized to `None`
    (Kubernetes treats an empty name the same as an omitted one).
    """

    from_group: str
    from_kind: str
    from_namespace: str
    to_group: str
    to_kind: str
    namespace: str
    field: str
    to_name: str | None = None


@dataclass(frozen=True, slots=True)
class RelationshipFacts:
    """All relationship facts extracted from one manifest."""

    api_group: str = ""
    references: tuple[ReferenceFact, ...] = ()
    selectors: tuple[SelectorFact, ...] = ()
    grants: tuple[ReferenceGrantFact, ...] = ()


def _mapping(value: object) -> dict[str, Any]:
    """Return *value* as a `dict` when it is a mapping, else an empty dict."""
    return dict(value) if isinstance(value, Mapping) else {}


def _split_api_version(api_version: str) -> tuple[str, str]:
    """Split an `apiVersion` string into `(group, version)`.

    Core resources (`"v1"`) have no slash and produce group `""`; grouped
    resources (`"apps/v1"`) produce `("apps", "v1")`.
    """
    before, sep, after = api_version.partition("/")
    if sep:
        return before, after
    return "", before


def _owner_references(meta: Mapping[str, Any], namespace: str) -> list[ReferenceFact]:
    """`metadata.ownerReferences` -> `OWNED_BY` facts."""
    owners = meta.get("ownerReferences")
    if not isinstance(owners, list):
        return []
    references: list[ReferenceFact] = []
    for index, owner in enumerate(owners):
        if not isinstance(owner, Mapping):
            continue
        kind = owner.get("kind")
        name = owner.get("name")
        if not isinstance(kind, str) or not kind or not isinstance(name, str) or not name:
            continue
        group, _version = _split_api_version(str(owner.get("apiVersion") or ""))
        uid = owner.get("uid")
        references.append(
            ReferenceFact(
                relation=RelationKind.OWNED_BY,
                # Owner references never carry a namespace field; the owner
                # is always in the subject's own namespace (or cluster-scoped).
                target=TargetReference(group, kind, namespace, name, str(uid) if uid else None),
                confidence=FactConfidence.DECLARED,
                field=f"metadata.ownerReferences[{index}]",
            )
        )
    return references


def _container_refs(
    containers: object, namespace: str, base: str, list_field: str
) -> list[ReferenceFact]:
    """`envFrom` / `env[].valueFrom` config references for one container list."""
    if not isinstance(containers, list):
        return []
    references: list[ReferenceFact] = []
    for ci, container in enumerate(containers):
        if not isinstance(container, Mapping):
            continue
        env_from = container.get("envFrom")
        if isinstance(env_from, list):
            for ei, entry in enumerate(env_from):
                if not isinstance(entry, Mapping):
                    continue
                field = f"{base}.{list_field}[{ci}].envFrom[{ei}]"
                references.extend(_config_reference(entry, "secretRef", "Secret", namespace, field))
                references.extend(
                    _config_reference(entry, "configMapRef", "ConfigMap", namespace, field)
                )
        env = container.get("env")
        if isinstance(env, list):
            for vi, var in enumerate(env):
                if not isinstance(var, Mapping):
                    continue
                value_from = _mapping(var.get("valueFrom"))
                field = f"{base}.{list_field}[{ci}].env[{vi}].valueFrom"
                references.extend(
                    _config_reference(value_from, "secretKeyRef", "Secret", namespace, field)
                )
                references.extend(
                    _config_reference(value_from, "configMapKeyRef", "ConfigMap", namespace, field)
                )
    return references


def _config_reference(
    container: Mapping[str, Any], key: str, kind: str, namespace: str, field: str
) -> list[ReferenceFact]:
    """One `secretRef`/`configMapRef`-shaped `{"name": ...}` reference, if valid."""
    ref = _mapping(container.get(key))
    name = ref.get("name")
    if not isinstance(name, str) or not name:
        return []
    return [
        ReferenceFact(
            relation=RelationKind.USES_CONFIG,
            target=TargetReference("", kind, namespace, name),
            confidence=FactConfidence.DECLARED,
            field=f"{field}.{key}",
        )
    ]


def _volume_refs(volumes: object, namespace: str, base: str) -> list[ReferenceFact]:
    """`spec.volumes` -> `USES_VOLUME` / `USES_CONFIG` facts."""
    if not isinstance(volumes, list):
        return []
    references: list[ReferenceFact] = []
    for index, volume in enumerate(volumes):
        if not isinstance(volume, Mapping):
            continue
        field = f"{base}.volumes[{index}]"
        pvc = _mapping(volume.get("persistentVolumeClaim"))
        claim_name = pvc.get("claimName")
        if isinstance(claim_name, str) and claim_name:
            references.append(
                ReferenceFact(
                    relation=RelationKind.USES_VOLUME,
                    target=TargetReference("", "PersistentVolumeClaim", namespace, claim_name),
                    confidence=FactConfidence.DECLARED,
                    field=f"{field}.persistentVolumeClaim",
                )
            )
        config_map = _mapping(volume.get("configMap"))
        cm_name = config_map.get("name")
        if isinstance(cm_name, str) and cm_name:
            references.append(
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference("", "ConfigMap", namespace, cm_name),
                    confidence=FactConfidence.DECLARED,
                    field=f"{field}.configMap",
                )
            )
        secret = _mapping(volume.get("secret"))
        secret_name = secret.get("secretName")
        if isinstance(secret_name, str) and secret_name:
            references.append(
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference("", "Secret", namespace, secret_name),
                    confidence=FactConfidence.DECLARED,
                    field=f"{field}.secret",
                )
            )
        references.extend(_projected_volume_refs(volume.get("projected"), namespace, field))
    return references


def _projected_volume_refs(projected: object, namespace: str, field: str) -> list[ReferenceFact]:
    """`volume.projected.sources[]` -> `USES_CONFIG` facts."""
    sources = _mapping(projected).get("sources")
    if not isinstance(sources, list):
        return []
    references: list[ReferenceFact] = []
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            continue
        source_field = f"{field}.projected.sources[{index}]"
        config_map = _mapping(source.get("configMap"))
        cm_name = config_map.get("name")
        if isinstance(cm_name, str) and cm_name:
            references.append(
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference("", "ConfigMap", namespace, cm_name),
                    confidence=FactConfidence.DECLARED,
                    field=f"{source_field}.configMap",
                )
            )
        secret = _mapping(source.get("secret"))
        secret_name = secret.get("name")
        if isinstance(secret_name, str) and secret_name:
            references.append(
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference("", "Secret", namespace, secret_name),
                    confidence=FactConfidence.DECLARED,
                    field=f"{source_field}.secret",
                )
            )
    return references


def _image_pull_secret_refs(pull_secrets: object, namespace: str, base: str) -> list[ReferenceFact]:
    """`spec.imagePullSecrets` -> `USES_CONFIG` facts."""
    if not isinstance(pull_secrets, list):
        return []
    references: list[ReferenceFact] = []
    for index, entry in enumerate(pull_secrets):
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        references.append(
            ReferenceFact(
                relation=RelationKind.USES_CONFIG,
                target=TargetReference("", "Secret", namespace, name),
                confidence=FactConfidence.DECLARED,
                field=f"{base}.imagePullSecrets[{index}]",
            )
        )
    return references


def _pod_spec(pod_spec: Mapping[str, Any], namespace: str, base: str) -> list[ReferenceFact]:
    """Metadata-only references from a `PodSpec` mapping.

    Reads only volume references (including projected volume sources),
    `envFrom`, `env[].valueFrom` (across regular, init, and ephemeral
    containers), and `imagePullSecrets`. Never reads container
    `command`/`args`/`env[].value`, so literal values never reach a
    relationship fact.

    Node placement is deliberately *not* read here: a `nodeName` in a
    workload's pod template is declarative configuration, not an observed
    placement of the workload object itself. See `_node_placement`, which
    only the live-Pod handler calls.
    """
    references: list[ReferenceFact] = []
    references.extend(_volume_refs(pod_spec.get("volumes"), namespace, base))
    references.extend(_image_pull_secret_refs(pod_spec.get("imagePullSecrets"), namespace, base))
    references.extend(_container_refs(pod_spec.get("containers"), namespace, base, "containers"))
    references.extend(
        _container_refs(pod_spec.get("initContainers"), namespace, base, "initContainers")
    )
    references.extend(
        _container_refs(pod_spec.get("ephemeralContainers"), namespace, base, "ephemeralContainers")
    )
    return references


def _node_placement(pod_spec: Mapping[str, Any], base: str) -> list[ReferenceFact]:
    """A live Pod's `spec.nodeName` -> an `OBSERVED` `SCHEDULED_ON` fact.

    Only a Pod is ever actually scheduled onto a Node. A workload's
    `spec.template.spec.nodeName` is a template constraint for the Pods it
    will create -- reading it here would claim the Deployment/Job object
    itself is running on that Node, which no controller ever observes.
    """
    node_name = pod_spec.get("nodeName")
    if not isinstance(node_name, str) or not node_name:
        return []
    return [
        ReferenceFact(
            relation=RelationKind.SCHEDULED_ON,
            # Nodes are cluster-scoped: namespace is always "".
            target=TargetReference("", "Node", "", node_name),
            confidence=FactConfidence.OBSERVED,
            field=f"{base}.nodeName",
        )
    ]


def _selector_fact(
    relation: RelationKind,
    target_group: str,
    target_kind: str,
    raw: object,
    field: str,
    *,
    match_is_subject: bool,
    empty_matches: bool,
) -> SelectorFact | None:
    """Build a `SelectorFact` from a standard `LabelSelector`-shaped mapping.

    Returns `None` when the selector is absent, or when it is explicitly
    empty and an empty selector carries no meaningful match (the common
    case for Deployment/Service selectors; PDB `policy/v1` is the one
    resource type where an empty selector legitimately means "all pods").
    """
    selector = parse_label_selector(raw)
    if not selector.present:
        return None
    has_content = bool(selector.match_labels or selector.match_expressions)
    if not has_content and not empty_matches:
        return None
    return SelectorFact(
        relation=relation,
        target_group=target_group,
        target_kind=target_kind,
        selector=selector,
        confidence=FactConfidence.DECLARED,
        field=field,
        empty_matches=empty_matches,
        match_is_subject=match_is_subject,
    )


def _workload_selector(spec: Mapping[str, Any]) -> list[SelectorFact]:
    """Deployment/ReplicaSet/StatefulSet/DaemonSet/Job `spec.selector` -> Pod.

    Workloads *manage* the pods their selector matches (they own the
    template that creates them), which is a stronger relation than a
    Service's traffic-routing `SELECTS`; hence `MANAGED_BY`.
    """
    fact = _selector_fact(
        RelationKind.MANAGED_BY,
        "",
        "Pod",
        spec.get("selector"),
        "spec.selector",
        match_is_subject=True,
        empty_matches=False,
    )
    return [fact] if fact is not None else []


def _service_selector(spec: Mapping[str, Any]) -> list[SelectorFact]:
    """Service `spec.selector` (a flat label map, not a `LabelSelector`) -> Pod."""
    raw = spec.get("selector")
    # Service selectors are a bare {key: value} map; wrap it so
    # parse_label_selector's matchLabels/matchExpressions shape applies.
    wrapped = {"matchLabels": raw} if isinstance(raw, Mapping) else raw
    fact = _selector_fact(
        RelationKind.SELECTS,
        "",
        "Pod",
        wrapped,
        "spec.selector",
        match_is_subject=False,
        empty_matches=False,
    )
    return [fact] if fact is not None else []


def _pdb_selector(spec: Mapping[str, Any], api_version: str) -> list[SelectorFact]:
    """PodDisruptionBudget `spec.selector` -> Pod.

    `policy/v1` gives an explicitly empty selector "matches every pod in
    the namespace" semantics; older `policy/v1beta1` selects no pods, same
    as any other empty selector.
    """
    fact = _selector_fact(
        RelationKind.PROTECTED_BY,
        "",
        "Pod",
        spec.get("selector"),
        "spec.selector",
        match_is_subject=True,
        empty_matches=api_version == "v1",
    )
    return [fact] if fact is not None else []


def _endpoint_targets(manifest: Mapping[str, Any]) -> list[ReferenceFact]:
    """EndpointSlice `endpoints[].targetRef` -> observed `ROUTES_TO` facts."""
    endpoints = manifest.get("endpoints")
    if not isinstance(endpoints, list):
        return []
    references: list[ReferenceFact] = []
    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, Mapping):
            continue
        target_ref = endpoint.get("targetRef")
        if not isinstance(target_ref, Mapping):
            continue
        kind = target_ref.get("kind")
        name = target_ref.get("name")
        if not isinstance(kind, str) or not kind or not isinstance(name, str) or not name:
            continue
        group, _version = _split_api_version(str(target_ref.get("apiVersion") or ""))
        namespace = str(target_ref.get("namespace") or "")
        uid = target_ref.get("uid")
        references.append(
            ReferenceFact(
                relation=RelationKind.ROUTES_TO,
                target=TargetReference(group, kind, namespace, name, str(uid) if uid else None),
                confidence=FactConfidence.OBSERVED,
                field=f"endpoints[{index}].targetRef",
            )
        )
    return references


def _ingress_backends(spec: Mapping[str, Any], namespace: str) -> list[ReferenceFact]:
    """Ingress `spec.defaultBackend` / `spec.rules[].http.paths[].backend` -> Service."""
    references: list[ReferenceFact] = []
    default_service = _mapping(_mapping(spec.get("defaultBackend")).get("service"))
    default_name = default_service.get("name")
    if isinstance(default_name, str) and default_name:
        references.append(
            ReferenceFact(
                relation=RelationKind.ROUTES_TO,
                target=TargetReference("", "Service", namespace, default_name),
                confidence=FactConfidence.DECLARED,
                field="spec.defaultBackend.service",
            )
        )
    rules = spec.get("rules")
    if not isinstance(rules, list):
        return references
    for ri, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            continue
        paths = _mapping(rule.get("http")).get("paths")
        if not isinstance(paths, list):
            continue
        for pi, path in enumerate(paths):
            if not isinstance(path, Mapping):
                continue
            service = _mapping(_mapping(path.get("backend")).get("service"))
            name = service.get("name")
            if not isinstance(name, str) or not name:
                continue
            references.append(
                ReferenceFact(
                    relation=RelationKind.ROUTES_TO,
                    target=TargetReference("", "Service", namespace, name),
                    confidence=FactConfidence.DECLARED,
                    field=f"spec.rules[{ri}].http.paths[{pi}].backend.service",
                )
            )
    return references


def _gateway_backends(spec: Mapping[str, Any], namespace: str) -> list[ReferenceFact]:
    """Gateway Route `spec.rules[].backendRefs[]` -> declared `ROUTES_TO` facts.

    Shared by every `gateway.networking.k8s.io` `*Route` kind (HTTPRoute,
    GRPCRoute, TLSRoute, TCPRoute, UDPRoute): the Gateway API defines
    `backendRefs` identically for all of them (`BackendRef` = a
    group/kind/namespace/name pointer defaulting to a same-namespace
    `Service`). Only those pointers are read; a rule whose shape does not
    match is skipped rather than guessed at.
    """
    rules = spec.get("rules")
    if not isinstance(rules, list):
        return []
    references: list[ReferenceFact] = []
    for ri, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            continue
        backend_refs = rule.get("backendRefs")
        if not isinstance(backend_refs, list):
            continue
        for bi, ref in enumerate(backend_refs):
            if not isinstance(ref, Mapping):
                continue
            name = ref.get("name")
            if not isinstance(name, str) or not name:
                continue
            group = str(ref.get("group") or "")
            kind = str(ref.get("kind") or "Service")
            ref_namespace = str(ref.get("namespace") or namespace)
            references.append(
                ReferenceFact(
                    relation=RelationKind.ROUTES_TO,
                    target=TargetReference(group, kind, ref_namespace, name),
                    confidence=FactConfidence.DECLARED,
                    field=f"spec.rules[{ri}].backendRefs[{bi}]",
                )
            )
    return references


def _reference_grants(spec: Mapping[str, Any], namespace: str) -> list[ReferenceGrantFact]:
    """ReferenceGrant `spec.from`/`spec.to` -> cross-namespace grant facts.

    `spec.to[].name` is carried through when it is a non-empty string and
    recorded as `None` otherwise, so an unnamed grant stays distinguishable
    from one naming a specific object.
    """
    from_refs = spec.get("from")
    to_refs = spec.get("to")
    if not isinstance(from_refs, list) or not isinstance(to_refs, list):
        return []
    grants: list[ReferenceGrantFact] = []
    for from_entry in from_refs:
        if not isinstance(from_entry, Mapping):
            continue
        from_kind = from_entry.get("kind")
        from_namespace = from_entry.get("namespace")
        if not isinstance(from_kind, str) or not from_kind:
            continue
        if not isinstance(from_namespace, str) or not from_namespace:
            continue
        from_group = str(from_entry.get("group") or "")
        for to_entry in to_refs:
            if not isinstance(to_entry, Mapping):
                continue
            to_kind = to_entry.get("kind")
            if not isinstance(to_kind, str) or not to_kind:
                continue
            to_group = str(to_entry.get("group") or "")
            to_name = to_entry.get("name")
            grants.append(
                ReferenceGrantFact(
                    from_group=from_group,
                    from_kind=from_kind,
                    from_namespace=from_namespace,
                    to_group=to_group,
                    to_kind=to_kind,
                    namespace=namespace,
                    field="spec",
                    to_name=to_name if isinstance(to_name, str) and to_name else None,
                )
            )
    return grants


def _pvc_binding(spec: Mapping[str, Any]) -> list[ReferenceFact]:
    """PersistentVolumeClaim `spec.volumeName` -> `BOUND_TO` a PersistentVolume."""
    volume_name = spec.get("volumeName")
    if not isinstance(volume_name, str) or not volume_name:
        return []
    return [
        ReferenceFact(
            relation=RelationKind.BOUND_TO,
            # PersistentVolumes are cluster-scoped: namespace is always "".
            target=TargetReference("", "PersistentVolume", "", volume_name),
            confidence=FactConfidence.DECLARED,
            field="spec.volumeName",
        )
    ]


def _pv_binding(spec: Mapping[str, Any]) -> list[ReferenceFact]:
    """PersistentVolume `spec.claimRef` -> `BOUND_TO` a PersistentVolumeClaim.

    `claimRef` is an `ObjectReference`, so it carries the bound claim's UID.
    Retaining it lets the graph's UID-first resolution report a binding to a
    deleted claim as missing instead of silently reattaching it to a
    replacement claim that merely reuses the name.
    """
    claim_ref = _mapping(spec.get("claimRef"))
    name = claim_ref.get("name")
    if not isinstance(name, str) or not name:
        return []
    claim_namespace = str(claim_ref.get("namespace") or "")
    uid = claim_ref.get("uid")
    return [
        ReferenceFact(
            relation=RelationKind.BOUND_TO,
            target=TargetReference(
                "", "PersistentVolumeClaim", claim_namespace, name, str(uid) if uid else None
            ),
            confidence=FactConfidence.DECLARED,
            field="spec.claimRef",
        )
    ]


#: Kind -> its owning API group, for workload kinds whose `spec.template.spec`
#: holds a PodSpec (and, except Job, whose `spec.selector` selects Pods).
_WORKLOAD_POD_TEMPLATE_GROUPS = {
    "Deployment": "apps",
    "ReplicaSet": "apps",
    "StatefulSet": "apps",
    "DaemonSet": "apps",
    "Job": "batch",
}

#: A kind-specific handler returns the `(references, selectors, grants)`
#: it contributes, given the subject's group/version/spec/manifest/namespace.
#: Each handler is group-gated internally so a CRD that happens to reuse a
#: well-known kind name (e.g. a custom "Service") never matches by accident.
_HandlerResult = tuple[list[ReferenceFact], list[SelectorFact], list[ReferenceGrantFact]]
_Handler = Callable[[str, str, Mapping[str, Any], Mapping[str, Any], str], _HandlerResult]


def _handle_pod(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    namespace: str,
) -> _HandlerResult:
    if group:
        return [], [], []
    references = _pod_spec(spec, namespace, "spec")
    references.extend(_node_placement(spec, "spec"))
    return references, [], []


def _make_workload_handler(kind: str) -> _Handler:
    expected_group = _WORKLOAD_POD_TEMPLATE_GROUPS[kind]

    def _handler(
        group: str,
        _api_version: str,
        spec: Mapping[str, Any],
        _manifest: Mapping[str, Any],
        namespace: str,
    ) -> _HandlerResult:
        if group != expected_group:
            return [], [], []
        selectors = _workload_selector(spec)
        template_spec = _mapping(_mapping(spec.get("template")).get("spec"))
        references = _pod_spec(template_spec, namespace, "spec.template.spec")
        return references, selectors, []

    return _handler


def _handle_cronjob(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    namespace: str,
) -> _HandlerResult:
    if group != "batch":
        return [], [], []
    job_spec = _mapping(_mapping(spec.get("jobTemplate")).get("spec"))
    template_spec = _mapping(_mapping(job_spec.get("template")).get("spec"))
    references = _pod_spec(template_spec, namespace, "spec.jobTemplate.spec.template.spec")
    return references, [], []


def _handle_service(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    _namespace: str,
) -> _HandlerResult:
    if group:
        return [], [], []
    return [], _service_selector(spec), []


def _handle_pdb(
    group: str,
    api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    _namespace: str,
) -> _HandlerResult:
    if group != "policy":
        return [], [], []
    return [], _pdb_selector(spec, api_version), []


def _handle_endpoint_slice(
    group: str,
    _api_version: str,
    _spec: Mapping[str, Any],
    manifest: Mapping[str, Any],
    _namespace: str,
) -> _HandlerResult:
    if group != "discovery.k8s.io":
        return [], [], []
    return _endpoint_targets(manifest), [], []


def _handle_ingress(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    namespace: str,
) -> _HandlerResult:
    if group != "networking.k8s.io":
        return [], [], []
    return _ingress_backends(spec, namespace), [], []


def _handle_gateway_route(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    namespace: str,
) -> _HandlerResult:
    if group != GATEWAY_GROUP:
        return [], [], []
    return _gateway_backends(spec, namespace), [], []


def _handle_reference_grant(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    namespace: str,
) -> _HandlerResult:
    if group != GATEWAY_GROUP:
        return [], [], []
    return [], [], _reference_grants(spec, namespace)


def _handle_pvc(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    _namespace: str,
) -> _HandlerResult:
    if group:
        return [], [], []
    return _pvc_binding(spec), [], []


def _handle_pv(
    group: str,
    _api_version: str,
    spec: Mapping[str, Any],
    _manifest: Mapping[str, Any],
    _namespace: str,
) -> _HandlerResult:
    if group:
        return [], [], []
    return _pv_binding(spec), [], []


_KIND_HANDLERS: dict[str, _Handler] = {
    "Pod": _handle_pod,
    "Deployment": _make_workload_handler("Deployment"),
    "ReplicaSet": _make_workload_handler("ReplicaSet"),
    "StatefulSet": _make_workload_handler("StatefulSet"),
    "DaemonSet": _make_workload_handler("DaemonSet"),
    "Job": _make_workload_handler("Job"),
    "CronJob": _handle_cronjob,
    "Service": _handle_service,
    "PodDisruptionBudget": _handle_pdb,
    "EndpointSlice": _handle_endpoint_slice,
    "Ingress": _handle_ingress,
    "ReferenceGrant": _handle_reference_grant,
    "PersistentVolumeClaim": _handle_pvc,
    "PersistentVolume": _handle_pv,
}


def _handler_for(kind: str, group: str) -> _Handler | None:
    """The handler for `kind`, or the shared Gateway Route one.

    Gateway `*Route` kinds are resolved by group+suffix rather than by an
    enumerated name because the loader LISTs every discovered
    `gateway.networking.k8s.io` `*Route` — HTTPRoute, GRPCRoute, and the
    stream routes (TLSRoute/TCPRoute/UDPRoute) all declare backends in the
    same `spec.rules[].backendRefs[]` shape, and a kind-by-kind table would
    keep silently reporting complete coverage for the ones it forgot.
    """
    handler = _KIND_HANDLERS.get(kind)
    if handler is not None:
        return handler
    if is_gateway_route_kind(group, kind):
        return _handle_gateway_route
    return None


def extract_relationship_facts(
    kind: str, group: str, api_version: str, manifest: Mapping[str, Any]
) -> RelationshipFacts:
    """Extract metadata-only `RelationshipFacts` from a raw manifest.

    Args:
        kind: The Kubernetes kind name (e.g. `"Pod"`, `"Deployment"`).
        group: The resource's bare API group (`""` for core, `"apps"` for
            `apps/v1`, etc.) - the authoritative group, not parsed from the
            manifest, so callers must resolve it the same way discovery does.
        api_version: The resource's bare API version (e.g. `"v1"`,
            `"v1beta1"`), without the group prefix.
        manifest: The raw object manifest.

    Returns:
        A `RelationshipFacts` carrying only relationship metadata: never
        secret `data`/`stringData`, literal env values, commands, args, or
        annotations.
    """
    meta = _mapping(manifest.get("metadata"))
    namespace = str(meta.get("namespace") or "")
    spec = _mapping(manifest.get("spec"))

    references: list[ReferenceFact] = _owner_references(meta, namespace)
    selectors: list[SelectorFact] = []
    grants: list[ReferenceGrantFact] = []

    handler = _handler_for(kind, group)
    if handler is not None:
        extra_references, extra_selectors, extra_grants = handler(
            group, api_version, spec, manifest, namespace
        )
        references.extend(extra_references)
        selectors.extend(extra_selectors)
        grants.extend(extra_grants)

    return RelationshipFacts(
        api_group=group,
        references=tuple(references),
        selectors=tuple(selectors),
        grants=tuple(grants),
    )
