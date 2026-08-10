"""Helm install/upgrade/rollback keybindings (issue #31): `i`/`u` on the
`:helm` view and `r` on the revision drill-down shell out to the detected
helm binary — approval-gated and audited fail-closed like every other write.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from textual.widgets import OptionList, Static

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
from korvid.k8s.helmcli import ChartHit, HelmCLI, HelmError, HelmPreviewUnsupported, HelmRepo
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.widgets.helm_chart_search import HelmChartSearchScreen
from korvid.ui.widgets.helm_install import HelmInstallPrompt
from korvid.ui.widgets.helm_repos import HelmRepoScreen
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
        self.uninstall_error: str | None = None
        self.diff_plugin = False
        self.diff_error: str | None = None
        self.diff_output = "+ UPGRADE-DIFF-LINE"
        self.values_seen: str | None = None
        #: Exceptions raised by successive dry-run calls (install and
        #: upgrade), consumed one per call; an empty list means success.
        self.dry_run_excs: list[Exception] = []
        self.repos = [HelmRepo(name="bitnami", url="https://charts.bitnami.com/bitnami")]

    async def search_repo(self, keyword: str = "") -> list[ChartHit]:
        self.calls.append(("search", keyword))
        if self.search_error is not None:
            raise HelmError(self.search_error)
        return self.hits

    async def has_diff_plugin(self) -> bool:
        return self.diff_plugin

    async def repo_list(self) -> list[HelmRepo]:
        self.calls.append(("repo-list",))
        return list(self.repos)

    async def repo_add(self, name: str, url: str, ca_file: str | None = None) -> str:
        self.calls.append(("repo-add", name, url))
        return f'"{name}" has been added to your repositories'

    async def repo_update(self) -> str:
        self.calls.append(("repo-update",))
        return "Update Complete."

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
        if self.dry_run_excs:
            raise self.dry_run_excs.pop(0)
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
        reuse_values: bool = False,
    ) -> str:
        self.calls.append(("dry-run-upgrade", release, chart, namespace, version, reuse_values))
        if self.dry_run_excs:
            raise self.dry_run_excs.pop(0)
        return "RENDERED-UPGRADE-MANIFEST"

    async def upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
        reuse_values: bool = False,
    ) -> str:
        self.calls.append(("upgrade", release, chart, namespace, version, reuse_values))
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
        reuse_values: bool = False,
    ) -> str:
        self.calls.append(("diff-upgrade", release, chart, namespace, version, reuse_values))
        if self.diff_error is not None:
            raise HelmError(self.diff_error)
        return self.diff_output

    async def rollback(self, release: str, revision: int, namespace: str) -> str:
        self.calls.append(("rollback", release, revision, namespace))
        return "Rollback was a success!"

    async def diff_rollback(self, release: str, revision: int, namespace: str) -> str:
        self.calls.append(("diff-rollback", release, revision, namespace))
        return "- ROLLBACK-DIFF-LINE"

    async def dry_run_uninstall(
        self, release: str, namespace: str, *, keep_history: bool = False
    ) -> str:
        self.calls.append(("dry-run-uninstall", release, namespace, keep_history))
        return "UNINSTALL-DRY-RUN-SUMMARY"

    async def uninstall(self, release: str, namespace: str, *, keep_history: bool = False) -> str:
        self.calls.append(("uninstall", release, namespace, keep_history))
        if self.uninstall_error is not None:
            raise HelmError(self.uninstall_error)
        return f'release "{release}" uninstalled'


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


async def _pick_first_chart(pilot: Any, app: KorvidApp, *, search_first: bool = True) -> None:
    """Drive the search-first picker: Enter searches with the typed keyword
    (empty lists everything), then Enter picks the highlighted chart. The
    upgrade flow pre-searches its chart name, so no explicit search there."""
    await until(
        pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search screen"
    )
    if search_first:
        await pilot.press("enter")
    await until(
        pilot,
        lambda: (
            isinstance(app.screen, HelmChartSearchScreen)
            and app.screen.query_one(OptionList).option_count > 0
        ),
        label="charts listed",
    )
    await pilot.press("enter")


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
        await _pick_first_chart(pilot, app)
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
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("RENDERED-INSTALL-MANIFEST" in line for line in preview)
        title = app.screen._preview_title  # type: ignore[attr-defined]  # test peeks the dialog
        assert title == "helm install --dry-run preview:"
        assert ("dry-run-install", "nginx", "bitnami/nginx", "default", "18.1.0") in helm.calls


async def test_install_search_failure_is_reported(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.search_error = "no repositories configured"
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.press("enter")  # search everything
        await until(
            pilot,
            lambda: (
                "no repositories configured"
                in str(app.screen.query_one("#chart-status", Static).render())
            ),
            label="search failure shown",
        )
        # the screen stays open so the user can fix the keyword or add a repo
        assert isinstance(app.screen, HelmChartSearchScreen)


async def test_install_no_charts_hints_at_repo_add(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.hits = []
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.press("enter")
        await until(
            pilot,
            lambda: (
                "add a repository" in str(app.screen.query_one("#chart-status", Static).render())
            ),
            label="empty search hinted",
        )
        assert isinstance(app.screen, HelmChartSearchScreen)


async def test_install_cancel_at_picker_runs_nothing(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="picker closed")
        # search-first (issue #106): nothing is fetched until the user searches
        assert helm.calls == []
        assert not audit_path.exists()


async def test_install_denied_at_approval_runs_nothing(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
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
        await _pick_first_chart(pilot, app)
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
        await _pick_first_chart(pilot, app)
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


# ---------------------------------------------------------------------------
# Dry-run render failures stop the flow before approval (issue #139)
# ---------------------------------------------------------------------------

_RENDER_ERROR = "execution error at (nginx/templates/NOTES.txt:2:3): 'image.repository' must be set"


async def _drive_install_to_preview(pilot: Any, app: KorvidApp) -> None:
    await _navigate(pilot, "helm", "helmreleases")
    await pilot.press("i")
    await _pick_first_chart(pilot, app)
    await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
    await pilot.press("enter")


async def test_install_dry_run_render_failure_stops_before_approval(tmp_path: Path) -> None:
    """A failing dry-run render proves the real install would fail the same
    way: the flow must stop with the helm stderr shown, not open an approval
    dialog for a doomed install."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmError(_RENDER_ERROR)]
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _drive_install_to_preview(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        title = app.screen._pick_title  # type: ignore[attr-defined]  # test peeks the dialog
        assert "image.repository" in title  # helm's actionable stderr is shown
        await pilot.press("escape")  # cancel
        await until(pilot, lambda: len(app.screen_stack) == 1, label="dialog closed")
        await until(
            pilot,
            lambda: not any(w.group == "helm-write" and not w.is_finished for w in app.workers),
            label="helm flow finished",
        )
        assert not any(call[0] == "install" for call in helm.calls)
        assert not audit_path.exists()


async def test_install_render_failure_edit_values_and_retry(tmp_path: Path) -> None:
    """From the failure dialog, "edit values and retry" reopens the editor
    and re-renders; a fixed render proceeds to the normal approval."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmError(_RENDER_ERROR)]
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    templates_seen: list[str] = []

    async def fake_editor(text: str) -> str | None:
        templates_seen.append(text)
        return "image:\n  repository: otel/opentelemetry-collector\n"

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _drive_install_to_preview(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        await pilot.press("enter")  # first option: edit values and retry
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert templates_seen
        assert "values override for bitnami/nginx" in templates_seen[0]
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("RENDERED-INSTALL-MANIFEST" in line for line in preview)
        assert "edited in $EDITOR" in app.screen._operation  # type: ignore[attr-defined]  # test peeks
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(call[0] == "install" for call in helm.calls),
            label="install executed",
        )
        assert helm.values_seen == "image:\n  repository: otel/opentelemetry-collector\n"


async def test_install_render_failure_retry_preserves_edited_values(tmp_path: Path) -> None:
    """Retrying after a failed render must reopen the editor with the
    previous inputs intact, not a blank template."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmError(_RENDER_ERROR), HelmError(_RENDER_ERROR)]
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    edits: list[str] = []

    async def fake_editor(text: str) -> str | None:
        edits.append(text)
        return f"attempt: {len(edits)}\n"

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        from textual.widgets import Select

        from korvid.ui.widgets.helm_install import VALUES_MODES

        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        await pilot.press("enter")  # editor #1 -> dry-run fails
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        await pilot.press("enter")  # edit values and retry -> editor #2 -> fails again
        await until(
            pilot,
            lambda: isinstance(app.screen, PickScreen) and len(edits) == 2,
            label="second failure dialog",
        )
        assert edits[1] == "attempt: 1\n"  # previous inputs, not the template
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="cancelled")


