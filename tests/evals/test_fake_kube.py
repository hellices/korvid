"""Tests for the scenario-seeded FakeKubeClient (issue #69)."""

from __future__ import annotations

from typing import Any

import pytest

from korvid.evals.fake_kube import FakeKubeClient, builtin_aliases
from korvid.evals.scenario import ContainerLogs, Scenario
from korvid.k8s.errors import ApiStatusError
from korvid.tools.executor import ToolExecutor


def _scenario(**overrides: Any) -> Scenario:
    fields: dict[str, Any] = {
        "id": "s1",
        "question": "q",
        "screen": "pods view",
        "root_cause": "oom_killed",
        "must_mention": (("oom",),),
        "objects": (
            {
                "kind": "Pod",
                "apiVersion": "v1",
                "metadata": {"name": "api-1", "namespace": "shop", "uid": "u1"},
                "spec": {"nodeName": "node-a", "containers": [{"name": "app"}]},
                "status": {"phase": "Running"},
            },
            {
                "kind": "Pod",
                "apiVersion": "v1",
                "metadata": {"name": "web-1", "namespace": "front", "uid": "u2"},
                "spec": {"containers": [{"name": "web"}]},
                "status": {"phase": "Running"},
            },
            {
                "kind": "Node",
                "apiVersion": "v1",
                "metadata": {"name": "node-a"},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
        ),
        "events": (
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "restarting failed container",
                "involvedObject": {
                    "kind": "Pod",
                    "name": "api-1",
                    "namespace": "shop",
                    "uid": "u1",
                },
            },
            {
                "type": "Normal",
                "reason": "Pulled",
                "message": "pulled image",
                "involvedObject": {
                    "kind": "Pod",
                    "name": "web-1",
                    "namespace": "front",
                    "uid": "u2",
                },
            },
        ),
        "logs": {
            "shop/api-1/app": ContainerLogs(
                current=("line 1", "line 2", "line 3"),
                previous=("old crash",),
            )
        },
    }
    fields.update(overrides)
    return Scenario(**fields)


def _kube() -> FakeKubeClient:
    return FakeKubeClient(_scenario())


def _pods_meta() -> Any:
    return builtin_aliases()["pods"]


def test_builtin_aliases_cover_the_read_tool_kinds() -> None:
    aliases = builtin_aliases()
    for kind in ("pods", "deployments", "replicasets", "nodes", "persistentvolumeclaims"):
        assert kind in aliases
        assert not aliases[kind].synthetic


async def test_list_objects_filters_by_kind_and_namespace() -> None:
    kube = _kube()
    shop = await kube.list_objects(_pods_meta(), "shop")
    assert [s.name for s in shop] == ["api-1"]
    everywhere = await kube.list_objects(_pods_meta(), None)
    assert sorted(s.name for s in everywhere) == ["api-1", "web-1"]


async def test_list_objects_summaries_render_like_the_tool_expects() -> None:
    summaries = await _kube().list_objects(_pods_meta(), "shop")
    s = summaries[0]
    assert s.namespace == "shop"
    assert isinstance(s.age(), str)


async def test_get_object_returns_the_manifest() -> None:
    manifest = await _kube().get_object(_pods_meta(), "shop", "api-1")
    assert manifest["metadata"]["uid"] == "u1"


async def test_get_object_returns_a_fresh_copy_per_call() -> None:
    """The real client deserializes a fresh response per call; a caller
    mutating the returned manifest (ToolExecutor strips managedFields)
    must not change what later calls observe."""
    kube = _kube()
    first = await kube.get_object(_pods_meta(), "shop", "api-1")
    first["metadata"].pop("uid")
    first["status"]["phase"] = "Failed"
    second = await kube.get_object(_pods_meta(), "shop", "api-1")
    assert second["metadata"]["uid"] == "u1"
    assert second["status"]["phase"] == "Running"


async def test_list_events_for_returns_fresh_copies_per_call() -> None:
    kube = _kube()
    first = await kube.list_events_for("shop", "api-1", kind="Pod")
    first[0]["message"] = "mutated"
    second = await kube.list_events_for("shop", "api-1", kind="Pod")
    assert second[0]["message"] == "restarting failed container"


async def test_get_object_for_cluster_scoped_kind_ignores_namespace() -> None:
    node = await _kube().get_object(builtin_aliases()["nodes"], None, "node-a")
    assert node["kind"] == "Node"


async def test_get_object_missing_raises_404_api_status_error() -> None:
    with pytest.raises(ApiStatusError, match="404") as excinfo:
        await _kube().get_object(_pods_meta(), "shop", "ghost")
    assert excinfo.value.status == 404


async def test_list_events_for_scopes_to_the_named_object() -> None:
    events = await _kube().list_events_for("shop", "api-1", kind="Pod", uid="u1")
    assert [e["reason"] for e in events] == ["BackOff"]


