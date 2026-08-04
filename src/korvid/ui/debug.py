"""Debug-fallback execution lifecycle, extracted from the app (issue #97 U3c).

`DebugController` owns the post-approval half of the kubectl debug fallback:
the readonly and fail-closed audit gates, re-verifying the approved pod
incarnation, running kubectl debug with image-pull monitoring while the TUI
is suspended, auditing the outcome, and handing a pull failure back for the
retry offer. The probe, RBAC pre-check, image picker, approval dialogs, and
`run_worker` ownership stay on the app — it hands this controller narrow
callables instead of itself.

The `audit` dependency is a getter because wiring may replace it after
construction; `suspend`/`refresh` are late-binding callables so tests that
patch the app's methods keep working.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from time import monotonic
from typing import Any

from korvid.core.audit import AuditLog
from korvid.core.debugimage import ephemeral_container_names, find_pull_failure
from korvid.ui.shell import build_debug_argv, build_pod_get_argv

logger = logging.getLogger(__name__)


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
        notify: Callable[..., None],
        audit: Callable[[], AuditLog | None],
        readonly: Callable[[], bool],
        kube_context: Callable[[], str | None],
        pod_uid_unchanged: Callable[..., Awaitable[bool]],
        suspend: Callable[[], AbstractContextManager[Any]],
        refresh: Callable[[], object],
        offer_pull_retry: Callable[[str, str, str | None, str | None, str, str], None],
    ) -> None:
        self._notify = notify
        self._audit = audit
        self._readonly = readonly
        self._kube_context = kube_context
        self._pod_uid_unchanged = pod_uid_unchanged
        self._suspend = suspend
        self._refresh = refresh
        self._offer_pull_retry = offer_pull_retry

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
            self._notify("Read-only mode: cluster writes are disabled", severity="warning")
            return
        audit = self._audit()
        if audit is None:
            self._notify("Writes disabled: no audit log configured", severity="warning")
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
            self._notify("Write blocked: audit log unavailable", severity="error")
            return
        argv = build_debug_argv(
            namespace, name, container, context=self._kube_context(), image=image
        )
        target = f"{name}/{container}" if container else name
        with self._suspend():
            exit_code, pull_failure = self.run_process(
                argv,
                f"korvid debug -> {target} (exit to return)",
                namespace,
                name,
                image,
                approved_uid,
            )
        self._refresh()
        if exit_code is None:
            # The baseline snapshot saw a different pod incarnation: the
            # attach never started.
            await self._audit_outcome(
                audit, namespace, name, detail, "error: pod replaced before attach"
            )
            self._notify(
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
            self._offer_pull_retry(namespace, name, container, approved_uid, image, pull_failure)
            return
        if exit_code != 0:
            self._notify(
                f"kubectl debug exited with status {exit_code}"
                " — check RBAC (pods/ephemeralcontainers) and cluster version",
                severity="warning",
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
        argv = build_pod_get_argv(namespace, name, context=self._kube_context())
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
            self._notify("Audit write failed for the executed debug", severity="warning")

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


__all__ = ["DebugController"]