async def test_render_failure_retry_preserves_a_comments_only_buffer(tmp_path: Path) -> None:
    """A buffer with only comments normalizes to "no override" for helm,
    but the retry editor must still reopen with that buffer - the user's
    commented notes are prior inputs too."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmError(_RENDER_ERROR)]
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    edits: list[str] = []

    async def fake_editor(text: str) -> str | None:
        edits.append(text)
        if len(edits) == 1:
            return "# my note: try image.repository next\n"
        return "image:\n  repository: otel\n"

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        from textual.widgets import Select

        from korvid.ui.widgets.helm_install import VALUES_MODES

        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        await pilot.press("enter")  # editor #1 (comments only) -> dry-run fails
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        await pilot.press("enter")  # edit values and retry
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert edits[1] == "# my note: try image.repository next\n"


async def test_install_render_failure_retry_preview_without_editing(tmp_path: Path) -> None:
    """ "retry preview" re-runs the dry-run unchanged — the escape hatch for
    transient failures (registry blip) that clear on their own."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmError("connection refused fetching chart")]
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _drive_install_to_preview(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        await pilot.press("down")  # second option: retry preview
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert sum(1 for call in helm.calls if call[0] == "dry-run-install") == 2


async def test_old_helm_hide_secret_rejection_does_not_block_approval(tmp_path: Path) -> None:
    """helm < 3.15 rejects the preview-only `--hide-secret` flag: that is a
    preview incompatibility, not a verdict on the install (the real command
    never carries the flag) — approval must stay available."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmPreviewUnsupported("Error: unknown flag: --hide-secret")]
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _drive_install_to_preview(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("preview unavailable" in line for line in preview)


async def test_install_preview_environmental_failure_notes_unavailable(tmp_path: Path) -> None:
    """A non-helm preview failure (timeout etc.) must not block approval —
    but the dialog says the preview was unavailable instead of silently
    showing none."""
    helm = FakeHelm()
    helm.dry_run_excs = [TimeoutError()]
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _drive_install_to_preview(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("preview unavailable" in line for line in preview)


async def test_upgrade_diff_failure_falls_back_to_dry_run_preview(tmp_path: Path) -> None:
    """A helm-diff plugin failure is not a verdict on the upgrade: fall back
    to the plain --dry-run render instead of dropping the preview."""
    helm = FakeHelm()
    helm.diff_plugin = True
    helm.diff_error = "diff plugin exploded"
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("RENDERED-UPGRADE-MANIFEST" in line for line in preview)
        title = app.screen._preview_title  # type: ignore[attr-defined]  # test peeks the dialog
        assert title == "helm upgrade --dry-run preview:"


async def test_empty_diff_says_no_changes_not_preview_unavailable(tmp_path: Path) -> None:
    """`helm diff upgrade` returns an empty diff when nothing changes: that
    is a successful render whose answer is "no changes", not an incomplete
    one - the dialog must not claim the preview was unavailable."""
    helm = FakeHelm()
    helm.diff_plugin = True
    helm.diff_output = ""
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        # [] renders as ConfirmScreen's explicit "no changes reported" line
        assert preview == []
        title = app.screen._preview_title  # type: ignore[attr-defined]  # test peeks the dialog
        assert title == "helm diff upgrade preview:"


async def test_render_failure_dialog_keeps_options_reachable_with_long_stderr(
    tmp_path: Path,
) -> None:
    """Multi-line helm stderr rides in the failure dialog's title: it must
    scroll inside a capped area instead of pushing the choices off-screen."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmError("\n".join(f"error line {i}" for i in range(40)))]
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _drive_install_to_preview(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        from textual.containers import VerticalScroll
        from textual.widgets import OptionList

        # the long title lives inside the capped scroll area...
        scroll = app.screen.query_one("#pick-title-scroll", VerticalScroll)
        assert scroll.query_one("#pick-title") is not None
        # ...and the options stay reachable: picking cancel works normally
        options = app.screen.query_one(OptionList)
        assert options.option_count == 3
        await pilot.press("down", "down", "enter")  # cancel
        await until(pilot, lambda: len(app.screen_stack) == 1, label="cancelled")
        assert not any(call[0] == "install" for call in helm.calls)


async def test_upgrade_dry_run_render_failure_stops_before_approval(tmp_path: Path) -> None:
    """The same stop-before-approval applies to upgrades."""
    helm = FakeHelm()
    helm.dry_run_excs = [HelmError(_RENDER_ERROR)]
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="cancelled")
        await until(
            pilot,
            lambda: not any(w.group == "helm-write" and not w.is_finished for w in app.workers),
            label="helm flow finished",
        )
        assert not any(call[0] == "upgrade" for call in helm.calls)
        assert not audit_path.exists()


async def test_upgrade_key_reuses_wizard_with_fixed_release(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        # the search was narrowed by the release's chart base name
        assert ("search", "nginx") in helm.calls
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert "HELM UPGRADE web" in app.screen._operation  # type: ignore[attr-defined]  # test peeks
        # the wizard's upgrade default keeps the release's current overrides
        assert "values: reuse current values" in app.screen._operation  # type: ignore[attr-defined]  # test peeks
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="audited success",
        )
        assert ("upgrade", "web", "bitnami/nginx", "default", "18.1.0", True) in helm.calls
        assert _audit_entries(audit_path)[-1]["action"] == "helm-upgrade"


async def test_upgrade_preview_prefers_diff_plugin(tmp_path: Path) -> None:
    helm = FakeHelm()
    helm.diff_plugin = True
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("UPGRADE-DIFF-LINE" in line for line in preview)
        title = app.screen._preview_title  # type: ignore[attr-defined]  # test peeks the dialog
        assert title == "helm diff upgrade preview:"
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
        title = app.screen._preview_title  # type: ignore[attr-defined]  # test peeks the dialog
        assert title == "helm diff rollback preview:"


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


async def test_uninstall_ctrl_d_on_release_confirms_and_executes(tmp_path: Path) -> None:
    """Ctrl+D on a release row (issue #117): dry-run preview, typed-name
    approval, fail-closed audit, then `helm uninstall`."""
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        operation = app.screen._operation  # type: ignore[attr-defined]  # test peeks
        assert "HELM UNINSTALL web" in operation
        assert "namespace default" in operation
        # High blast radius: the release name must be typed, y alone is inert.
        await pilot.press("y")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        assert ("uninstall", "web", "default", False) not in helm.calls
        await pilot.press("backspace")  # the y landed in the name input
        for ch in "web":
            await pilot.press(ch)
        await pilot.press("enter")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="audited success",
        )
        assert ("uninstall", "web", "default", False) in helm.calls
        entries = _audit_entries(audit_path)
        assert entries[0]["action"] == "helm-uninstall"
        assert entries[0]["outcome"] == "intent"
        assert entries[-1]["outcome"] == "success"


