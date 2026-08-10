"""Interactive shell and kubectl-debug flows, extracted from the app (#187).

`ShellController` owns three related journeys: `kubectl exec` into a pod,
the ephemeral-container `kubectl debug` fallback offered when that fails
(typically a distroless image with no shell), and the node shell that runs
a privileged debug pod behind the approval gate (issue #46).

The security perimeter stays with the app, but note *how*: these flows do
not use `WriteGate.confirm` / `WriteGate.run`. They build a `ConfirmScreen`
through the injected factory and drive their own audited subprocess, which
is what they did before the extraction and is deliberately unchanged here -
a "no behaviour change" refactor is the wrong place to reroute an approval
path. What they do share is the rest of the perimeter: `permitted` for the
RBAC pre-check, `epoch` / `switching` for revalidation across an awaited
gap, `reads_allowed` to refuse mid-`:ctx` starts, and `reserve_write` so an
approved shell counts as an in-flight cluster write.

Routing these two lifecycles through a typed gate operation is worth doing;
it is a behavioural change and belongs in its own review.

Suspending the terminal is why `UiSurface` grew `suspend`, `refresh` and
`call_from_thread`: an interactive child process takes the screen, and the
subprocess is driven off the message pump, so getting back onto it is a
capability rather than a convenience.

The injected getters are read at call time because a `:ctx` switch rebinds
the debug wrapper and the manifest source after construction.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import shutil
import subprocess
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, Concatenate, ParamSpec, TypeVar

from textual.app import SuspendNotSupported

from korvid.core.audit import AuditLog
from korvid.core.debugimage import recommend_debug_images
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.writes import WriteOps
from korvid.ui.debug import DebugController
from korvid.ui.shell import (
    DEBUG_IMAGE,
    build_exec_argv,
    build_node_debug_create_argv,
    build_pod_attach_argv,
    build_pod_get_argv,
    build_pod_wait_argv,
    build_probe_argv,
    parse_debug_pod_name,
)
from korvid.ui.ui_surface import UiSurface
from korvid.ui.view_state import ViewState
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ImagePrompt
from korvid.ui.widgets.pick_screen import PickScreen
from korvid.ui.write_gate import WriteGate

logger = logging.getLogger(__name__)

#: Budget for the target-uid lookup that pins a write to one incarnation.
_UID_LOOKUP_TIMEOUT = 10.0


def _looks_like_admission_rejection(stderr: str) -> bool:
    """True when kubectl stderr clearly shows the API server refused the
    create — only then is it safe to state that no pod was committed.

    Matches the stable phrases of the two refusal shapes: API-server
    refusals (`Error from server (Forbidden): ... is forbidden: ...`) and
    admission webhooks (`admission webhook ... denied the request`). The
    match is deliberately tight — a false positive here suppresses the
    cleanup hint and can strand a privileged pod, so a bare `forbidden`
    substring (which could appear in a pod or image name, or quoted inside
    an unrelated server error) is not enough.
    """
    lowered = stderr.lower()
    return "error from server (forbidden)" in lowered or "denied the request" in lowered


_WriteParams = ParamSpec("_WriteParams")
_WriteResult = TypeVar("_WriteResult")


def _tracks_cluster_write(
    method: Callable[Concatenate[ShellController, _WriteParams], Awaitable[_WriteResult]],
) -> Callable[Concatenate[ShellController, _WriteParams], Coroutine[Any, Any, _WriteResult]]:
    """Count an in-flight cluster mutation through the write gate (issue #36).

    Mirrors the app-side decorator: an approved write worker is neither an
    open dialog nor the agent task, so `:ctx` switching checks this count. A
    mutation approved for one cluster must never execute against another
    after a mid-flight retarget.
    """

    def wrapper(
        self: ShellController, /, *args: _WriteParams.args, **kwargs: _WriteParams.kwargs
    ) -> Coroutine[Any, Any, _WriteResult]:
        # Reserve synchronously, at the call: confirmation callbacks build
        # this coroutine and hand it to run_worker, which starts it on a
        # later loop iteration - a `:ctx` queued in that gap must already
        # see the write in flight. Deferring this to run() is the exact
        # defect review caught twice during this decomposition.
        release = self._gate.reserve_write()

        async def run() -> _WriteResult:
            try:
                return await method(self, *args, **kwargs)
            finally:
                release()

        coro = run()
        # Release when the coroutine is collected without ever running -
        # a worker cancelled before its first step, or app shutdown. A
        # leaked reservation would block every later `:ctx` switch.
        #
        # This fires on collection rather than on close(): a coroutine
        # that never started ignores close(), and priming it to arm the
        # `finally` instead makes it unawaitable
        # ("coroutine is being awaited already") wherever a decorated
        # method is consumed by `await` rather than by a worker Task.
        weakref.finalize(coro, release)
        return coro

    # Not functools.wraps: its _Wrapped return type keeps the explicit
    # 'self' arg and fails the plain-Callable annotation under --strict.
    wrapper.__name__ = method.__name__
    wrapper.__qualname__ = method.__qualname__
    return wrapper


@dataclasses.dataclass(frozen=True)
class ShellSettings:
    """The configuration these flows read, as an immutable snapshot.

    Narrower than `KorvidConfig`, deliberately: that object is only
    shallowly frozen, so passing it whole would hand a controller mutable
    `keybindings` and `agent_options` dicts along with the five values it
    actually uses. `debug_images` is a `Mapping` for the same reason.
    """

    kube_context: str | None
    debug_default_image: str | None
    debug_images: Mapping[str, str] | None
    node_shell_image: str | None
    node_shell_namespace: str | None


class ShellController:
    """Owns pod exec, the debug-container fallback, and the node shell."""

    def __init__(
        self,
        *,
        gate: WriteGate,
        view: ViewState,
        ui: UiSurface,
        debug: Callable[[], DebugController | None],
        audit: Callable[[], AuditLog | None],
        get_manifest: Callable[[], Callable[..., Any] | None],
        pod_containers: Callable[[str, str], tuple[str, ...]],
        node_target: Callable[[str], tuple[WriteOps, ResourceMeta, str, str | None] | None],
        confirm_screen: Callable[..., ConfirmScreen],
        settings: Callable[[], ShellSettings],
        target_uid: Callable[[str, str | None, str], Awaitable[str | None]],
    ) -> None:
        self._gate = gate
        self._view = view
        self._ui = ui
        self._debug_ctl = debug
        self._audit_log = audit
        self._get_manifest_fn = get_manifest
        self._pod_containers = pod_containers
        self._node_target_fn = node_target
        self._confirm_screen_fn = confirm_screen
        # Read at call time: a `:ctx` switch retargets kube_context.
        self._settings = settings
        self._target_uid_fn = target_uid

    def shell(self) -> None:
        """Drop into a shell inside the selected pod via kubectl exec.

        Multi-container pods show a container picker first; if exec fails
        (typically a distroless image without sh/bash) a `kubectl debug`
        ephemeral-container fallback is offered. On the nodes view the same
        key opens a node shell via `kubectl debug node/` behind an approval
        dialog (issue #46).
        """
        kind = self._view.canonical_kind(self._view.current_kind())
        meta = self._view.aliases().get(kind)
        # The exec would race the teardown/retarget and could attach to
        # whichever cluster wins — refuse up front.
        if not self._gate.reads_allowed():
            return
        if meta is not None and (meta.group, meta.plural) == ("", "nodes"):
            self._ui.run_worker(self._node_shell_flow())
            return
        if kind != "pods":
            self._ui.notify("Shell is available for pods and nodes", severity="warning")
            return

        ns, name = self._view.selected_ns_name()
        if ns is None or name is None:
            return
        namespace = ns

        if shutil.which("kubectl") is None:
            self._ui.notify(
                "kubectl not found on PATH — shell-in requires kubectl",
                severity="error",
            )
            return

        containers = self._pod_containers(namespace, name)
        if len(containers) > 1:
            epoch = self._gate.epoch()

            def _on_pick(container: str | None) -> None:
                if container is None:
                    return
                if self._gate.switching() or epoch != self._gate.epoch():
                    # The picker stayed open across a context switch: the
                    # selection belongs to the old cluster while kubectl
                    # would now target the new one.
                    self._ui.notify(
                        f"shell into {name} cancelled - the kube context"
                        " changed while the container picker was open",
                        severity="warning",
                    )
                    return
                self.run_shell(namespace, name, container)

            self._ui.push_screen(
                PickScreen(f"Container in {name}:", list(containers)),
                _on_pick,
            )
            return

        self.run_shell(namespace, name, containers[0] if containers else None)

    @staticmethod
    def _run_interactive(argv: list[str], banner: str) -> int:
        """Run an interactive subprocess on a cleared screen for a direct feel.

        Suspending Textual drops back to the primary screen, exposing old
        scrollback (including the command that launched korvid). Clearing
        first makes it look like we connected straight into the pod.
        """
        print(f"\x1b[2J\x1b[H\x1b[2m{banner}\x1b[0m", flush=True)
        return subprocess.call(argv)

    def run_shell(self, namespace: str, name: str, container: str | None) -> None:
        """Run kubectl exec; offer the kubectl debug fallback only if sh is missing."""
        epoch = self._gate.epoch()
        argv = build_exec_argv(namespace, name, container, context=self._settings().kube_context)
        target = f"{name}/{container}" if container else name
        try:
            with self._ui.suspend():
                exit_code = self._run_interactive(
                    argv, f"korvid shell -> {target} (exit to return)"
                )
        except SuspendNotSupported:
            # Non-suspending drivers (Windows console, web): refuse rather
            # than crash the `s` key, exactly as the node path does.
            self._ui.notify(
                "shell unavailable: this environment does not support"
                " suspending the TUI for an interactive shell",
                severity="error",
            )
            return
        except OSError as exc:
            # kubectl can vanish between the PATH check and the exec.
            self._ui.notify(f"shell failed to start kubectl: {exc}", severity="error")
            return
        self._ui.refresh()
        if exit_code == 0:
            return

        # kubectl exec propagates the remote command's exit code, so a non-zero
        # status can just mean the user's last command failed or they hit Ctrl+C.
        # Probe non-interactively: if sh runs fine, the shell session was real.
        # Run in a thread worker so a slow API server can't freeze the UI.
        def _probe_and_maybe_offer() -> None:
            try:
                probe = subprocess.run(
                    build_probe_argv(
                        namespace, name, container, context=self._settings().kube_context
                    ),
                    capture_output=True,
                    timeout=5,
                )
                shell_exists = probe.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                shell_exists = False  # inconclusive — keep offering the fallback
            if shell_exists:
                return
            self._ui.call_from_thread(
                self._schedule_debug_offer, namespace, name, container, exit_code, epoch
            )

        self._ui.run_worker(_probe_and_maybe_offer, thread=True)

    def _schedule_debug_offer(
        self, namespace: str, name: str, container: str | None, exit_code: int, epoch: int
    ) -> None:
        """Sync shim for call_from_thread: the offer itself is async because
        it awaits the RBAC pre-check."""
        self._ui.run_worker(
            self._offer_debug_fallback(namespace, name, container, exit_code, epoch)
        )

    async def _offer_debug_fallback(
        self, namespace: str, name: str, container: str | None, exit_code: int, epoch: int
    ) -> None:
        """Ask whether to attach a kubectl debug container after a failed shell."""
        if self._view.readonly() or self._audit_log() is None:
            # kubectl debug mutates the pod spec (ephemeral container):
            # never offer a write we would refuse to run.
            self._ui.notify(
                "Shell failed and the debug fallback is unavailable"
                " (read-only mode or no audit log)",
                severity="warning",
            )
            return
        pods_meta = self._view.aliases().get("pods")
        if pods_meta is None:
            # Fail-open like the other permission paths, but never silently.
            logger.warning("pods alias missing; skipping debug RBAC pre-check (fail-open)")
        elif not await self._gate.permitted("debug", pods_meta, namespace, name):
            # RBAC pre-check (spec debug safety contract): don't offer a
            # picker the API server would reject; _permitted notified with
            # "missing permission: patch pods/ephemeralcontainers".
            return
        target = f"{name}/{container}" if container else name
        try:
            # One manifest fetch serves two purposes: binding the offer to
            # this pod incarnation (kubectl debug addresses the pod by
            # namespace/name only, so without the uid a same-named
            # replacement created while the dialogs are open would receive
            # the ephemeral container - _run_debug re-checks the uid just
            # before executing) and runtime detection for the image
            # recommendation (issue #52). 404 -> the pod is already gone.
            manifest = await self._debug_manifest(namespace, name)
        except ApiStatusError:
            self._ui.notify(
                f"Debug fallback for {target} not offered - the pod no longer exists.",
                severity="warning",
            )
            return
        approved_uid: str | None = None
        if manifest is not None:
            raw_uid = (manifest.get("metadata") or {}).get("uid")
            approved_uid = str(raw_uid) if raw_uid else None
        if self._gate.switching() or epoch != self._gate.epoch():
            # The probe/RBAC/manifest awaits crossed a context switch: the
            # offer describes an old-cluster pod while kubectl debug would
            # now target the new context.
            self._ui.notify(
                f"Debug fallback for {target} cancelled - the kube context changed",
                severity="warning",
            )
            return
        if self._ui.screen_depth() > 1:
            # The probe/RBAC pre-check ran concurrently with user input: never
            # stack the offer over a dialog that opened meanwhile.
            self._ui.notify(
                f"Debug fallback for {target} not offered - another dialog is open."
                " Close it and press 's' again to retry.",
                severity="warning",
            )
            return
        self._pick_debug_image(
            namespace, name, container, exit_code, approved_uid, manifest or {}, epoch
        )

    def _pick_debug_image(
        self,
        namespace: str,
        name: str,
        container: str | None,
        exit_code: int,
        approved_uid: str | None,
        manifest: dict[str, Any],
        epoch: int,
    ) -> None:
        """Debug image picker (issue #52): runtime-aware recommendation first,
        alternatives after, plus a custom-image prompt."""
        target = f"{name}/{container}" if container else name
        options = recommend_debug_images(
            manifest,
            container,
            images_cfg=self._settings().debug_images,
            default_image=self._settings().debug_default_image,
        )
        prompts = {f"{opt.image}  ({opt.label})": opt.image for opt in options}
        custom_choice = "Custom image…"

        def _on_image(choice: str | None) -> None:
            if choice is None:
                return
            if choice == custom_choice:

                def _on_custom(image: str | None) -> None:
                    if image:
                        self._confirm_debug(
                            namespace, name, container, exit_code, approved_uid, image, epoch
                        )

                self._ui.push_screen(ImagePrompt(target), _on_custom)
                return
            self._confirm_debug(
                namespace, name, container, exit_code, approved_uid, prompts[choice], epoch
            )

        # Choosing an image is read-only: even if input buffered before this
        # asynchronous picker existed selects an entry, the pod mutation is
        # still gated by the ConfirmScreen pushed in _confirm_debug, whose
        # creation-time key cutoff discards such buffered keystrokes.
        # Air-gapped configs without a matching mapping produce no options:
        # the picker then offers only the custom-image prompt.
        title = f"Shell failed in {target} (exit {exit_code}) - choose a debug image."
        if options:
            title += f"\nRecommended: {options[0].image} - {options[0].reason}"
        self._ui.push_screen(
            PickScreen(title, [*prompts, custom_choice]),
            _on_image,
        )

    async def _debug_manifest(self, namespace: str, name: str) -> dict[str, Any] | None:
        """Pod manifest at debug-offer time (uid binding + runtime detection).

        Same semantics as `_target_uid`: raises `ApiStatusError(404)` when the
        pod is gone; fails open (`None`) when no manifest source is wired or
        the lookup fails or times out - the debug stays approval-gated and
        audited, just without a uid precondition or a runtime recommendation.
        """
        get_manifest = self._get_manifest_fn()
        if get_manifest is None:
            return None
        try:
            return await asyncio.wait_for(
                get_manifest("pods", namespace, name), _UID_LOOKUP_TIMEOUT
            )
        except ApiStatusError as exc:
            if exc.status == 404:
                raise
            logger.warning(
                "manifest lookup for %s/%s failed; offering debug without it", namespace, name
            )
            return None
        except TimeoutError:
            logger.warning(
                "manifest lookup for %s/%s timed out; offering debug without it", namespace, name
            )
            return None
        except Exception:
            # Fail open like _target_uid: an infrastructure error must not
            # escape the worker and silently swallow the debug offer.
            logger.exception(
                "manifest lookup for %s/%s failed; offering debug without it", namespace, name
            )
            return None

    def _confirm_debug(
        self,
        namespace: str,
        name: str,
        container: str | None,
        exit_code: int,
        approved_uid: str | None,
        image: str,
        epoch: int,
    ) -> None:
        """Approval gate for the debug fallback with the chosen image.

        ConfirmScreen, not a generic picker: its creation-time key cutoff
        discards any input buffered before the prompt existed - a queued
        Enter or y must never start a pod mutation the user has not seen.
        """
        target = f"{name}/{container}" if container else name

        def _on_choice(confirmed: bool | None) -> None:
            if not confirmed:
                return
            if self._gate.switching() or epoch != self._gate.epoch():
                # The image picker / approval stayed open across a context
                # switch: kubectl debug would mutate a same-named pod on the
                # new cluster (the uid re-check fails open without a uid).
                self._ui.notify(
                    f"Debug fallback for {target} cancelled - the kube context changed",
                    severity="warning",
                )
                return
            self._ui.run_worker(self.run_debug(namespace, name, container, approved_uid, image))

        self._ui.push_screen(
            self._confirm_screen_fn(
                f"Shell failed in {target} (exit {exit_code})",
                f"kubectl debug: attach a {image} debug container to pod"
                f" {name}{self._view.write_locus(namespace)} - the target image likely"
                " has no sh/bash (distroless). Note: the ephemeral container stays"
                " in the pod spec until restart.",
            ),
            _on_choice,
        )

    @_tracks_cluster_write
    async def run_debug(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str = DEBUG_IMAGE,
    ) -> None:
        """Delegate the gated, audited kubectl debug run to the controller
        (issue #97 U3c); the decorator keeps the write counted against
        `:ctx` switching, and worker ownership stays with the callers."""
        debug = self._debug_ctl()
        if debug is None:  # wiring guarantees one; a missing wrapper must not write
            self._ui.notify("Debug container unavailable in this build", severity="error")
            return
        await debug.run(namespace, name, container, approved_uid, image)

    async def _node_shell_flow(self) -> None:
        """`s` on the nodes view: approval-gated `kubectl debug node/` shell.

        The shell runs in a `node-debugger-…` pod with the node's filesystem
        mounted at `/host` — a privilege escalation, so it always passes the
        approval gate with that stated explicitly, is audit-logged
        fail-closed, and the debug pod is deleted when the shell exits.
        """
        resolved = self._node_target_fn("node shell")
        if resolved is None:
            return
        ops, meta, name, uid = resolved
        if shutil.which("kubectl") is None:
            self._ui.notify(
                "kubectl not found on PATH — node shell requires kubectl", severity="error"
            )
            return
        image = self._settings().node_shell_image or DEBUG_IMAGE
        shell_ns = self._settings().node_shell_namespace or "default"
        pods_meta = self._view.aliases().get("pods")
        epoch = self._gate.epoch()
        if pods_meta is None:
            # Fail-open like the pod-debug pre-check, but never silently.
            logger.warning("pods alias missing; skipping node-shell RBAC pre-check (fail-open)")
        elif not await self._gate.permitted("node-shell", pods_meta, shell_ns, ""):
            return
        if not self._gate.context_intact("node shell", meta, None, name, epoch=epoch):
            # The RBAC round-trip ran concurrently with user input: the
            # approval must stay bound to the selection that initiated it,
            # and never stack over a dialog that opened meanwhile.
            return

        def _on_choice(confirmed: bool | None) -> None:
            if confirmed:
                self._ui.run_worker(self._run_node_shell(ops, name, shell_ns, image, uid))

        self._ui.push_screen(
            self._confirm_screen_fn(
                f"Node shell on {name}",
                f"kubectl debug node/{name}: creates a privileged debug pod"
                f" (image {image}) in namespace {shell_ns} with the node's"
                " filesystem mounted at /host (uses --profile=sysadmin;"
                " requires kubectl 1.30+). The pod is deleted when the shell"
                " exits. This action is audit-logged.",
            ),
            _on_choice,
        )

    @_tracks_cluster_write
    async def _run_node_shell(
        self, ops: WriteOps, node: str, namespace: str, image: str, approved_uid: str | None
    ) -> None:
        """Run the approved node shell, then delete the debugger pod.

        A cluster write (pod creation via kubectl): the intent record must
        persist before the subprocess starts, or the shell is blocked.
        kubectl addresses the node by name only, so the approved node
        incarnation is re-verified just before creating the pod (like the
        pod debug path). The pod is created detached (`--attach=false`); its
        name is parsed from kubectl's creation message and its uid fetched
        with an exact `kubectl get pod`, so korvid knows precisely which pod
        it owns: the interactive session then `kubectl attach`es to it, and
        cleanup deletes exactly that pod with a uid precondition — a debugger
        another operator starts meanwhile is never touched.
        """
        audit = self._audit_log()
        if audit is None:  # _node_target already refused; defensive re-check
            return
        if approved_uid is not None and not await self._node_uid_unchanged(node, approved_uid):
            return
        detail = f"privileged node shell (kubectl debug node, image {image}, namespace {namespace})"
        try:
            await asyncio.to_thread(self._audit_node_shell, audit, node, detail, "intent")
        except Exception:
            logger.exception("audit append failed; blocking node shell")
            self._ui.notify("Write blocked: audit log unavailable", severity="error")
            return
        # The create itself is shielded + settled: cancelling an
        # asyncio.to_thread await does not stop the kubectl subprocess, so a
        # cancellation here could otherwise leak a pod that was created
        # moments later with no finalizer installed.
        create_task = asyncio.ensure_future(self._create_node_debug_pod(node, namespace, image))
        try:
            created = await asyncio.shield(create_task)
        except asyncio.CancelledError:
            try:
                created = await create_task
            except asyncio.CancelledError:
                # The create task itself was cancelled outright (e.g. loop
                # shutdown cancels every task, bypassing the shield): nothing
                # to settle, but a pod may still appear — leave a trace.
                logger.warning(
                    "node shell create cancelled outright; cleanup skipped -"
                    " check namespace %s for leftover node-debugger pods",
                    namespace,
                )
                raise
            if isinstance(created, str):
                await self._audit_create_failure(audit, node, detail, created)
            else:
                await self._finalize_node_shell(
                    ops,
                    audit,
                    node,
                    namespace,
                    created[0],
                    created[1],
                    detail,
                    "error: interrupted",
                )
            raise
        if isinstance(created, str):  # creation failed or pod unidentifiable
            await self._audit_create_failure(audit, node, detail, created)
            return
        pod_name, pod_uid = created
        # Everything after a successful create runs under a finalizer:
        # a worker cancellation, an attach launch error, or Ctrl-C raising
        # KeyboardInterrupt from subprocess.call must still delete the
        # privileged host-mounted pod and record the outcome.
        outcome = "error: interrupted"
        try:
            outcome = await self._wait_and_attach_node_shell(node, namespace, pod_name)
        finally:
            # Shielded + settled so a cancelled worker still deletes the pod
            # and records the outcome: shield() raises CancelledError here on
            # outer cancellation while the finalizer keeps running, so it is
            # re-awaited before the cancellation propagates.
            finalize = asyncio.ensure_future(
                self._finalize_node_shell(
                    ops, audit, node, namespace, pod_name, pod_uid, detail, outcome
                )
            )
            try:
                await asyncio.shield(finalize)
            except asyncio.CancelledError:
                await finalize
                raise

    async def _wait_and_attach_node_shell(self, node: str, namespace: str, pod_name: str) -> str:
        """Wait for the debugger pod, attach interactively, return the outcome.

        Runs entirely under the caller's finalizer, so every exit path —
        including an attach binary that cannot be launched — leaves the pod
        deletion and outcome audit to run.
        """
        wait_argv = build_pod_wait_argv(namespace, pod_name, context=self._settings().kube_context)
        ready = await self._run_kubectl_ok(wait_argv, timeout=75)
        if not ready:
            self._ui.notify(
                f"Debugger pod {pod_name} did not become Ready — the shell may"
                " fail to attach (image pull error or admission problem?)",
                severity="warning",
            )
        attach_argv = build_pod_attach_argv(
            namespace, pod_name, context=self._settings().kube_context
        )
        try:
            with self._ui.suspend():
                exit_code = self._run_interactive(
                    attach_argv, f"korvid node shell -> {node} (exit to return)"
                )
        except SuspendNotSupported:
            # Non-suspending drivers (e.g. Windows, web): refuse gracefully —
            # the finalizer still deletes the pod that was just created.
            self._ui.notify(
                "node shell unavailable: this environment does not support"
                " suspending the TUI for an interactive shell",
                severity="error",
            )
            outcome = "error: suspend not supported"
        except OSError as exc:
            # kubectl itself could not be launched (removed or not executable
            # since the create): keep a specific outcome and let the finalizer
            # delete the pod — an escaping exception would kill the worker and
            # take the TUI down with it.
            logger.warning("kubectl attach could not be launched", exc_info=True)
            self._ui.notify(f"Could not launch kubectl attach: {exc}", severity="error")
            outcome = "error: attach could not be launched"
        else:
            outcome = "success" if exit_code == 0 else f"error: exit {exit_code}"
        self._ui.refresh()
        return outcome

    async def _audit_create_failure(
        self, audit: AuditLog, node: str, detail: str, outcome: str
    ) -> None:
        """Persist a failed/unidentifiable create outcome; surfaced on
        failure because the outcome may record a skipped cleanup the user
        must act on."""
        try:
            await asyncio.to_thread(self._audit_node_shell, audit, node, detail, outcome)
        except Exception:
            logger.exception("audit append failed after node shell create failure")
            self._ui.notify("Audit write failed for the node shell attempt", severity="warning")

    async def _finalize_node_shell(
        self,
        ops: WriteOps,
        audit: AuditLog,
        node: str,
        namespace: str,
        pod_name: str,
        pod_uid: str,
        detail: str,
        outcome: str,
    ) -> None:
        """Delete the debugger pod and record the outcome — always runs,
        even when the shell worker was cancelled or interrupted. The audit
        write is best-effort here: the cluster write already happened, so
        failing it must not hide the cleanup."""
        cleanup = await self._delete_node_debug_pod(ops, namespace, pod_name, pod_uid)
        try:
            await asyncio.to_thread(
                self._audit_node_shell, audit, node, detail, f"{outcome}; {cleanup}"
            )
        except Exception:
            logger.exception("audit append failed after node shell")
            self._ui.notify("Audit write failed for the executed node shell", severity="warning")

    async def _create_node_debug_pod(
        self, node: str, namespace: str, image: str
    ) -> tuple[str, str] | str:
        """Create the node-debugger pod detached; returns (name, uid).

        On failure returns the audit outcome string instead — distinct per
        cause, because they leave different cluster states: a kubectl launch
        failure never reached the cluster, a clearly identified admission
        rejection (where PodSecurity refusals surface, hence the namespace
        hint) leaves nothing behind, while any other non-zero exit, a
        timeout, or a create whose output cannot be parsed may have created
        a pod korvid cannot identify, so the audit records cleanup as
        skipped and names the namespace to inspect.
        """
        argv = build_node_debug_create_argv(
            node, namespace, context=self._settings().kube_context, image=image
        )
        try:
            proc = await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=30)
        except OSError as exc:
            # kubectl itself could not be launched (removed or not executable
            # since the PATH check): no request reached the cluster, so no
            # pod exists and no namespace inspection is needed.
            logger.warning("kubectl could not be launched for node debug", exc_info=True)
            self._ui.notify(f"Could not launch kubectl: {exc}", severity="error")
            return "error: kubectl could not be launched; no pod created"
        except subprocess.TimeoutExpired:
            logger.warning("node-debugger pod creation timed out", exc_info=True)
            self._ui.notify(
                f"kubectl debug node did not respond — a debugger pod may still have"
                f" been created; check {namespace} for leftover node-debugger pods",
                severity="error",
            )
            return f"error: pod creation timed out; cleanup skipped: check namespace {namespace}"
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            logger.warning("node-debugger pod creation failed: %s", stderr)
            if _looks_like_admission_rejection(stderr):
                # The API server refused the create: nothing was committed.
                # The namespace remediation only applies when PodSecurity
                # did the refusing — an RBAC forbid or an unrelated webhook
                # denial would make that hint actionably wrong.
                hint = (
                    " — the cluster refuses privileged pods (PodSecurity admission);"
                    " try setting node_shell.namespace to a namespace that allows them"
                    if "podsecurity" in stderr.lower()
                    else ""
                )
                self._ui.notify(
                    f"Could not create the debugger pod: {stderr}{hint}",
                    severity="error",
                )
                return "error: pod creation rejected"
            # A non-zero exit does not prove rejection: the server can commit
            # the pod and kubectl still fail afterwards (lost response, local
            # output error) — treat as ambiguous, the pod may exist.
            self._ui.notify(
                f"Could not create the debugger pod: {stderr or f'exit {proc.returncode}'}"
                f" — a pod may still have been created; check {namespace} for"
                " leftover node-debugger pods",
                severity="error",
            )
            return f"error: pod creation failed; cleanup skipped: check namespace {namespace}"
        pod_name = parse_debug_pod_name(proc.stdout.decode(errors="replace"))
        if pod_name is None:
            # Pod created (exit 0) but unidentifiable: refuse to guess.
            self._ui.notify(
                f"kubectl did not report the created pod — check {namespace} for"
                " leftover node-debugger pods",
                severity="error",
            )
            return (
                "error: created pod could not be identified;"
                f" cleanup skipped: check namespace {namespace}"
            )
        uid = await self._fetch_created_pod_uid(namespace, pod_name)
        if uid is None:
            # Without the uid the cleanup delete would lose its precondition
            # and could remove a same-name replacement pod: refuse.
            self._ui.notify(
                f"kubectl did not report the created pod's uid — check pod"
                f" {pod_name} in namespace {namespace}",
                severity="error",
            )
            return (
                "error: created pod could not be identified;"
                f" cleanup skipped: check namespace {namespace}"
            )
        return pod_name, uid

    async def _fetch_created_pod_uid(self, namespace: str, pod_name: str) -> str | None:
        """Fetch the just-created debugger pod's uid with an exact get.

        `kubectl debug` has no machine-readable output, so after parsing the
        pod name from its message the uid — required as the cleanup delete's
        precondition — comes from `kubectl get pod <name> -o json`. Any
        failure (launch, timeout, non-zero exit, malformed JSON) returns
        None: the caller treats the pod as unidentifiable rather than guess.
        """
        argv = build_pod_get_argv(namespace, pod_name, context=self._settings().kube_context)
        try:
            proc = await asyncio.to_thread(subprocess.run, argv, capture_output=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("could not fetch created debugger pod", exc_info=True)
            return None
        if proc.returncode != 0:
            logger.warning(
                "created debugger pod fetch failed: %s",
                proc.stderr.decode(errors="replace").strip(),
            )
            return None
        try:
            payload = json.loads(proc.stdout)
        except ValueError:
            return None
        item_meta = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(item_meta, dict):
            # Valid JSON with an unexpected shape (e.g. metadata is a scalar)
            # must land in the unidentifiable branch, not raise past the
            # finalizer while a privileged pod may exist.
            return None
        uid = item_meta.get("uid")
        return uid if isinstance(uid, str) and uid else None

    async def _run_kubectl_ok(self, argv: list[str], timeout: float) -> bool:
        """Run a non-interactive kubectl helper; True on exit 0."""
        try:
            proc = await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, timeout=timeout
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.warning("kubectl helper failed", exc_info=True)
            return False
        return proc.returncode == 0

    async def _node_uid_unchanged(self, name: str, approved_uid: str) -> bool:
        """Re-verify the approved node incarnation just before the shell
        launches; notifies and returns False when the node is gone or was
        replaced under the same name while the dialog was open."""
        try:
            current_uid = await self._target_uid_fn("nodes", None, name)
        except ApiStatusError:
            self._ui.notify(
                f"node shell cancelled - node {name} no longer exists.",
                severity="warning",
            )
            return False
        if current_uid is not None and current_uid != approved_uid:
            self._ui.notify(
                f"node shell cancelled - node {name} was replaced since the prompt was shown.",
                severity="warning",
            )
            return False
        return True

    async def _delete_node_debug_pod(
        self, ops: WriteOps, namespace: str, pod_name: str, pod_uid: str
    ) -> str:
        """Delete exactly the debugger pod this session created (uid
        precondition), returning the audit note."""
        pods_meta = self._view.aliases().get("pods")
        if pods_meta is None:
            self._ui.notify(
                f"Cannot delete debug pod {pod_name} in {namespace} — remove it manually",
                severity="warning",
            )
            return f"cleanup failed for: {pod_name}"
        try:
            await ops.delete_object(pods_meta, namespace, pod_name, uid=pod_uid)
        except Exception:
            logger.exception("node-debugger pod deletion failed")
            self._ui.notify(
                f"Failed to delete debug pod {pod_name} in {namespace} — remove it manually",
                severity="warning",
            )
            return f"cleanup failed for: {pod_name}"
        return f"cleanup: deleted {pod_name}"

    @staticmethod
    def _audit_node_shell(audit: AuditLog, node: str, detail: str, outcome: str) -> None:
        audit.append(
            action="node-shell",
            kind="nodes",
            group="",  # nodes are core/v1
            version="v1",
            namespace=None,
            name=node,
            detail=detail,
            outcome=outcome,
        )
