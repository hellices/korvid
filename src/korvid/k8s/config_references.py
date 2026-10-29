"""The ConfigMap and Secret references a Pod spec makes (#410).

One extraction, shared by the relationship graph (`relationship_facts`) and
the propagation view (`korvid.core.config_consumers`), so the two cannot
disagree about which fields name a configuration object. It reads only the
fields that name one: `envFrom`, `env[].valueFrom`, `configMap`, `secret` and
`projected` volumes, and `imagePullSecrets`. It never reads `env[].value`,
`command` or `args`, so no literal value reaches a reference.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ReferenceSource(StrEnum):
    """The kind of field a reference was read from."""

    ENV_FROM = "env_from"
    ENV_KEY = "env_key"
    VOLUME = "volume"
    IMAGE_PULL = "image_pull"


@dataclass(frozen=True, slots=True)
class ConfigReference:
    """One reference to a ConfigMap or Secret, in the referring object's namespace.

    Attributes:
        kind: `ConfigMap` or `Secret`.
        name: The referenced object's name.
        field: The path of the field that names it.
        source: The kind of field.
        optional: Whether the reference is marked `optional: true`.
        env_name: The environment variable, for an `ENV_KEY` reference.
        key: The key read, for an `ENV_KEY` reference.
        volume: The volume's name, for a `VOLUME` reference.
    """

    kind: str
    name: str
    field: str
    source: ReferenceSource
    optional: bool = False
    env_name: str | None = None
    key: str | None = None
    volume: str | None = None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _name(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _reference(
    ref: Mapping[str, Any],
    name_key: str,
    kind: str,
    field: str,
    source: ReferenceSource,
    **detail: str | None,
) -> list[ConfigReference]:
    name = _name(ref.get(name_key))
    if name is None:
        return []
    optional = ref.get("optional") is True
    return [ConfigReference(kind, name, field, source, optional, **detail)]


def container_references(container: Mapping[str, Any], field: str) -> list[ConfigReference]:
    """The `envFrom` and `env[].valueFrom` references of one container at `field`."""
    references: list[ConfigReference] = []
    env_from = container.get("envFrom")
    for index, entry in enumerate(env_from if isinstance(env_from, list) else []):
        entry_field = f"{field}.envFrom[{index}]"
        for key, kind in (("secretRef", "Secret"), ("configMapRef", "ConfigMap")):
            ref = _mapping(_mapping(entry).get(key))
            references += _reference(
                ref, "name", kind, f"{entry_field}.{key}", ReferenceSource.ENV_FROM
            )
    env = container.get("env")
    for index, var in enumerate(env if isinstance(env, list) else []):
        value_from = _mapping(_mapping(var).get("valueFrom"))
        env_name = _name(_mapping(var).get("name"))
        for key, kind in (("secretKeyRef", "Secret"), ("configMapKeyRef", "ConfigMap")):
            ref = _mapping(value_from.get(key))
            references += _reference(
                ref,
                "name",
                kind,
                f"{field}.env[{index}].valueFrom.{key}",
                ReferenceSource.ENV_KEY,
                env_name=env_name,
                key=_name(ref.get("key")),
            )
    return references


def volume_references(volume: Mapping[str, Any], field: str) -> list[ConfigReference]:
    """The ConfigMap and Secret references of one volume at `field`.

    A `secret` volume names its object with `secretName`; a projected
    `secret` source with `name`.
    """
    volume_name = _name(volume.get("name"))
    references = _reference(
        _mapping(volume.get("configMap")),
        "name",
        "ConfigMap",
        f"{field}.configMap",
        ReferenceSource.VOLUME,
        volume=volume_name,
    )
    references += _reference(
        _mapping(volume.get("secret")),
        "secretName",
        "Secret",
        f"{field}.secret",
        ReferenceSource.VOLUME,
        volume=volume_name,
    )
    sources = _mapping(volume.get("projected")).get("sources")
    for index, source in enumerate(sources if isinstance(sources, list) else []):
        source_field = f"{field}.projected.sources[{index}]"
        for key, kind in (("configMap", "ConfigMap"), ("secret", "Secret")):
            ref = _mapping(_mapping(source).get(key))
            references += _reference(
                ref,
                "name",
                kind,
                f"{source_field}.{key}",
                ReferenceSource.VOLUME,
                volume=volume_name,
            )
    return references


def image_pull_secret_references(pull_secrets: object, base: str) -> list[ConfigReference]:
    """The `imagePullSecrets` of the Pod spec at `base`; they belong to no container."""
    references: list[ConfigReference] = []
    for index, entry in enumerate(pull_secrets if isinstance(pull_secrets, list) else []):
        references += _reference(
            _mapping(entry),
            "name",
            "Secret",
            f"{base}.imagePullSecrets[{index}]",
            ReferenceSource.IMAGE_PULL,
        )
    return references