async def test_uninstall_preview_shows_dry_run_output(tmp_path: Path) -> None:
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert ("dry-run-uninstall", "web", "default", False) in helm.calls
        preview = app.screen._preview  # type: ignore[attr-defined]  # test peeks the dialog
        assert preview is not None
        assert any("UNINSTALL-DRY-RUN-SUMMARY" in line for line in preview)
        title = app.screen._preview_title  # type: ignore[attr-defined]  # test peeks
        assert title == "helm uninstall --dry-run preview:"


async def test_uninstall_declined_runs_nothing(tmp_path: Path) -> None:
    helm = FakeHelm()
    audit_path = tmp_path / "audit.jsonl"
    app = make_app(helm=helm, audit_path=audit_path)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("ctrl+d")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="dialog dismissed")
        assert not any(call[0] == "uninstall" for call in helm.calls)
        assert not audit_path.exists() or "helm-uninstall" not in audit_path.read_text()


async def test_uninstall_without_helm_binary_reports_absence(tmp_path: Path) -> None:
    app = make_app(helm=None, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: any("helm CLI not found" in n.message for n in app._notifications),
            label="absence notified",
        )
        assert len(app.screen_stack) == 1


async def test_uninstall_readonly_mode_blocks(tmp_path: Path) -> None:
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl", readonly=True)
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("ctrl+d")
        await until(
            pilot,
            lambda: any("Read-only mode" in n.message for n in app._notifications),
            label="readonly notified",
        )
        assert helm.calls == []
        assert len(app.screen_stack) == 1


