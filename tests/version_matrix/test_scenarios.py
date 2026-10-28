"""Read-only capability scenarios against a live API server (#409, slice S5).

Each Kubernetes version in the matrix runs the same scenarios: version
resolution from real discovery, an unavailable aggregated API, metadata reads
of configuration payloads, denied principals, and Events through either API
group. They read; the only writes are the fixtures `setup.sh` installed.
"""

from __future__ import annotations

import json

import pytest

from korvid.core.api_versions import EVENTS, Candidate, LogicalSource, Resolution, resolve
from korvid.core.capability import (
    ApiSource,
    Basis,
    EvidenceState,
    Observation,
    Projection,
    SourceScope,
    Verb,
)
from korvid.core.evidence_reads import (
    LAST_APPLIED_ANNOTATION,
    read_list,
    read_logical_list,
    read_object,
)
from korvid.k8s.discovery import ResourceMeta
from tests.version_matrix.conftest import (
    ADMIN_CONTEXT,
    ANONYMOUS_CONTEXT,
    EVENTS_CORE_CONTEXT,
    EVENTS_GROUP_CONTEXT,
    NAMESPACE,
    Connect,
    snapshot_of,
)

pytestmark = pytest.mark.version_matrix

SCOPE = SourceScope.namespace(NAMESPACE)
WIDGETS = LogicalSource("widgets", (Candidate("matrix.korvid.dev", "widgets", ("v1", "v1beta1")),))
OLD_WIDGETS = LogicalSource(
    "old-widgets", (Candidate("matrix.korvid.dev", "widgets", ("v1beta1",)),)
)
GADGETS = LogicalSource("gadgets", (Candidate("future.korvid.dev", "gadgets", ("v1",)),))
ABSENT = LogicalSource("absent", (Candidate("absent.korvid.dev", "things", ("v1",)),))
UNAVAILABLE = LogicalSource(
    "unavailable", (Candidate("unavailable.korvid.dev", "things", ("v1",)),)
)
PODS = LogicalSource("pods", (Candidate("", "pods", ("v1",)),))
PAYLOADS = {
    "configmaps": ("settings", "matrix-configmap-payload"),
    "secrets": ("credentials", "matrix-secret-payload"),
}
CONFIGMAP_META = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)
SECRET_META = ResourceMeta("Secret", "secrets", "", "v1", True)


def _state(observation: Observation | None) -> tuple[EvidenceState, Basis]:
    assert observation is not None
    return observation.state, observation.basis


def _only_source(resolution: Resolution) -> ApiSource:
    (source,) = resolution.sources
    return source


def _failure(resolution: Resolution) -> tuple[EvidenceState, Basis]:
    (outcome,) = resolution.outcomes
    return _state(outcome.observation)


def _group(observation: Observation) -> str:
    assert isinstance(observation.source, ApiSource)
    return observation.source.group


async def test_the_preferred_version_is_read_when_known(connect: Connect) -> None:
    kube = await connect(ADMIN_CONTEXT)
    resolution = resolve(WIDGETS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_list(kube, _only_source(resolution))

    assert _only_source(resolution).version == "v1"
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)


async def test_a_non_preferred_version_is_read_when_only_it_is_known(connect: Connect) -> None:
    kube = await connect(ADMIN_CONTEXT)
    resolution = resolve(OLD_WIDGETS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_list(kube, _only_source(resolution))

    assert _only_source(resolution).version == "v1beta1"
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)


