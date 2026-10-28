"""Which Pod containers consume a ConfigMap or Secret, and how (#410).

A Pod is projected to its references to one object, each with the mode
that decides whether a change reaches the running container. The projection
reads only the fields `korvid.k8s.config_references` reads, plus each
container's name, `restartPolicy` and `volumeMounts`, and the Pod's identity
and controlling owner. No value of the Pod is kept.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from korvid.k8s.config_references import (
    ConfigReference,
    ReferenceSource,
    container_references,
    image_pull_secret_references,
    volume_references,
)


class ConsumptionMode(StrEnum):
    """How a container consumes a configuration object."""

    #: `envFrom`: every key as a variable, read when the container starts.
    ENV_FROM = "env_from"
    #: `env[].valueFrom`: one key as a variable, read when the container starts.
    ENV_KEY = "env_key"
    #: A mounted volume: updated in place by the kubelet, if the object is mutable.
    VOLUME = "volume"
    #: A `subPath` or `subPathExpr` mount: never updated.
    SUBPATH = "subpath"
    #: A volume no container mounts: nothing reads it.
    UNMOUNTED = "unmounted"
    #: An image pull secret: used only when an image is pulled.
    IMAGE_PULL = "image_pull"


class ContainerCategory(StrEnum):
    """Which list a container is in, and so when it runs."""

    CONTAINER = "container"
    #: An init container: runs to completion before the other containers start.
    INIT = "init container"
    #: An init container with `restartPolicy: Always`: runs alongside them.
    SIDECAR = "sidecar"
    EPHEMERAL = "ephemeral container"


_CONTAINER_LISTS = (
    ("containers", ContainerCategory.CONTAINER),
    ("initContainers", ContainerCategory.INIT),
    ("ephemeralContainers", ContainerCategory.EPHEMERAL),
)
_ENV_MODES = {
    ReferenceSource.ENV_FROM: ConsumptionMode.ENV_FROM,
    ReferenceSource.ENV_KEY: ConsumptionMode.ENV_KEY,
}


@dataclass(frozen=True, slots=True)
class PodIdentity:
    """A consuming Pod, and the controller that owns it, if one does."""

    namespace: str
    name: str
    uid: str | None = None
    owner_kind: str | None = None
    owner_name: str | None = None


@dataclass(frozen=True, slots=True)
class Consumer:
    """One reference a Pod makes to the selected object.

    `container` and `category` are `None` for the Pod-level modes,
    `UNMOUNTED` and `IMAGE_PULL`. `env_name` and `key` are set for `ENV_KEY`;
    `volume`, `mount_path` and `sub_path` for the volume modes.
    """

    pod: PodIdentity
    mode: ConsumptionMode
    path: str
    optional: bool = False
    container: str | None = None
    category: ContainerCategory | None = None
    env_name: str | None = None
    key: str | None = None
    volume: str | None = None
    mount_path: str | None = None
    sub_path: str | None = None


@dataclass(frozen=True, slots=True)
class _Container:
    name: str | None
    category: ContainerCategory
    field: str
    spec: Mapping[str, Any]


class _Matcher:
    """Whether a reference names the selected object."""

    def __init__(self, kind: str, name: str) -> None:
        self._kind = kind
        self._name = name

    def __call__(self, reference: ConfigReference) -> bool:
        return reference.kind == self._kind and reference.name == self._name


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identity(metadata: Mapping[str, Any]) -> PodIdentity | None:
    name, namespace = _text(metadata.get("name")), _text(metadata.get("namespace"))
    if name is None or namespace is None:
        return None
    owners = metadata.get("ownerReferences")
    controller = next(
        (
            _mapping(owner)
            for owner in (owners if isinstance(owners, list) else [])
            if _mapping(owner).get("controller") is True
        ),
        {},
    )
    return PodIdentity(
        namespace,
        name,
        _text(metadata.get("uid")),
        _text(controller.get("kind")),
        _text(controller.get("name")),
    )


def _containers(spec: Mapping[str, Any]) -> Iterator[_Container]:
    for list_field, category in _CONTAINER_LISTS:
        entries = spec.get(list_field)
        for index, entry in enumerate(entries if isinstance(entries, list) else []):
            if not isinstance(entry, Mapping):
                continue
            sidecar = category is ContainerCategory.INIT and entry.get("restartPolicy") == "Always"
            yield _Container(
                _text(entry.get("name")),
                ContainerCategory.SIDECAR if sidecar else category,
                f"spec.{list_field}[{index}]",
                entry,
            )


def _env_consumers(
    pod: PodIdentity, containers: list[_Container], matches: _Matcher
) -> Iterator[Consumer]:
    for container in containers:
        for reference in filter(matches, container_references(container.spec, container.field)):
            yield Consumer(
                pod,
                _ENV_MODES[reference.source],
                reference.field,
                reference.optional,
                container.name,
                container.category,
                env_name=reference.env_name,
                key=reference.key,
            )


def _mounts(
    containers: list[_Container], volume: str | None
) -> Iterator[tuple[_Container, Mapping[str, Any]]]:
    for container in containers:
        mounts = container.spec.get("volumeMounts")
        for mount in mounts if isinstance(mounts, list) else []:
            if volume is not None and isinstance(mount, Mapping) and mount.get("name") == volume:
                yield container, mount


def _volume_consumers(
    pod: PodIdentity, spec: Mapping[str, Any], containers: list[_Container], matches: _Matcher
) -> Iterator[Consumer]:
    volumes = spec.get("volumes")
    for index, volume in enumerate(volumes if isinstance(volumes, list) else []):
        references = volume_references(_mapping(volume), f"spec.volumes[{index}]")
        for reference in filter(matches, references):
            mounted = list(_mounts(containers, reference.volume))
            if not mounted:
                yield Consumer(
                    pod,
                    ConsumptionMode.UNMOUNTED,
                    reference.field,
                    reference.optional,
                    volume=reference.volume,
                )
            for container, mount in mounted:
                sub_path = _text(mount.get("subPath")) or _text(mount.get("subPathExpr"))
                yield Consumer(
                    pod,
                    ConsumptionMode.SUBPATH if sub_path else ConsumptionMode.VOLUME,
                    reference.field,
                    reference.optional,
                    container.name,
                    container.category,
                    volume=reference.volume,
                    mount_path=_text(mount.get("mountPath")),
                    sub_path=sub_path,
                )


def pod_consumers(pod: Mapping[str, Any], kind: str, name: str) -> tuple[Consumer, ...]:
    """The references `pod` makes to the `kind` object named `name`, in its namespace.

    Environment rows come first, in container order, then volume rows, in
    volume order, then image pull rows. A Pod without a name or namespace
    has none.
    """
    identity = _identity(_mapping(pod.get("metadata")))
    if identity is None:
        return ()
    spec = _mapping(pod.get("spec"))
    containers = list(_containers(spec))
    matches = _Matcher(kind, name)
    consumers = [*_env_consumers(identity, containers, matches)]
    consumers += _volume_consumers(identity, spec, containers, matches)
    pulls = image_pull_secret_references(spec.get("imagePullSecrets"), "spec")
    consumers += (
        Consumer(identity, ConsumptionMode.IMAGE_PULL, reference.field, reference.optional)
        for reference in filter(matches, pulls)
    )
    return tuple(consumers)


def consumers_of(pods: Iterable[Mapping[str, Any]], kind: str, name: str) -> tuple[Consumer, ...]:
    """Every Pod's references to the `kind` object named `name`, in Pod order."""
    return tuple(
        consumer
        for pod in pods
        if isinstance(pod, Mapping)
        for consumer in pod_consumers(pod, kind, name)
    )
