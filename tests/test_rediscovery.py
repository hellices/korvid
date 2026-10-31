"""The composition root's rediscovery: what reading discovery again does to the aliases (#517)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from korvid.composition_support import AliasReservations, rediscover_aliases
from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import GroupVersionOutcome, RawDiscovery, RootOutcome
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helm import HELM_RELEASES_META, HELM_REVISIONS_META
from korvid.k8s.olm import PACKAGES_GROUP
from korvid.ui.api_resources_controller import Rediscovery

NOW = datetime(2026, 10, 31, tzinfo=UTC)
BASE = [PODS_META, HELM_RELEASES_META, HELM_REVISIONS_META]
CORE = ResourceMeta("ConfigMap", "configmaps", "", "v1", True, ("cm",))
#: A core kind the snapshots below do not serve.
SECRET = ResourceMeta("Secret", "secrets", "", "v1", True)
WIDGET = ResourceMeta("Widget", "widgets", "example.com", "v1", True, ("wd",))
GADGET = ResourceMeta("Gadget", "gadgets", "example.com", "v1", True)
GADGET_V2 = ResourceMeta("Gadget", "gadgets", "example.com", "v2", True)
METRIC = ResourceMeta("PodMetrics", "pods", "metrics.k8s.io", "v1beta1", True)
PACKAGES = ResourceMeta("PackageManifest", "packagemanifests", PACKAGES_GROUP, "v1", True)


def _outcome(
    meta: ResourceMeta | None = None,
    *,
    group: str = "",
    version: str = "v1",
    complete: bool = True,
    error: ApiStatusError | None = None,
) -> GroupVersionOutcome:
    if meta is not None:
        group, version = meta.group, meta.version
    metas = (meta,) if meta is not None else ()
    return GroupVersionOutcome(group, version, (), metas, complete, error)


def _snapshot(
    *outcomes: GroupVersionOutcome,
    generation: int = 1,
    groups_complete: bool = True,
    versions_complete: bool = True,
    core: GroupVersionOutcome | None = None,
) -> RawDiscovery:
    advertised = tuple((o.group, o.version) for o in outcomes)
    return RawDiscovery(
        generation,
        NOW,
        0.0,
        RootOutcome("/api", (("", "v1"),), versions_complete),
        RootOutcome("/apis", advertised, groups_complete),
        (core or _outcome(CORE), *outcomes),
    )


class Kube(KubeClient):
    """Serves one discovery snapshot, and records that it was invalidated first."""

    def __init__(self, snapshot: RawDiscovery, *, generation: int = 1) -> None:
        super().__init__()
        self.snapshot = snapshot
        self.generation = generation
        self.calls: list[str] = []
        self.switch_during_read = False

    @property
    def session_generation(self) -> int:
        return self.generation

    def invalidate_discovery(self) -> None:
        self.calls.append("invalidate")

    async def discovery_snapshot(self) -> RawDiscovery:
        self.calls.append("read")
        if self.switch_during_read:
            self.generation += 1
        return self.snapshot


def _aliases(*metas: ResourceMeta) -> dict[str, ResourceMeta]:
    return build_alias_map([*BASE, CORE, *metas])


async def test_a_kind_served_since_the_last_read_is_added() -> None:
    aliases = _aliases()
    kube = Kube(_snapshot(_outcome(WIDGET)))

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert kube.calls == ["invalidate", "read"]
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.example.com"] is WIDGET
    assert aliases["helmreleases"] is HELM_RELEASES_META
    assert outcome == Rediscovery(added=("widgets.example.com",))


async def test_a_kind_no_longer_served_is_dropped_and_a_new_version_replaces_the_old() -> None:
    aliases = _aliases(WIDGET, GADGET)
    kube = Kube(_snapshot(_outcome(GADGET_V2)))

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert "widgets" not in aliases
    assert "wd" not in aliases
    assert aliases["gadgets"] is GADGET_V2
    assert aliases["pods"] is PODS_META
    assert outcome == Rediscovery(
        removed=("widgets.example.com",), changed=("gadgets.example.com",)
    )


async def test_a_group_version_that_could_not_be_read_keeps_its_kinds() -> None:
    aliases = _aliases(WIDGET, METRIC)
    failed = _outcome(
        group="metrics.k8s.io",
        version="v1beta1",
        complete=False,
        error=ApiStatusError(503, "ServiceUnavailable", "unavailable"),
    )
    kube = Kube(_snapshot(failed))

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["pods.metrics.k8s.io"] is METRIC
    assert "widgets" not in aliases
    assert outcome == Rediscovery(
        removed=("widgets.example.com",), unreadable=("metrics.k8s.io/v1beta1",)
    )


@pytest.mark.parametrize(
    ("root", "unreadable"),
    [("groups_complete", "the API group list"), ("versions_complete", "the core API version list")],
)
async def test_an_incomplete_version_list_removes_nothing(root: str, unreadable: str) -> None:
    aliases = _aliases(WIDGET, SECRET)
    snapshot = _snapshot(
        _outcome(GADGET),
        groups_complete=root != "groups_complete",
        versions_complete=root != "versions_complete",
    )
    kube = Kube(snapshot)

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["widgets"] is WIDGET
    assert aliases["secrets"] is SECRET
    assert aliases["gadgets"] is GADGET
    assert outcome == Rediscovery(added=("gadgets.example.com",), unreadable=(unreadable,))


async def test_a_read_from_an_earlier_session_is_discarded() -> None:
    aliases = _aliases(WIDGET)
    before = dict(aliases)
    kube = Kube(_snapshot(_outcome(GADGET)))
    kube.switch_during_read = True

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases == before
    assert outcome == Rediscovery(applied=False)


async def test_a_failed_core_read_raises_and_keeps_every_kind() -> None:
    aliases = _aliases(WIDGET)
    before = dict(aliases)
    error = ApiStatusError(500, "InternalError", "boom")
    kube = Kube(_snapshot(_outcome(WIDGET), core=_outcome(complete=False, error=error)))

    with pytest.raises(ApiStatusError, match="InternalError"):
        await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases == before


async def test_operators_opens_the_package_catalog_once_it_is_served() -> None:
    aliases = _aliases()
    kube = Kube(_snapshot(_outcome(PACKAGES)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["operators"] is PACKAGES


OTHER_WIDGET = ResourceMeta("Widget", "widgets", "other.example.org", "v1", True, ("wd",))


async def test_a_new_kind_never_takes_an_alias_another_kind_holds() -> None:
    aliases = _aliases(WIDGET)
    # The other group is advertised first, so a fresh map would give it `widgets`.
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET), _outcome(WIDGET)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["widgets"] is WIDGET
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET


async def test_an_alias_of_a_kind_no_longer_served_is_not_given_to_another() -> None:
    aliases = _aliases(WIDGET)
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert "widgets" not in aliases
    assert "wd" not in aliases
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET


OPERATOR = ResourceMeta("Operator", "operators", "operators.operatorframework.io", "v1", False)


async def test_an_alias_whose_resource_went_away_stays_reserved_for_the_session() -> None:
    aliases = _aliases(WIDGET)
    reservations = AliasReservations()
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))

    await rediscover_aliases(kube, aliases, reservations)
    await rediscover_aliases(kube, aliases, reservations)

    assert "widgets" not in aliases
    assert "wd" not in aliases


async def test_a_new_session_lifts_the_reservations() -> None:
    aliases = _aliases(WIDGET)
    reservations = AliasReservations()
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))
    await rediscover_aliases(kube, aliases, reservations)

    kube.generation = 2
    kube.snapshot = _snapshot(_outcome(OTHER_WIDGET), generation=2)
    await rediscover_aliases(kube, aliases, reservations)

    assert aliases["widgets"] is OTHER_WIDGET


async def test_operators_is_not_handed_to_the_catalog_once_its_kind_is_gone() -> None:
    aliases = _aliases(OPERATOR, PACKAGES)
    kube = Kube(_snapshot(_outcome(PACKAGES)))

    await rediscover_aliases(kube, aliases, AliasReservations())

    assert "operators" not in aliases


async def test_a_reinstalled_resource_gets_its_reserved_aliases_back() -> None:
    aliases = _aliases(WIDGET)
    reservations = AliasReservations()
    kube = Kube(_snapshot(_outcome(OTHER_WIDGET)))
    await rediscover_aliases(kube, aliases, reservations)

    kube.snapshot = _snapshot(_outcome(OTHER_WIDGET), _outcome(WIDGET))
    await rediscover_aliases(kube, aliases, reservations)

    assert aliases["widgets"] is WIDGET
    assert aliases["wd"] is WIDGET
    assert aliases["widgets.other.example.org"] is OTHER_WIDGET


async def test_a_kind_served_again_takes_the_version_read_even_if_another_version_failed() -> None:
    aliases = _aliases(GADGET)
    failed = _outcome(group="example.com", version="v1", complete=False)
    kube = Kube(_snapshot(_outcome(GADGET_V2), failed))

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert aliases["gadgets"] is GADGET_V2
    assert outcome.unreadable == ("example.com/v1",)


async def test_only_the_version_that_could_not_be_read_keeps_its_kinds() -> None:
    aliases = _aliases(GADGET_V2)
    failed = _outcome(group="example.com", version="v1", complete=False)
    kube = Kube(_snapshot(_outcome(group="example.com", version="v2"), failed))

    outcome = await rediscover_aliases(kube, aliases, AliasReservations())

    assert "gadgets" not in aliases
    assert outcome.removed == ("gadgets.example.com",)
