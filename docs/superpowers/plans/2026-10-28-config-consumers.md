# ConfigMap and Secret Consumers, Core (#410 P1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find, for a selected ConfigMap or Secret, every Pod container in its namespace that consumes it and how, from bounded evidence reads, keeping no configuration value (#501, the core half of slice P1 of #410).

**Architecture:** `k8s/config_references.py` extracts the ConfigMap and Secret references of a Pod spec once, for both the relationship graph and this feature. `core/config_consumers.py` projects a Pod to its references to one object, by consumption mode. `core/config_propagation.py` runs the diagnostic: it GETs the object's metadata and LISTs the namespace's Pods through the #409 readers, under a run token, and reports both observations, the baseline and the consumers.

**Tech Stack:** Python 3.11+ standard library, pytest with `asyncio_mode = "auto"`, mypy `--strict`, ruff, tach.

## Global Constraints

- **Design:** `docs/superpowers/specs/2026-10-28-config-propagation-design.md`. Where this plan departs from it, the section below says why.
- **No payloads:** ConfigMaps and Secrets are read as metadata only; the #409 reader refuses anything else.
- **No values kept from Pods:** the Pod list is read whole, but only references, names and the controlling owner leave the run. Never `env[].value`, annotations or status.
- **One namespace:** the object's own. Nothing is read cluster-wide.
- **Conclusions from absence:** "no Pod consumes it" only when the Pod list is conclusive.
- **Layers:** `core` imports no Textual and no optional extra; `core` may import `k8s`; `k8s` imports no `core`.
- **Code rules:** no `assert` in `src/`; every `pytest.raises` takes `match=`; maximum complexity 10; Google-style docstrings.
- **Commands:** use `uv run --frozen …`. Behind the corporate mirror, call the project venv's tools directly with `PYTHONPATH=src`.

## Departures from the design

