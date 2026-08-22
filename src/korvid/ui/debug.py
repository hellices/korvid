"""Debug-fallback execution lifecycle, extracted from the app (issue #97 U3c;
the image-pull retry offer followed in Deep Task 10).

`DebugController` owns the post-approval half of the kubectl debug fallback:
the readonly and fail-closed audit gates, re-verifying the approved pod
incarnation, running kubectl debug with image-pull monitoring while the TUI
is suspended, auditing the outcome, and — when the pull failed — the retry
offer with its air-gap guard, its equivalent-reference guard and the
approval dialog that reruns the debug on the fallback image. The probe, RBAC
pre-check, image picker and the *initial* approval stay with `ShellController`.

Textual arrives as `UiSurface`, so the controller never touches the app: the
retry dialog is pushed, and the accepted rerun scheduled, through that one
boundary. `audit`, `settings` and `run_debug` are getters because wiring, a
`:ctx` switch and the tests may all replace them after construction.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import subprocess
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from time import monotonic
from typing import Any

from korvid.core.audit import AuditLog
from korvid.core.debugimage import (
    FALLBACK_IMAGE,
    ephemeral_container_names,
    find_pull_failure,
    same_image_ref,
)
from korvid.ui.shell import build_debug_argv, build_pod_get_argv
from korvid.ui.ui_surface import UiSurface
from korvid.ui.widgets.confirm_screen import ConfirmScreen
from korvid.ui.write_coordinator import write_locus

logger = logging.getLogger(__name__)

#: Rerunning `kubectl debug` on the fallback image after an approved retry.
RunDebug = Callable[[str, str, str | None, str | None, str], Coroutine[Any, Any, None]]


@dataclasses.dataclass(frozen=True)
class DebugSettings:
    """The configuration the debug flow reads, as an immutable snapshot.

    Narrower than `KorvidConfig`, for the same reason `ShellSettings` is:
    that object is only shallowly frozen, so passing it whole would hand
    over mutable `keybindings`/`agent_options` dicts along with the three
    values used here. `images` is a `Mapping` for the same reason.
    """

    kube_context: str | None
    default_image: str | None
    #: `images is None` means unconfigured; an explicit (possibly empty)
    #: mapping means the operator curated the debug images, which is what
    #: makes offering a public busybox fallback wrong.
    images: Mapping[str, str] | None


class DebugController:
    """Owns the gated, audited kubectl debug run with pull-failure monitoring."""

    #: How often the pod's ephemeral container statuses are polled while the
    #: attach may still be hanging on an image pull.
    PULL_CHECK_INTERVAL = 2.5
    #: How long pulls are monitored before the attach is trusted to be alive.
    PULL_CHECK_DEADLINE = 30.0

    def __init__(
        self,
        *,
        ui: UiSurface,
        audit: Callable[[], AuditLog | None],
        readonly: Callable[[], bool],
        settings: Callable[[], DebugSettings],
        pod_uid_unchanged: Callable[..., Awaitable[bool]],
        get_epoch: Callable[[], int],
        epoch_crossed: Callable[[int], bool],
        #: `WriteCoordinator.confirm_screen`; typed by its return so the
        #: retry callback is checked against the dialog's own result type.
        confirm_screen: Callable[..., ConfirmScreen],
        #: Late-binding: the rerun goes back through `ShellController`, which
        #: is constructed around this controller and keeps the write decorator.
        run_debug: Callable[[], RunDebug],
    ) -> None:
        self._ui = ui
        self._audit = audit
        self._readonly = readonly
        self._settings = settings
        self._pod_uid_unchanged = pod_uid_unchanged
        self._get_epoch = get_epoch
        self._epoch_crossed = epoch_crossed
        self._confirm_screen = confirm_screen
        self._run_debug = run_debug

    async def run(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str,
    ) -> None:
        """Attach an ephemeral debug container via kubectl debug.

        This is a pod mutation: blocked in readonly sessions and audited
        fail-closed like every other write (user approval came from the
        fallback prompt). kubectl cannot carry a uid precondition, so the
        approved pod incarnation is re-verified immediately before executing
        and the debug aborts when the pod was replaced or removed while the
        dialog was open (narrowing the race from the unbounded dialog
        lifetime to the exec latency). Audit appends take blocking locks and
        fsync, so they run off the event loop — intent is still recorded
        before the mutation starts.
        """
        if self._readonly():
            self._ui.notify("Read-only mode: cluster writes are disabled", severity="warning")
            return
        audit = self._audit()
        if audit is None:
            self._ui.notify("Writes disabled: no audit log configured", severity="warning")
            return
        if approved_uid is not None and not await self._pod_uid_unchanged(
            namespace, name, approved_uid, action="kubectl debug"
        ):
            return
        detail = f"ephemeral debug container (kubectl debug, image {image})"
        try:
            await asyncio.to_thread(self._audit_entry, audit, namespace, name, detail, "intent")
        except Exception:
            logger.exception("audit append failed; blocking kubectl debug")
            self._ui.notify("Write blocked: audit log unavailable", severity="error")
            return
        argv = build_debug_argv(
            namespace, name, container, context=self._settings().kube_context, image=image
        )
        target = f"{name}/{container}" if container else name
        with self._ui.suspend():
            exit_code, pull_failure = self.run_process(
                argv,
                f"korvid debug -> {target} (exit to return)",
                namespace,
                name,
                image,
                approved_uid,
            )
        self._ui.refresh()
        if exit_code is None:
            # The baseline snapshot saw a different pod incarnation: the
            # attach never started.
            await self._audit_outcome(
                audit, namespace, name, detail, "error: pod replaced before attach"
            )
            self._ui.notify(
                f"kubectl debug cancelled - pod {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return
        if pull_failure is not None:
            outcome = f"error: image pull failed ({pull_failure})"
        else:
            outcome = "success" if exit_code == 0 else f"error: exit {exit_code}"
        await self._audit_outcome(audit, namespace, name, detail, outcome)
        if pull_failure is not None:
            self.offer_pull_retry(namespace, name, container, approved_uid, image, pull_failure)
            return
        if exit_code != 0:
            self._ui.notify(
                f"kubectl debug exited with status {exit_code}"
                " — check RBAC (pods/ephemeralcontainers) and cluster version",
                severity="warning",
            )

    def offer_pull_retry(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str,
        reason: str,
    ) -> None:
        """Offer an immediate retry with the fallback image after a pull failure.

        Air-gapped guard: when `debug.images` is configured without a
        `debug.default_image`, no public busybox is offered - notify only.
        """
        settings = self._settings()
        if settings.images is not None and not settings.default_image:
            fallback = None
        else:
            fallback = settings.default_image or FALLBACK_IMAGE
        target = f"{name}/{container}" if container else name
        # Equivalent references (untagged vs :latest) would retry the very
        # image that just failed - and each retry permanently adds another
        # ephemeral container entry to the pod spec.
        if fallback is None or same_image_ref(fallback, image) or self._ui.screen_depth() > 1:
            self._ui.notify(
                f"kubectl debug: image pull failed for {image} ({reason})",
                severity="error",
            )
            return
        retry_image = fallback
        epoch = self._get_epoch()

        def _on_choice(confirmed: bool | None) -> None:
            if not confirmed:
                return
            if self._epoch_crossed(epoch):
                self._ui.notify(
                    "kubectl debug retry cancelled - the kube context changed",
                    severity="warning",
                )
                return
            self._ui.run_worker(
                self._run_debug()(namespace, name, container, approved_uid, retry_image)
            )

        self._ui.push_screen(
            self._confirm_screen(
                f"Image pull failed for {image} ({reason})",
                f"Retry kubectl debug on {target}{write_locus(namespace)} with"
                f" {retry_image}? Note: the failed ephemeral container entry cannot be"
                " removed from the pod spec; the retry attaches an additional"
                " container.",
            ),
            _on_choice,
        )

    def run_process(
        self,
        argv: list[str],
        banner: str,
        namespace: str,
        name: str,
        image: str,
        approved_uid: str | None,
    ) -> tuple[int | None, str | None]:
        """Run kubectl debug while watching for image pull failures.

        kubectl debug hangs silently on `ErrImagePull`/`ImagePullBackOff`; for
        the first `PULL_CHECK_DEADLINE` seconds the pod's
        `ephemeralContainerStatuses` are polled and a failing pull kills the
        attach so the caller can offer a retry with the fallback image instead
        of leaving the user staring at a hung terminal. Returns
        `(exit_code, pull_failure_reason)`; an exit code of `None` means the
        attach was aborted because the baseline snapshot saw a different pod
        incarnation than the one approved.
        """
        print(f"\x1b[2J\x1b[H\x1b[2m{banner}\x1b[0m", flush=True)
        # Snapshot ephemeral containers already on the pod: failed entries
        # from earlier attempts can never be removed from the spec, and one
        # using the same image must not be blamed on this new attach. Without
        # a reliable baseline (snapshot failed) pull monitoring is disabled
        # for this attempt - a plain wait, exactly as before this feature.
        baseline = self._pod_status(namespace, name)
        if baseline is not None and approved_uid is not None:
            # The snapshot can block for seconds; don't let it widen the UID
            # TOCTOU window - re-verify the incarnation it actually saw.
            baseline_uid = (baseline.get("metadata") or {}).get("uid")
            if baseline_uid is not None and baseline_uid != approved_uid:
                return None, None
        proc = subprocess.Popen(argv)
        if baseline is None:
            return proc.wait(), None
        pre_existing = ephemeral_container_names(baseline)
        deadline = monotonic() + self.PULL_CHECK_DEADLINE
        while True:
            try:
                exit_code = proc.wait(timeout=self.PULL_CHECK_INTERVAL)
            except subprocess.TimeoutExpired:
                pass
            else:
                return self._exit_result(exit_code, namespace, name, image, pre_existing)
            # Check for a failure after EVERY timed wait - including the one
            # during which the deadline elapsed - so a pull failure appearing
            # at the edge of the window is still caught.
            failure = self._check_pull_failure(namespace, name, image, ignore=pre_existing)
            if failure is not None:
                proc.kill()
                proc.wait()
                return 1, failure
            if monotonic() > deadline:
                # Pulls that survive the window are treated as slow-but-alive:
                # stop polling and wait for the interactive session to end.
                return self._exit_result(proc.wait(), namespace, name, image, pre_existing)

    def _exit_result(
        self, exit_code: int, namespace: str, name: str, image: str, ignore: frozenset[str]
    ) -> tuple[int, str | None]:
        """Final result for a finished kubectl debug process.

        kubectl can give up and exit nonzero on its own when the pull fails:
        a nonzero exit triggers one last pull-failure check so the fallback
        retry is offered instead of a generic exit warning.
        """
        if exit_code != 0:
            failure = self._check_pull_failure(namespace, name, image, ignore=ignore)
            if failure is not None:
                return exit_code, failure
        return exit_code, None

    def _pod_status(self, namespace: str, name: str) -> dict[str, Any] | None:
        """Pod manifest via kubectl shell-out, best-effort (None on any error).

        Used while the TUI is suspended - the async client sits behind the
        paused event loop, so the manifest is fetched with a subprocess.
        """
        argv = build_pod_get_argv(namespace, name, context=self._settings().kube_context)
        try:
            result = subprocess.run(argv, capture_output=True, timeout=5)
            if result.returncode != 0:
                return None
            manifest: dict[str, Any] = json.loads(result.stdout)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return None
        return manifest

    def _check_pull_failure(
        self, namespace: str, name: str, image: str, *, ignore: frozenset[str] = frozenset()
    ) -> str | None:
        """Pull-failure reason for `image`'s ephemeral container, best-effort.

        Runs while the TUI is suspended, so it shells out to kubectl; any
        infrastructure error means "no failure detected" and the attach keeps
        running. Containers named in `ignore` (pre-existing before the attach)
        are never blamed.
        """
        manifest = self._pod_status(namespace, name)
        if manifest is None:
            return None
        return find_pull_failure(manifest, image, ignore=ignore)

    async def _audit_outcome(
        self, audit: AuditLog, namespace: str, name: str, detail: str, outcome: str
    ) -> None:
        """Record how a kubectl debug run ended. Best-effort: the mutation
        already happened (or was aborted), so a failed append only warns."""
        try:
            await asyncio.to_thread(self._audit_entry, audit, namespace, name, detail, outcome)
        except Exception:
            logger.exception("audit append failed after kubectl debug")
            self._ui.notify("Audit write failed for the executed debug", severity="warning")

    @staticmethod
    def _audit_entry(audit: AuditLog, namespace: str, name: str, detail: str, outcome: str) -> None:
        audit.append(
            action="debug",
            kind="pods",
            group="",  # pods are core/v1; kubectl debug always targets a pod
            version="v1",
            namespace=namespace,
            name=name,
            detail=detail,
            outcome=outcome,
        )


__all__ = ["DebugController", "DebugSettings"]