async def test_a_resource_served_only_in_an_unknown_version_is_never_read(
    connect: Connect,
) -> None:
    kube = await connect(ADMIN_CONTEXT)

    resolution = resolve(GADGETS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    assert resolution.sources == ()
    assert _failure(resolution) == (EvidenceState.UNSUPPORTED, Basis.NO_KNOWN_VERSION)


async def test_an_installed_optional_api_resolves_and_an_absent_one_is_not_served(
    connect: Connect,
) -> None:
    kube = await connect(ADMIN_CONTEXT)
    snapshot = await snapshot_of(kube)

    assert resolve(WIDGETS, Verb.LIST, snapshot, scope=SCOPE).sources
    assert _failure(resolve(ABSENT, Verb.LIST, snapshot, scope=SCOPE)) == (
        EvidenceState.UNSUPPORTED,
        Basis.NOT_SERVED,
    )


async def test_an_unavailable_aggregated_api_is_unavailable_not_unsupported(
    connect: Connect,
) -> None:
    kube = await connect(ADMIN_CONTEXT)

    resolution = resolve(UNAVAILABLE, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    assert _failure(resolution) == (EvidenceState.UNAVAILABLE, Basis.DISCOVERY_FAILED)
    assert "pods" in [meta.plural for meta in await kube.discover_resources()]


@pytest.mark.parametrize("resource", sorted(PAYLOADS))
async def test_no_configuration_payload_reaches_a_metadata_list(
    connect: Connect, resource: str
) -> None:
    kube = await connect(ADMIN_CONTEXT)
    name, payload = PAYLOADS[resource]
    source = LogicalSource(resource, (Candidate("", resource, ("v1",)),))
    resolution = resolve(
        source, Verb.LIST, await snapshot_of(kube), scope=SCOPE, projection=Projection.METADATA
    )

    result = await read_list(kube, _only_source(resolution))

    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert name in [item["metadata"]["name"] for item in result.items]
    assert all(set(item) == {"metadata"} for item in result.items)
    assert all("annotations" not in item["metadata"] for item in result.items)
    assert payload not in json.dumps(result.items)


@pytest.mark.parametrize("resource", sorted(PAYLOADS))
async def test_no_configuration_payload_reaches_a_metadata_get(
    connect: Connect, resource: str
) -> None:
    kube = await connect(ADMIN_CONTEXT)
    name, payload = PAYLOADS[resource]
    meta = CONFIGMAP_META if resource == "configmaps" else SECRET_META
    on_the_wire = await kube.get_object(meta, NAMESPACE, name)
    source = LogicalSource(resource, (Candidate("", resource, ("v1",)),))
    resolution = resolve(
        source,
        Verb.GET,
        await snapshot_of(kube),
        scope=SCOPE,
        projection=Projection.METADATA,
        name=name,
    )

    result = await read_object(kube, _only_source(resolution))

    assert LAST_APPLIED_ANNOTATION in on_the_wire["metadata"]["annotations"]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert result.item is not None
    assert set(result.item) == {"metadata"}
    assert "annotations" not in result.item["metadata"]
    assert payload not in json.dumps(result.item)


async def test_a_principal_denied_a_list_is_denied(connect: Connect) -> None:
    kube = await connect(EVENTS_GROUP_CONTEXT)
    resolution = resolve(PODS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_list(kube, _only_source(resolution))

    assert _state(result.observation) == (EvidenceState.DENIED, Basis.FORBIDDEN)


async def test_a_principal_denied_discovery_is_denied_not_unsupported(connect: Connect) -> None:
    kube = await connect(ANONYMOUS_CONTEXT)
    snapshot = await snapshot_of(kube)

    assert _failure(resolve(WIDGETS, Verb.LIST, snapshot, scope=SCOPE)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )
    assert _failure(resolve(PODS, Verb.LIST, snapshot, scope=SCOPE)) == (
        EvidenceState.DENIED,
        Basis.DISCOVERY_FORBIDDEN,
    )


async def test_events_are_read_through_both_groups_from_the_first(connect: Connect) -> None:
    kube = await connect(ADMIN_CONTEXT)
    resolution = resolve(EVENTS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_logical_list(kube, resolution)

    assert [source.group for source in resolution.sources] == ["events.k8s.io", ""]
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert [_group(outcome) for outcome in result.outcomes] == ["events.k8s.io"]
    assert "matrix-scenario" in [item["metadata"]["name"] for item in result.items]


async def test_events_are_read_through_events_k8s_io_alone(connect: Connect) -> None:
    kube = await connect(EVENTS_GROUP_CONTEXT)
    resolution = resolve(EVENTS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_logical_list(kube, resolution)

    assert [_group(outcome) for outcome in result.outcomes] == ["events.k8s.io"]
    assert _group(result.observation) == "events.k8s.io"
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert "matrix-scenario" in [item["metadata"]["name"] for item in result.items]


async def test_events_fall_through_to_core_when_events_k8s_io_is_denied(
    connect: Connect,
) -> None:
    kube = await connect(EVENTS_CORE_CONTEXT)
    resolution = resolve(EVENTS, Verb.LIST, await snapshot_of(kube), scope=SCOPE)

    result = await read_logical_list(kube, resolution)

    assert [_state(outcome) for outcome in result.outcomes] == [
        (EvidenceState.DENIED, Basis.FORBIDDEN),
        (EvidenceState.AVAILABLE, Basis.OBSERVED),
    ]
    assert _group(result.observation) == ""
    assert _state(result.observation) == (EvidenceState.AVAILABLE, Basis.OBSERVED)
    assert "matrix-scenario" in [item["metadata"]["name"] for item in result.items]