async def test_uninstall_ctrl_d_on_revision_drilldown_stays_blocked(tmp_path: Path) -> None:
    """The revision drill-down rows are individual history Secrets, not the
    release: ctrl+d must stay inert there (issue #117)."""
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        await pilot.press("ctrl+d")
        await pilot.pause()
        assert helm.calls == []
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


class GatedPreviewHelm(FakeHelm):
    """The upgrade dry-run blocks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def dry_run_upgrade(
        self,
        release: str,
        chart: str,
        namespace: str,
        *,
        version: str | None = None,
        values_file: str | None = None,
        reuse_values: bool = False,
    ) -> str:
        await self.gate.wait()
        return await super().dry_run_upgrade(
            release,
            chart,
            namespace,
            version=version,
            values_file=values_file,
            reuse_values=reuse_values,
        )


async def test_install_opens_picker_instantly_without_fetching_charts(tmp_path: Path) -> None:
    """Issue #106: the picker is search-first. Opening it must not run
    `helm search repo` over every configured repository — the modal appears
    immediately and charts are fetched only when the user searches."""
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.pause()
        assert helm.calls == []


async def test_install_picker_ctrl_r_manages_repositories(tmp_path: Path) -> None:
    """Ctrl-R inside the chart picker opens repository management wired to
    the app's helm CLI: the configured repos are listed."""
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.press("ctrl+r")
        await until(pilot, lambda: isinstance(app.screen, HelmRepoScreen), label="repo screen")
        await until(
            pilot,
            lambda: app.screen.query_one("#repo-list", OptionList).option_count == 1,
            label="repos listed",
        )
        assert ("repo-list",) in helm.calls


