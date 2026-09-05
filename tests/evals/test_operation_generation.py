"""Deterministic metamorphic generation of operation instances.

A generated instance is what a campaign actually runs, so the invariants
here are the ones that decide whether the campaign grades the scenario it
reports: same seed - same instance, the instance's own assertions and
fixtures still point at the generated target, and the RBAC rule still
denies the namespace the target moved to.
"""

from __future__ import annotations

import random
import re
from copy import deepcopy
from dataclasses import replace

import korvid.evals.operation_generation as operation_generation
from korvid.evals.operation import (
    OPERATION_SCHEMA_VERSION,
    bundled_operations_dir,
    load_operation_journeys,
)
from korvid.evals.operation_generation import generate_instance

_TEMPLATES = {journey.id: journey for journey in load_operation_journeys(bundled_operations_dir())}


def test_the_same_seed_reproduces_the_same_instance() -> None:
    """A campaign publishes the seed as the reproduction recipe; a
    generator that drifted would make every published run unreplayable."""
    first, first_record = generate_instance(_TEMPLATES["scale-deployment-up"], 7)
    second, second_record = generate_instance(_TEMPLATES["scale-deployment-up"], 7)
    assert first == second
    assert first_record == second_record


def test_a_different_seed_moves_the_target_identity() -> None:
    identities: set[tuple[str, str]] = set()
    for seed in range(8):
        instance, _ = generate_instance(_TEMPLATES["scale-deployment-up"], seed)
        identities.add((instance.target.namespace, instance.target.name))
    assert len(identities) > 1


def test_the_instance_stays_internally_consistent() -> None:
    """Renaming moves the target's identity everywhere at once.

    An assertion, a manifest or a turn left on the template's identity
    grades a different object than the one the instance names.
    """
    instance, record = generate_instance(_TEMPLATES["scale-deployment-up"], 3)
    target = instance.target
    assert record.template_id == "scale-deployment-up"
    assert record.schema_version == OPERATION_SCHEMA_VERSION
    assert (record.namespace, record.name) == (target.namespace, target.name)
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


def test_the_rbac_rule_follows_the_generated_namespace() -> None:
    """A denial left on the template's namespace stops denying, and the
    campaign then grades a permitted write while reporting `scale-rbac-denied`."""
    instance, _ = generate_instance(_TEMPLATES["scale-rbac-denied"], 4)
    assert [rule.namespace for rule in instance.permission_denials] == [instance.target.namespace]


def test_every_shipped_template_generates_a_schema_valid_instance() -> None:
    """Generation is the only path a campaign template takes into a run; a
    template that cannot generate would first fail during a paid campaign."""
    for template_id in sorted(_TEMPLATES):
        instance, record = generate_instance(_TEMPLATES[template_id], 13)
        assert instance.id == f"{template_id}-s13"
        assert record.seed == 13
        assert record.template_id == template_id
        assert len(instance.target.name) <= 253
        assert all(
            re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", label) and len(label) <= 63
            for label in instance.target.name.split(".")
        )
        identities = [
            (
                str(manifest.get("apiVersion") or "").rpartition("/")[0],
                manifest.get("kind"),
                manifest["metadata"].get("namespace"),
                manifest["metadata"].get("name"),
            )
            for manifest in instance.cluster.objects
        ]
        uids = [manifest["metadata"].get("uid") for manifest in instance.cluster.objects]
        assert record.distractors
        assert len(set(identities)) == len(identities)
        assert all(uids)
        assert len(set(uids)) == len(uids)


def test_generated_target_name_remains_a_dns_subdomain_after_truncation() -> None:
    target = replace(
        _TEMPLATES["scale-deployment-up"].target,
        name=f"{'a' * 64}.checkout",
    )

    generated = operation_generation._generated_name(random.Random(7), target, set())

    assert len(generated) <= 253
    assert all(
        re.fullmatch(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?", label) and len(label) <= 63
        for label in generated.split(".")
    )


def test_generated_distractor_does_not_collide_with_an_existing_peer() -> None:
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
    identities = [
        (
            manifest.get("apiVersion"),
            manifest.get("kind"),
            manifest["metadata"].get("namespace"),
            manifest["metadata"].get("name"),
        )
        for manifest in instance.cluster.objects
    ]
    assert record.distractors
    assert len(set(identities)) == len(identities)
