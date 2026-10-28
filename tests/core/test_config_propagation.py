"""Explain a ConfigMap's or Secret's propagation from evidence reads (#410)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from korvid.core.api_versions import DiscoverySnapshot, observe_discovery
from korvid.core.capability import Basis, EvidenceState, Observation, RunToken
from korvid.core.config_consumers import ConsumptionMode
from korvid.core.config_propagation import ConfigTarget, explain_propagation
from korvid.core.evidence_reads import DEFAULT_MAX_BYTES, LIST_PAGE_SIZE, MAX_BYTES, MAX_PAGES
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
