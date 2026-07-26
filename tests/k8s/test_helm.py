"""Helm release parsing from release Secrets (issue #28).

Helm stores each release revision as a Secret of type ``helm.sh/release.v1``
whose ``data.release`` is base64(base64(gzip(json))). korvid browses releases
with zero dependency on the helm binary by decoding those payloads.
"""

from __future__ import annotations

import base64
import gzip
import json
from typing import Any

import pytest

from korvid.k8s.helm import (
    HELM_SECRET_TYPE,
    HelmReleaseSummary,
    HelmRevisionSummary,
    ReleaseTracker,
    decode_release,
    release_from_secret,
    revision_from_secret,
)


def _payload(
    name: str = "web",
    version: int = 3,
    status: str = "deployed",
    chart: str = "nginx",
    chart_version: str = "1.2.3",
    app_version: str = "1.25",
    description: str = "Upgrade complete",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": "default",
        "version": version,
        "info": {
            "status": status,
            "last_deployed": "2026-07-26T10:00:00Z",
            "description": description,
        },
        "chart": {"metadata": {"name": chart, "version": chart_version, "appVersion": app_version}},
        "config": config if config is not None else {"replicaCount": 2},
        "manifest": "apiVersion: v1\nkind: Service\n",
    }


def _encode(payload: dict[str, Any]) -> str:
    inner = base64.b64encode(gzip.compress(json.dumps(payload).encode()))
    return base64.b64encode(inner).decode()


def _secret(
    name: str = "web",
    version: int = 3,
    status: str = "deployed",
    payload: dict[str, Any] | None = None,
    *,
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    if data is None:
        data = {"release": _encode(payload if payload is not None else _payload(name, version))}
    return {
        "metadata": {
            "name": f"sh.helm.release.v1.{name}.v{version}",
            "namespace": "default",
            "uid": f"secret-uid-{name}-{version}",
            "creationTimestamp": "2026-07-26T10:00:00Z",
            "labels": {
                "owner": "helm",
                "name": name,
                "version": str(version),
                "status": status,
            },
        },
        "type": HELM_SECRET_TYPE,
        "data": data,
    }


class TestDecodeRelease:
    def test_decodes_double_base64_gzip_json(self) -> None:
        payload = _payload(config={"image": {"tag": "v2"}})
        decoded = decode_release(_secret(payload=payload))
        assert decoded["name"] == "web"
        assert decoded["config"] == {"image": {"tag": "v2"}}
        assert decoded["chart"]["metadata"]["appVersion"] == "1.25"

    def test_missing_release_key_raises(self) -> None:
        with pytest.raises(ValueError, match="release"):
            decode_release(_secret(data={}))

    def test_corrupt_payload_raises_value_error(self) -> None:
        bad = base64.b64encode(b"not gzip at all").decode()
        with pytest.raises(ValueError, match="payload"):
            decode_release(_secret(data={"release": bad}))


class TestReleaseFromSecret:
    def test_release_fields_from_labels_and_payload(self) -> None:
        rel = release_from_secret(_secret("web", 3))
        assert isinstance(rel, HelmReleaseSummary)
        assert rel.name == "web"
        assert rel.namespace == "default"
        assert rel.revision == 3
        assert rel.status == "deployed"
        assert rel.chart == "nginx-1.2.3"
        assert rel.app_version == "1.25"
        assert rel.created == "2026-07-26T10:00:00Z"

    def test_release_uid_is_stable_across_revisions(self) -> None:
        """Drill-down anchors on the parent uid; it must not change when a
        new revision Secret replaces the previous latest."""
        assert (
            release_from_secret(_secret("web", 1)).uid == release_from_secret(_secret("web", 2)).uid
        )

    def test_undecodable_payload_falls_back_to_labels(self) -> None:
        secret = _secret("web", 2, data={"release": base64.b64encode(b"junk").decode()})
        rel = release_from_secret(secret)
        assert rel.name == "web"
        assert rel.revision == 2
        assert rel.chart == "-"
        assert rel.app_version == "-"

    def test_revision_summary_is_keyed_per_revision_and_owned_by_release(self) -> None:
        rev = revision_from_secret(_secret("web", 3))
        assert isinstance(rev, HelmRevisionSummary)
        assert rev.name == "web.v3"
        assert rev.revision == 3
        assert rev.description == "Upgrade complete"
        assert release_from_secret(_secret("web", 3)).uid in rev.owner_uids


class TestReleaseTracker:
    def test_first_revision_emits_added(self) -> None:
        tracker = ReleaseTracker()
        out = tracker.apply("ADDED", release_from_secret(_secret("web", 1)))
        assert [(ev, r.name, r.revision) for ev, r in out] == [("ADDED", "web", 1)]

    def test_newer_revision_emits_modified_with_latest(self) -> None:
        tracker = ReleaseTracker()
        tracker.apply("ADDED", release_from_secret(_secret("web", 1)))
        out = tracker.apply("ADDED", release_from_secret(_secret("web", 2, status="deployed")))
        assert [(ev, r.revision) for ev, r in out] == [("MODIFIED", 2)]

    def test_older_revision_arriving_later_does_not_regress_the_row(self) -> None:
        """The initial LIST is unordered - v1 arriving after v3 must not
        repaint the release at revision 1."""
        tracker = ReleaseTracker()
        tracker.apply("ADDED", release_from_secret(_secret("web", 3)))
        assert tracker.apply("ADDED", release_from_secret(_secret("web", 1))) == []

    def test_deleting_latest_revision_falls_back_to_previous(self) -> None:
        """helm rollback deletes nothing, but a manual Secret delete or TTL
        pruning of the newest revision must repaint from the next-best."""
        tracker = ReleaseTracker()
        tracker.apply("ADDED", release_from_secret(_secret("web", 1)))
        tracker.apply("ADDED", release_from_secret(_secret("web", 2)))
        out = tracker.apply("DELETED", release_from_secret(_secret("web", 2)))
        assert [(ev, r.revision) for ev, r in out] == [("MODIFIED", 1)]

    def test_deleting_last_revision_emits_deleted(self) -> None:
        """helm uninstall deletes every revision Secret; the release row
        disappears when the final one goes."""
        tracker = ReleaseTracker()
        tracker.apply("ADDED", release_from_secret(_secret("web", 1)))
        out = tracker.apply("DELETED", release_from_secret(_secret("web", 1)))
        assert [(ev, r.name) for ev, r in out] == [("DELETED", "web")]

    def test_releases_are_tracked_per_namespace(self) -> None:
        tracker = ReleaseTracker()
        a = release_from_secret(_secret("web", 1))
        b_secret = _secret("web", 1)
        b_secret["metadata"]["namespace"] = "prod"
        b = release_from_secret(b_secret)
        tracker.apply("ADDED", a)
        out = tracker.apply("DELETED", b)  # different namespace: not the same release
        assert out == []
        assert tracker.apply("DELETED", a)[0][0] == "DELETED"


# --- client integration: watch/list helm secrets ---------------------------

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from korvid.k8s.client import KubeClient  # noqa: E402
from korvid.k8s.errors import ApiStatusError  # noqa: E402

from .test_client import _FakeWatch  # noqa: E402


class TestWatchHelmReleases:
    async def test_lists_then_watches_with_type_field_selector(self) -> None:
        client = KubeClient()
        list_resp = {
            "metadata": {"resourceVersion": "42"},
            "items": [_secret("web", 1), _secret("db", 5)],
        }
        fake_watch = _FakeWatch([])
        request_json = AsyncMock(return_value=list_resp)
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", request_json),
            patch("korvid.k8s.client.k8s_watch.Watch", return_value=fake_watch),
        ):
            collected = [(ev, r.name) async for ev, r in client.watch_helm_releases("default")]
        assert ("ADDED", "web") in collected
        assert ("ADDED", "db") in collected
        assert request_json.await_args is not None
        path = request_json.await_args.args[0]
        assert "/api/v1/namespaces/default/secrets" in path
        assert "fieldSelector=type%3Dhelm.sh%2Frelease.v1" in path

    async def test_all_namespaces_uses_cluster_path(self) -> None:
        client = KubeClient()
        list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "1"}, "items": []}
        request_json = AsyncMock(return_value=list_resp)
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", request_json),
            patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch([])),
        ):
            _ = [r async for r in client.watch_helm_releases(None)]
        assert request_json.await_args is not None
        assert request_json.await_args.args[0].startswith("/api/v1/secrets?")

    async def test_multiple_revisions_collapse_to_latest(self) -> None:
        client = KubeClient()
        list_resp = {
            "metadata": {"resourceVersion": "9"},
            "items": [_secret("web", 1), _secret("web", 2, status="deployed")],
        }
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
            patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch([])),
        ):
            collected = [
                (ev, r.name, r.revision) async for ev, r in client.watch_helm_releases("default")
            ]
        assert collected == [("ADDED", "web", 1), ("MODIFIED", "web", 2)]

    async def test_watch_events_flow_through_tracker(self) -> None:
        client = KubeClient()
        list_resp = {"metadata": {"resourceVersion": "5"}, "items": [_secret("web", 1)]}
        watch_events = [
            {"type": "ADDED", "raw_object": _secret("web", 2)},
            {"type": "DELETED", "raw_object": _secret("web", 2)},
        ]
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
            patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch(watch_events)),
        ):
            collected = [(ev, r.revision) async for ev, r in client.watch_helm_releases("default")]
        assert collected == [("ADDED", 1), ("MODIFIED", 2), ("MODIFIED", 1)]


