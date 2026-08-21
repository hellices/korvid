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
    return re.sub(rf"(?<![\w/]){re.escape(old)}(?![\w:/-])", new, text)


def _retarget(
    target: OperationTarget, old: OperationTarget, namespace: str, name: str
) -> OperationTarget:
    """Apply the same independent identity substitutions as fixture objects."""

    return replace(
        target,
        namespace=namespace if target.namespace == old.namespace else target.namespace,
        name=name if target.name == old.name else target.name,
    )


def _distractor(
    template: OperationJourney, namespace: str, name: str, index: int
) -> dict[str, Any]:
    target = template.target
    source = next(
        manifest
        for manifest in template.cluster.objects
        if manifest.get("kind") == target.kind
        and (manifest.get("metadata") or {}).get("name") == target.name
        and (manifest.get("metadata") or {}).get("namespace") == target.namespace
    )
    item = deepcopy(source)
    metadata = item.setdefault("metadata", {})
    metadata.update(
        {
            "name": name,
            "namespace": namespace,
            "uid": f"{target.kind.lower()}-{name}-{namespace}",
            "generation": 1,
            "resourceVersion": f"90{index}0",
            "creationTimestamp": "2026-07-27T00:30:00Z",
            "labels": {"app": f"idle-{index}"},
        }
    )
    return item


def _distractor_name(objects: list[dict[str, Any]], namespace: str, index: int) -> str:
    used = {
        str((manifest.get("metadata") or {}).get("name") or "")
        for manifest in objects
        if str((manifest.get("metadata") or {}).get("namespace") or "") == namespace
    }
    base = f"idle-{index}"
    candidate = base
    suffix = 1
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


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


def _generated_namespace(rng: random.Random, used: set[str]) -> str:
    pool = [candidate for candidate in _NAMESPACE_POOL if candidate not in used]
    if pool:
        return rng.choice(pool)
    base = f"korvid-eval-{rng.getrandbits(48):012x}"
    candidate = base
    suffix = 1
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _generated_name(rng: random.Random, template: OperationJourney) -> str:
    old = template.target
    used = {
        str((manifest.get("metadata") or {}).get("name") or "")
        for manifest in template.cluster.objects
    }
    start = rng.randrange(len(_NAME_SUFFIXES))
    for offset in range(len(_NAME_SUFFIXES)):
        suffix = _NAME_SUFFIXES[(start + offset) % len(_NAME_SUFFIXES)]
        candidate = f"{old.name}-{suffix}"
        if candidate not in used:
            return candidate
    base = old.name[:244].rstrip("-") or "object"
    while True:
        candidate = f"{base}-{rng.getrandbits(32):08x}"
        if candidate not in used:
            return candidate


def generate_instance(
    template: OperationJourney, seed: int
) -> tuple[OperationJourney, GenerationRecord]:
    """Build one deterministic instance of *template* for *seed*.

    Returns:
        The instance and its provenance record. The same seed always
        reproduces the same instance, byte for byte.
    """

    instance_id = f"{template.id}-s{seed}"
    if len(instance_id) > 63:
        raise ValueError(
            "generated instance id must be at most 63 characters; use a shorter template id or seed"
        )
    rng = random.Random(seed)
    old = template.target
    used = {
        str((manifest.get("metadata") or {}).get("namespace") or "")
        for manifest in template.cluster.objects
    }
    namespace = _generated_namespace(rng, used)
    name = _generated_name(rng, template)
    count = rng.randint(0, _MAX_DISTRACTORS)
    objects = list(_moved_objects(template, namespace, name))
    for index in range(1, count + 1):
        distractor_name = _distractor_name(objects, namespace, index)
        objects.append(_distractor(template, namespace, distractor_name, index))
    rng.shuffle(objects)
    target = replace(old, namespace=namespace, name=name)
    turns = tuple(
        _rename(_rename(text, old.name, name), old.namespace, namespace) for text in template.turns
    )
    instance = replace(
        template,
        id=instance_id,
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
