"""OLM operator catalog pure helpers (issue #29)."""

from __future__ import annotations

from typing import Any

import pytest

from korvid.k8s.olm import (
    OPERATORS_GROUP,
    PACKAGES_GROUP,
    build_subscription,
    package_description,
    package_install_facts,
)


def _package_manifest(**overrides: Any) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "apiVersion": f"{PACKAGES_GROUP}/v1",
        "kind": "PackageManifest",
        "metadata": {"name": "cert-manager", "namespace": "olm"},
        "status": {
            "catalogSource": "operatorhubio-catalog",
            "catalogSourceNamespace": "olm",
            "defaultChannel": "stable",
            "channels": [
                {"name": "candidate", "currentCSV": "cert-manager.v1.15.0"},
                {"name": "stable", "currentCSV": "cert-manager.v1.14.4"},
            ],
        },
    }
    manifest.update(overrides)
    return manifest


class TestPackageInstallFacts:
    def test_extracts_channels_default_and_catalog(self) -> None:
        facts = package_install_facts(_package_manifest())
        assert facts.package == "cert-manager"
        assert facts.channels == ("candidate", "stable")
        assert facts.default_channel == "stable"
        assert facts.catalog_source == "operatorhubio-catalog"
        assert facts.catalog_source_namespace == "olm"

    def test_malformed_status_yields_empty_facts(self) -> None:
        """A PackageManifest with a missing or mistyped status must not crash
        the install flow - the wizard simply has nothing to preselect."""
        facts = package_install_facts(_package_manifest(status="oops"))
        assert facts.package == "cert-manager"
        assert facts.channels == ()
        assert facts.default_channel == ""
        assert facts.catalog_source == ""

    def test_non_mapping_channel_entries_are_skipped(self) -> None:
        manifest = _package_manifest()
        manifest["status"]["channels"] = ["bad", {"name": "stable"}, {"nope": 1}]
        facts = package_install_facts(manifest)
        assert facts.channels == ("stable",)


class TestBuildSubscription:
    def test_builds_a_complete_subscription_manifest(self) -> None:
        manifest = build_subscription(
            package="cert-manager",
            namespace="operators",
            channel="stable",
            source="operatorhubio-catalog",
            source_namespace="olm",
            approval="Manual",
        )
        assert manifest == {
            "apiVersion": f"{OPERATORS_GROUP}/v1alpha1",
            "kind": "Subscription",
            "metadata": {"name": "cert-manager", "namespace": "operators"},
            "spec": {
                "name": "cert-manager",
                "channel": "stable",
                "source": "operatorhubio-catalog",
                "sourceNamespace": "olm",
                "installPlanApproval": "Manual",
            },
        }

    def test_rejects_unknown_approval_mode(self) -> None:
        with pytest.raises(ValueError, match="approval"):
            build_subscription(
                package="x",
                namespace="ns",
                channel="stable",
                source="cat",
                source_namespace="olm",
                approval="Sometimes",
            )

    @pytest.mark.parametrize(
        "field", ["package", "namespace", "channel", "source", "source_namespace"]
    )
    def test_rejects_blank_required_fields(self, field: str) -> None:
        kwargs = {
            "package": "cert-manager",
            "namespace": "operators",
            "channel": "stable",
            "source": "cat",
            "source_namespace": "olm",
            "approval": "Automatic",
        }
        kwargs[field] = "  "
        with pytest.raises(ValueError, match=field.replace("_", " ")):
            build_subscription(**kwargs)


def test_package_install_facts_tolerates_non_list_channels() -> None:
    """A truthy scalar in status.channels must not crash the wizard path -
    the server stays the sole validator for such a catalog entry."""
    facts = package_install_facts(
        {"metadata": {"name": "p"}, "status": {"channels": 1, "defaultChannel": "stable"}}
    )
    assert facts.channels == ()
    assert facts.default_channel == "stable"


def test_package_description_annotation_is_normalized_to_one_line() -> None:
    """A catalog-controlled multiline annotation must not create a tall
    table row: either description source is reduced to its first line."""
    status = {
        "defaultChannel": "stable",
        "channels": [
            {
                "name": "stable",
                "currentCSVDesc": {
                    "annotations": {"description": "First line.\nSecond line.\nThird."}
                },
            }
        ],
    }
    assert package_description(status) == "First line."


def test_build_subscription_rejects_non_dns1123_package_name() -> None:
    """Catalog entries are cluster-supplied data: a package name that is not
    a DNS-1123 subdomain must fail here, not in the API request path."""
    with pytest.raises(ValueError, match="DNS-1123"):
        build_subscription(
            package="Bad_Name!",
            namespace="operators",
            channel="stable",
            source="cat",
            source_namespace="olm",
            approval="Automatic",
        )