- **P1 lands in two pull requests.** This one is the core: the shared extraction, the projection and the run. The view, the shared evidence renderer (#486) and the wiring follow in #502, so the renderer still ships with its first consumer's view. One pull request for all of P1 would have mixed a graph refactor, a new diagnostic and a new screen with its composition-root wiring.
- **The shared extraction is its own module.** The design says the graph's extraction gains the mount details "through shared helpers". The helpers live in `k8s/config_references.py` rather than inside `relationship_facts.py`, which is already over 1,000 lines; `relationship_facts.py` calls them. Mount details (`volumeMounts`) are read by `core/config_consumers.py`, since the graph does not need them.

## File structure

| File | Change | Responsibility |
| --- | --- | --- |
| `src/korvid/k8s/config_references.py` | create | the ConfigMap and Secret references of a container, a volume and `imagePullSecrets` |
| `src/korvid/k8s/relationship_facts.py` | modify | build `USES_CONFIG` facts from those references |
| `src/korvid/core/config_consumers.py` | create | modes, container categories, and a Pod's consumers of one object |
| `src/korvid/core/config_propagation.py` | create | the run: sources, declarations, reads and the report |
| `tests/k8s/test_config_references.py` | create | each reference form, `optional`, malformed entries, no literal value |
| `tests/core/test_config_consumers.py` | create | each mode and category, Pod-level rows, matching, no literal value |
| `tests/core/test_config_propagation.py` | create | each source outcome, the token, absence and recreation |
| `tests/test_optional_extras.py` | modify | the new modules import without extras |

---

### Task 1: Extract configuration references once

**Files:**
- Create: `src/korvid/k8s/config_references.py`
- Modify: `src/korvid/k8s/relationship_facts.py` (replace `_container_refs`, `_config_reference`, the configuration half of `_volume_refs`, `_projected_volume_refs` and `_image_pull_secret_refs`)
- Test: `tests/k8s/test_config_references.py`

**Interfaces:**
- Produces: `ReferenceSource` (`ENV_FROM`, `ENV_KEY`, `VOLUME`, `IMAGE_PULL`); `ConfigReference(kind, name, field, source, optional=False, env_name=None, key=None, volume=None)`; `container_references(container: Mapping[str, Any], field: str) -> list[ConfigReference]`; `volume_references(volume: Mapping[str, Any], field: str) -> list[ConfigReference]`; `image_pull_secret_references(pull_secrets: object, base: str) -> list[ConfigReference]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/k8s/test_config_references.py` with:

```python
"""The ConfigMap and Secret references a Pod spec makes (#410)."""

from __future__ import annotations

from typing import Any

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
    volumes: list[dict[str, Any]] = [
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest -p no:tach tests/k8s/test_config_references.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'korvid.k8s.config_references'`.

- [ ] **Step 3: Write the module**

Create `src/korvid/k8s/config_references.py` with:

```python
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
```

- [ ] **Step 4: Build the graph's facts from it**

In `src/korvid/k8s/relationship_facts.py`, import `ConfigReference`, `container_references`, `image_pull_secret_references` and `volume_references` from `korvid.k8s.config_references`, delete `_config_reference` and `_projected_volume_refs`, and replace `_container_refs`, `_volume_refs` and `_image_pull_secret_refs` with:

```python
def _config_facts(references: list[ConfigReference], namespace: str) -> list[ReferenceFact]:
    """Shared `ConfigReference`s -> `USES_CONFIG` facts in `namespace`."""
    return [
        ReferenceFact(
            relation=RelationKind.USES_CONFIG,
            target=TargetReference("", reference.kind, namespace, reference.name),
            confidence=FactConfidence.DECLARED,
            field=reference.field,
        )
        for reference in references
    ]


def _container_refs(
    containers: object, namespace: str, base: str, list_field: str
) -> list[ReferenceFact]:
    """`envFrom` / `env[].valueFrom` config references for one container list."""
    if not isinstance(containers, list):
        return []
    references: list[ReferenceFact] = []
    for ci, container in enumerate(containers):
        if isinstance(container, Mapping):
            field = f"{base}.{list_field}[{ci}]"
            references.extend(_config_facts(container_references(container, field), namespace))
    return references


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
        references.extend(_config_facts(volume_references(volume, field), namespace))
    return references


def _image_pull_secret_refs(pull_secrets: object, namespace: str, base: str) -> list[ReferenceFact]:
    """`spec.imagePullSecrets` -> `USES_CONFIG` facts."""
    return _config_facts(image_pull_secret_references(pull_secrets, base), namespace)
```

The facts keep their order (per volume: claim, ConfigMap, Secret, projected sources) and their field paths, so the graph is unchanged.

- [ ] **Step 5: Run the new and the graph's tests**

Run: `uv run --frozen pytest -p no:tach tests/k8s/ tests/core/test_relationships*.py tests/ui/test_relationship* -q`
Expected: all pass, the graph's tests unchanged.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/k8s/config_references.py src/korvid/k8s/relationship_facts.py tests/k8s/test_config_references.py
git commit -m "k8s: extract the ConfigMap and Secret references of a Pod spec once"
```

### Task 2: Project a Pod's consumers by mode

**Files:**
- Create: `src/korvid/core/config_consumers.py`
- Test: `tests/core/test_config_consumers.py`

**Interfaces:**
- Consumes: `container_references`, `volume_references`, `image_pull_secret_references`, `ConfigReference` and `ReferenceSource` (Task 1).
- Produces: `ConsumptionMode` (`ENV_FROM`, `ENV_KEY`, `VOLUME`, `SUBPATH`, `UNMOUNTED`, `IMAGE_PULL`); `ContainerCategory` (`CONTAINER`, `INIT`, `SIDECAR`, `EPHEMERAL`); `PodIdentity(namespace, name, uid=None, owner_kind=None, owner_name=None)`; `Consumer(pod, mode, path, optional=False, container=None, category=None, env_name=None, key=None, volume=None, mount_path=None, sub_path=None)`; `pod_consumers(pod: Mapping[str, Any], kind: str, name: str) -> tuple[Consumer, ...]`; `consumers_of(pods: Iterable[Mapping[str, Any]], kind: str, name: str) -> tuple[Consumer, ...]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_config_consumers.py` with:

```python
"""Project a Pod to its references to one ConfigMap or Secret, by mode (#410)."""

from __future__ import annotations

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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_consumers.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'korvid.core.config_consumers'`.

- [ ] **Step 3: Write the module**

Create `src/korvid/core/config_consumers.py` with:

```python
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
```

- [ ] **Step 4: Run the tests, and check the mutations**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_consumers.py -q`
Expected: `11 passed`.

Each of these changes must make a test fail: treat no init container as a sidecar; ignore `subPathExpr`; never yield an `UNMOUNTED` row; take any owner reference instead of the controller; match by name alone.

- [ ] **Step 5: Commit**

```bash
git add src/korvid/core/config_consumers.py tests/core/test_config_consumers.py
git commit -m "core: project a Pod's consumers of one ConfigMap or Secret, by mode"
```

### Task 3: The run

**Files:**
- Create: `src/korvid/core/config_propagation.py`
- Modify: `tests/test_optional_extras.py` (the base-import list)
- Test: `tests/core/test_config_propagation.py`

**Interfaces:**
- Consumes: `consumers_of`, `Consumer` (Task 2); `resolve`, `LogicalSource`, `Candidate`, `Resolution`, `DiscoverySnapshot` (S2); `read_object`, `read_list`, `LIST_PAGE_SIZE`, `MAX_PAGES`, `MAX_BYTES`, `MAX_DEADLINE` (S3); `RunToken`, `EvidenceSet`, `Baseline`, `Declaration`, `Role`, `conclusive` (S1).
- Produces: `CONFIG_MAPS`, `SECRETS`, `PODS` (`LogicalSource`); `ConfigTarget(kind, namespace, name, uid=None)`; `declarations(target) -> tuple[Declaration, ...]`; `PropagationReport(target, object, pods, consumers, baseline)` with `object_absent`, `recreated` and `consumers_conclusive(now)`; `async explain_propagation(reader, snapshot, target, *, token=None, clock=...) -> PropagationReport`. The view (#502) consumes these.

- [ ] **Step 1: Write the failing tests**

Create `tests/core/test_config_propagation.py` with:

```python
"""Explain a ConfigMap's or Secret's propagation from evidence reads (#410)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core import config_propagation
from korvid.core.api_versions import DiscoverySnapshot, observe_discovery
from korvid.core.capability import Basis, EvidenceState, Observation, RunToken
from korvid.core.config_consumers import ConsumptionMode
from korvid.core.config_propagation import ConfigTarget, explain_propagation
from korvid.core.evidence_reads import (
    DEFAULT_MAX_BYTES,
    LIST_PAGE_SIZE,
    MAX_BYTES,
    MAX_DEADLINE,
    MAX_PAGES,
    read_list,
)
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.evidence_requests import EvidencePage, EvidenceReader, EvidenceTarget

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TOKEN = RunToken(4, 2, 7)
TARGET = ConfigTarget("ConfigMap", "team-a", "settings", "uid-settings")
SETTINGS_META = {"metadata": {"name": "settings", "namespace": "team-a", "uid": "uid-settings"}}


def _clock() -> datetime:
    return NOW


def _snapshot(*resources: str) -> DiscoverySnapshot:
    served = tuple(
        ServedResource(name, frozenset({"get", "list"}))
        for name in resources or ("configmaps", "secrets", "pods")
    )
    raw = RawDiscovery(
        1,
        NOW,
        0.0,
        RootOutcome("/api", (("", "v1"),)),
        RootOutcome("/apis", ()),
        (GroupVersionOutcome("", "v1", served),),
    )
    return observe_discovery(raw)


def _status(code: int, reason: str, *, name: str = "", kind: str = "") -> ApiStatusError:
    details = {"name": name, "group": "", "kind": kind}
    body = json.dumps({"kind": "Status", "reason": reason, "details": details})
    return ApiStatusError(code, reason, body)


def _pod(name: str, *, env_from: str = "settings", value: str = "") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": "team-a", "uid": f"uid-{name}"},
        "spec": {
            "containers": [
                {
                    "name": "app",
                    "envFrom": [{"configMapRef": {"name": env_from}}],
                    "env": [{"name": "LITERAL", "value": value}],
                }
            ]
        },
    }


class FakeReader(EvidenceReader):
    """Answers GETs and LISTs from separate scripts, since the two run together."""

    def __init__(self, *, get: object = SETTINGS_META, pages: tuple[object, ...] = ()) -> None:
        self.get = get
        self.pages = list(pages) or [EvidencePage(())]
        self.gets: list[tuple[EvidenceTarget, str, int]] = []
        self.lists: list[tuple[EvidenceTarget, int, int]] = []
        self.continuations: list[str] = []

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.lists.append((target, limit, max_bytes))
        self.continuations.append(continuation)
        page = self.pages.pop(0)
        if isinstance(page, BaseException):
            raise page
        assert isinstance(page, EvidencePage)
        return page

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.gets.append((target, name, max_bytes))
        if isinstance(self.get, BaseException):
            raise self.get
        assert isinstance(self.get, dict)
        return self.get


def _state(observation: Observation) -> tuple[EvidenceState, Basis]:
    return observation.state, observation.basis


async def _explain(
    reader: FakeReader, target: ConfigTarget = TARGET, snapshot: DiscoverySnapshot | None = None
) -> Any:
    return await explain_propagation(
        reader, snapshot or _snapshot(), target, token=TOKEN, clock=_clock
    )


async def test_consumers_are_read_from_the_namespaces_pods() -> None:
    reader = FakeReader(
        pages=(EvidencePage((_pod("api-1", value="s3cr3t-literal"), _pod("other", env_from="x"))),)
    )

    report = await _explain(reader)

    assert [(c.pod.name, c.mode) for c in report.consumers] == [("api-1", ConsumptionMode.ENV_FROM)]
    assert _state(report.object) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert _state(report.pods) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert report.baseline.state is EvidenceState.AVAILABLE
    assert report.consumers_conclusive(NOW)
    assert "s3cr3t-literal" not in repr(report)


async def test_the_object_is_read_as_metadata_and_only_the_pods_at_the_maximum_budgets() -> None:
    reader = FakeReader()

    await _explain(reader)

    ((get_target, name, max_bytes),) = reader.gets
    assert (get_target, name) == (
        EvidenceTarget("", "v1", "configmaps", "team-a", metadata_only=True),
        "settings",
    )
    assert max_bytes == DEFAULT_MAX_BYTES
    assert reader.lists == [(EvidenceTarget("", "v1", "pods", "team-a"), LIST_PAGE_SIZE, MAX_BYTES)]


async def test_the_pod_list_gets_every_maximum_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    budgets: list[dict[str, Any]] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        budgets.append({k: v for k, v in kwargs.items() if k not in ("token", "clock")})
        return await read_list(*args, **kwargs)

    monkeypatch.setattr(config_propagation, "read_list", spy)

    await _explain(FakeReader())

    assert budgets == [
        {
            "page_size": LIST_PAGE_SIZE,
            "max_pages": MAX_PAGES,
            "deadline": MAX_DEADLINE,
            "max_bytes": MAX_BYTES,
        }
    ]


async def test_a_secret_is_read_from_secrets() -> None:
    reader = FakeReader(get={"metadata": {"name": "creds", "namespace": "team-a"}})

    await _explain(reader, ConfigTarget("Secret", "team-a", "creds"))

    assert reader.gets[0][0].resource == "secrets"


async def test_every_observation_carries_the_run_token() -> None:
    report = await _explain(FakeReader())

    assert (report.object.token, report.pods.token) == (TOKEN, TOKEN)


async def test_a_denied_pod_list_establishes_no_consumer_set() -> None:
    report = await _explain(FakeReader(pages=(_status(403, "Forbidden"),)))

    assert _state(report.pods) == (EvidenceState.DENIED, Basis.FORBIDDEN)
    assert report.consumers == ()
    assert not report.consumers_conclusive(NOW)
    assert "pods" in report.baseline.blocking


async def test_a_truncated_pod_list_keeps_what_it_read_but_is_not_conclusive() -> None:
    pages = tuple(EvidencePage((_pod(f"api-{n}"),), f"p{n + 1}") for n in range(MAX_PAGES))

    reader = FakeReader(pages=pages)

    report = await _explain(reader)

    assert reader.continuations == ["", *(f"p{n}" for n in range(1, MAX_PAGES))]
    assert _state(report.pods) == (EvidenceState.PARTIAL, Basis.TRUNCATED)
    assert len(report.consumers) == MAX_PAGES
    assert not report.consumers_conclusive(NOW)


async def test_a_404_that_names_the_object_reports_it_absent() -> None:
    reader = FakeReader(
        get=_status(404, "NotFound", name="settings", kind="configmaps"),
        pages=(EvidencePage((_pod("api-1"),)),),
    )

    report = await _explain(reader)

    assert _state(report.object) == (EvidenceState.AVAILABLE, Basis.OBJECT_ABSENT)
    assert report.object_absent
    assert [c.pod.name for c in report.consumers] == ["api-1"]


async def test_a_404_that_does_not_name_the_object_is_not_a_deletion() -> None:
    reader = FakeReader(get=_status(404, "NotFound", name="other", kind="configmaps"))

    report = await _explain(reader)

    assert _state(report.object) == (EvidenceState.UNAVAILABLE, Basis.DISCOVERY_STALE)
    assert not report.object_absent


async def test_a_recreated_object_is_reported() -> None:
    reader = FakeReader(
        get={"metadata": {"name": "settings", "namespace": "team-a", "uid": "uid-new"}}
    )

    report = await _explain(reader)

    assert report.recreated
    assert report.object.observed_uid == "uid-new"


async def test_a_refused_metadata_projection_does_not_stop_the_pod_list() -> None:
    reader = FakeReader(get=_status(406, "NotAcceptable"), pages=(EvidencePage((_pod("a"),)),))

    report = await _explain(reader)

    assert _state(report.object) == (EvidenceState.UNSUPPORTED, Basis.PROJECTION_UNSUPPORTED)
    assert [c.pod.name for c in report.consumers] == ["a"]


async def test_an_unserved_source_is_reported_with_the_token_and_not_read() -> None:
    reader = FakeReader()

    report = await _explain(reader, snapshot=_snapshot("configmaps"))

    assert reader.lists == []
    assert _state(report.pods) == (EvidenceState.UNSUPPORTED, Basis.NOT_SERVED)
    assert report.pods.token == TOKEN
    assert report.baseline.state is EvidenceState.PARTIAL


def test_a_target_is_a_config_map_or_a_secret() -> None:
    with pytest.raises(ValueError, match="ConfigMap or a Secret"):
        ConfigTarget("Pod", "team-a", "api-1")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_propagation.py -q`
Expected: collection error, `ModuleNotFoundError: No module named 'korvid.core.config_propagation'`.

- [ ] **Step 3: Write the module**

Create `src/korvid/core/config_propagation.py` with:

```python
"""Explain how a ConfigMap's or Secret's changes reach the Pods that use it (#410).

One run reads two sources through the #409 readers, under the run token it
started with:

- `config-object`: the object's metadata, by name, to confirm it exists and
  which incarnation it is. A ConfigMap or Secret is never read as an object.
- `pods`: the Pods in its namespace, at the readers' maximum budgets. Each
  Pod is projected to its references at once, and only the projection
  leaves this module.

"No Pod consumes it" may be concluded only when the Pod list is conclusive.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from korvid.core.api_versions import (
    Candidate,
    DiscoverySnapshot,
    LogicalSource,
    Resolution,
    resolve,
)
from korvid.core.capability import (
    Baseline,
    Basis,
    Declaration,
    EvidenceSet,
    Observation,
    Projection,
    Role,
    RunToken,
    SourceScope,
    Verb,
    conclusive,
)
from korvid.core.config_consumers import Consumer, consumers_of
from korvid.core.evidence_reads import (
    LIST_PAGE_SIZE,
    MAX_BYTES,
    MAX_DEADLINE,
    MAX_PAGES,
    read_list,
    read_object,
)
from korvid.k8s.evidence_requests import EvidenceReader

CONFIG_MAPS = LogicalSource("config-object", (Candidate("", "configmaps", ("v1",)),))
SECRETS = LogicalSource("config-object", (Candidate("", "secrets", ("v1",)),))
PODS = LogicalSource("pods", (Candidate("", "pods", ("v1",)),))
_OBJECT_SOURCES = {"ConfigMap": CONFIG_MAPS, "Secret": SECRETS}


@dataclass(frozen=True, slots=True)
class ConfigTarget:
    """The selected ConfigMap or Secret, with the UID the selection showed."""

    kind: str
    namespace: str
    name: str
    uid: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _OBJECT_SOURCES or not self.namespace or not self.name:
            raise ValueError("a target is a ConfigMap or a Secret, with a namespace and a name")


def declarations(target: ConfigTarget) -> tuple[Declaration, ...]:
    """The two sources a run reads, both required.

    The object `CONFIRMS`: only what it shows matters. The Pod list
    `RULES_OUT`: "no Pod consumes it" rests on its completeness.
    """
    return (
        _OBJECT_SOURCES[target.kind].declaration(required=True, role=Role.CONFIRMS),
        PODS.declaration(required=True, role=Role.RULES_OUT),
    )


@dataclass(frozen=True, slots=True)
class PropagationReport:
    """What one run established about the target and its consumers."""

    target: ConfigTarget
    object: Observation
    pods: Observation
    consumers: tuple[Consumer, ...]
    baseline: Baseline

    @property
    def object_absent(self) -> bool:
        """Whether the object is known to be gone: a 404 that named it."""
        return self.object.basis is Basis.OBJECT_ABSENT

    @property
    def recreated(self) -> bool:
        """Whether the object read is another incarnation than the one selected."""
        return self.object.recreated

    def consumers_conclusive(self, now: datetime) -> bool:
        """Whether `consumers` is every recognised consumer, so absence is a fact."""
        return conclusive(self.pods, PODS.policy, now)


def _now() -> datetime:
    return datetime.now(UTC)


def _unresolved(resolution: Resolution, token: RunToken | None) -> Observation:
    """Why discovery resolved no source; it carries the run's token."""
    observation = resolution.outcomes[0].observation
    if observation is None:
        raise ValueError("an unresolved resolution records why")
    return replace(observation, token=token)


async def _read_object(
    reader: EvidenceReader,
    resolution: Resolution,
    token: RunToken | None,
    clock: Callable[[], datetime],
) -> Observation:
    if not resolution.sources:
        return _unresolved(resolution, token)
    evidence = await read_object(reader, resolution.sources[0], token=token, clock=clock)
    return evidence.observation


async def _read_consumers(
    reader: EvidenceReader,
    resolution: Resolution,
    target: ConfigTarget,
    token: RunToken | None,
    clock: Callable[[], datetime],
) -> tuple[Observation, tuple[Consumer, ...]]:
    if not resolution.sources:
        return _unresolved(resolution, token), ()
    pods = await read_list(
        reader,
        resolution.sources[0],
        page_size=LIST_PAGE_SIZE,
        max_pages=MAX_PAGES,
        deadline=MAX_DEADLINE,
        max_bytes=MAX_BYTES,
        token=token,
        clock=clock,
    )
    return pods.observation, consumers_of(pods.items, target.kind, target.name)


async def explain_propagation(
    reader: EvidenceReader,
    snapshot: DiscoverySnapshot,
    target: ConfigTarget,
    *,
    token: RunToken | None = None,
    clock: Callable[[], datetime] = _now,
) -> PropagationReport:
    """Read the target's metadata and its namespace's Pods, together, and report.

    Sources are resolved from `snapshot`, so a source the cluster does not
    serve is reported, not read. A source that fails does not stop the other.
    """
    scope = SourceScope.namespace(target.namespace)
    object_resolution = resolve(
        _OBJECT_SOURCES[target.kind],
        Verb.GET,
        snapshot,
        scope=scope,
        projection=Projection.METADATA,
        name=target.name,
        expected_uid=target.uid,
    )
    pod_resolution = resolve(PODS, Verb.LIST, snapshot, scope=scope)
    object_observation, (pod_observation, consumers) = await asyncio.gather(
        _read_object(reader, object_resolution, token, clock),
        _read_consumers(reader, pod_resolution, target, token, clock),
    )
    evidence = EvidenceSet({"config-object": object_observation, "pods": pod_observation})
    baseline = evidence.baseline(declarations(target), clock())
    return PropagationReport(target, object_observation, pod_observation, consumers, baseline)
```

- [ ] **Step 4: Add the new modules to the base-import list**

In `tests/test_optional_extras.py`, after `"korvid.core.run_tokens",` add:

```python
        "korvid.k8s.config_references",
        "korvid.core.config_consumers",
        "korvid.core.config_propagation",
```

- [ ] **Step 5: Run the tests, and check the mutations**

Run: `uv run --frozen pytest -p no:tach tests/core/test_config_propagation.py tests/test_optional_extras.py -q`
Expected: all pass.

Each of these changes must make a test fail: drop `max_pages=MAX_PAGES`, `max_bytes=MAX_BYTES` or `deadline=MAX_DEADLINE` from the Pod read; give the metadata GET the maximum byte budget; return an unresolved observation without the run's token; make `consumers_conclusive` always true; call any available object absent; drop `expected_uid`.

- [ ] **Step 6: Commit**

```bash
git add src/korvid/core/config_propagation.py tests/core/test_config_propagation.py tests/test_optional_extras.py
git commit -m "core: explain a ConfigMap's or Secret's propagation from evidence reads"
```

### Task 4: Run the full gate

- [ ] **Step 1: Run every check**

Run: `make check`
Expected: ruff, format, mypy, tach, the source-size check and the test suite all pass, with coverage at least 80%.