async def test_list_events_for_other_object_yields_nothing() -> None:
    events = await _kube().list_events_for("shop", "ghost", kind="Pod", uid=None)
    assert events == []


async def test_list_events_for_excludes_events_with_a_different_uid() -> None:
    events = await _kube().list_events_for("shop", "api-1", kind="Pod", uid="other-uid")
    assert events == []


async def test_stream_logs_yields_current_lines_with_tail() -> None:
    kube = _kube()
    lines = [
        line.text
        async for line in kube.stream_logs("shop", "api-1", "app", follow=False, tail_lines=2)
    ]
    assert lines == ["line 2", "line 3"]


async def test_stream_logs_previous_yields_previous_instance() -> None:
    kube = _kube()
    lines = [
        line.text
        async for line in kube.stream_logs(
            "shop", "api-1", "app", previous=True, follow=False, tail_lines=10
        )
    ]
    assert lines == ["old crash"]


async def test_stream_logs_previous_missing_raises() -> None:
    scenario = _scenario(logs={"shop/api-1/app": ContainerLogs(current=("only current",))})
    kube = FakeKubeClient(scenario)
    with pytest.raises(ApiStatusError, match="previous"):
        async for _ in kube.stream_logs(
            "shop", "api-1", "app", previous=True, follow=False, tail_lines=10
        ):
            pass


async def test_stream_logs_unknown_container_raises() -> None:
    with pytest.raises(ApiStatusError, match="not found"):
        async for _ in _kube().stream_logs("shop", "api-1", "ghost", follow=False, tail_lines=10):
            pass


async def test_stream_logs_empty_container_uses_the_single_container() -> None:
    """The real client omits the container param for single-container pods."""
    lines = [
        line.text
        async for line in _kube().stream_logs("shop", "api-1", "", follow=False, tail_lines=10)
    ]
    assert lines == ["line 1", "line 2", "line 3"]


def _timed_scenario() -> Scenario:
    """Pod created 3h before SCENARIO_NOW; one event 40m before it."""
    return _scenario(
        objects=(
            {
                "kind": "Pod",
                "apiVersion": "v1",
                "metadata": {
                    "name": "api-1",
                    "namespace": "shop",
                    "uid": "u1",
                    "creationTimestamp": "2026-07-27T05:00:00Z",
                },
                "spec": {"containers": [{"name": "app"}]},
                "status": {"phase": "Running"},
            },
        ),
        events=(
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "restarting failed container",
                "lastTimestamp": "2026-07-27T07:20:00Z",
                "involvedObject": {
                    "kind": "Pod",
                    "name": "api-1",
                    "namespace": "shop",
                    "uid": "u1",
                },
            },
        ),
    )


async def test_fixture_ages_are_stable_relative_to_the_real_clock() -> None:
    """A pod authored 3h before SCENARIO_NOW reads as 3h old whenever the
    eval runs — fixture ages must not drift with the real calendar."""
    kube = FakeKubeClient(_timed_scenario())
    summaries = await kube.list_objects(_pods_meta(), "shop")
    assert summaries[0].age() == "3h"


async def test_event_timestamps_are_rebased_to_the_real_clock() -> None:
    from datetime import UTC, datetime, timedelta

    kube = FakeKubeClient(_timed_scenario())
    events = await kube.list_events_for("shop", "api-1", kind="Pod")
    parsed = datetime.fromisoformat(events[0]["lastTimestamp"].replace("Z", "+00:00"))
    expected = datetime.now(UTC) - timedelta(minutes=40)
    assert abs((parsed - expected).total_seconds()) < 60


async def test_rebasing_never_mutates_the_shared_scenario() -> None:
    scenario = _timed_scenario()
    kube = FakeKubeClient(scenario)
    await kube.list_objects(_pods_meta(), "shop")
    assert scenario.objects[0]["metadata"]["creationTimestamp"] == "2026-07-27T05:00:00Z"
    assert scenario.events[0]["lastTimestamp"] == "2026-07-27T07:20:00Z"


async def test_rebasing_accepts_fractional_seconds_and_explicit_offsets() -> None:
    """RFC 3339 allows fractional seconds and non-UTC offsets; both must be
    rebased (not left anchored to 2026) and normalized back to UTC."""
    from datetime import UTC, datetime, timedelta

    scenario = _scenario(
        objects=(),
        events=(
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "restarting failed container",
                "lastTimestamp": "2026-07-27T07:20:00.123456Z",
                "involvedObject": {
                    "kind": "Pod",
                    "name": "api-1",
                    "namespace": "shop",
                    "uid": "u1",
                },
            },
            {
                "type": "Warning",
                "reason": "Unhealthy",
                "message": "probe failed",
                "lastTimestamp": "2026-07-27T16:20:00+09:00",
                "involvedObject": {
                    "kind": "Pod",
                    "name": "api-1",
                    "namespace": "shop",
                    "uid": "u1",
                },
            },
        ),
    )
    events = await FakeKubeClient(scenario).list_events_for("shop", "api-1", kind="Pod")
    expected = datetime.now(UTC) - timedelta(minutes=40)  # both authored 40m before now
    for event in events:
        raw = event["lastTimestamp"]
        assert raw.endswith("Z"), raw
        parsed = datetime.fromisoformat(raw)
        assert abs((parsed - expected).total_seconds()) < 60


