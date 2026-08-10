"""Operator (OLM) install, approve and uninstall flows, extracted from the
app (issue #187).

`OperatorController` owns the subscription wizard, the InstallPlan approval,
and the uninstall that must also remove the installed CSV. Everything it
offers comes from the cluster's own catalog objects - there is no hardcoded
operator knowledge here.

The write perimeter stays on the app behind `WriteGate`. This area needs
more of that interface than helm did: the install dialog re-checks the
subscription UID inside its own approval callback, so it drives
`gate.permitted` and `gate.run` directly rather than the standard
`gate.confirm` flow. Naming those on the interface is what keeps the
security-carrying keywords (`action`, `meta`, `op_factory`, `epoch`) checked
by mypy at every call site.

The dependency getters are read at call time because a `:ctx` switch
retargets the write client and the alias table after construction.
"""

from __future__ import annotations

import logging
import weakref
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

import yaml

from korvid.core.config import KorvidConfig
from korvid.core.store import ALL_NAMESPACES, ResourceStore
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.models import manifest_uid
from korvid.k8s.olm import (
    OPERATORS_GROUP,
    PackageInstallFacts,
    build_subscription,
    package_install_facts,
    resolve_olm_meta,
)
from korvid.k8s.writes import WriteOps
from korvid.ui.widgets.operator_install import OperatorInstallPrompt
from korvid.ui.write_gate import WriteGate

logger = logging.getLogger(__name__)


def _installed_csv_name(manifest: dict[str, Any]) -> str:
    """`status.installedCSV` of a Subscription manifest, or '' when absent
    (the operator never finished installing, or the status is malformed)."""
    status = manifest.get("status")
    if not isinstance(status, dict):
        return ""
    return str(status.get("installedCSV") or "")


class _CsvTargetUnavailable(Exception):
    """The Subscription records an installed CSV that cannot be safely
    targeted right now. The uninstall must abort: skipping the CSV would
    leave the operator running after an approved *full* uninstall, and
    deleting it without a uid pin could remove a replacement incarnation
    created while the dialog was open."""


