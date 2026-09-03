from __future__ import annotations

import pytest

from korvid.core.store import Summary
from korvid.k8s.metrics import PodMetrics
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


def test_adapt_standard_renderer_uses_factory_and_display_name() -> None:
    """_adapt_standard_renderer resolves the bound method at call time via the factory."""
    received: list[tuple[list[Summary], bool, str, bool]] = []

    def bound_renderer(
        rows: list[Summary],
        *,
        all_namespaces: bool,
        pattern: str,
        presorted: bool = False,
    ) -> None:
        received.append((rows, all_namespaces, pattern, presorted))

    table = resource_table.ResourceTable()

    adapted = resource_table._adapt_standard_renderer(
        lambda _table: bound_renderer, "_render_fake_rows"
    )

    assert adapted.__name__ == "_render_fake_rows"

    rows: list[Summary] = []
    adapted(
        table,
        rows,
        all_namespaces=True,
        pattern="widgets",
        metrics=None,
        presorted=True,
    )

    assert received == [(rows, True, "widgets", True)]


def test_specialized_renderer_honors_subclass_override_of_add_rows() -> None:
    """Dispatching through _row_renderer must call the bound method on the actual
    table instance so that a subclass override of _add_replicaset_rows is honored,
    not bypassed by an import-time capture of the unbound base-class function.
    """
    override_calls: list[tuple[object, list[Summary], bool, str, bool]] = []

    class _SubTable(resource_table.ResourceTable):
        def _add_replicaset_rows(
            self,
            rows: list[Summary],
            *,
            all_namespaces: bool,
            pattern: str,
            presorted: bool = False,
        ) -> None:
            override_calls.append((self, rows, all_namespaces, pattern, presorted))

    table = _SubTable()
    rows: list[Summary] = []
    renderer = resource_table._row_renderer("replicasets")
    renderer(table, rows, all_namespaces=False, pattern="x", metrics=None, presorted=True)

    assert len(override_calls) == 1, (
        "subclass override of _add_replicaset_rows was not called; "
        "the renderer likely captured the unbound base-class method at import time"
    )
    assert override_calls[0][0] is table
    assert override_calls[0][1] is rows
    assert override_calls[0][2] is False  # all_namespaces
    assert override_calls[0][3] == "x"  # pattern
    assert override_calls[0][4] is True  # presorted


def test_render_pod_rows_forwards_exact_metrics_sentinel_and_all_arguments() -> None:
    """_render_pod_rows must pass the metrics lookup, presorted, all_namespaces, and
    pattern through to _add_pod_rows unchanged (subclass override as the witness).
    """
    received: list[tuple[list[Summary], bool, str, resource_table.MetricsLookup | None, bool]] = []

    def metrics_sentinel(_namespace: str, _name: str) -> PodMetrics | None:
        return None

    class _SubTable(resource_table.ResourceTable):
        def _add_pod_rows(
            self,
            rows: list[Summary],
            *,
            all_namespaces: bool,
            pattern: str,
            metrics: resource_table.MetricsLookup | None,
            presorted: bool = False,
        ) -> None:
            received.append((rows, all_namespaces, pattern, metrics, presorted))

    table = _SubTable()
    rows: list[Summary] = []
    resource_table._render_pod_rows(
        table,
        rows,
        all_namespaces=True,
        pattern="mypod",
        metrics=metrics_sentinel,
        presorted=True,
    )

    assert received == [(rows, True, "mypod", metrics_sentinel, True)]
