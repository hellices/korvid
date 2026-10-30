"""Project a Pod to its references to one ConfigMap or Secret, by mode (#410)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from korvid.core.config_consumers import (
    ConsumptionMode,
    ContainerCategory,
    PodIdentity,
    consumers_of,
    pod_consumers,
)

SETTINGS = {"configMap": {"name": "settings"}}
POD = PodIdentity("team-a", "api-1", "uid-api-1", "ReplicaSet", "api-7d9f")


def _pod(spec: dict[str, Any], **metadata: Any) -> dict[str, Any]:
    meta = {
        "name": "api-1",
        "namespace": "team-a",
        "uid": "uid-api-1",
        "ownerReferences": [
            {"kind": "Node", "name": "not-a-controller"},
            {"kind": "ReplicaSet", "name": "api-7d9f", "controller": True},
        ],
    }
    meta.update(metadata)
    return {"metadata": meta, "spec": spec}


def _modes(pod: dict[str, Any], kind: str = "ConfigMap", name: str = "settings") -> list[Any]:
    return [
        (consumer.mode, consumer.container, consumer.category)
        for consumer in pod_consumers(pod, kind, name)
    ]


def test_environment_references_are_read_at_start() -> None:
    pod = _pod(
        {
            "containers": [
                {
                    "name": "app",
                    "envFrom": [{"configMapRef": {"name": "settings"}}],
                    "env": [
                        {"name": "LITERAL", "value": "s3cr3t-literal"},
                        {
                            "name": "LOG_LEVEL",
                            "valueFrom": {
                                "configMapKeyRef": {
                                    "name": "settings",
                                    "key": "log-level",
                                    "optional": True,
                                }
                            },
                        },
                    ],
                }
            ]
        }
    )

    consumers = pod_consumers(pod, "ConfigMap", "settings")

    assert [(c.mode, c.path, c.env_name, c.key, c.optional) for c in consumers] == [
        (ConsumptionMode.ENV_FROM, "spec.containers[0].envFrom[0].configMapRef", None, None, False),
        (
            ConsumptionMode.ENV_KEY,
            "spec.containers[0].env[1].valueFrom.configMapKeyRef",
            "LOG_LEVEL",
            "log-level",
            True,
        ),
    ]
    assert {consumer.pod for consumer in consumers} == {POD}
    assert {(c.container, c.category) for c in consumers} == {("app", ContainerCategory.CONTAINER)}
    assert "s3cr3t-literal" not in repr(consumers)


def test_a_volume_mount_is_updated_and_a_sub_path_mount_is_not() -> None:
    pod = _pod(
        {
            "volumes": [{"name": "config", **SETTINGS}],
            "containers": [
                {"name": "app", "volumeMounts": [{"name": "config", "mountPath": "/etc/app"}]},
                {
                    "name": "worker",
                    "volumeMounts": [
                        {"name": "config", "mountPath": "/etc/w/app.yaml", "subPath": "app.yaml"},
                        {"name": "config", "mountPath": "/etc/w/x", "subPathExpr": "$(POD)"},
                        {"name": "other", "mountPath": "/data"},
                    ],
                },
            ],
        }
    )

    consumers = pod_consumers(pod, "ConfigMap", "settings")

    assert [(c.mode, c.container, c.volume, c.mount_path, c.sub_path) for c in consumers] == [
        (ConsumptionMode.VOLUME, "app", "config", "/etc/app", None),
        (ConsumptionMode.SUBPATH, "worker", "config", "/etc/w/app.yaml", "app.yaml"),
        (ConsumptionMode.SUBPATH, "worker", "config", "/etc/w/x", "$(POD)"),
    ]
    assert {c.path for c in consumers} == {"spec.volumes[0].configMap"}


def test_a_volume_no_container_mounts_is_a_pod_level_row() -> None:
    pod = _pod(
        {
            "volumes": [{"name": "config", **SETTINGS}],
            "containers": [{"name": "app", "volumeMounts": [{"name": "elsewhere"}]}],
        }
    )

    (consumer,) = pod_consumers(pod, "ConfigMap", "settings")

    assert consumer.mode is ConsumptionMode.UNMOUNTED
    assert (consumer.container, consumer.category, consumer.mount_path) == (None, None, None)
    assert consumer.volume == "config"


def test_projected_sources_and_secret_volumes_are_volumes() -> None:
    pod = _pod(
        {
            "volumes": [
                {"name": "bundle", "projected": {"sources": [{"secret": {"name": "creds"}}]}},
                {"name": "plain", "secret": {"secretName": "creds", "optional": True}},
            ],
            "containers": [
                {
                    "name": "app",
                    "volumeMounts": [
                        {"name": "bundle", "mountPath": "/bundle"},
                        {"name": "plain", "mountPath": "/plain"},
                    ],
                }
            ],
        }
    )

    consumers = pod_consumers(pod, "Secret", "creds")

    assert [(c.mode, c.volume, c.optional) for c in consumers] == [
        (ConsumptionMode.VOLUME, "bundle", False),
        (ConsumptionMode.VOLUME, "plain", True),
    ]


def test_container_categories_come_from_their_list() -> None:
    env_from = [{"configMapRef": {"name": "settings"}}]
    pod = _pod(
        {
            "containers": [{"name": "app", "envFrom": env_from}],
            "initContainers": [
                {"name": "setup", "envFrom": env_from},
                {"name": "proxy", "restartPolicy": "Always", "envFrom": env_from},
            ],
            "ephemeralContainers": [{"name": "debugger", "envFrom": env_from}],
        }
    )

    assert _modes(pod) == [
        (ConsumptionMode.ENV_FROM, "app", ContainerCategory.CONTAINER),
        (ConsumptionMode.ENV_FROM, "setup", ContainerCategory.INIT),
        (ConsumptionMode.ENV_FROM, "proxy", ContainerCategory.SIDECAR),
        (ConsumptionMode.ENV_FROM, "debugger", ContainerCategory.EPHEMERAL),
    ]


def test_an_init_container_mount_keeps_its_category() -> None:
    pod = _pod(
        {
            "volumes": [{"name": "config", **SETTINGS}],
            "initContainers": [
                {"name": "setup", "volumeMounts": [{"name": "config", "mountPath": "/c"}]}
            ],
        }
    )

    assert _modes(pod) == [(ConsumptionMode.VOLUME, "setup", ContainerCategory.INIT)]


def test_image_pull_secrets_are_pod_level_rows() -> None:
    pod = _pod({"imagePullSecrets": [{"name": "registry"}], "containers": [{"name": "app"}]})

    (consumer,) = pod_consumers(pod, "Secret", "registry")

    assert consumer.mode is ConsumptionMode.IMAGE_PULL
    assert (consumer.container, consumer.category) == (None, None)
    assert consumer.path == "spec.imagePullSecrets[0]"


def test_only_references_to_the_selected_object_are_kept() -> None:
    pod = _pod(
        {
            "volumes": [{"name": "config", "configMap": {"name": "other"}}],
            "containers": [
                {
                    "name": "app",
                    "envFrom": [
                        {"secretRef": {"name": "settings"}},
                        {"configMapRef": {"name": "settings-v2"}},
                    ],
                    "volumeMounts": [{"name": "config", "mountPath": "/c"}],
                }
            ],
            "imagePullSecrets": [{"name": "settings"}],
        }
    )

    assert pod_consumers(pod, "ConfigMap", "settings") == ()


def test_a_pod_without_a_controller_or_spec_is_still_identified() -> None:
    bare = {"metadata": {"name": "solo", "namespace": "team-a"}, "spec": {"volumes": "x"}}
    no_spec = {"metadata": {"name": "solo", "namespace": "team-a"}}

    assert pod_consumers(bare, "ConfigMap", "settings") == ()
    assert pod_consumers(no_spec, "ConfigMap", "settings") == ()
    pod = _pod({"containers": [{"envFrom": [{"configMapRef": {"name": "settings"}}]}]})
    pod["metadata"]["ownerReferences"] = "broken"
    (consumer,) = pod_consumers(pod, "ConfigMap", "settings")
    assert (consumer.pod.owner_kind, consumer.pod.owner_name, consumer.container) == (
        None,
        None,
        None,
    )


def test_malformed_containers_and_mounts_are_skipped() -> None:
    pod = _pod(
        {
            "volumes": [{"name": "config", **SETTINGS}],
            "containers": [
                "app",
                {"name": "app", "volumeMounts": ["config", {"name": "config", "subPath": 7}]},
            ],
        }
    )

    assert [
        (c.mode, c.sub_path, c.mount_path) for c in pod_consumers(pod, "ConfigMap", "settings")
    ] == [(ConsumptionMode.VOLUME, None, None)]


def test_consumers_of_keeps_pod_order() -> None:
    env_from = [{"configMapRef": {"name": "settings"}}]
    first = _pod({"containers": [{"name": "a", "envFrom": env_from}]}, name="first")
    second = _pod({"containers": [{"name": "b", "envFrom": env_from}]}, name="second")

    consumers = consumers_of([first, {"metadata": "broken"}, second], "ConfigMap", "settings")

    assert [consumer.pod.name for consumer in consumers] == ["first", "second"]


def _with_status(spec: dict[str, Any], status: dict[str, Any]) -> dict[str, Any]:
    pod = _pod(spec)
    pod["status"] = status
    return pod


def _running(name: str, started: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "state": {"running": {"startedAt": started}}, **extra}


def test_each_container_gets_its_running_start_from_its_own_status_list() -> None:
    env_from = [{"configMapRef": {"name": "settings"}}]
    pod = _with_status(
        {
            "containers": [{"name": "app", "envFrom": env_from}],
            "initContainers": [{"name": "proxy", "restartPolicy": "Always", "envFrom": env_from}],
            "ephemeralContainers": [{"name": "debugger", "envFrom": env_from}],
        },
        {
            "containerStatuses": [_running("app", "2026-10-30T09:00:00Z")],
            "initContainerStatuses": [_running("proxy", "2026-10-30T08:59:00Z")],
            "ephemeralContainerStatuses": [_running("debugger", "2026-10-30T09:05:00Z")],
        },
    )

    started = {c.container: c.started_at for c in pod_consumers(pod, "ConfigMap", "settings")}

    assert started == {
        "app": datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
        "proxy": datetime(2026, 10, 30, 8, 59, tzinfo=UTC),
        "debugger": datetime(2026, 10, 30, 9, 5, tzinfo=UTC),
    }


def test_a_status_from_another_list_or_container_is_not_used() -> None:
    pod = _with_status(
        {
            "containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "settings"}}]}],
        },
        {
            "initContainerStatuses": [_running("app", "2026-10-30T09:00:00Z")],
            "containerStatuses": [_running("other", "2026-10-30T09:00:00Z")],
        },
    )

    (consumer,) = pod_consumers(pod, "ConfigMap", "settings")

    assert consumer.started_at is None


def test_only_a_running_container_has_a_start() -> None:
    env_from = [{"configMapRef": {"name": "settings"}}]
    containers = [
        {"name": name, "envFrom": env_from} for name in ("waiting", "done", "bad", "none")
    ]
    pod = _with_status(
        {"containers": containers},
        {
            "containerStatuses": [
                {"name": "waiting", "state": {"waiting": {"reason": "ContainerCreating"}}},
                {
                    "name": "done",
                    "state": {"terminated": {"startedAt": "2026-10-30T09:00:00Z"}},
                    "lastState": {"running": {"startedAt": "2026-10-30T08:00:00Z"}},
                },
                _running("bad", "not-a-time"),
            ]
        },
    )

    started = [c.started_at for c in pod_consumers(pod, "ConfigMap", "settings")]

    assert started == [None, None, None, None]


def test_a_restarted_container_has_its_current_start() -> None:
    pod = _with_status(
        {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "settings"}}]}]},
        {
            "containerStatuses": [
                _running(
                    "app",
                    "2026-10-30T09:30:00Z",
                    restartCount=3,
                    lastState={"terminated": {"startedAt": "2026-10-30T09:00:00Z"}},
                )
            ]
        },
    )

    (consumer,) = pod_consumers(pod, "ConfigMap", "settings")

    assert consumer.started_at == datetime(2026, 10, 30, 9, 30, tzinfo=UTC)


def test_nothing_else_of_the_status_reaches_the_result() -> None:
    pod = _with_status(
        {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "settings"}}]}]},
        {
            "message": "status-message-literal",
            "containerStatuses": [
                _running("app", "2026-10-30T09:00:00Z", image="registry/app:status-literal")
            ],
        },
    )

    consumers = pod_consumers(pod, "ConfigMap", "settings")

    assert "status-message-literal" not in repr(consumers)
    assert "status-literal" not in repr(consumers)


def test_a_start_without_a_time_zone_is_not_used() -> None:
    pod = _with_status(
        {"containers": [{"name": "app", "envFrom": [{"configMapRef": {"name": "settings"}}]}]},
        {"containerStatuses": [_running("app", "2026-10-30T09:00:00")]},
    )

    (consumer,) = pod_consumers(pod, "ConfigMap", "settings")

    assert consumer.started_at is None


def test_a_mounting_container_gets_its_start() -> None:
    pod = _with_status(
        {
            "volumes": [{"name": "config", "configMap": {"name": "settings"}}],
            "containers": [
                {"name": "app", "volumeMounts": [{"name": "config", "mountPath": "/etc/app"}]}
            ],
        },
        {"containerStatuses": [_running("app", "2026-10-30T09:00:00Z")]},
    )

    (consumer,) = pod_consumers(pod, "ConfigMap", "settings")

    assert consumer.started_at == datetime(2026, 10, 30, 9, 0, tzinfo=UTC)
