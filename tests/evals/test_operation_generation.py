"""Deterministic metamorphic generation of operation instances."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

import korvid.evals.operation_generation as operation_generation
from korvid.evals.operation import (
    OPERATION_SCHEMA_VERSION,
    bundled_operations_dir,
    load_operation_journeys,
)
from korvid.evals.operation_generation import generate_instance

_TEMPLATES = {journey.id: journey for journey in load_operation_journeys(bundled_operations_dir())}


def test_the_same_seed_reproduces_the_same_instance() -> None:
    first, first_record = generate_instance(_TEMPLATES["scale-deployment-up"], 7)
    second, second_record = generate_instance(_TEMPLATES["scale-deployment-up"], 7)
    assert first == second
    assert first_record == second_record


def test_different_seeds_move_the_target_identity() -> None:
    first, _ = generate_instance(_TEMPLATES["scale-deployment-up"], 1)
    second, _ = generate_instance(_TEMPLATES["scale-deployment-up"], 2)
    assert (first.target.namespace, first.target.name) != (
        second.target.namespace,
        second.target.name,
    )


def test_exhausted_namespace_pool_uses_a_noncolliding_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = _TEMPLATES["scale-deployment-up"]
    monkeypatch.setattr(operation_generation, "_NAMESPACE_POOL", (template.target.namespace,))
    first, _ = generate_instance(template, 7)
    second, _ = generate_instance(template, 7)
    used = {
        str((manifest.get("metadata") or {}).get("namespace") or "")
        for manifest in template.cluster.objects
    }
    assert first.target.namespace == second.target.namespace
    assert first.target.namespace not in used


def test_assertion_identity_dimensions_follow_generated_objects_independently() -> None:
    template = _TEMPLATES["scale-deployment-up"]
    same_name = replace(
        template.preconditions[0],
        target=replace(template.target, namespace="shop-b"),
    )
    same_namespace = replace(
        template.preconditions[0],
        target=replace(template.target, name="inventory"),
    )
    instance, _ = generate_instance(
        replace(template, preconditions=(same_name, same_namespace)),
        7,
    )
    generated_same_name, generated_same_namespace = instance.preconditions
    assert generated_same_name.target.namespace == "shop-b"
    assert generated_same_name.target.name == instance.target.name
    assert generated_same_namespace.target.namespace == instance.target.namespace
    assert generated_same_namespace.target.name == "inventory"


def test_generated_target_name_does_not_collide_with_an_existing_peer() -> None:
    template = _TEMPLATES["scale-deployment-up"]
    peer = deepcopy(template.cluster.objects[0])
    peer["metadata"] = {
        **peer["metadata"],
        "name": f"{template.target.name}-blue",
        "uid": "deployment-existing-blue",
    }
    custom = replace(
        template,
        cluster=replace(template.cluster, objects=(*template.cluster.objects, peer)),
    )
    instance, _ = generate_instance(custom, 2)
    identities = [
        (
            manifest["apiVersion"],
            manifest["kind"],
            manifest["metadata"]["namespace"],
            manifest["metadata"]["name"],
        )
        for manifest in instance.cluster.objects
    ]
    assert len(identities) == len(set(identities))


def test_generated_name_is_unique_in_every_renamed_namespace() -> None:
    template = _TEMPLATES["scale-ambiguous-namespace"]
    peer = deepcopy(template.cluster.objects[0])
    peer["metadata"] = {
        **peer["metadata"],
        "name": f"{template.target.name}-green",
        "namespace": "shop-a",
        "uid": "deployment-existing-green",
    }
    custom = replace(
        template,
        cluster=replace(template.cluster, objects=(*template.cluster.objects, peer)),
    )
    instance, _ = generate_instance(custom, 2)
    identities = [
        (
            manifest["apiVersion"],
            manifest["kind"],
            manifest["metadata"]["namespace"],
            manifest["metadata"]["name"],
        )
        for manifest in instance.cluster.objects
    ]
    assert len(identities) == len(set(identities))


def test_generated_distractor_name_does_not_collide_with_an_existing_peer() -> None:
    template = _TEMPLATES["scale-statefulset-down"]
    peer = deepcopy(template.cluster.objects[0])
    peer["metadata"] = {
        **peer["metadata"],
        "name": "idle-1",
        "uid": "statefulset-existing-idle-1",
    }
    custom = replace(
        template,
        cluster=replace(template.cluster, objects=(*template.cluster.objects, peer)),
    )
    instance, record = generate_instance(custom, 0)
    assert record.distractors > 0
    identities = [
        (
            manifest["apiVersion"],
            manifest["kind"],
            manifest["metadata"]["namespace"],
            manifest["metadata"]["name"],
        )
        for manifest in instance.cluster.objects
    ]
    assert len(identities) == len(set(identities))


@pytest.mark.parametrize(
    ("template_id", "seed"),
    [
        ("a" * 63, 1),
        ("scale-deployment-up", int("9" * 300)),
    ],
)
def test_generated_instance_id_fails_before_exceeding_the_schema_limit(
    template_id: str, seed: int
) -> None:
    template = replace(_TEMPLATES["scale-deployment-up"], id=template_id)
    with pytest.raises(ValueError, match="generated instance id must be at most 63 characters"):
        generate_instance(template, seed)


def test_the_instance_stays_internally_consistent() -> None:
    instance, record = generate_instance(_TEMPLATES["scale-deployment-up"], 3)
    target = instance.target
    assert record.template_id == "scale-deployment-up"
    assert record.schema_version == OPERATION_SCHEMA_VERSION
    assert record.namespace == target.namespace
    assert record.name == target.name
    assert instance.id == "scale-deployment-up-s3"
    assert all(
        assertion.target == target for assertion in instance.preconditions + instance.postconditions
    )
    matched = [
        manifest
        for manifest in instance.cluster.objects
        if manifest["metadata"]["name"] == target.name
        and manifest["metadata"]["namespace"] == target.namespace
        and manifest["metadata"]["uid"] == target.uid
    ]
    assert len(matched) == 1
    assert target.name in instance.turns[0]
    assert target.namespace in instance.turns[0]


def test_distractors_never_collide_with_the_target() -> None:
    instance, record = generate_instance(_TEMPLATES["scale-deployment-up"], 11)
    assert record.distractors >= 0
    names = [manifest["metadata"]["name"] for manifest in instance.cluster.objects]
    assert names.count(instance.target.name) == 1
    assert len(set(names)) == len(names)


@pytest.mark.parametrize(
    "template_id",
    ["scale-statefulset-down", "restart-daemonset"],
)
def test_distractors_are_visible_in_the_target_kind_listing(template_id: str) -> None:
    instance, record = generate_instance(_TEMPLATES[template_id], 0)
    assert record.distractors > 0
    distractors = [
        manifest
        for manifest in instance.cluster.objects
        if manifest["metadata"]["name"] != instance.target.name
    ]
    assert len(distractors) == record.distractors
    assert {manifest["kind"] for manifest in distractors} == {instance.target.kind}


def test_the_ambiguity_template_keeps_both_same_named_copies() -> None:
    instance, _ = generate_instance(_TEMPLATES["scale-ambiguous-namespace"], 5)
    same_named = [
        manifest
        for manifest in instance.cluster.objects
        if manifest["metadata"]["name"] == instance.target.name
    ]
    assert len(same_named) == 2
    assert len({manifest["metadata"]["namespace"] for manifest in same_named}) == 2


def test_the_rbac_rule_follows_the_generated_namespace() -> None:
    instance, _ = generate_instance(_TEMPLATES["scale-rbac-denied"], 4)
    assert [rule.namespace for rule in instance.permission_denials] == [instance.target.namespace]


def test_a_declared_dialog_intervention_survives_generation() -> None:
    """Identity moves; graded semantics do not. The replacement uid is
    still the one the fixture declared, and still not the target's."""

    template = _TEMPLATES["scale-same-name-replacement"]
    instance, _ = generate_instance(template, 9)
    assert template.dialog_intervention is not None
    assert instance.dialog_intervention == template.dialog_intervention
    assert instance.dialog_intervention.replace_target.uid != instance.target.uid


def test_standalone_identity_mentions_with_punctuation_still_rename() -> None:
    template = replace(
        _TEMPLATES["scale-deployment-up"],
        turns=("Scale checkout-a, then confirm checkout-a.",),
    )
    instance, _ = generate_instance(template, 9)
    assert instance.turns == (
        f"Scale {instance.target.name}, then confirm {instance.target.name}.",
    )


def test_edit_unsupported_preserves_the_image_token_while_renaming_the_target() -> None:
    instance, _ = generate_instance(_TEMPLATES["edit-unsupported"], 9)
    turn = instance.turns[0]
    assert f"Change the {instance.target.name} deployment image" in turn
    assert "registry.example.com/billing:9.9.9." in turn
    assert f"registry.example.com/{instance.target.name}:9.9.9." not in turn


@pytest.mark.parametrize("template_id", sorted(_TEMPLATES))
def test_every_template_generates(template_id: str) -> None:
    instance, record = generate_instance(_TEMPLATES[template_id], 13)
    assert instance.id == f"{template_id}-s13"
    assert record.seed == 13