class GatedRollbackPreviewHelm(FakeHelm):
    """The rollback diff blocks until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.diff_plugin = True

    async def diff_rollback(self, release: str, revision: int, namespace: str) -> str:
        await self.gate.wait()
        return await super().diff_rollback(release, revision, namespace)


async def test_upgrade_aborts_when_selection_changes_during_preview(tmp_path: Path) -> None:
    """The preview runs after the wizard closes, over an interactive table:
    a selection/view change during it must cancel the confirmation, like
    every other write flow after an awaited preview."""
    helm = GatedPreviewHelm()
    data = _default_data()
    data["pods"] = []
    app = make_app(data, helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")  # preview now pending on the gate
        await _navigate(pilot, "pods", "pods")
        helm.gate.set()
        await until(
            pilot,
            lambda: any("cancelled" in n.message for n in app._notifications),
            label="abort notified",
        )
        assert len(app.screen_stack) == 1
        assert not any(call[0] == "upgrade" for call in helm.calls)


async def test_preview_progress_shows_while_render_pending_and_clears_after(
    tmp_path: Path,
) -> None:
    """The dry-run render can take up to 20s: the status bar must show
    progress exactly while `_helm_change_preview` is awaited (issue #106) —
    present while the render is pending, gone once the dialog opens."""
    from korvid.ui.widgets.status_bar import StatusBar

    def _bar(app: KorvidApp) -> str:
        return str(app.query_one(StatusBar).render())

    helm = GatedPreviewHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("u")
        await _pick_first_chart(pilot, app, search_first=False)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("enter")  # preview now pending on the gate
        await until(
            pilot, lambda: "helm preview" in _bar(app), label="progress visible during render"
        )
        helm.gate.set()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert "helm preview" not in _bar(app)
        await pilot.press("n")


async def test_rollback_preview_progress_shows_and_clears(tmp_path: Path) -> None:
    """Same lifecycle guarantee for the rollback diff preview."""
    from korvid.ui.widgets.status_bar import StatusBar

    def _bar(app: KorvidApp) -> str:
        return str(app.query_one(StatusBar).render())

    helm = GatedRollbackPreviewHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        await pilot.press("r")
        await until(
            pilot, lambda: "rollback preview" in _bar(app), label="progress visible during render"
        )
        helm.gate.set()
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert "rollback preview" not in _bar(app)
        await pilot.press("n")


async def test_install_and_upgrade_pickers_open_synchronously_with_the_keypress(
    tmp_path: Path,
) -> None:
    """The modal-opening prefix has no awaits and must run inside the action
    itself: buffered navigation keys processed before a deferred worker
    could otherwise re-derive the target from a different view/row."""
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await _rows_listed(pilot, app, 1)
        await pilot.press("i")
        assert isinstance(app.screen, HelmChartSearchScreen)  # no until: synchronous
        await pilot.press("escape")
        await until(pilot, lambda: len(app.screen_stack) == 1, label="picker closed")
        await pilot.press("u")
        assert isinstance(app.screen, HelmChartSearchScreen)


async def test_rollback_target_is_captured_by_the_action_not_the_worker(tmp_path: Path) -> None:
    """The rollback worker must receive the row selected at keypress time as
    explicit arguments — reading the selection inside the worker would race
    buffered cursor keys."""
    from unittest import mock

    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    seen: list[tuple[Any, ...]] = []

    async def spy(helm_arg: Any, row: Any, ns: Any, name: Any, namespace: Any, epoch: Any) -> None:
        seen.append((row.release, row.revision, namespace))

    async with app.run_test() as pilot:
        await _navigate(pilot, "helmrevisions", "helmrevisions")
        await _rows_listed(pilot, app, 1)
        with mock.patch.object(app._helm_ctl, "rollback", spy):
            await pilot.press("r")
            # captured synchronously: the facts exist before any worker ran
            assert seen == [("web", 2, "default")]


async def test_progress_labels_are_owner_scoped(tmp_path: Path) -> None:
    """Drain and helm previews can overlap (different worker groups): one
    finishing must not clear the other's status-bar label."""
    from korvid.ui.widgets.status_bar import StatusBar

    app = make_app(helm=FakeHelm(), audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        app._set_progress("drain", "evicting pods 1/5")
        with app._progress("rendering helm preview (dry-run)"):
            bar = str(app.query_one(StatusBar).render())
            assert "evicting pods 1/5" in bar
            assert "rendering helm preview" in bar
        bar = str(app.query_one(StatusBar).render())
        assert "evicting pods 1/5" in bar  # drain label survived the helm scope
        assert "rendering helm preview" not in bar
        app._set_progress("drain", "")
        assert "evicting" not in str(app.query_one(StatusBar).render())


async def test_stale_progress_cleanup_cannot_clear_the_replacements_label(
    tmp_path: Path,
) -> None:
    """An exclusive `helm-write` replacement can publish its progress label
    before the cancelled predecessor's cleanup runs: the stale cleanup must
    only clear the label it owns."""
    from korvid.ui.widgets.status_bar import StatusBar

    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        first = app._progress("first preview")
        first.__enter__()
        second = app._progress("second preview")
        second.__enter__()
        first.__exit__(None, None, None)  # stale predecessor cleanup runs late
        await pilot.pause()
        assert "second preview" in str(app.query_one(StatusBar).render())
        second.__exit__(None, None, None)
        await pilot.pause()
        assert "second preview" not in str(app.query_one(StatusBar).render())


async def test_enter_on_a_repo_row_browses_that_repos_charts(tmp_path: Path) -> None:
    """Repo-centric browsing (issue #137): Enter on a repository row closes
    the repo screen and scopes the chart search to that repo (the
    `repoName/` prefix convention, typed for you)."""
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.press("ctrl+r")
        await until(pilot, lambda: isinstance(app.screen, HelmRepoScreen), label="repo screen")
        await until(
            pilot,
            lambda: app.screen.query_one(OptionList).option_count == 1,
            label="repos listed",
        )
        app.screen.query_one(OptionList).focus()
        await pilot.press("down")  # highlight the bitnami repo row
        await pilot.press("enter")  # pick it
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="back to search"
        )
        await until(
            pilot, lambda: ("search", "bitnami/") in helm.calls, label="repo-scoped search ran"
        )
        from textual.widgets import Input

        assert app.screen.query_one("#chart-keyword", Input).value == "bitnami/"
        # the scoped results are pickable exactly like a keyword search
        await until(
            pilot,
            lambda: app.screen.query_one(OptionList).option_count == 1,
            label="charts listed",
        )


async def test_escape_on_repo_screen_keeps_the_search_keyword(tmp_path: Path) -> None:
    """Closing the repo screen without picking a repo must not rewrite the
    search keyword underneath."""
    helm = FakeHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        from textual.widgets import Input

        app.screen.query_one("#chart-keyword", Input).value = "nginx"
        await pilot.press("ctrl+r")
        await until(pilot, lambda: isinstance(app.screen, HelmRepoScreen), label="repo screen")
        await pilot.press("escape")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="back to search"
        )
        assert app.screen.query_one("#chart-keyword", Input).value == "nginx"


