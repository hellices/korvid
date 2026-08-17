from __future__ import annotations

from korvid.core.resize_impact import ResizeImpactContext, classify_pod_resize


def _manifest(*, policy: object = None, memory_limit: str = "512Mi") -> dict[str, object]:
    container: dict[str, object] = {
        "name": "app",
        "resources": {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "1", "memory": memory_limit},
        },
    }
    if policy is not None:
        container["resizePolicy"] = policy
    return {"spec": {"containers": [container]}}


def test_cpu_change_uses_default_not_required_policy() -> None:
    context = classify_pod_resize(
        _manifest(),
        {"app": {"requests": {"cpu": "200m"}}},
    )
    assert context == ResizeImpactContext(
        cpu_changed=True,
        memory_request_changed=False,
        memory_limit_changed=False,
        restart_required=False,
        restart_policy_unknown=False,
        all_changed_resources_not_required=True,
        memory_limit_decreased=False,
        memory_limit_decrease_not_required=False,
        memory_limit_assessment_unknown=False,
    )


def test_memory_limit_decrease_with_not_required_is_identified_numerically() -> None:
    context = classify_pod_resize(
        _manifest(memory_limit="1Gi"),
        {"app": {"limits": {"memory": "900Mi"}}},
    )
    assert context.memory_limit_changed is True
    assert context.memory_limit_decreased is True
    assert context.memory_limit_decrease_not_required is True
    assert context.memory_limit_assessment_unknown is False


def test_equivalent_memory_quantities_are_not_a_decrease() -> None:
    context = classify_pod_resize(
        _manifest(memory_limit="1Gi"),
        {"app": {"limits": {"memory": "1024Mi"}}},
    )
    assert context.memory_limit_decreased is False
    assert context.memory_limit_decrease_not_required is False
    assert context.memory_limit_assessment_unknown is False


def test_restart_container_policy_is_scoped_to_the_changed_resource() -> None:
    context = classify_pod_resize(
        _manifest(
            policy=[
                {"resourceName": "cpu", "restartPolicy": "NotRequired"},
                {"resourceName": "memory", "restartPolicy": "RestartContainer"},
            ]
        ),
        {"app": {"limits": {"memory": "768Mi"}}},
    )
    assert context.restart_required is True
    assert context.all_changed_resources_not_required is False


def test_missing_container_is_unknown_not_optimistic() -> None:
    context = classify_pod_resize(
        _manifest(),
        {"missing": {"limits": {"memory": "256Mi"}}},
    )
    assert context.restart_policy_unknown is True
    assert context.memory_limit_assessment_unknown is True


def test_malformed_resize_policy_is_unknown() -> None:
    context = classify_pod_resize(
        _manifest(policy={"resourceName": "memory"}),
        {"app": {"requests": {"memory": "256Mi"}}},
    )
    assert context.restart_policy_unknown is True


def test_invalid_captured_memory_quantity_is_unknown() -> None:
    context = classify_pod_resize(
        _manifest(memory_limit="not-a-quantity"),
        {"app": {"limits": {"memory": "256Mi"}}},
    )
    assert context.memory_limit_assessment_unknown is True
