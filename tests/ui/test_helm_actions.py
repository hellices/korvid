"""Helm install/upgrade/rollback keybindings (issue #31): `i`/`u` on the
`:helm` view and `r` on the revision drill-down shell out to the detected
helm binary — approval-gated and audited fail-closed like every other write.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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
from korvid.k8s.helmcli import ChartHit, HelmCLI, HelmError
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.helm_install import HelmInstallPrompt
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.widgets.resource_table import ResourceTable

from .waits import until

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))

_ALIASES: dict[str, ResourceMeta] = {
    "pods": _PODS_META,
    "helm": HELM_RELEASES_META,
    "helmreleases": HELM_RELEASES_META,
    "helmrevisions": HELM_REVISIONS_META,
}

_NGINX = ChartHit("bitnami/nginx", "18.1.0", "1.27.0", "NGINX Open Source")


class FakeHelm(HelmCLI):
    """Records invocations instead of spawning subprocesses."""

    def __init__(self) -> None:
        super().__init__("/fake/helm")
        self.calls: list[tuple[Any, ...]] = []
        self.hits: list[ChartHit] = [_NGINX]
        self.search_error: str | None = None
        self.install_error: str | None = None
        self.diff_plugin = False
        self.values_seen: str | None = None

    async def search_repo(self, keyword: str = "") -> list[ChartHit]:
        self.calls.append(("search", keyword))
        if self.search_error is not None:
            raise HelmError(self.search_error)
        return self.hits

    async def has_diff_plugin(self) -> bool:
        return self.diff_plugin

    def _snoop_values(self, values_file: str | None) -> None:
        # The temp values file is deleted right after the call: capture its
        # content at invocation time, the only moment it exists.
        self.values_seen = Path(values_file).read_text(encoding="utf-8") if values_file else None

    async def dry_run_install(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
    ) -> str:
        self.calls.append(("dry-run-install", release, chart, namespace, version))
        return "RENDERED-INSTALL-MANIFEST"

    async def install(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
    ) -> str:
        self.calls.append(("install", release, chart, namespace, version))
        self._snoop_values(values_file)
        if self.install_error is not None:
            raise HelmError(self.install_error)
        return "deployed"

    async def dry_run_upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
    ) -> str:
        self.calls.append(("dry-run-upgrade", release, chart, namespace, version))
        return "RENDERED-UPGRADE-MANIFEST"

    async def upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
    ) -> str:
        self.calls.append(("upgrade", release, chart, namespace, version))
        self._snoop_values(values_file)
        return "upgraded"

    async def diff_upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
    ) -> str:
        self.calls.append(("diff-upgrade", release, chart, namespace, version))
        return "+ UPGRADE-DIFF-LINE"

    async def rollback(self, release: str, revision: int, namespace: str) -> str:
        self.calls.append(("rollback", release, revision, namespace))
        return "Rollback was a success!"

    async def diff_rollback(self, release: str, revision: int, namespace: str) -> str:
        self.calls.append(("diff-rollback", release, revision, namespace))
        return "- ROLLBACK-DIFF-LINE"


def _release_row(name: str, chart: str = "nginx-18.1.0") -> HelmReleaseSummary:
    return HelmReleaseSummary(
        name=name,
        namespace="default",
        kind="HelmRelease",
        created="2026-07-26T10:00:00Z",
        uid=release_uid("default", name),
        revision=3,
        status="deployed",
        chart=chart,
        app_version="1.27.0",
    )


def _revision_row(release: str, revision: int) -> HelmRevisionSummary:
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
        "helmreleases": [_release_row("web")],
        "helmrevisions": [_revision_row("web", 2)],
    }


def make_app(
    data: dict[str, list[Summary]] | None = None,
    *,
    helm: HelmCLI | None = None,
    audit_path: Path | None = None,
    readonly: bool = False,
) -> KorvidApp:
    store = ResourceStore()
    rows = data if data is not None else _default_data()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in rows.get(kind, []):
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
        audit=AuditLog(audit_path) if audit_path is not None else None,
        helm=helm,
    )


async def _navigate(pilot: Any, command: str, expect_kind: str) -> None:
    await pilot.press("colon")
    for ch in command:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")
    await until(
        pilot, lambda: pilot.app.current_kind == expect_kind, label=f"view is {expect_kind}"
    )


async def _rows_listed(pilot: Any, app: KorvidApp, n: int) -> None:
    table = app.query_one(ResourceTable)
    await until(pilot, lambda: table.row_count == n, label=f"{n} rows listed")


def _audit_entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_install_key_without_helm_binary_reports_absence(tmp_path: Path) -> None:
    app = make_app(helm=None, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot,
            lambda: any("helm CLI not found" in n.message for n in app._notifications),
            label="absence notified",
        )
        assert len(app.screen_stack) == 1


async def test_install_key_readonly_mode_blocks(tmp_path: Path) -> None:
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot,
            lambda: any("Read-only mode" in n.message for n in app._notifications),
            label="readonly notified",
        )
        assert helm.calls == []


async def test_install_key_without_audit_blocks(tmp_path: Path) -> None:
    """Fail-closed auditing: no audit sink means no helm writes either."""
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=None)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot,
            lambda: any("no audit log" in n.message for n in app._notifications),
            label="audit-less blocked",
        )
        assert helm.calls == []


async def test_install_happy_path_executes_and_audits(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        await pilot.press("enter")  # pick the only chart
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")  # accept prefilled defaults
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks the dialog text
        assert "HELM INSTALL nginx" in operation
        assert "bitnami/nginx 18.1.0" in operation
        assert "namespace default" in operation
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="audited success",
        )
        assert ("install", "nginx", "bitnami/nginx", "default", "18.1.0") in helm.calls
        entries = _audit_entries(audit_path)
        assert entries[0]["action"] == "helm-install"
        assert entries[0]["outcome"] == "intent"  # recorded before the write
        assert entries[-1]["outcome"] == "success"
        assert entries[-1]["kind"] == "helmreleases"
        assert entries[-1]["name"] == "nginx"


async def test_install_preview_shows_dry_run_output(tmp_path: Path) -> None:
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("RENDERED-INSTALL-MANIFEST" in line for line in preview)
        assert ("dry-run-install", "nginx", "bitnami/nginx", "default", "18.1.0") in helm.calls


async def test_install_search_failure_is_reported(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.search_error = "no repositories configured"
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot,
            lambda: any("no repositories configured" in n.message for n in app._notifications),
            label="search failure notified",
        )
        assert len(app.screen_stack) == 1


async def test_install_no_charts_hints_at_repo_add(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.hits = []
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot,
            lambda: any("helm repo add" in n.message for n in app._notifications),
            label="empty search hinted",
        )
        assert len(app.screen_stack) == 1


async def test_install_cancel_at_picker_runs_nothing(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="picker closed")
        assert all(call[0] == "search" for call in helm.calls)
        assert not audit_path.exists()


async def test_install_denied_at_approval_runs_nothing(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("n")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="dialog closed")
        await pilot.pause()
        assert not any(call[0] == "install" for call in helm.calls)
        assert not audit_path.exists()


async def test_install_failure_notifies_and_audits_error(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.install_error = "chart requires kubeVersion >=1.30"
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("y")
        await until(
            pilot,
            lambda: any("kubeVersion" in n.message for n in app._notifications),
            label="failure notified",
        )
        entries = _audit_entries(audit_path)
        assert entries[0]["outcome"] == "intent"
        assert entries[-1]["outcome"].startswith("error:")


async def test_install_with_edited_values_passes_values_file(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)

    async def fake_editor(text: str) -> str | None:
        assert "values override for bitnami/nginx" in text
        return "replicaCount: 3\n"

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        # switch the values field to "edit in $EDITOR"
        from textual.widgets import Select

        from korvid.ui.widgets.helm_install import VALUES_MODES

        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert "edited in $EDITOR" in app.screen._operation  # type: ignore[attr-defined]  # test peeks
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(call[0] == "install" for call in helm.calls),
            label="install executed",
        )
        assert helm.values_seen == "replicaCount: 3\n"


async def test_upgrade_key_reuses_wizard_with_fixed_release(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        # the search was narrowed by the release's chart base name
        assert ("search", "nginx") in helm.calls
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert "HELM UPGRADE web" in app.screen._operation  # type: ignore[attr-defined]  # test peeks
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="audited success",
        )
        assert ("upgrade", "web", "bitnami/nginx", "default", "18.1.0") in helm.calls
        assert _audit_entries(audit_path)[-1]["action"] == "helm-upgrade"


async def test_upgrade_preview_prefers_diff_plugin(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.diff_plugin = True
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="chart picker")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("UPGRADE-DIFF-LINE" in line for line in preview)
        assert not any(call[0] == "dry-run-upgrade" for call in helm.calls)


async def test_rollback_key_on_revision_confirms_and_executes(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks
        assert "HELM ROLLBACK web to revision 2" in operation
        assert "namespace default" in operation
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="audited success",
        )
        assert ("rollback", "web", 2, "default") in helm.calls
        entries = _audit_entries(audit_path)
        assert entries[0]["action"] == "helm-rollback"
        assert entries[0]["outcome"] == "intent"
        assert entries[-1]["outcome"] == "success"


async def test_rollback_preview_uses_diff_plugin_when_present(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.diff_plugin = True
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("ROLLBACK-DIFF-LINE" in line for line in preview)


async def test_rollback_without_helm_binary_reports_absence(tmp_path: Path) -> None:
    app = make_app(helm=None, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        await pilot.press("r")
        await until(
            pilot,
            lambda: any("helm CLI not found" in n.message for n in app._notifications),
            label="absence notified",
        )
        assert len(app.screen_stack) == 1


async def test_helm_keys_do_not_leak_into_other_views(tmp_path: Path) -> None:
    """On non-helm views the same keys keep their original meaning: `r` on a
    pods view must not open a helm rollback dialog (it goes down the rollout
    path, which rejects pods), and helm must never be invoked."""
    helm = FakeHelm()
    data = _default_data()
    data["pods"] = []
    app = make_app(data, helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "pods", "pods")
        await pilot.press("i")
        await pilot.press("u")
        await pilot.press("r")
        await pilot.pause()
        assert helm.calls == []
        assert len(app.screen_stack) == 1
