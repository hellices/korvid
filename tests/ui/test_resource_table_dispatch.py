from __future__ import annotations

import pytest

from korvid.ui.widgets import resource_table


@pytest.mark.parametrize(
    ("kind", "renderer_name"),
    [
        ("pods", "_render_pod_rows"),
        ("replicasets", "_render_replicaset_rows"),
        ("helmreleases", "_render_helm_release_rows"),
        ("helmrevisions", "_render_helm_revision_rows"),
        ("packagemanifests", "_render_package_rows"),
        ("subscriptions", "_render_subscription_rows"),
        ("clusterserviceversions", "_render_csv_rows"),
        ("deployments", "_render_generic_rows"),
    ],
)
def test_row_renderer_selects_specialized_and_fallback_renderers(
    kind: str, renderer_name: str
) -> None:
    assert resource_table._row_renderer(kind).__name__ == renderer_name
