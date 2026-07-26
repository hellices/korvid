"""Describe footer pointing at the agent for CSP annotation knowledge (issue #30).

On describe of a Service/Ingress in a cluster with a detected provider, a
one-line footer points the user at the agent — a pointer, not a catalog.
"""

from typing import Any

from korvid.ui.widgets.describe_screen import DescribePane, DescribeScreen
from tests.ui.test_app import _pod, make_app
from tests.ui.waits import until


def _service_manifest() -> dict[str, Any]:
    return {
        "kind": "Service",
        "metadata": {"name": "web", "namespace": "default"},
        "spec": {"type": "LoadBalancer"},
    }


def _pod_manifest() -> dict[str, Any]:
    return {"kind": "Pod", "metadata": {"name": "web-1", "namespace": "default"}, "spec": {}}


async def test_service_describe_shows_provider_footer() -> None:
    app = make_app([_pod("web-1")], provider_hint="aks")
    async with app.run_test() as pilot:
        await app._show_describe(False, "services/default/web", _service_manifest(), [])
        await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe modal")
        footer = app.screen.query_one("#describe-footer")
        assert footer.display
        text = str(footer.render())
        assert "provider: aks" in text
        assert "ctrl+a" in text


async def test_ingress_describe_shows_provider_footer() -> None:
    app = make_app([_pod("web-1")], provider_hint="aws")
    manifest = {"kind": "Ingress", "metadata": {"name": "ing", "namespace": "default"}}
    async with app.run_test() as pilot:
        await app._show_describe(False, "ingresses/default/ing", manifest, [])
        await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe modal")
        footer = app.screen.query_one("#describe-footer")
        assert footer.display
        assert "provider: aws" in str(footer.render())


async def test_pod_describe_has_no_footer() -> None:
    app = make_app([_pod("web-1")], provider_hint="aks")
    async with app.run_test() as pilot:
        await app._show_describe(False, "pods/default/web-1", _pod_manifest(), [])
        await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe modal")
        footer = app.screen.query_one("#describe-footer")
        assert not footer.display


async def test_unknown_provider_has_no_footer() -> None:
    app = make_app([_pod("web-1")])  # no provider_hint
    async with app.run_test() as pilot:
        await app._show_describe(False, "services/default/web", _service_manifest(), [])
        await until(pilot, lambda: isinstance(app.screen, DescribeScreen), label="describe modal")
        footer = app.screen.query_one("#describe-footer")
        assert not footer.display


async def test_pane_describe_shows_provider_footer() -> None:
    """The agent-shared (non-modal) pane carries the same footer."""
    app = make_app([_pod("web-1")], provider_hint="gke")
    async with app.run_test() as pilot:
        await app._show_describe(True, "services/default/web", _service_manifest(), [])
        pane = app.query_one(DescribePane)
        await until(pilot, lambda: pane.display, label="describe pane visible")
        footer = pane.query_one("#describe-pane-footer")
        assert footer.display
        assert "provider: gke" in str(footer.render())


async def test_pane_footer_cleared_for_non_service() -> None:
    """A footer from a previous Service describe must not leak onto a Pod."""
    app = make_app([_pod("web-1")], provider_hint="aks")
    async with app.run_test() as pilot:
        await app._show_describe(True, "services/default/web", _service_manifest(), [])
        pane = app.query_one(DescribePane)
        footer = pane.query_one("#describe-pane-footer")
        await until(pilot, lambda: footer.display, label="service footer visible")
        await app._show_describe(True, "pods/default/web-1", _pod_manifest(), [])
        await until(pilot, lambda: not footer.display, label="footer cleared for pod")
        assert not footer.display
