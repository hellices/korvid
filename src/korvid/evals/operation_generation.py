"""Deterministic metamorphic generation of operation-journey instances.

This is an open-source benchmark, so a committed secret holdout would not
be credible. Generalization comes from generating fresh instances of a
public semantic template: namespace, object name, target position, and
irrelevant healthy distractors move, while the graded semantics do not.
Only the concrete milestone instances are withheld operationally, and
every artifact records the template id and the seed that produced it.

Slice A varies identity and surroundings. Replica counts, approval
outcomes, and phrasing families stay fixed here so a generated instance
keeps a deterministic script; widening the generator is a later change
that must be made together with the scripts that drive it.

Shipped code: imports `korvid.evals.operation` and stdlib only.
"""

from __future__ import annotations

import random
import re
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from korvid.evals.operation import (
    OPERATION_SCHEMA_VERSION,
    OperationCluster,
    OperationJourney,
    OperationTarget,
    PermissionDenial,
    StateAssertion,
)

__all__ = ["GenerationRecord", "generate_instance"]

#: Namespaces a generated instance may move into. Anything already used by
#: the template is excluded, so a move can never merge two fixture objects.
_NAMESPACE_POOL = (
    "shop-a",
    "shop-b",
    "shop-c",
    "retail-a",
    "retail-b",
    "ops-a",
    "ops-b",
    "team-x",
)
_NAME_SUFFIXES = ("blue", "green", "teal", "amber", "slate", "ivory", "coral", "onyx")
_MAX_DISTRACTORS = 2


@dataclass(frozen=True)
class GenerationRecord:
    """Provenance for one generated instance."""

    template_id: str
    instance_id: str
    seed: int
    schema_version: int
    namespace: str
    name: str
    distractors: int


def _rename(text: str, old: str, new: str) -> str:
    return re.sub(rf"(?<!\w){re.escape(old)}(?!\w)", new, text)


def _retarget(
    target: OperationTarget, old: OperationTarget, namespace: str, name: str
) -> OperationTarget:
    """Move a target that points at the template's object; leave others."""

    if (target.namespace, target.name) != (old.namespace, old.name):
        return target
    return replace(target, namespace=namespace, name=name)


def _distractor(namespace: str, index: int) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": f"idle-{index}",
            "namespace": namespace,
            "uid": f"deployment-idle-{index}-{namespace}",
            "generation": 1,
            "resourceVersion": f"90{index}0",
            "creationTimestamp": "2026-07-27T00:30:00Z",
            "labels": {"app": f"idle-{index}"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": f"idle-{index}"}},
            "template": {
                "metadata": {"labels": {"app": f"idle-{index}"}},
                "spec": {
                    "containers": [{"name": "idle", "image": "registry.example.com/idle:1.0.0"}]
                },
            },
        },
        "status": {
            "replicas": 1,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "observedGeneration": 1,
        },
    }


def _moved_objects(
    template: OperationJourney, namespace: str, name: str
) -> tuple[dict[str, Any], ...]:
    """Rename every same-named copy and move the ones in the target
    namespace. Renaming all copies is what keeps the ambiguity template
    ambiguous after generation."""

    old = template.target
    moved: list[dict[str, Any]] = []
    for manifest in template.cluster.objects:
        item = deepcopy(manifest)
        metadata = item.setdefault("metadata", {})
        if str(metadata.get("name") or "") == old.name:
            metadata["name"] = name
        if str(metadata.get("namespace") or "") == old.namespace:
            metadata["namespace"] = namespace
        moved.append(item)
    return tuple(moved)


def _moved_assertions(
    assertions: tuple[StateAssertion, ...], old: OperationTarget, namespace: str, name: str
) -> tuple[StateAssertion, ...]:
    return tuple(
        replace(assertion, target=_retarget(assertion.target, old, namespace, name))
        for assertion in assertions
    )


def _moved_denials(
    denials: tuple[PermissionDenial, ...], old: OperationTarget, namespace: str
) -> tuple[PermissionDenial, ...]:
    return tuple(
        replace(rule, namespace=namespace) if rule.namespace == old.namespace else rule
        for rule in denials
    )


def generate_instance(
    template: OperationJourney, seed: int
) -> tuple[OperationJourney, GenerationRecord]:
    """Build one deterministic instance of *template* for *seed*.

    Returns:
        The instance and its provenance record. The same seed always
        reproduces the same instance, byte for byte.
    """

    rng = random.Random(seed)
    old = template.target
    used = {
        str((manifest.get("metadata") or {}).get("namespace") or "")
        for manifest in template.cluster.objects
    }
    pool = [candidate for candidate in _NAMESPACE_POOL if candidate not in used]
    namespace = rng.choice(pool)
    name = f"{old.name}-{rng.choice(_NAME_SUFFIXES)}"
    count = rng.randint(0, _MAX_DISTRACTORS)
    objects = list(_moved_objects(template, namespace, name))
    objects.extend(_distractor(namespace, index) for index in range(1, count + 1))
    rng.shuffle(objects)
    target = replace(old, namespace=namespace, name=name)
    turns = tuple(
        _rename(_rename(text, old.name, name), old.namespace, namespace) for text in template.turns
    )
    instance = replace(
        template,
        id=f"{template.id}-s{seed}",
        target=target,
        preconditions=_moved_assertions(template.preconditions, old, namespace, name),
        postconditions=_moved_assertions(template.postconditions, old, namespace, name),
        permission_denials=_moved_denials(template.permission_denials, old, namespace),
        turns=turns,
        cluster=OperationCluster(
            objects=tuple(objects),
            events=template.cluster.events,
            logs=dict(template.cluster.logs),
            forbidden=template.cluster.forbidden,
            reconcile_status=template.cluster.reconcile_status,
        ),
    )
    record = GenerationRecord(
        template_id=template.id,
        instance_id=instance.id,
        seed=seed,
        schema_version=OPERATION_SCHEMA_VERSION,
        namespace=namespace,
        name=name,
        distractors=count,
    )
    return instance, record