async def test_repo_browse_filters_to_the_exact_repo_prefix(tmp_path: Path) -> None:
    """`helm search repo` substring-matches, so browsing `stable` would also
    surface `my-stable/...` charts: the browse must filter hits to the exact
    `repo/` name prefix."""
    helm = FakeHelm()
    helm.hits = [
        ChartHit("stable/good", "1.0.0", "1.0", "in-scope"),
        ChartHit("my-stable/sneaky", "2.0.0", "2.0", "substring match, other repo"),
    ]
    helm.repos = [HelmRepo(name="stable", url="https://charts.example/stable")]
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.press("ctrl+r")
        await until(pilot, lambda: isinstance(app.screen, HelmRepoScreen), label="repo screen")
        await until(
            pilot,
            lambda: app.screen.query_one(OptionList).option_count == 1,
            label="repos listed",
        )
        app.screen.query_one(OptionList).focus()
        await pilot.press("down", "enter")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="back to search"
        )
        await until(
            pilot,
            lambda: app.screen.query_one(OptionList).option_count == 1,
            label="only in-scope chart listed",
        )
        options = app.screen.query_one(OptionList)
        assert "stable/good" in str(options.get_option_at_index(0).prompt)
        # a manual keyword search afterwards is unfiltered again
        await pilot.press("enter")  # picks stable/good -> wizard; close it
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await pilot.press("escape")


