"""Helm release browsing from release Secrets - no helm binary needed.

Helm 3 stores each release revision as a Secret of type
``helm.sh/release.v1`` named ``sh.helm.release.v1.<release>.v<revision>``.
The ``data.release`` value is base64(gzip(json)) and arrives from the API
base64-encoded once more. Labels carry the cheap metadata (``name``,
``version``, ``status``); the payload adds chart/appVersion/values.

Out of scope (issue #31): install/upgrade/rollback, Helm 4 SQL storage,
ConfigMap storage backends.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
import zlib
from dataclasses import dataclass, field
from typing import Any

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary

#: Secret type helm 3 uses for its default (Secret) storage backend.
HELM_SECRET_TYPE = "helm.sh/release.v1"

#: Synthetic metas: these kinds never hit ``/api/v1/helmreleases`` - the
#: client watches Secrets and adapts - but navigation, aliasing, and
#: drill-down all key off ResourceMeta like any real kind.
HELM_RELEASES_META = ResourceMeta(
    "HelmRelease", "helmreleases", "", "v1", True, ("helm",), synthetic=True
)
HELM_REVISIONS_META = ResourceMeta("HelmRevision", "helmrevisions", "", "v1", True, synthetic=True)


def release_uid(namespace: str, name: str) -> str:
    """Stable synthetic uid for a release: the latest revision Secret (and
    its uid) changes on every upgrade, but drill-down must anchor on a
    parent uid that survives them."""
    return f"helm:{namespace}/{name}"


def decode_release(secret: dict[str, Any]) -> dict[str, Any]:
    """Decode a release Secret's payload to the helm release JSON object.

    Raises:
        ValueError: when ``data.release`` is missing or is not
            base64(base64(gzip(json))).
    """
    encoded = (secret.get("data") or {}).get("release")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("Secret has no data.release key")
    try:
        inner = base64.b64decode(encoded, validate=True)
        raw = gzip.decompress(base64.b64decode(inner, validate=True))
        payload = json.loads(raw)
    except (binascii.Error, gzip.BadGzipFile, zlib.error, json.JSONDecodeError, EOFError) as exc:
        raise ValueError(f"not a helm release payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("not a helm release payload: JSON root is not an object")
    return payload


@dataclass(frozen=True)
class HelmReleaseSummary(GenericSummary):
    """One installed release at its latest revision."""

    revision: int = 0
    status: str = ""
    chart: str = "-"
    app_version: str = "-"


@dataclass(frozen=True)
class HelmRevisionSummary(GenericSummary):
    """One revision of a release (drill-down row under a release)."""

    release: str = ""
    revision: int = 0
    status: str = ""
    chart: str = "-"
    app_version: str = "-"
    description: str = ""


def _secret_facts(secret: dict[str, Any]) -> tuple[str, str, int, str, str]:
    meta = secret.get("metadata") or {}
    labels = meta.get("labels") or {}
    name = str(labels.get("name") or "")
    namespace = str(meta.get("namespace") or "")
    try:
        revision = int(labels.get("version") or 0)
    except ValueError:
        revision = 0
    status = str(labels.get("status") or "")
    created = str(meta.get("creationTimestamp") or "")
    return name, namespace, revision, status, created


def _mapping(value: Any) -> dict[str, Any]:
    """The value if it is a JSON object, else {} - nested payload fields can
    be the wrong type in a hand-mangled Secret and must degrade, not raise."""
    return value if isinstance(value, dict) else {}


def _chart_facts(secret: dict[str, Any]) -> tuple[str, str, str]:
    """(chart name-version, app version, description) from the payload;
    label-only fallbacks when the payload does not decode - a corrupt
    Secret must not hide the release from the browser."""
    try:
        payload = decode_release(secret)
    except ValueError:
        return "-", "-", ""
    chart_meta = _mapping(_mapping(payload.get("chart")).get("metadata"))
    chart_name = str(chart_meta.get("name") or "")
    chart_version = str(chart_meta.get("version") or "")
    chart = f"{chart_name}-{chart_version}" if chart_name and chart_version else chart_name or "-"
    app_version = str(chart_meta.get("appVersion") or "-")
    description = str(_mapping(payload.get("info")).get("description") or "")
    return chart, app_version, description


def release_from_secret(secret: dict[str, Any]) -> HelmReleaseSummary:
    """Release summary for one revision Secret (labels + decoded payload)."""
    name, namespace, revision, status, created = _secret_facts(secret)
    chart, app_version, _ = _chart_facts(secret)
    return HelmReleaseSummary(
        name=name,
        namespace=namespace,
        kind="HelmRelease",
        created=created,
        uid=release_uid(namespace, name),
        revision=revision,
        status=status,
        chart=chart,
        app_version=app_version,
    )


def revision_from_secret(secret: dict[str, Any]) -> HelmRevisionSummary:
    """Revision summary; named ``<release>.v<revision>`` so every revision
    keeps a unique ``namespace/name`` row key, owned by the release uid so
    the drill-down filter shows only the parent's history."""
    name, namespace, revision, status, created = _secret_facts(secret)
    chart, app_version, description = _chart_facts(secret)
    return HelmRevisionSummary(
        name=f"{name}.v{revision}",
        namespace=namespace,
        kind="HelmRevision",
        created=created,
        uid=str((secret.get("metadata") or {}).get("uid") or ""),
        owner_uids=(release_uid(namespace, name),),
        release=name,
        revision=revision,
        status=status,
        chart=chart,
        app_version=app_version,
        description=description,
    )


@dataclass
class ReleaseTracker:
    """Folds per-revision Secret events into per-release rows.

    The store keys rows by ``namespace/name``; a release has many revision
    Secrets, so the tracker keeps every live revision and always surfaces
    the highest one (upgrades add a new Secret, uninstall deletes them all).
    """

    _revisions: dict[tuple[str, str], dict[int, HelmReleaseSummary]] = field(default_factory=dict)

    def apply(
        self, event_type: str, rel: HelmReleaseSummary
    ) -> list[tuple[str, HelmReleaseSummary]]:
        """Fold one Secret-level event; return release-level events to emit."""
        key = (rel.namespace, rel.name)
        if event_type == "DELETED":
            revs = self._revisions.get(key)
            if revs is None or rel.revision not in revs:
                # Never surfaced (other namespace, or a revision pruned before
                # our LIST): nothing on screen can change.
                return []
            was_max = rel.revision == max(revs)
            del revs[rel.revision]
            if not revs:
                del self._revisions[key]
                return [("DELETED", rel)]
            if was_max:
                return [("MODIFIED", revs[max(revs)])]
            return []
        revs = self._revisions.setdefault(key, {})
        known = bool(revs)
        had_max = max(revs) if revs else None
        revs[rel.revision] = rel
        if had_max is not None and rel.revision < had_max:
            return []  # an unordered LIST replaying history must not regress the row
        return [("MODIFIED" if known else "ADDED", rel)]
