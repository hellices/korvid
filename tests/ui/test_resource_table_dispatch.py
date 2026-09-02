from __future__ import annotations

import pytest

from korvid.core.store import Summary
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
        ("widgets", "_render_generic_rows"),
    ],
)
def test_row_renderer_selects_specialized_and_fallback_renderers(
    kind: str, renderer_name: str
) -> None:
    assert resource_table._row_renderer(kind).__name__ == renderer_name


def test_adapt_standard_renderer_uses_callable_and_display_name() -> None:
    calls: list[tuple[object, list[Summary], bool, str, bool]] = []

    def renderer(
        table: object,
        rows: list[Summary],
        *,
        all_namespaces: bool,
        pattern: str,
        presorted: bool,
    ) -> None:
        calls.append((table, rows, all_namespaces, pattern, presorted))

    adapted = resource_table._adapt_standard_renderer(renderer, "_render_fake_rows")

    assert adapted.__name__ == "_render_fake_rows"

    marker = object()
    rows: list[Summary] = []
    adapted(
        marker,
        rows,
        all_namespaces=True,
        pattern="widgets",
        metrics=None,
        presorted=True,
    )

    assert calls == [(marker, rows, True, "widgets", True)]