class OperatorController:
    """Owns the OLM install / approve / uninstall workflows."""

    def __init__(
        self,
        *,
        gate: WriteGate,
        write_ops: Callable[[], WriteOps | None],
        #: optional: no manifest fetcher means the flows report and stop.
        get_manifest: Callable[
            [], Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None
        ],
        aliases: Callable[[], dict[str, ResourceMeta]],
        store: Callable[[], ResourceStore],
        config: Callable[[], KorvidConfig],
        notify: Callable[..., None],
        push_screen: Callable[..., Any],
        run_worker: Callable[..., Any],
        current_kind: Callable[[], str],
        current_scope: Callable[[], str],
        current_namespace: Callable[[], str],
        canonical_kind: Callable[[str], str],
        gvr_label: Callable[[ResourceMeta], str],
        write_locus: Callable[[str | None], str],
        confirm_screen: Callable[..., Any],
        selected_uid: Callable[..., str | None],
        uid_intact_after_fetch: Callable[..., bool],
        write_target: Callable[[], tuple[ResourceMeta, str | None, str, str | None] | None],
        precheck_keybinding_write: Callable[..., Any],
    ) -> None:
        self._gate = gate
        self._write_ops = write_ops
        self._get_manifest = get_manifest
        self._aliases = aliases
        self._store = store
        self._config = config
        self._notify = notify
        self._push_screen = push_screen
        self._run_worker = run_worker
        self._current_kind = current_kind
        self._current_scope = current_scope
        self._current_namespace = current_namespace
        self._canonical_kind = canonical_kind
        self._gvr_label = gvr_label
        self._write_locus = write_locus
        self._confirm_screen = confirm_screen
        self._selected_uid = selected_uid
        self._uid_intact_after_fetch = uid_intact_after_fetch
        self._write_target = write_target
        self._precheck_keybinding_write = precheck_keybinding_write

    async def install(
        self, pkg_meta: ResourceMeta, ns: str | None, name: str, uid: str | None
    ) -> None:
        """Fetch the PackageManifest and open the install wizard."""
        sub_meta = resolve_olm_meta(self._aliases(), "subscriptions", OPERATORS_GROUP)
        if sub_meta is None:
            self._notify(
                "Install unavailable: the OLM Subscription API was not discovered",
                severity="warning",
            )
            return
        get_manifest = self._get_manifest()
        if get_manifest is None:
            self._notify("Install unavailable: no manifest source", severity="warning")
            return
        epoch = self._gate.epoch()
        try:
            # Fetch by the canonical view kind (which may be a group-qualified
            # alias), as the edit path does: a bare plural would resolve to a
            # colliding foreign CRD's meta when names overlap.
            manifest = await get_manifest(self._canonical_kind(self._current_kind()), ns, name)
        except Exception as exc:
            self._notify(f"Could not fetch the package manifest: {exc}", severity="error")
            return
        # The wizard must be fed the incarnation the user selected: if the
        # catalog entry was deleted and recreated under the same name during
        # the fetch, its facts (channels, catalog source) may differ.
        if not self._uid_intact_after_fetch(manifest, ns, name, uid):
            self._notify(
                f"install {self._gvr_label(pkg_meta)}/{name} cancelled -"
                " the catalog entry changed during the manifest fetch",
                severity="warning",
            )
            return
        facts = package_install_facts(manifest)
        if not self._gate.context_intact(
            "install", pkg_meta, ns, name, phase="the manifest fetch", epoch=epoch
        ):
            return

        def _on_choices(choices: tuple[str, str, str] | None) -> None:
            if choices is None:
                return
            # The SSAR round trip must not run inside a screen callback:
            # a worker re-checks, revalidates, then confirms.
            self._run_worker(
                self._confirm_operator_install(pkg_meta, sub_meta, ns, uid, facts, choices, epoch)
            )

        # The row namespace is where the catalog lives (e.g. "olm"), not
        # where the user works: prefill the wizard with the active view
        # namespace, or the configured workload namespace on the
        # all-namespaces view (the catalog default since `:operators`
        # opens cluster-wide).
        view_ns = self._current_namespace()
        # Same fallback as current_scope's initialization: with zero config,
        # config.namespace is None while the effective workload namespace is
        # "default" - an empty prefill would fail validation on submit.
        default_ns = (
            view_ns if view_ns != ALL_NAMESPACES else (self._config().namespace or "default")
        )
        await self._push_screen(
            OperatorInstallPrompt(facts, namespace=default_ns),
            _on_choices,
        )

    async def _confirm_operator_install(
        self,
        pkg_meta: ResourceMeta,
        sub_meta: ResourceMeta,
        ns: str | None,
        uid: str | None,
        facts: PackageInstallFacts,
        choices: tuple[str, str, str],
        epoch: int,
    ) -> None:
        """Approval dialog for an operator install: the full Subscription
        manifest is shown before it is created (issue #29 requirement)."""
        ops = self._write_ops()
        if ops is None:
            return
        namespace, channel, approval = choices
        try:
            manifest = build_subscription(
                package=facts.package,
                namespace=namespace,
                channel=channel,
                source=facts.catalog_source,
                source_namespace=facts.catalog_source_namespace,
                approval=approval,
            )
        except ValueError as exc:
            # Blank catalog facts (malformed PackageManifest status) land
            # here; the wizard already validated its own inputs.
            self._notify(f"install cancelled: {exc}", severity="warning")
            return
        # Create is authorized against the collection POST before the object
        # name exists (resourceNames rules cannot grant create), so the SSAR
        # must omit the name to match the real request.
        if not await self._gate.permitted("install", sub_meta, namespace, ""):
            return
        if not self._gate.context_intact(
            "install", pkg_meta, ns, facts.package, phase="the install wizard", epoch=epoch
        ):
            return
        if uid and self._selected_uid(ns, facts.package) != uid:
            # Same name, different incarnation: the catalog entry was
            # replaced while the wizard was open.
            self._notify(
                f"install {self._gvr_label(pkg_meta)}/{facts.package} cancelled -"
                " the catalog entry changed during the install wizard",
                severity="warning",
            )
            return
        operation = (
            f"CREATE subscriptions/{facts.package} in namespace {namespace}\n"
            "note: OLM requires an OperatorGroup in the target namespace -"
            " without one the Subscription is accepted but stays pending\n\n"
            + yaml.safe_dump(manifest, sort_keys=False)
        )

        def _done(confirmed: bool | None) -> None:
            if not confirmed:
                return
            # create has no server-side uid precondition (there is no target
            # object yet): recheck the catalog incarnation one last time at
            # execution, after the confirmation gap.
            if uid and self._selected_uid(ns, facts.package) != uid:
                self._notify(
                    f"install {self._gvr_label(pkg_meta)}/{facts.package} cancelled -"
                    " the catalog entry changed during the approval dialog",
                    severity="warning",
                )
                return
            self._run_worker(
                self._gate.run(
                    "install",
                    sub_meta,
                    namespace,
                    facts.package,
                    lambda: ops.create_object(sub_meta, namespace, manifest),
                    detail=f"channel={channel} approval={approval} source={facts.catalog_source}",
                )
            )

        await self._push_screen(
            self._confirm_screen(f"Install operator {facts.package}?", operation), _done
        )

    async def approve_plan(
        self, meta: ResourceMeta, ns: str | None, name: str, uid: str | None
    ) -> None:
        """Approve a pending manual InstallPlan: fetch, flip spec.approved,
        and replace behind the standard approval dialog listing the CSVs the
        approval unblocks."""
        ops = self._write_ops()
        if ops is None:
            return
        epoch = self._gate.epoch()
        if not await self._precheck_keybinding_write("approve", meta, ns, name):
            return
        get_manifest = self._get_manifest()
        if get_manifest is None:
            self._notify("Approve unavailable: no manifest source", severity="warning")
            return
        try:
            # Canonical view kind, not the bare plural: safe under alias
            # collisions (see _start_operator_install).
            manifest = await get_manifest(self._canonical_kind(self._current_kind()), ns, name)
        except Exception as exc:
            self._notify(f"Could not fetch the install plan: {exc}", severity="error")
            return
        if not self._uid_intact_after_fetch(manifest, ns, name, uid):
            self._notify(
                f"approve installplans/{name} cancelled -"
                " the install plan changed during the manifest fetch",
                severity="warning",
            )
            return
        spec = self._approvable_plan_spec(manifest, name)
        if spec is None:
            return
        if not self._gate.context_intact(
            "approve", meta, ns, name, phase="the manifest fetch", epoch=epoch
        ):
            return
        updated = dict(manifest)
        updated["spec"] = {**spec, "approved": True}
        csvs = ", ".join(str(c) for c in spec.get("clusterServiceVersionNames") or []) or "?"
        operation = (
            f"REPLACE installplans/{name} with spec.approved=true"
            f"{self._write_locus(ns)}\ninstalls: {csvs}"
        )
        await self._gate.confirm(
            f"Approve installplans/{name}?",
            operation,
            action="approve",
            meta=meta,
            namespace=ns,
            name=name,
            op_factory=lambda: ops.replace_object(meta, ns, name, updated, uid=uid),
            detail=f"installs: {csvs}",
        )

    def _approvable_plan_spec(self, manifest: dict[str, Any], name: str) -> dict[str, Any] | None:
        """The plan's spec if it is a pending Manual plan, else None (with
        the reason notified). An Automatic (or malformed) plan is OLM's own
        to approve; flipping it manually would race the operator."""
        spec = manifest.get("spec")
        spec = spec if isinstance(spec, dict) else {}
        approval_mode = str(spec.get("approval") or "")
        if approval_mode != "Manual":
            self._notify(
                f"installplans/{name} has approval mode"
                f" {approval_mode or '?'!r} - only pending Manual plans"
                " can be approved here",
                severity="warning",
            )
            return None
        if spec.get("approved"):
            self._notify(f"installplans/{name} is already approved", severity="information")
            return None
        return spec

    # ------------------------------------------------------------------
    # operator uninstall (issue #117)
    # ------------------------------------------------------------------

    def alias_key(self, plural: str) -> str | None:
        """The aliases key resolving to the OLM *plural* (prefers the
        group-qualified alias, like `resolve_olm_meta`), or None when the
        API was not discovered."""
        for key in (f"{plural}.{OPERATORS_GROUP}", plural):
            meta = self._aliases().get(key)
            if meta is not None and meta.group == OPERATORS_GROUP:
                return key
        return None

    async def uninstall(
        self,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        fetch_kind: str,
        ctx: tuple[ResourceMeta, str | None, str],
    ) -> None:
        """Ctrl+D on an OLM Subscription (or redirected from its CSV):
        uninstall the operator - delete the Subscription, then its installed
        CSV; OLM garbage-collects the operator's Deployment and RBAC owned
        by the CSV. CRDs and custom resources are never touched (their data
        outlives the operator by design). ``ctx`` names the selection the
        user acted on - the CSV row on the redirect path - so the post-await
        re-validation checks the right row."""
        ops = self._write_ops()
        if ops is None:
            return
        epoch = self._gate.epoch()
        if not await self._gate.permitted("uninstall", sub_meta, ns, name):
            return
        manifest = await self._fetch_subscription_for_uninstall(fetch_kind, sub_meta, ns, name, uid)
        if manifest is None:
            return
        csv_name = _installed_csv_name(manifest)
        try:
            csv_meta, csv_uid = await self._installed_csv_target(ns, csv_name)
        except _CsvTargetUnavailable as exc:
            self._notify(
                f"uninstall {name} aborted: {exc} -"
                f" installed CSV {csv_name} cannot be safely removed",
                severity="error",
            )
            return
        if csv_meta is not None and not await self._gate.permitted(
            "uninstall", csv_meta, ns, csv_name
        ):
            return
        ctx_meta, ctx_ns, ctx_name = ctx
        if not self._gate.context_intact(
            "uninstall", ctx_meta, ctx_ns, ctx_name, phase="the manifest fetch", epoch=epoch
        ):
            return
        operation = self._operator_uninstall_operation(sub_meta, ns, name, csv_meta, csv_name)

        def _done(confirmed: bool | None) -> None:
            if confirmed:
                self._run_worker(
                    self._operator_apply_uninstall(
                        ops,
                        sub_meta,
                        ns,
                        name,
                        uid,
                        fetch_kind=fetch_kind,
                        csv_meta=csv_meta,
                        csv_name=csv_name,
                        csv_uid=csv_uid,
                    )
                )

        await self._push_screen(
            self._confirm_screen(f"Uninstall operator {name}?", operation), _done
        )

    async def _fetch_subscription_for_uninstall(
        self, fetch_kind: str, sub_meta: ResourceMeta, ns: str | None, name: str, uid: str | None
    ) -> dict[str, Any] | None:
        """The Subscription manifest for the uninstall dialog, or None (with
        a notification) when it cannot be fetched or is a different
        incarnation than the row the user acted on."""
        get_manifest = self._get_manifest()
        if get_manifest is None:
            self._notify("Uninstall unavailable: no manifest source", severity="warning")
            return None
        try:
            manifest = await get_manifest(fetch_kind, ns, name)
        except Exception as exc:
            self._notify(f"Could not fetch the subscription: {exc}", severity="error")
            return None
        fetched = manifest_uid(manifest)
        if uid and fetched and fetched != uid:
            self._notify(
                f"uninstall {self._gvr_label(sub_meta)}/{name} cancelled -"
                " the subscription changed during the manifest fetch",
                severity="warning",
            )
            return None
        return dict(manifest)

    async def _installed_csv_target(
        self, ns: str | None, csv_name: str
    ) -> tuple[ResourceMeta | None, str | None]:
        """(meta, uid) of the Subscription's installed CSV, or (None, None)
        when there is nothing to delete - no CSV recorded, or the CSV is
        already gone (404). Raises `_CsvTargetUnavailable` when the CSV
        exists but cannot be uid-pinned (API undiscovered, lookup failed):
        the uninstall aborts rather than skip the CSV or delete it
        unpinned."""
        if not csv_name:
            return None, None
        key = self.alias_key("clusterserviceversions")
        if key is None:
            raise _CsvTargetUnavailable("the CSV API was not discovered")
        get_manifest = self._get_manifest()
        if get_manifest is None:
            raise _CsvTargetUnavailable("no manifest source to pin the CSV uid")
        try:
            manifest = await get_manifest(key, ns, csv_name)
        except ApiStatusError as exc:
            if exc.status == 404:
                return None, None
            raise _CsvTargetUnavailable(f"the CSV uid lookup failed (API {exc.status})") from exc
        except Exception as exc:
            raise _CsvTargetUnavailable("the CSV uid lookup failed") from exc
        csv_uid = manifest_uid(manifest)
        if not csv_uid:
            raise _CsvTargetUnavailable("the CSV manifest has no uid to pin")
        return self._aliases()[key], csv_uid

    def _operator_uninstall_operation(
        self,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        csv_meta: ResourceMeta | None,
        csv_name: str,
    ) -> str:
        """The uninstall dialog body: exactly what will be deleted, what OLM
        garbage-collects, and what is deliberately kept."""
        lines = [
            f"OPERATOR UNINSTALL {name}{self._write_locus(ns)}",
            "",
            f"  DELETE {self._gvr_label(sub_meta)}/{name}",
        ]
        if csv_meta is not None and csv_name:
            lines += [
                f"  DELETE {self._gvr_label(csv_meta)}/{csv_name}",
                "",
                "OLM garbage-collects the operator's Deployment and RBAC owned by the CSV.",
            ]
        elif csv_name:
            lines += [
                "",
                f"(installed CSV {csv_name} is already gone - only the Subscription is removed)",
            ]
        else:
            lines += ["", "(no installed CSV recorded - only the Subscription is removed)"]
        lines.append("CRDs and custom resources are KEPT - remove them manually if needed.")
        return "\n".join(lines)

    def _operator_apply_uninstall(
        self,
        ops: WriteOps,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        fetch_kind: str,
        csv_meta: ResourceMeta | None,
        csv_name: str,
        csv_uid: str | None,
    ) -> Coroutine[Any, Any, None]:
        """Subscription first (stops OLM from reinstalling), then the CSV;
        each delete individually audited fail-closed. A failed or blocked
        Subscription delete leaves the CSV untouched - removing the CSV
        alone would only make OLM reinstall it.

        Synchronous on purpose: it reserves the in-flight cluster write
        *here*, while building the coroutine, and returns it unstarted. A
        confirmation callback hands the result to `run_worker`, which starts
        it a loop iteration later - and a `:ctx` queued in that gap has to
        see the write already in flight (issue #36).
        """
        release = self._gate.reserve_write()

        async def run() -> None:
            try:
                await self._apply_uninstall_locked(
                    ops,
                    sub_meta,
                    ns,
                    name,
                    uid,
                    fetch_kind=fetch_kind,
                    csv_meta=csv_meta,
                    csv_name=csv_name,
                    csv_uid=csv_uid,
                )
            finally:
                release()

        coro = run()
        # The reservation must not leak if the coroutine is closed or
        # collected without ever running (worker cancelled before start,
        # app shutdown): release is idempotent, so this is safe to add.
        weakref.finalize(coro, release)
        return coro

    async def _apply_uninstall_locked(
        self,
        ops: WriteOps,
        sub_meta: ResourceMeta,
        ns: str | None,
        name: str,
        uid: str | None,
        *,
        fetch_kind: str,
        csv_meta: ResourceMeta | None,
        csv_name: str,
        csv_uid: str | None,
    ) -> None:
        """The uninstall itself, with the in-flight reservation already held."""
        if await self._subscription_target_stale(fetch_kind, ns, name, uid, csv_name):
            self._notify(
                f"uninstall {name} aborted: the subscription changed while"
                " the dialog was open - refresh and retry",
                severity="warning",
            )
            return
        outcome = await self._gate.run(
            "uninstall",
            sub_meta,
            ns,
            name,
            lambda: ops.delete_object(sub_meta, ns, name, uid=uid),
            detail=f"csv={csv_name or '-'}",
        )
        if outcome != "done" or csv_meta is None or not csv_name:
            return
        await self._gate.run(
            "uninstall",
            csv_meta,
            ns,
            csv_name,
            lambda: ops.delete_object(csv_meta, ns, csv_name, uid=csv_uid),
            detail=f"subscription={name}",
        )

    async def _subscription_target_stale(
        self, fetch_kind: str, ns: str | None, name: str, uid: str | None, csv_name: str
    ) -> bool:
        """Whether the Subscription no longer matches what the user approved:
        a different incarnation (uid changed), or OLM advanced
        `status.installedCSV` in place while the dialog was open - the
        approved deletes would then target a stale CSV and leave the new one
        running. Fail-open on fetch errors: the deletes' own uid
        preconditions still guard, and a vanished Subscription just makes
        the first delete fail loudly."""
        get_manifest = self._get_manifest()
        if get_manifest is None:
            return False
        try:
            manifest = await get_manifest(fetch_kind, ns, name)
        except Exception:
            return False
        fetched_uid = manifest_uid(manifest)
        if uid and fetched_uid and fetched_uid != uid:
            return True
        return _installed_csv_name(manifest) != csv_name

    async def csv_uninstall_redirect(
        self, csv_meta: ResourceMeta, ns: str | None, name: str
    ) -> bool:
        """Ctrl+D on a CSV installed by a known Subscription: warn that OLM
        would reinstall a deleted CSV and offer the full uninstall instead
        (issue #117). False - the plain delete proceeds - when no owning
        Subscription is found; the lookup reads the store, so only
        Subscriptions this session has watched count."""
        sub_key = self.alias_key("subscriptions")
        if sub_key is None:
            return False
        row = next(
            (
                obj
                for obj in self._store().get(self._canonical_kind(sub_key), self._current_scope())
                if getattr(obj, "installed_csv", "") == name and (ns is None or obj.namespace == ns)
            ),
            None,
        )
        if row is None:
            return False
        self._notify(
            f"{name} was installed by subscriptions/{row.name} - OLM would"
            " reinstall a deleted CSV; uninstalling the operator instead",
            severity="warning",
        )
        await self.uninstall(
            self._aliases()[sub_key],
            row.namespace or None,
            row.name,
            str(getattr(row, "uid", "") or "") or None,
            fetch_kind=sub_key,
            ctx=(csv_meta, ns, name),
        )
        return True

    # ------------------------------------------------------------------
    # helm install / upgrade / rollback via the detected helm CLI (issue #31)
    # ------------------------------------------------------------------
