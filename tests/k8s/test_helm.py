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
    MAX_DISPLAY_CHARS,
    HelmReleaseIdentity,
    HelmReleaseSummary,
    HelmRevisionSummary,
    ReleaseTracker,
    decode_release,
    release_from_secret,
    release_identity_from_secret,
    release_uid,
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

    def test_gzip_bomb_is_rejected_with_value_error(self) -> None:
        """The payload is cluster-controlled: a small Secret can gzip-expand
        to hundreds of MB. Decompression is bounded and overflow becomes the
        same ValueError as any other corrupt payload."""
        huge_but_valid_json = (
            '{"name": "web", "pad": "' + "a" * (64 * 1024 * 1024) + '"}'
        ).encode()
        bomb = base64.b64encode(base64.b64encode(gzip.compress(huge_but_valid_json)))
        with pytest.raises(ValueError, match="payload"):
            decode_release(_secret(data={"release": bomb.decode()}))

    def test_deeply_nested_json_is_rejected_with_value_error(self) -> None:
        """json.loads raises RecursionError on pathological nesting; that must
        normalize to ValueError so the label-only fallback applies instead of
        the watch dying over one hostile Secret."""
        hostile = ("[" * 200_000) + ("]" * 200_000)
        bad = base64.b64encode(base64.b64encode(gzip.compress(hostile.encode()))).decode()
        with pytest.raises(ValueError, match="payload"):
            decode_release(_secret(data={"release": bad}))

    def test_truncated_gzip_trailer_raises_value_error(self) -> None:
        """A complete DEFLATE body with a chopped gzip trailer decodes to
        valid JSON but is not a complete stream - it must take the
        corrupt-payload fallback, not be silently accepted."""
        compressed = gzip.compress(json.dumps(_payload()).encode())
        truncated = compressed[:-4]  # drop half the CRC/size trailer
        bad = base64.b64encode(base64.b64encode(truncated)).decode()
        with pytest.raises(ValueError, match="payload"):
            decode_release(_secret(data={"release": bad}))

    def test_corrupt_deflate_stream_raises_value_error(self) -> None:
        """A valid gzip header with a broken DEFLATE body raises zlib.error,
        which must be normalized to ValueError like every other decode
        failure - otherwise one corrupt Secret kills the whole watch."""
        gzip_header_plus_garbage = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\x03" + b"\xff" * 16
        bad = base64.b64encode(base64.b64encode(gzip_header_plus_garbage)).decode()
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

    def test_release_identity_uses_concrete_secret_uid_and_revision(self) -> None:
        secret = _secret("web", 3)

        assert release_identity_from_secret(secret) == HelmReleaseIdentity(
            secret_uid="secret-uid-web-3",
            revision=3,
        )
        release = release_from_secret(secret)
        assert release.uid == release_uid("default", "web")
        assert release.identity == HelmReleaseIdentity("secret-uid-web-3", 3)

    @pytest.mark.parametrize(
        ("uid", "version"),
        [
            ("", "3"),
            (None, "3"),
            ("secret-uid", ""),
            ("secret-uid", "0"),
            ("secret-uid", "not-an-int"),
        ],
    )
    def test_release_identity_rejects_missing_or_invalid_facts(
        self, uid: str | None, version: str
    ) -> None:
        secret = _secret("web", 3)
        secret["metadata"]["uid"] = uid
        secret["metadata"]["labels"]["version"] = version

        assert release_identity_from_secret(secret) is None
        assert release_from_secret(secret).identity is None

    def test_undecodable_payload_falls_back_to_labels(self) -> None:
        secret = _secret("web", 2, data={"release": base64.b64encode(b"junk").decode()})
        rel = release_from_secret(secret)
        assert rel.name == "web"
        assert rel.revision == 2
        assert rel.chart == "-"
        assert rel.app_version == "-"

    def test_malformed_nested_payload_falls_back_without_raising(self) -> None:
        """A payload can be a JSON object while nested fields are the wrong
        type (list chart, string info); the extraction must fall back, not
        raise AttributeError and kill the watch."""
        broken: dict[str, Any] = {"name": "web", "chart": ["bad"], "info": "oops"}
        secret = _secret("web", 2, data={"release": _encode(broken)})
        rel = release_from_secret(secret)
        assert rel.chart == "-"
        assert rel.app_version == "-"
        rev = revision_from_secret(secret)
        assert rev.description == ""

    def test_oversized_display_fields_are_truncated(self) -> None:
        """The decode ceiling bounds total bytes, not individual strings: a
        highly compressible Secret can put ~everything into one field. Each
        rendered value is capped so hostile Secrets cannot balloon the table."""
        huge = "a" * 1_000_000
        payload = _payload()
        payload["chart"]["metadata"]["name"] = huge
        payload["info"]["description"] = huge
        secret = _secret("web", 2, data={"release": _encode(payload)})
        rel = release_from_secret(secret)
        assert len(rel.chart) <= MAX_DISPLAY_CHARS + 1  # +1 for the ellipsis
        assert rel.chart.endswith("\u2026")
        rev = revision_from_secret(secret)
        assert len(rev.description) <= MAX_DISPLAY_CHARS + 1
        assert rev.description.endswith("\u2026")

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

    def test_delete_of_unobserved_revision_is_ignored(self) -> None:
        """A DELETE for a revision the tracker never saw (e.g. pruned before
        our LIST) must not repaint the release - only tracked revisions can
        change the surfaced row."""
        tracker = ReleaseTracker()
        tracker.apply("ADDED", release_from_secret(_secret("web", 1)))
        assert tracker.apply("DELETED", release_from_secret(_secret("web", 2))) == []
        # v1 is still tracked: deleting it ends the release.
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
        assert "labelSelector=owner%3Dhelm" in path  # non-helm Secrets of this type stay out

    async def test_watch_phase_gets_bare_path_with_selector_params(self) -> None:
        """The watch adapter passes query params through call_api; a path that
        already embeds ?fieldSelector=... would get a second '?' appended and
        lose the selectors for live updates."""
        client = KubeClient()
        list_resp: dict[str, Any] = {"metadata": {"resourceVersion": "3"}, "items": []}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
            patch("korvid.k8s.client.k8s_watch.Watch", return_value=_FakeWatch([])),
            patch.object(
                client, "_make_raw_watch_callable", wraps=client._make_raw_watch_callable
            ) as factory,
        ):
            _ = [r async for r in client.watch_helm_releases("default")]
        assert factory.call_args is not None
        path = factory.call_args.args[0]
        assert "?" not in path
        extra = dict(factory.call_args.kwargs["extra_query"])
        assert extra["fieldSelector"] == "type=helm.sh/release.v1"
        assert extra["labelSelector"] == "owner=helm"

    async def test_watch_callable_forwards_extra_query_to_call_api(self) -> None:
        client = KubeClient()
        api = MagicMock()
        ok = MagicMock()
        ok.status = 200
        api.call_api = AsyncMock(return_value=ok)
        with patch.object(client, "_api", api):
            fn = client._make_raw_watch_callable(
                "/api/v1/secrets", extra_query=(("fieldSelector", "type=x"),)
            )
            await fn(watch=True, _preload_content=False, resource_version="7")
        assert api.call_api.await_args is not None
        assert api.call_api.await_args.args[0] == "/api/v1/secrets"
        params = api.call_api.await_args.kwargs["query_params"]
        assert ("fieldSelector", "type=x") in params
        assert ("watch", "true") in params
        assert ("resourceVersion", "7") in params

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
    async def test_get_helm_release_identity_selects_latest_revision(self) -> None:
        client = KubeClient()
        response = {"items": [_secret("web", 1), _secret("web", 3), _secret("web", 2)]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=response)),
        ):
            identity = await client.get_helm_release_identity("default", "web")

        assert identity == HelmReleaseIdentity("secret-uid-web-3", 3)

    async def test_get_helm_release_identity_returns_none_for_invalid_latest_secret(self) -> None:
        latest = _secret("web", 3)
        latest["metadata"]["uid"] = ""
        client = KubeClient()
        response = {"items": [_secret("web", 2), latest]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=response)),
        ):
            identity = await client.get_helm_release_identity("default", "web")

        assert identity is None

    async def test_get_helm_release_identity_rejects_malformed_newest_version_label(
        self,
    ) -> None:
        latest = _secret("web", 4)
        latest["metadata"]["labels"]["version"] = "invalid"
        client = KubeClient()
        response = {"items": [_secret("web", 3), latest]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=response)),
        ):
            identity = await client.get_helm_release_identity("default", "web")

        assert identity is None

    async def test_get_helm_release_identity_rejects_name_label_revision_mismatch(
        self,
    ) -> None:
        latest = _secret("web", 4)
        latest["metadata"]["labels"]["version"] = "3"
        client = KubeClient()
        response = {"items": [_secret("web", 3), latest]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=response)),
        ):
            identity = await client.get_helm_release_identity("default", "web")

        assert identity is None

    async def test_get_helm_release_identity_rejects_noncanonical_secret_names(
        self,
    ) -> None:
        secret = _secret("web", 3)
        secret["metadata"]["name"] = "not-a-helm-release-secret"
        client = KubeClient()
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(
                client,
                "_request_json",
                AsyncMock(return_value={"items": [secret]}),
            ),
            pytest.raises(ApiStatusError, match=r"helm release .* not found"),
        ):
            await client.get_helm_release_identity("default", "web")

    async def test_get_helm_release_identity_preserves_missing_release_404(self) -> None:
        client = KubeClient()
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value={"items": []})),
            pytest.raises(ApiStatusError, match=r"helm release .* not found"),
        ):
            await client.get_helm_release_identity("default", "ghost")

    async def test_undecodable_payload_describes_from_labels(self) -> None:
        """A release whose latest Secret payload does not decode still lists
        via the label fallback - describe on it must degrade to label-only
        detail instead of raising ValueError at the caller."""
        secret = _secret("web", 2, data={"release": base64.b64encode(b"junk").decode()})
        client = KubeClient()
        list_resp = {"metadata": {"resourceVersion": "1"}, "items": [secret]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        ):
            detail = await client.get_helm_release("default", "web")
        assert detail["revision"] == 2
        assert detail["status"] == "deployed"  # from the Secret label
        assert detail["values"] == {}
        assert "could not be decoded" in detail["warning"]

    async def test_malformed_nested_payload_describes_with_fallbacks(self) -> None:
        """The row survives a mangled payload via label fallbacks; describe on
        that row must degrade the same way instead of raising AttributeError
        outside action_describe's handled exceptions."""
        broken: dict[str, Any] = {"name": "web", "chart": ["bad"], "info": "oops", "config": "x"}
        secret = _secret("web", 2, data={"release": _encode(broken)})
        client = KubeClient()
        list_resp = {"metadata": {"resourceVersion": "1"}, "items": [secret]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        ):
            detail = await client.get_helm_release("default", "web")
        assert detail["revision"] == 2
        assert detail["chart"] == "-"
        assert detail["values"] == {}
        assert detail["status"] == "deployed"  # label fallback, matching the row

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


