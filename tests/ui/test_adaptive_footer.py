"""Adaptive footer key legend (issue #114).

The top-docked Footer must show only the keys that act on the current view:
helm's ``i``/``u``/``r`` get dedicated bindings, view-specific bindings are
gated through ``check_action`` (which also routes overloaded keys to the
right action), and the legend refreshes when the view changes. The ``?``
help overlay keeps documenting every view regardless of the current one.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from textual.binding import Binding
from textual.widgets import Footer
from textual.widgets._footer import FooterKey

from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.helm import (
    HELM_RELEASES_META,
    HELM_REVISIONS_META,
    HelmReleaseSummary,
    HelmRevisionSummary,
    release_uid,
)
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.log_pane import LogPane
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_NODES_META = ResourceMeta("Node", "nodes", "", "v1", False, ("no",))
_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_SVC_META = ResourceMeta("Service", "services", "", "v1", True, ("svc",))

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "nodes": _NODES_META,
    "deployments": _DEPLOY_META,
    "deploy": _DEPLOY_META,
    "services": _SVC_META,
    "helm": HELM_RELEASES_META,
    "helmreleases": HELM_RELEASES_META,
    "helmrevisions": HELM_REVISIONS_META,
}


def _pod(name: str) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        qos="-",
    )


def _release(name: str) -> HelmReleaseSummary:
    return HelmReleaseSummary(
        name=name,
        namespace="default",
        kind="HelmRelease",
        created="2026-07-26T10:00:00Z",
        uid=release_uid("default", name),
        revision=3,
        status="deployed",
        chart="nginx-18.1.0",
        app_version="1.27.0",
    )


def _revision(release: str, revision: int) -> HelmRevisionSummary:
    return HelmRevisionSummary(
        name=f"{release}.v{revision}",
        namespace="default",
        kind="HelmRevision",
        created="2026-07-26T10:00:00Z",
        uid=f"secret-uid-{release}-{revision}",
        owner_uids=(release_uid("default", release),),
        release=release,
        revision=revision,
        status="superseded",
        chart="nginx-18.1.0",
        app_version="1.27.0",
        description="Upgrade complete",
    )


def _default_data() -> dict[str, list[Summary]]:
    return {
        "pods": [_pod("api-1")],
        "nodes": [GenericSummary(name="node-a", namespace="", kind="Node", created="")],
        "deployments": [
            GenericSummary(name="web", namespace="default", kind="Deployment", created="")
        ],
        "services": [GenericSummary(name="svc", namespace="default", kind="Service", created="")],
        "helmreleases": [_release("web")],
        "helmrevisions": [_revision("web", 2)],
    }


def make_app(*, audit: AuditLog | None = None) -> KorvidApp:
    store = ResourceStore()
    rows = _default_data()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in rows.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return KorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
        audit=audit,
    )


async def _navigate(pilot: Any, command: str, expect_kind: str) -> None:
    await pilot.press("colon")
    for ch in command:
        await pilot.press(ch)
    await pilot.press("enter")
    await until(
        pilot, lambda: pilot.app.current_kind == expect_kind, label=f"view is {expect_kind}"
    )


async def _rows_listed(pilot: Any, app: KorvidApp) -> None:
    table = app.query_one(ResourceTable)
    await until(pilot, lambda: table.row_count > 0, label="rows listed")


def _app_bindings() -> dict[str, Binding]:
    return {
        binding.id: binding
        for binding in KorvidApp.BINDINGS
        if isinstance(binding, Binding) and binding.id
    }


# ---------------------------------------------------------------------------
# Dedicated helm bindings (no more piggybacking on unrelated action ids)
# ---------------------------------------------------------------------------


def test_helm_actions_have_dedicated_remappable_bindings() -> None:
    bindings = _app_bindings()
    install = bindings["helm_install"]
    assert (install.key, install.action, install.description) == (
        "i",
        "helm_install",
        "Install chart",
    )
    upgrade = bindings["helm_upgrade"]
    assert (upgrade.key, upgrade.action, upgrade.description) == ("u", "helm_upgrade", "Upgrade")
    rollback = bindings["helm_rollback"]
    assert (rollback.key, rollback.action, rollback.description) == (
        "r",
        "helm_rollback",
        "Rollback",
    )


def test_helm_rows_no_longer_piggyback_in_handler_key_help() -> None:
    helm_rows = [row for row in KorvidApp.HANDLER_KEY_HELP if row[0] == "Helm"]
    assert helm_rows == []


# ---------------------------------------------------------------------------
# check_action gates bindings to their views
# ---------------------------------------------------------------------------


async def test_pods_view_gates_node_and_helm_keys() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert active["l"].binding.id == "logs"
        assert active["i"].binding.id == "hint_details"
        assert active["shift+f"].binding.id == "port_forward"
        assert "c" not in active  # cordon is nodes-only
        assert "u" not in active  # uncordon / helm upgrade both off-view
        assert "r" not in active  # restart / helm rollback both off-view


async def test_nodes_view_shows_node_ops_and_hides_pod_keys() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _navigate(pilot, "nodes", "nodes")
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert active["c"].binding.id == "cordon_node"
        assert active["c"].binding.show is True
        assert active["u"].binding.id == "uncordon_node"
        assert active["u"].binding.description == "Uncordon"
        assert active["shift+d"].binding.id == "drain_node"
        assert active["s"].binding.id == "shell"  # node shell stays available
        assert "l" not in active  # logs are pods-only
        assert "i" not in active  # hint details / helm install both off-view


async def test_helm_view_labels_the_overloaded_keys() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert active["i"].binding.id == "helm_install"
        assert active["i"].binding.description == "Install chart"
        assert active["i"].binding.show is True
        assert active["u"].binding.id == "helm_upgrade"
        assert active["u"].binding.description == "Upgrade"
        assert "r" not in active  # rollback lives on the revision drill-down
        assert "c" not in active


async def test_helm_revisions_view_shows_rollback() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert active["r"].binding.id == "helm_rollback"
        assert active["r"].binding.description == "Rollback"
        assert "u" not in active
        assert "i" not in active


async def test_deployments_view_shows_restart_and_scale() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _navigate(pilot, "deploy", "deployments")
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert active["r"].binding.id == "rollout_restart"
        assert active["r"].binding.show is True
        assert active["S"].binding.id == "scale_resource"
        assert active["S"].binding.show is True
        assert "l" not in active
        assert "c" not in active


async def test_services_view_offers_port_forward() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _navigate(pilot, "services", "services")
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert active["shift+f"].binding.id == "port_forward"
        assert "l" not in active


# ---------------------------------------------------------------------------
# Key dispatch routes through the gate
# ---------------------------------------------------------------------------


async def test_u_on_helm_view_starts_the_upgrade_flow(tmp_path: Path) -> None:
    # Audited but no helm binary wired: the flow's own gate must speak up,
    # proving the key routed to the helm action rather than node uncordon.
    app = make_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app)
        await pilot.press("u")
        await until(
            pilot,
            lambda: any("helm CLI not found" in n.message for n in app._notifications),
            label="upgrade flow reached its helm gate",
        )


async def test_gated_key_is_inert_off_view() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)  # pods view
        await pilot.press("c")  # cordon is nodes-only: no dialog, no warning
        await pilot.pause()
        assert len(app.screen_stack) == 1
        assert not any("cordon" in n.message.lower() for n in app._notifications)


# ---------------------------------------------------------------------------
# The help overlay still documents every view
# ---------------------------------------------------------------------------


async def test_help_overlay_documents_offview_keys() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _navigate(pilot, "nodes", "nodes")
        await _rows_listed(pilot, app)
        await pilot.press("question_mark")
        await until(pilot, lambda: isinstance(app.screen, HelpScreen), label="help overlay open")
        assert isinstance(app.screen, HelpScreen)
        body = app.screen.body_text()
        assert "Install chart" in body  # helm keys documented off the helm view
        assert "Logs" in body  # pod keys documented off the pods view
        assert "Cordon" in body


# ---------------------------------------------------------------------------
# The footer legend refreshes when the view changes
# ---------------------------------------------------------------------------


def _footer_descriptions(app: KorvidApp) -> set[str]:
    footer = app.query_one(Footer)
    return {key.description for key in footer.query(FooterKey)}


async def test_footer_legend_follows_view_changes() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        await until(pilot, lambda: "Logs" in _footer_descriptions(app), label="pod keys in footer")
        assert "Cordon" not in _footer_descriptions(app)
        await _navigate(pilot, "nodes", "nodes")
        await _rows_listed(pilot, app)
        await until(
            pilot, lambda: "Cordon" in _footer_descriptions(app), label="node keys in footer"
        )
        assert "Logs" not in _footer_descriptions(app)


# ---------------------------------------------------------------------------
# Log-pane controls follow the visible pane, not the focused view (review)
# ---------------------------------------------------------------------------


async def test_log_pane_controls_hidden_while_no_pane_is_open() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)  # pods view, no log pane yet
        active = app.screen.active_bindings
        assert "f" not in active
        assert "w" not in active
        assert "t" not in active
        assert "p" not in active
        assert "ctrl+s" not in active


async def test_log_pane_controls_survive_focus_on_another_view() -> None:
    """The split workflow tails logs from one pane while the other shows a
    different kind: the pane controls act on the visible stream and must
    stay live (and footer-visible) regardless of the focused view."""
    app = make_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        await app._open_log_pane("default", [("api-1", "app")])
        log_pane = app.query_one(LogPane)
        await until(pilot, lambda: log_pane.display, label="log pane open")
        await until(
            pilot, lambda: "JSON/raw" in _footer_descriptions(app), label="pane keys in footer"
        )
        await pilot.press("ctrl+w", "v")  # split; focus moves to the new pane
        await _navigate(pilot, "deploy", "deployments")
        active = app.screen.active_bindings
        assert active["f"].binding.id == "log_format"
        assert active["p"].binding.id == "log_previous"
        formatted = log_pane.formatted
        await pilot.press("f")  # dispatch must reach the visible pane
        await until(pilot, lambda: log_pane.formatted != formatted, label="format toggled off-view")


async def test_log_pane_controls_vanish_when_the_pane_closes() -> None:
    app = make_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        await app._open_log_pane("default", [("api-1", "app")])
        await until(
            pilot, lambda: "JSON/raw" in _footer_descriptions(app), label="pane keys in footer"
        )
        await app._close_log_pane()
        await until(
            pilot,
            lambda: "JSON/raw" not in _footer_descriptions(app),
            label="pane keys gone from footer",
        )
        assert "f" not in app.screen.active_bindings


# ---------------------------------------------------------------------------
# Generic write keys are gated off synthetic (read-only) views (review)
# ---------------------------------------------------------------------------


async def test_synthetic_helm_views_hide_generic_write_keys() -> None:
    """Helm releases/revisions are synthetic client-side views: the generic
    delete/edit path rejects them, so advertising Ctrl-D / e there would be
    a lie. The dedicated helm write actions remain."""
    app = make_app()
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert active["ctrl+d"].binding.id == "delete_resource"  # real kinds keep it
        assert active["e"].binding.id == "edit_resource"
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert "ctrl+d" not in active
        assert "e" not in active
        assert active["u"].binding.id == "helm_upgrade"  # helm writes stay
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app)
        active = app.screen.active_bindings
        assert "ctrl+d" not in active
        assert "e" not in active
        assert active["r"].binding.id == "helm_rollback"


# ---------------------------------------------------------------------------
# Helm actions guard direct invocation off-view (review round 2)
# ---------------------------------------------------------------------------


async def test_helm_actions_direct_invocation_off_view_warns(tmp_path: Path) -> None:
    """`check_action` gates only key dispatch; a direct action call (e.g. via
    the command palette or a remap race) must not open a Helm write flow with
    a pod/deployment row as the release name. Each action explains itself and
    stops before the Helm gate."""
    app = make_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        await _rows_listed(pilot, app)  # pods view

        def warned() -> bool:
            return any(
                "helm" in str(n.message).lower() and "view" in str(n.message).lower()
                for n in app._notifications
            )

        app.action_helm_install()
        await until(pilot, warned, label="install warned")
        assert len(app.screen_stack) == 1  # no wizard pushed

        app._notifications.clear()
        app.action_helm_upgrade()
        await until(pilot, warned, label="upgrade warned")
        assert len(app.screen_stack) == 1

        app._notifications.clear()
        app.action_helm_rollback()
        await until(pilot, warned, label="rollback warned")
        assert len(app.screen_stack) == 1


async def test_helm_rollback_direct_on_releases_view_warns(tmp_path: Path) -> None:
    """Rollback resolves the selected row against the *revisions* store; on the
    releases view a same-named release row must not be misread as a revision."""
    app = make_app(audit=AuditLog(tmp_path / "audit.jsonl"))
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app)
        app.action_helm_rollback()
        await until(
            pilot,
            lambda: any(
                "helm" in str(n.message).lower() and "view" in str(n.message).lower()
                for n in app._notifications
            ),
            label="rollback warned",
        )
        assert len(app.screen_stack) == 1