async def test_repo_pick_is_rejected_while_a_mutation_is_pending(tmp_path: Path) -> None:
    """A pending `helm repo add` owns the screen: Enter on a repo row must
    not dismiss it mid-mutation (dismissal cancels the worker and kills the
    subprocess)."""
    import asyncio as aio

    gate: aio.Event = aio.Event()

    class GatedRepoAddHelm(FakeHelm):
        async def repo_add(self, name: str, url: str, ca_file: str | None = None) -> str:
            await gate.wait()
            return await super().repo_add(name, url)

    helm = GatedRepoAddHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await until(
            pilot, lambda: isinstance(app.screen, HelmChartSearchScreen), label="chart search"
        )
        await pilot.press("ctrl+r")
        await until(pilot, lambda: isinstance(app.screen, HelmRepoScreen), label="repo screen")
        await until(
            pilot,
            lambda: app.screen.query_one(OptionList).option_count == 1,
            label="repos listed",
        )
        from textual.widgets import Input

        app.screen.query_one("#repo-name", Input).value = "extra"
        app.screen.query_one("#repo-url", Input).value = "https://charts.example/extra"
        app.screen.query_one("#repo-url", Input).focus()
        await pilot.press("enter")  # add starts, gated -> mutation pending
        app.screen.query_one(OptionList).focus()
        await pilot.press("down", "enter")  # browse attempt mid-mutation
        await pilot.pause()
        assert isinstance(app.screen, HelmRepoScreen)  # still owned by the add
        gate.set()
        await until(
            pilot,
            lambda: ("repo-add", "extra", "https://charts.example/extra") in helm.calls,
            label="mutation completed",
        )


# ---------------------------------------------------------------------------
# Chart metadata in the install flow (issue #151)
# ---------------------------------------------------------------------------


async def test_editor_prefills_with_the_charts_default_values(tmp_path: Path) -> None:
    """'edit in $EDITOR' opens on `helm show values` output - the chart's
    own annotated defaults, exactly like the CLI workflow - instead of the
    2-line comment stub (issue #151)."""

    class ShowValuesHelm(FakeHelm):
        async def show_values(self, chart: str, version: str = "") -> str:
            self.calls.append(("show-values", chart, version))
            return '# Default values for nginx\nmode: ""\nreplicaCount: 1\n'

    helm = ShowValuesHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    seen: list[str] = []

    async def fake_editor(text: str) -> str | None:
        seen.append(text)
        return text  # unchanged content

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        from textual.widgets import Select

        from korvid.ui.widgets.helm_install import VALUES_MODES

        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        assert seen
        assert "replicaCount: 1" in seen[0]  # the chart's own defaults
        assert ("show-values", "bitnami/nginx", "18.1.0") in helm.calls
        # unchanged defaults == no override (issue #151): same semantics as
        # the old comments-only stub - the dialog says chart defaults.
        assert "values: chart defaults" in app.screen._operation  # type: ignore[attr-defined]  # test peeks


async def test_editor_falls_back_to_the_stub_when_show_values_fails(tmp_path: Path) -> None:
    class BrokenShowValuesHelm(FakeHelm):
        async def show_values(self, chart: str, version: str = "") -> str:
            raise HelmError("no such chart")

    helm = BrokenShowValuesHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    seen: list[str] = []

    async def fake_editor(text: str) -> str | None:
        seen.append(text)
        return None  # abort after capturing the template

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        from textual.widgets import Select

        from korvid.ui.widgets.helm_install import VALUES_MODES

        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        await pilot.press("enter")
        await until(pilot, lambda: bool(seen), label="editor opened")
        assert "values override for bitnami/nginx" in seen[0]  # the old stub


async def test_wizard_receives_schema_and_readme_providers(tmp_path: Path) -> None:
    """The app wires helm's show_schema/show_readme into the wizard: a chart
    with a schema renders the Required values section end to end."""

    class SchemaHelm(FakeHelm):
        async def show_schema(self, chart: str, version: str = "") -> dict[str, Any] | None:
            self.calls.append(("show-schema", chart, version))
            return {
                "required": ["mode"],
                "properties": {"mode": {"type": "string", "enum": ["deployment"]}},
            }

        async def show_readme(self, chart: str, version: str = "") -> str:
            return "# README"

    helm = SchemaHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        await until(
            pilot,
            lambda: "mode" in str(app.screen.query_one("#helm-required", Static).render()),
            label="required section rendered",
        )
        assert ("show-schema", "bitnami/nginx", "18.1.0") in helm.calls


