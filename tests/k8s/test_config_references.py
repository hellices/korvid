"""The ConfigMap and Secret references a Pod spec makes (#410)."""

from __future__ import annotations

from korvid.k8s.config_references import (
    ConfigReference,
    ReferenceSource,
    container_references,
    image_pull_secret_references,
    volume_references,
)

BASE = "spec.containers[0]"


def test_env_from_names_each_object_with_its_own_path() -> None:
    container = {
        "envFrom": [
            {"configMapRef": {"name": "settings"}},
            {"secretRef": {"name": "credentials", "optional": True}, "prefix": "DB_"},
        ]
    }

    assert container_references(container, BASE) == [
        ConfigReference(
            "ConfigMap", "settings", f"{BASE}.envFrom[0].configMapRef", ReferenceSource.ENV_FROM
        ),
        ConfigReference(
            "Secret",
            "credentials",
            f"{BASE}.envFrom[1].secretRef",
            ReferenceSource.ENV_FROM,
            optional=True,
        ),
    ]


def test_an_env_key_reference_keeps_the_variable_and_key_but_no_value() -> None:
    container = {
        "env": [
            {"name": "LITERAL", "value": "s3cr3t-literal"},
            {
                "name": "LOG_LEVEL",
                "valueFrom": {"configMapKeyRef": {"name": "settings", "key": "log-level"}},
            },
            {
                "name": "PASSWORD",
                "valueFrom": {
                    "secretKeyRef": {"name": "credentials", "key": "password", "optional": True}
                },
            },
        ]
    }

    references = container_references(container, BASE)

    assert references == [
        ConfigReference(
            "ConfigMap",
            "settings",
            f"{BASE}.env[1].valueFrom.configMapKeyRef",
            ReferenceSource.ENV_KEY,
            env_name="LOG_LEVEL",
            key="log-level",
        ),
        ConfigReference(
            "Secret",
            "credentials",
            f"{BASE}.env[2].valueFrom.secretKeyRef",
            ReferenceSource.ENV_KEY,
            optional=True,
            env_name="PASSWORD",
            key="password",
        ),
    ]
    assert "s3cr3t-literal" not in repr(references)


def test_a_secret_ref_comes_before_a_config_map_ref_in_one_env_from_entry() -> None:
    container = {"envFrom": [{"configMapRef": {"name": "a"}, "secretRef": {"name": "b"}}]}

    assert [ref.field for ref in container_references(container, BASE)] == [
        f"{BASE}.envFrom[0].secretRef",
        f"{BASE}.envFrom[0].configMapRef",
    ]


def test_malformed_container_entries_are_skipped() -> None:
    container = {
        "envFrom": [None, {"configMapRef": {"name": ""}}, {"secretRef": "credentials"}],
        "env": ["LOG_LEVEL", {"name": "X", "valueFrom": {"configMapKeyRef": {"name": 3}}}],
    }

    assert container_references(container, BASE) == []
    assert container_references({"envFrom": "settings", "env": {}}, BASE) == []


def test_config_map_and_secret_volumes_name_their_volume() -> None:
    field = "spec.volumes[2]"

    config_map = volume_references(
        {"name": "settings-volume", "configMap": {"name": "settings", "optional": True}}, field
    )
    secret = volume_references(
        {"name": "credentials-volume", "secret": {"secretName": "credentials"}}, field
    )

    assert config_map == [
        ConfigReference(
            "ConfigMap",
            "settings",
            f"{field}.configMap",
            ReferenceSource.VOLUME,
            optional=True,
            volume="settings-volume",
        )
    ]
    assert secret == [
        ConfigReference(
            "Secret",
            "credentials",
            f"{field}.secret",
            ReferenceSource.VOLUME,
            volume="credentials-volume",
        )
    ]


def test_projected_sources_are_volume_references_of_their_volume() -> None:
    volume = {
        "name": "bundle",
        "projected": {
            "sources": [
                {"serviceAccountToken": {"path": "token"}},
                {"configMap": {"name": "settings"}},
                {"secret": {"name": "credentials", "optional": True}},
            ]
        },
    }

    assert volume_references(volume, "spec.volumes[0]") == [
        ConfigReference(
            "ConfigMap",
            "settings",
            "spec.volumes[0].projected.sources[1].configMap",
            ReferenceSource.VOLUME,
            volume="bundle",
        ),
        ConfigReference(
            "Secret",
            "credentials",
            "spec.volumes[0].projected.sources[2].secret",
            ReferenceSource.VOLUME,
            optional=True,
            volume="bundle",
        ),
    ]


def test_a_volume_without_a_name_still_names_its_object() -> None:
    references = volume_references({"configMap": {"name": "settings"}}, "spec.volumes[0]")

    assert [(ref.name, ref.volume) for ref in references] == [("settings", None)]


def test_a_volume_that_names_no_object_has_no_reference() -> None:
    volumes = [
        {"name": "data", "persistentVolumeClaim": {"claimName": "data"}},
        {"name": "x", "secret": {"name": "wrong-field"}},
        {"name": "y", "projected": {"sources": "settings"}},
        {"name": "z", "configMap": None},
    ]

    assert [volume_references(volume, "spec.volumes[0]") for volume in volumes] == [[], [], [], []]


def test_image_pull_secrets_are_pod_level_references() -> None:
    pull_secrets = [{"name": "registry"}, {"name": ""}, "registry", {"name": "mirror"}]

    assert image_pull_secret_references(pull_secrets, "spec") == [
        ConfigReference(
            "Secret", "registry", "spec.imagePullSecrets[0]", ReferenceSource.IMAGE_PULL
        ),
        ConfigReference("Secret", "mirror", "spec.imagePullSecrets[3]", ReferenceSource.IMAGE_PULL),
    ]
    assert image_pull_secret_references(None, "spec") == []
