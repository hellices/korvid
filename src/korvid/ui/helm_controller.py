"""Helm write flows, extracted from the app (issue #187).

`HelmController` owns the install / upgrade / rollback / uninstall
workflows: the write gate, the chart-search and values-editing wizard, the
bounded dry-run preview with its render-failure recovery, and the mapping
from a user's choices to an audited mutation.

What it deliberately does *not* own is the security perimeter. Approval
still goes through the app's `push_write_confirmation`, context
revalidation through `write_context_intact`, and the mutation itself
through the app's `_run_write` worker — so the approval gate and the
fail-closed audit rule keep exactly one implementation each.

The controller receives named boundaries — `WriteGate`, `ViewState`,
`UiSurface` — plus the few helm-specific getters, rather than the app. That
is what stops it reaching back for anything it was not given, and unlike a
bag of `Callable[..., Any]` it keeps the argument contract checkable.

The helm-specific getters are read at call time because a `:ctx` switch
retargets the helm wrapper after construction. `ViewState` reads are live
for the same reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable

from korvid.core.store import ALL_NAMESPACES
from korvid.k8s.helm import (
    HELM_RELEASES_META,
    HELM_REVISIONS_META,
    HelmReleaseSummary,
    HelmRevisionSummary,
)
from korvid.k8s.helmcli import ChartHit, HelmCLI, HelmError, HelmPreviewUnsupported
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.helm_chart_search import HelmChartSearchScreen
from korvid.ui.widgets.helm_install import HelmInstallPrompt, HelmReleaseChoices
from korvid.ui.widgets.helm_repos import HelmRepoScreen
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.write_gate import WriteGate

logger = logging.getLogger(__name__)

#: Upper bound on a helm preview (issue #31): `helm ... --dry-run` shells out
#: and may pull the chart from a repo, so it gets more budget than an API
#: server dry-run - still bounded, the approval dialog is never wedged.
_HELM_PREVIEW_TIMEOUT = 20.0

#: A rendered chart can run to thousands of lines; the approval dialog shows
#: at most this many so the operation summary stays reviewable.
_HELM_PREVIEW_MAX_LINES = 60


def _chart_base(chart: str) -> str:
    """`"nginx-18.1.0"` -> `"nginx"`: strip the version suffix helm appends
    to a release's chart field, so an upgrade can pre-filter the chart search
    by name. Charts whose last dash segment is not a version stay whole."""
    base, sep, tail = chart.rpartition("-")
    if sep and tail[:1].isdigit():
        return base
    return chart


@dataclasses.dataclass(frozen=True)
class _HelmRenderFailure:
    """A dry-run render that helm itself rejected (issue #139).

    The dry-run runs the same command the approval would execute, so its
    failure is the real failure delivered early — the flow must stop before
    approval and show `error` (helm's stderr tail names the missing value)
    instead of letting the user approve a doomed mutation.
    """

    error: str


def _clip_preview(text: str) -> list[str] | None:
    """Dry-run/diff output as approval-dialog preview lines, capped at
    `_HELM_PREVIEW_MAX_LINES`; None when there is nothing to show."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) > _HELM_PREVIEW_MAX_LINES:
        hidden = len(lines) - _HELM_PREVIEW_MAX_LINES
        return [*lines[:_HELM_PREVIEW_MAX_LINES], f"... ({hidden} more lines)"]
    return lines


@contextlib.asynccontextmanager
async def _temp_values_file(values_text: str | None) -> AsyncIterator[str | None]:
    """A 0600 temp file holding the edited values for one helm invocation,
    deleted as soon as the command returns (values may embed credentials);
    None passes straight through as "no values override"."""
    if values_text is None:
        yield None
        return
    fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="korvid-helm-values-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(values_text)
        yield tmp
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


class HelmController:
    """Owns the helm install / upgrade / rollback / uninstall workflows.

    Every dependency arrives as a narrow callable so the controller cannot
    reach the app for anything it was not given. The security-relevant ones
    - `push_write_confirmation`, `write_context_intact`, `audit` - stay
    implemented on the app: this moves the helm *workflow* out, not the
    approval perimeter.
    """

    def __init__(
        self,
        *,
        helm: Callable[[], HelmCLI | None],
        gate: WriteGate,
        view: ViewState,
        ui: UiSurface,
        edit_in_external_editor: Callable[..., Awaitable[str | None]],
        #: optional injected editor (tests); None falls back to the real one.
        edit_text: Callable[[], Callable[..., Awaitable[str | None]] | None],
    ) -> None:
        self._helm = helm
        self._gate = gate
        self._view = view
        self._ui = ui
        self._edit_in_external_editor = edit_in_external_editor
        self._edit_text = edit_text

    def gate(self) -> HelmCLI | None:
        """Common gate for helm write flows: read-only mode and the
        fail-closed audit rule apply exactly as to API writes, plus the
        binary must have been detected at startup. None (with a
        notification) blocks the flow."""
        if self._view.readonly():
            self._ui.notify("Read-only mode: cluster writes are disabled", severity="warning")
            return None
        if not self._gate.audit_configured():
            # Fail-closed auditing (AGENTS.md): no audit sink means no writes.
            self._ui.notify("Writes disabled: no audit log configured", severity="warning")
            return None
        # Read once: checking one call and returning another could hand back
        # a client the check never saw - and after a `:ctx` switch rebinds the
        # wrapper, one bound to the previous cluster.
        helm = self._helm()
        if helm is None:
            self._ui.notify(
                "helm CLI not found on PATH - install/upgrade/rollback/uninstall unavailable",
                severity="error",
            )
            return None
        return helm

    def _view_namespace(self) -> str:
        """Namespace a fresh install targets by default: the active view
        namespace, or the configured workload namespace on the
        all-namespaces view (same fallback as the operator install wizard)."""
        view_ns = self._view.current_namespace()
        return (
            view_ns if view_ns != ALL_NAMESPACES else (self._view.default_namespace() or "default")
        )

    def install(self) -> None:
        """Install (the `hint_details` key on the helm view): search-first
        chart picker -> wizard -> dry-run preview -> approval -> audited
        `helm install`. The picker opens instantly and fetches charts per
        keyword (issue #106) instead of listing every repo upfront.
        Synchronous by design: no await may separate the keypress from the
        namespace/epoch capture and the modal push."""
        helm = self.gate()
        if helm is None:
            return
        self._open_chart_search(
            helm,
            release=None,
            namespace=self._view_namespace(),
            epoch=self._gate.epoch(),
            initial="",
        )

    def upgrade(self) -> None:
        """Upgrade (the `helm_upgrade` key on a release row): the same
        wizard with the release name and namespace fixed to the selected
        row's facts; the picker pre-searches the release's chart name.
        Synchronous by design: no await may separate the keypress from the
        row/epoch capture and the modal push."""
        helm = self.gate()
        if helm is None:
            return
        epoch = self._gate.epoch()
        ns, name = self._view.selected_ns_name()
        if name is None:
            return
        row = self.release_row(ns, name)
        keyword = _chart_base(row.chart) if row is not None else ""
        namespace = ns or (row.namespace if row is not None else self._view_namespace())
        self._open_chart_search(
            helm, release=name, namespace=namespace, epoch=epoch, initial=keyword
        )

    def _open_chart_search(
        self, helm: HelmCLI, *, release: str | None, namespace: str, epoch: int, initial: str
    ) -> None:
        """Keyword-driven chart picker feeding the install/upgrade wizard;
        everything offered comes from `helm search repo`, nothing is
        hardcoded. Ctrl-R inside the picker manages chart repositories."""

        def _picked(hit: ChartHit | None) -> None:
            if hit is None:
                return

            def _chosen(choices: HelmReleaseChoices | None) -> None:
                if choices is None:
                    return
                self._ui.run_worker(
                    self._confirm_change(hit, choices, upgrade=release is not None, epoch=epoch),
                    exclusive=True,
                    group="helm-write",
                )

            self._ui.push_screen(
                HelmInstallPrompt(
                    hit,
                    namespace=namespace,
                    release=release,
                    # Chart metadata (issue #151): required values from the
                    # chart's schema and README access, both repo-local.
                    get_schema=helm.show_schema,
                    get_readme=helm.show_readme,
                ),
                _chosen,
            )

        title = f"Upgrade {release} with chart:" if release else "Install helm chart"
        search_screen = HelmChartSearchScreen(
            helm.search_repo,
            title=title,
            initial=initial,
            on_manage_repos=lambda: self._open_repos(helm, browse_in=search_screen),
        )
        self._ui.push_screen(search_screen, _picked)

    def _open_repos(self, helm: HelmCLI, *, browse_in: HelmChartSearchScreen | None = None) -> None:
        """Chart repository management (list/add/update). `helm repo` writes
        local helm config only — never the cluster — so the typed form in
        the screen is the confirmation, not the write-approval gate.

        Enter on a repo row hands its name back (issue #137): the chart
        picker in *browse_in* — when it is still the screen underneath —
        scopes its search to that repository."""

        def _picked(repo: str | None) -> None:
            if repo is None or browse_in is None:
                return
            if self._ui.screen() is browse_in:
                browse_in.browse_repo(repo)

        self._ui.push_screen(
            HelmRepoScreen(
                repo_list=helm.repo_list,
                repo_add=helm.repo_add,
                repo_update=helm.repo_update,
            ),
            _picked,
        )

    async def _confirm_change(
        self, hit: ChartHit, choices: HelmReleaseChoices, *, upgrade: bool, epoch: int
    ) -> None:
        """Optional values editing, dry-run/diff preview, then the standard
        approval dialog; the mutation itself runs through `_run_write`, so
        the fail-closed audit rule applies unchanged. A dry-run the helm
        binary itself rejects stops the flow before approval (issue #139):
        the same command would fail identically after approval, so the
        user gets helm's stderr now, with the option to fix the values and
        retry instead of approving a doomed mutation."""
        helm = self._helm()
        if helm is None:  # gate already passed; helm cannot vanish, but be safe
            return
        values_text: str | None = None
        editor_buffer: str | None = None
        defaults_baseline: str | None = None
        if choices.edit_values:
            proceed, values_text, editor_buffer, defaults_baseline = await self._edit_values(
                helm, hit, choices, previous=None
            )
            if not proceed:
                return  # editor failed or was aborted; already notified
        action = "helm-upgrade" if upgrade else "helm-install"
        outcome = await self._preview_with_recovery(
            helm,
            hit,
            choices,
            values_text,
            editor_buffer,
            defaults_baseline,
            upgrade=upgrade,
            epoch=epoch,
            action=action,
        )
        if outcome is None:
            return
        rendered, values_text = outcome
        if rendered is not None:
            preview, preview_title = rendered
        else:
            # Environmental failure (timeout, unexpected error): approval
            # stays available, but say so instead of a silent blank.
            preview = ["(preview unavailable - the dry-run render did not complete)"]
            preview_title = "helm preview:"
        verb = "UPGRADE" if upgrade else "INSTALL"
        version_label = choices.version or "latest"
        if values_text is not None:
            values_label, values_detail = "edited in $EDITOR", "custom"
        elif choices.reuse_values:
            values_label, values_detail = "reuse current values", "reused"
        else:
            values_label, values_detail = "chart defaults", "defaults"
        operation = (
            f"HELM {verb} {choices.release} (chart {hit.name} {version_label})"
            f" in namespace {choices.namespace}\n"
            f"values: {values_label}"
        )
        detail = f"chart={hit.name} version={version_label} values={values_detail}"

        title = f"{'Upgrade' if upgrade else 'Install'} {choices.release}?"
        await self._gate.confirm(
            title,
            operation,
            action=action,
            meta=HELM_RELEASES_META,
            namespace=choices.namespace,
            name=choices.release,
            op_factory=lambda: self._apply_change(helm, hit, choices, values_text, upgrade=upgrade),
            detail=detail,
            preview=preview,
            preview_title=preview_title,
        )

    def _context_after_preview(
        self, action: str, choices: HelmReleaseChoices, *, upgrade: bool, epoch: int
    ) -> bool:
        """The preview runs over the interactive table: the state the user
        approves must still be the state that was previewed."""
        if upgrade:
            # The row selected for upgrade must still be the one approved.
            return self._gate.context_intact(
                action,
                HELM_RELEASES_META,
                choices.namespace,
                choices.release,
                phase="the preview render",
                epoch=epoch,
            )
        if self._gate.switching() or epoch != self._gate.epoch():
            # The helm wrapper this flow captured is bound to the old
            # cluster's --kube-context: a switch completed during the wizard
            # or preview must cancel before an approval can open.
            self._ui.notify(
                "helm install cancelled - the kube context changed during the preview",
                severity="warning",
            )
            return False
        if self._ui.screen_depth() > 1:  # another dialog opened during the preview
            return False
        if self._view.canonical_kind(self._view.current_kind()) != HELM_RELEASES_META.plural:
            self._ui.notify(
                "helm install cancelled - left the helm view during the preview",
                severity="warning",
            )
            return False
        return True

    async def _edit_values(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        *,
        previous: str | None,
        defaults_baseline: str | None = None,
    ) -> tuple[bool, str | None, str | None, str | None]:
        """(proceed, values override, raw buffer, defaults baseline) from
        `$EDITOR`.

        A first edit opens on the chart's own annotated defaults
        (`helm show values`, issue #151) - the standard CLI workflow -
        falling back to the old comment stub when the fetch fails. Content
        matching the fetched defaults (or comments-only) keeps the chart
        defaults: no override file is passed. The *baseline* rides along so
        a retry after a failed render (issue #139, pre-filled with the
        previous raw buffer) still recognizes unchanged defaults instead of
        freezing them into the release. False means the editor was aborted
        or failed and the flow must stop.
        """
        template = previous
        if template is None:
            with self._ui.progress("fetching chart default values"):
                defaults_baseline = await self._default_values(helm, hit, choices)
            template = defaults_baseline
        if template is None:
            template = (
                f"# values override for {hit.name} {choices.version or hit.version}\n"
                "# an empty file (or comments only) keeps the chart defaults\n"
            )
        edit = self._edit_text() or self._edit_in_external_editor
        text = await edit(template)
        if text is None:
            return False, None, None, defaults_baseline
        meaningful = any(
            line.strip() and not line.lstrip().startswith("#") for line in text.splitlines()
        )
        if defaults_baseline is not None and text == defaults_baseline:
            # Unchanged chart defaults are not an override (issue #151):
            # passing them as -f would freeze today's defaults into the
            # release for no reason.
            meaningful = False
        return True, (text if meaningful else None), text, defaults_baseline

    async def _default_values(
        self, helm: HelmCLI, hit: ChartHit, choices: HelmReleaseChoices
    ) -> str | None:
        """`helm show values` output for the picked chart, or None when the
        fetch fails (the caller falls back to the comment stub). The wizard
        version passes through unchanged: an empty version means "latest",
        matching what the install itself will resolve."""
        try:
            return await asyncio.wait_for(
                helm.show_values(hit.name, choices.version),
                _HELM_PREVIEW_TIMEOUT,
            )
        except (HelmError, TimeoutError):
            logger.debug("helm show values failed; editor opens on the stub", exc_info=True)
            return None

    async def _render_failure_choice(self, error: str, *, upgrade: bool) -> str:
        """Stop-before-approval decision on a rejected dry-run (issue #139):
        "edit", "retry", or "cancel" - Esc cancels. The picker is
        informational, not an approval gate: whatever the choice, the
        mutation still has to pass the ConfirmScreen approval afterwards."""
        verb = "upgrade" if upgrade else "install"
        title = f"helm {verb} --dry-run failed - the {verb} would fail the same way.\n\n{error}\n"
        options = ["edit values and retry", "retry preview", "cancel"]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()

        def _done(choice: str | None) -> None:
            if not fut.done():
                fut.set_result(choice)

        await self._ui.push_screen(PickScreen(title, options), _done)
        choice = await fut
        if choice == options[0]:
            return "edit"
        if choice == options[1]:
            return "retry"
        return "cancel"

    async def _preview_with_recovery(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        values_text: str | None,
        editor_buffer: str | None,
        defaults_baseline: str | None,
        *,
        upgrade: bool,
        epoch: int,
        action: str,
    ) -> tuple[tuple[list[str], str] | None, str | None] | None:
        """Render the preview, recovering from rejected dry-runs (issue #139).

        Returns `(rendered, values_text)` once a render succeeds (or fails
        only environmentally - `rendered` is then None), with `values_text`
        carrying any fixes made through the failure dialog's edit path;
        `editor_buffer` is the raw text last seen in `$EDITOR` (kept apart
        from the normalized override so a comments-only buffer survives a
        retry) and `defaults_baseline` the fetched chart defaults, carried
        across retries so an unchanged-defaults buffer never turns into a
        frozen override. None when the flow must stop (context lost, editor
        aborted, or the user cancelled at the failure dialog).
        """
        while True:
            # Rendering can take up to _HELM_PREVIEW_TIMEOUT (20s): show
            # progress for exactly as long as the render is pending, or the
            # UI looks frozen between the wizard and the approval dialog
            # (issue #106).
            with self._ui.progress("rendering helm preview (dry-run)"):
                rendered = await self._change_preview(
                    helm, hit, choices, values_text, upgrade=upgrade
                )
            if not self._context_after_preview(action, choices, upgrade=upgrade, epoch=epoch):
                return None
            if not isinstance(rendered, _HelmRenderFailure):
                return rendered, values_text
            decision = await self._render_failure_choice(rendered.error, upgrade=upgrade)
            if decision == "cancel":
                return None  # nothing was executed, nothing to audit
            if decision == "edit":
                (
                    proceed,
                    values_text,
                    editor_buffer,
                    defaults_baseline,
                ) = await self._edit_values(
                    helm, hit, choices, previous=editor_buffer, defaults_baseline=defaults_baseline
                )
                if not proceed:
                    return None

    async def _apply_change(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        values_text: str | None,
        *,
        upgrade: bool,
    ) -> None:
        """The approved mutation, awaited by `_run_write` after the intent
        audit record persisted."""
        version = choices.version or None
        async with _temp_values_file(values_text) as values_file:
            if upgrade:
                await helm.upgrade(
                    choices.release,
                    hit.name,
                    choices.namespace,
                    version=version,
                    values_file=values_file,
                    reuse_values=choices.reuse_values,
                )
            else:
                await helm.install(
                    choices.release,
                    hit.name,
                    choices.namespace,
                    version=version,
                    values_file=values_file,
                )

    async def _change_preview(
        self,
        helm: HelmCLI,
        hit: ChartHit,
        choices: HelmReleaseChoices,
        values_text: str | None,
        *,
        upgrade: bool,
    ) -> tuple[list[str], str] | _HelmRenderFailure | None:
        """Preview lines plus their heading for the approval dialog: `helm
        diff upgrade` when the plugin exists (issue #31), else the plain
        `--dry-run` render - the heading names which one the user is looking
        at. A dry-run helm itself rejects returns `_HelmRenderFailure` - the
        approval would run the same command, so the caller must stop instead
        of approving a proven failure (issue #139). Environmental failures
        (timeout, unexpected errors) return None - they say nothing about
        the mutation, so a preview must never block the approval flow."""

        async def _render() -> tuple[str, str]:
            version = choices.version or None
            async with _temp_values_file(values_text) as values_file:
                if upgrade and await helm.has_diff_plugin():
                    try:
                        return "helm diff upgrade preview:", await helm.diff_upgrade(
                            choices.release,
                            hit.name,
                            choices.namespace,
                            version=version,
                            values_file=values_file,
                            reuse_values=choices.reuse_values,
                        )
                    except HelmError:
                        # A diff-plugin failure is not a verdict on the
                        # upgrade itself: fall back to the plain dry-run,
                        # whose failure would be.
                        logger.debug("helm diff failed; falling back to --dry-run", exc_info=True)
                if upgrade:
                    return "helm upgrade --dry-run preview:", await helm.dry_run_upgrade(
                        choices.release,
                        hit.name,
                        choices.namespace,
                        version=version,
                        values_file=values_file,
                        reuse_values=choices.reuse_values,
                    )
                return "helm install --dry-run preview:", await helm.dry_run_install(
                    choices.release,
                    hit.name,
                    choices.namespace,
                    version=version,
                    values_file=values_file,
                )

        try:
            title, text = await asyncio.wait_for(_render(), _HELM_PREVIEW_TIMEOUT)
        except HelmPreviewUnsupported:
            # helm < 3.15 rejecting the preview-only --hide-secret flag is
            # a preview incompatibility, not a verdict: the real command
            # never carries the flag (see HelmCLI._dry_run).
            logger.debug("helm preview failed; dialog opens without it", exc_info=True)
            return None
        except HelmError as exc:
            return _HelmRenderFailure(str(exc))
        except Exception:
            logger.debug("helm preview failed; dialog opens without it", exc_info=True)
            return None
        lines = _clip_preview(text)
        # [] is a *successful* empty render (helm diff: no changes) —
        # ConfirmScreen states it explicitly; None stays reserved for
        # failures, which the caller marks "preview unavailable".
        return (lines if lines is not None else [], title)

    async def rollback(
        self,
        helm: HelmCLI,
        row: HelmRevisionSummary,
        ns: str | None,
        name: str,
        namespace: str,
        epoch: int,
    ) -> None:
        """Rollback (the `rollout_restart` key on a revision row of the
        drill-down): approval-gated, audited `helm rollback` to that
        revision. The target row is captured by the action at keypress time
        and passed in — this worker must never re-read the selection."""
        with self._ui.progress("rendering rollback preview"):
            preview = await self._rollback_preview(helm, row.release, row.revision, namespace)
        if not self._gate.context_intact(
            "helm-rollback", HELM_REVISIONS_META, ns, name, phase="the diff preview", epoch=epoch
        ):
            return
        operation = (
            f"HELM ROLLBACK {row.release} to revision {row.revision} in namespace {namespace}"
        )

        await self._gate.confirm(
            f"Rollback {row.release} to revision {row.revision}?",
            operation,
            action="helm-rollback",
            meta=HELM_RELEASES_META,
            namespace=namespace,
            name=row.release,
            op_factory=lambda: self._apply_rollback(helm, row.release, row.revision, namespace),
            detail=f"revision={row.revision}",
            preview=preview,
            preview_title="helm diff rollback preview:",
        )

    async def _apply_rollback(
        self, helm: HelmCLI, release: str, revision: int, namespace: str
    ) -> None:
        await helm.rollback(release, revision, namespace)

    async def _rollback_preview(
        self, helm: HelmCLI, release: str, revision: int, namespace: str
    ) -> list[str] | None:
        """`helm diff rollback` preview when the plugin exists; None
        otherwise (plain rollback has no meaningful dry-run output)."""

        async def _render() -> str | None:
            if not await helm.has_diff_plugin():
                return None
            return await helm.diff_rollback(release, revision, namespace)

        try:
            text = await asyncio.wait_for(_render(), _HELM_PREVIEW_TIMEOUT)
        except Exception:
            logger.debug("helm rollback preview failed; dialog opens without it", exc_info=True)
            return None
        return _clip_preview(text) if text is not None else None

    async def uninstall(
        self,
        helm: HelmCLI,
        row: HelmReleaseSummary,
        ns: str | None,
        name: str,
        namespace: str,
        epoch: int,
    ) -> None:
        """Uninstall (ctrl+d on a release row): approval-gated, audited
        `helm uninstall`. The release name must be typed to confirm - the
        blast radius is every resource the release owns, so the y shortcut
        is not enough. The target row is captured by the action at keypress
        time and passed in - this worker must never re-read the selection."""
        with self._ui.progress("rendering uninstall preview"):
            preview = await self._uninstall_preview(helm, row.name, namespace)
        if not self._gate.context_intact(
            "helm-uninstall", HELM_RELEASES_META, ns, name, phase="the dry-run preview", epoch=epoch
        ):
            return
        operation = (
            f"HELM UNINSTALL {row.name} ({row.chart}) from namespace {namespace}\n"
            "Deletes every resource this release owns and removes its history."
        )
        await self._gate.confirm(
            f"Uninstall release {row.name}?",
            operation,
            action="helm-uninstall",
            meta=HELM_RELEASES_META,
            namespace=namespace,
            name=row.name,
            op_factory=lambda: self._apply_uninstall(helm, row.name, namespace),
            require_name=row.name,
            preview=preview,
            preview_title="helm uninstall --dry-run preview:",
        )

    async def _apply_uninstall(self, helm: HelmCLI, release: str, namespace: str) -> None:
        await helm.uninstall(release, namespace)

    async def _uninstall_preview(
        self, helm: HelmCLI, release: str, namespace: str
    ) -> list[str] | None:
        """`helm uninstall --dry-run` summary; None on any failure (the
        dialog then opens without a preview, like every other preview)."""
        try:
            text = await asyncio.wait_for(
                helm.dry_run_uninstall(release, namespace), _HELM_PREVIEW_TIMEOUT
            )
        except Exception:
            logger.debug("helm uninstall preview failed; dialog opens without it", exc_info=True)
            return None
        return _clip_preview(text)

    def release_row(self, ns: str | None, name: str) -> HelmReleaseSummary | None:
        for obj in self._view.resources("helmreleases", self._view.current_scope()):
            if (
                obj.name == name
                and (ns is None or obj.namespace == ns)
                and isinstance(obj, HelmReleaseSummary)
            ):
                return obj
        return None

    def revision_row(self, ns: str | None, name: str) -> HelmRevisionSummary | None:
        for obj in self._view.resources("helmrevisions", self._view.current_scope()):
            if (
                obj.name == name
                and (ns is None or obj.namespace == ns)
                and isinstance(obj, HelmRevisionSummary)
            ):
                return obj
        return None