class TestGetHelmReleaseComponents:
    async def test_components_come_from_the_rendered_manifest(self) -> None:
        payload = _payload()
        payload["manifest"] = (
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web-nginx\n"
            "---\n"
            "apiVersion: v1\nkind: Service\nmetadata:\n  name: web-nginx\n"
        )
        client = KubeClient()
        list_resp = {"items": [_secret("web", 1), _secret("web", 3, payload=payload)]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        ):
            refs = await client.get_helm_release_components("default", "web")
        assert [(r.kind, r.name) for r in refs] == [
            ("Deployment", "web-nginx"),
            ("Service", "web-nginx"),
        ]

    async def test_undecodable_payload_degrades_to_no_components(self) -> None:
        secret = _secret("web", 2, data={"release": base64.b64encode(b"junk").decode()})
        client = KubeClient()
        list_resp = {"items": [secret]}
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        ):
            assert await client.get_helm_release_components("default", "web") == []

    async def test_unknown_release_raises_not_found(self) -> None:
        client = KubeClient()
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value={"items": []})),
            pytest.raises(ApiStatusError, match="not found"),
        ):
            await client.get_helm_release_components("default", "ghost")


class TestListHelmReleases:
    """LIST-only release listing for the helm_list_releases tool (#161)."""

    async def test_latest_revision_per_release(self) -> None:
        client = KubeClient()
        list_resp: dict[str, Any] = {
            "items": [
                _secret("web", 1, "superseded"),
                _secret("web", 3, "deployed"),
                _secret("api", 2, "failed"),
            ]
        }
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", AsyncMock(return_value=list_resp)),
        ):
            releases = await client.list_helm_releases("default")
        assert [(r.name, r.revision, r.status) for r in releases] == [
            ("api", 2, "failed"),
            ("web", 3, "deployed"),
        ]

    async def test_cluster_wide_when_namespace_is_none(self) -> None:
        client = KubeClient()
        request_json = AsyncMock(return_value={"items": []})
        with (
            patch.object(client, "_api", MagicMock()),
            patch.object(client, "_request_json", request_json),
        ):
            assert await client.list_helm_releases(None) == []
        assert request_json.await_args is not None
        path = request_json.await_args.args[0]
        assert path.startswith("/api/v1/secrets?")
        assert "labelSelector=owner%3Dhelm" in path  # helm-owned Secrets only