async def test_editor_prefill_passes_an_empty_version_through_as_latest(tmp_path: Path) -> None:
    """An empty wizard version means "latest" for the install: the values
    prefill must ask helm for the same (no --version pin), not the
    search-time version - the defaults must describe the chart helm will
    actually install."""

    class ShowValuesHelm(FakeHelm):
        async def show_values(self, chart: str, version: str = "") -> str:
            self.calls.append(("show-values", chart, version))
            return "replicaCount: 1\n"

    helm = ShowValuesHelm()
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")

    async def fake_editor(text: str) -> str | None:
        return None  # abort after the prefill fetch

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        from textual.widgets import Input, Select

        from korvid.ui.widgets.helm_install import VALUES_MODES

        app.screen.query_one("#helm-version", Input).value = ""  # latest
        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        app.screen.query_one("#helm-release", Input).focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: ("show-values", "bitnami/nginx", "") in helm.calls,
            label="prefill fetched without a version pin",
        )


async def test_unchanged_defaults_stay_defaults_across_a_render_failure_retry(
    tmp_path: Path,
) -> None:
    """The prefetched defaults baseline must survive the failure dialog's
    'edit values and retry' loop: returning the defaults unchanged on the
    retry pass must still mean 'chart defaults' - not freeze the whole
    defaults file into the release as a custom override."""
    defaults = "# defaults\nreplicaCount: 1\n"

    class ShowValuesHelm(FakeHelm):
        async def show_values(self, chart: str, version: str = "") -> str:
            return defaults

    helm = ShowValuesHelm()
    helm.dry_run_excs = [HelmError(_RENDER_ERROR)]  # first render fails
    app = make_app(helm=helm, audit_path=tmp_path / "audit.jsonl")

    async def fake_editor(text: str) -> str | None:
        return text  # both passes: return the buffer unchanged

    app._edit_text = fake_editor
    async with app.run_test() as pilot:
        await _navigate(pilot, "helm", "helmreleases")
        await pilot.press("i")
        await _pick_first_chart(pilot, app)
        await until(pilot, lambda: isinstance(app.screen, HelmInstallPrompt), label="wizard")
        from textual.widgets import Select

        from korvid.ui.widgets.helm_install import VALUES_MODES

        app.screen.query_one("#helm-values", Select).value = VALUES_MODES[1]
        await pilot.press("enter")  # editor #1 -> dry-run fails
        await until(pilot, lambda: isinstance(app.screen, PickScreen), label="failure dialog")
        await pilot.press("enter")  # edit values and retry -> editor #2, unchanged
        await until(pilot, lambda: isinstance(app.screen, ConfirmScreen), label="approval")
        # unchanged defaults are still 'chart defaults', not a frozen override
        assert "values: chart defaults" in app.screen._operation  # type: ignore[attr-defined]  # test peeks
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(call[0] == "install" for call in helm.calls),
            label="install executed",
        )
        assert helm.values_seen is None  # no -f override was passed


async def test_helm_gate_blocks_every_write_precondition(tmp_path: Path) -> None:
    """Characterization for the extraction in issue #187.

    `HelmController.gate` is the single choke point every helm write flow passes
    through, and it enforces three separate refusals. Pinning them here as
    one statement means moving this logic into a controller cannot quietly
    drop one of them: the flows that call it are covered elsewhere, but
    nothing asserted the gate's own contract.
    """
    helm = FakeHelm()

    readonly_app = make_app(helm=helm, audit_path=tmp_path / "a.jsonl", readonly=True)
    assert readonly_app._helm_ctl.gate() is None, "read-only mode must refuse helm writes"

    unaudited = make_app(helm=helm, audit_path=None)
    assert unaudited._helm_ctl.gate() is None, "fail-closed audit must refuse helm writes"

    no_binary = make_app(helm=None, audit_path=tmp_path / "b.jsonl")
    assert no_binary._helm_ctl.gate() is None, "a missing helm binary must refuse helm writes"

    permitted = make_app(helm=helm, audit_path=tmp_path / "c.jsonl")
    assert permitted._helm_ctl.gate() is helm, "a fully configured app returns its helm client"


async def test_gate_returns_the_client_it_checked(tmp_path: Path) -> None:
    """The gate must read the helm wrapper once.

    Checking one read and returning another lets a `:ctx` switch that rebinds
    `KorvidApp._helm` between the two hand back a client the check never saw -
    including one bound to the previous cluster, which would write there.
    """
    first, second = FakeHelm(), FakeHelm()
    app = make_app(helm=first, audit_path=tmp_path / "audit.jsonl")
    reads: list[int] = []

    def rebinding_helm() -> HelmCLI:
        reads.append(1)
        return first if len(reads) == 1 else second

    app._helm_ctl._helm = rebinding_helm  # simulates a :ctx rebind mid-gate

    assert app._helm_ctl.gate() is first, "returned a client the check never saw"