class TestWatchHelmRevisions:
    async def test_yields_one_row_per_revision(self) -> None:
        client = KubeClient()
        list_resp = {
            "metadata": {"resourceVersion": "3"},
            "items": [_secret("web", 1), _secret("web", 2)],
        }
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
            patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch([])),
        ):
            collected = [(ev, r.name) async for ev, r in client.watch_helm_revisions("default")]
        assert collected == [("ADDED", "web.v1"), ("ADDED", "web.v2")]


class TestGetHelmRelease:
    async def test_returns_decoded_metadata_and_values_without_manifest(self) -> None:
        client = KubeClient()
        list_resp = {"items": [_secret("web", 1), _secret("web", 3)]}
        request_json = AsyncMock(return_value=list_resp)
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", request_json),
        ):
            detail = await client.get_helm_release("default", "web")
        assert detail["name"] == "web"
        assert detail["revision"] == 3
        assert detail["chart"] == "nginx-1.2.3"
        assert detail["values"] == {"replicaCount": 2}
        assert "manifest" not in detail  # rendered templates are too big for describe
        assert request_json.await_args is not None
        path = request_json.await_args.args[0]
        assert "labelSelector=" in path
        assert "name%3Dweb" in path

    async def test_specific_revision_is_selected(self) -> None:
        client = KubeClient()
        list_resp = {"items": [_secret("web", 1), _secret("web", 3)]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        ):
            detail = await client.get_helm_release("default", "web", revision=1)
        assert detail["revision"] == 1

    async def test_unknown_release_raises_not_found(self) -> None:
        client = KubeClient()
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value={"items": []})),
            pytest.raises(ApiStatusError, match="not found"),
        ):
            await client.get_helm_release("default", "ghost")