async def test_rebasing_handles_datetimes_parsed_from_unquoted_yaml() -> None:
    """`yaml.safe_load` turns unquoted RFC 3339 values into `datetime`
    objects; they must be rebased like strings (not left anchored to 2026)
    and serialized back to UTC strings so manifests stay JSON-shaped."""
    from datetime import UTC, datetime, timedelta

    scenario = _scenario(
        objects=(),
        events=(
            {
                "type": "Warning",
                "reason": "BackOff",
                "message": "restarting failed container",
                # tz-aware, as safe_load yields for a trailing Z.
                "lastTimestamp": datetime(2026, 7, 27, 7, 20, 0, tzinfo=UTC),
                "involvedObject": {
                    "kind": "Pod",
                    "name": "api-1",
                    "namespace": "shop",
                    "uid": "u1",
                },
            },
            {
                "type": "Warning",
                "reason": "Unhealthy",
                "message": "probe failed",
                # naive, as safe_load yields when no offset is written.
                "lastTimestamp": datetime(2026, 7, 27, 7, 20, 0),
                "involvedObject": {
                    "kind": "Pod",
                    "name": "api-1",
                    "namespace": "shop",
                    "uid": "u1",
                },
            },
        ),
    )
    events = await FakeKubeClient(scenario).list_events_for("shop", "api-1", kind="Pod")
    expected = datetime.now(UTC) - timedelta(minutes=40)  # both authored 40m before now
    for event in events:
        raw = event["lastTimestamp"]
        assert isinstance(raw, str)
        assert raw.endswith("Z"), raw
        parsed = datetime.fromisoformat(raw)
        assert abs((parsed - expected).total_seconds()) < 60


async def test_diagnose_service_works_against_the_fake_cluster() -> None:
    """`diagnose_service` reads EndpointSlices, so the fake cluster must
    serve that kind — otherwise no scenario can exercise the tool."""
    scenario = Scenario(
        id="svc",
        question="q",
        screen="s",
        root_cause="none",
        must_mention=(),
        must_not_mention=(),
        objects=(
            {
                "kind": "Service",
                "apiVersion": "v1",
                "metadata": {"name": "web", "namespace": "front", "uid": "svc-1"},
                "spec": {"selector": {"app": "web"}, "ports": [{"port": 80}]},
            },
            {
                "kind": "EndpointSlice",
                "apiVersion": "discovery.k8s.io/v1",
                "metadata": {
                    "name": "web-abc",
                    "namespace": "front",
                    "uid": "eps-1",
                    "labels": {"kubernetes.io/service-name": "web"},
                },
                "addressType": "IPv4",
                "endpoints": [{"addresses": ["10.0.3.11"], "conditions": {"ready": True}}],
                "ports": [{"port": 8080}],
            },
        ),
    )
    executor = ToolExecutor(FakeKubeClient(scenario), builtin_aliases())
    result = await executor.execute("diagnose_service", {"service": "web", "namespace": "front"})
    assert not result.startswith("ERROR:"), result
    assert "healthy" in result, result


async def test_endpointslices_are_readable_through_ordinary_reads() -> None:
    """A baseline arm has no `diagnose_service`, so it must still be able to
    observe endpoint readiness through `get_resource` / `list_resources` —
    otherwise the matched comparison measures tool availability."""
    scenario = Scenario(
        id="svc",
        question="q",
        screen="s",
        root_cause="none",
        must_mention=(),
        must_not_mention=(),
        objects=(
            {
                "kind": "EndpointSlice",
                "apiVersion": "discovery.k8s.io/v1",
                "metadata": {
                    "name": "web-abc",
                    "namespace": "front",
                    "uid": "eps-1",
                    "labels": {"kubernetes.io/service-name": "web"},
                },
                "addressType": "IPv4",
                "endpoints": [{"addresses": ["10.0.3.11"], "conditions": {"ready": False}}],
                "ports": [{"port": 8080}],
            },
        ),
    )
    executor = ToolExecutor(FakeKubeClient(scenario), builtin_aliases())
    listed = await executor.execute(
        "list_resources", {"kind": "endpointslices", "namespace": "front"}
    )
    assert not listed.startswith("ERROR:"), listed
    got = await executor.execute(
        "get_resource",
        {"kind": "endpointslices", "name": "web-abc", "namespace": "front"},
    )
    assert not got.startswith("ERROR:"), got
    assert "ready: false" in got, got
