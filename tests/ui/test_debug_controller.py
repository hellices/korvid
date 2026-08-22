"""Unit tests for `DebugController` (issue #97 U3c, retry offer in Deep Task 10).

The controller owns the whole post-approval half of the debug fallback —
readonly and audit gates, uid re-check, the suspended kubectl debug run with
pull monitoring, outcome audit, and the image-pull retry offer with its
air-gap guard. Textual arrives as `UiSurface`, so it is tested without an app.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from korvid.core.audit import AuditLog
from korvid.ui.debug import DebugController, DebugSettings
from korvid.ui.ui_surface import Severity, UiSurface
from korvid.ui.widgets.confirm_screen import ConfirmScreen


class FakeUi(UiSurface):
    """Records the notifications, modals, workers and suspends."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.screens: list[Any] = []
        self.callbacks: list[Any] = []
        self.workers: list[Any] = []
        self.suspends = 0
        self.refreshes = 0
        self.depth = 1

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: Severity = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notifications.append((message, severity))

    def push_screen(self, screen: Any, callback: Any = None) -> Any:
        self.screens.append(screen)
        self.callbacks.append(callback)
        return None

    def run_worker(
        self,
        work: Any,
        *,
        exclusive: bool = False,
        group: str = "default",
        name: str = "",
        exit_on_error: bool = True,
        thread: bool = False,
    ) -> Any:
        self.workers.append(work)
        return None

    async def drain(self) -> None:
        """Run the work the app would have scheduled off the message pump."""
        pending, self.workers = self.workers, []
        for work in pending:
            await work

    async def cancel_workers(self, group: str) -> None:  # pragma: no cover
        return None

    @contextlib.contextmanager
    def _suspended(self) -> Iterator[None]:
        self.suspends += 1
        yield

    def suspend(self) -> contextlib.AbstractContextManager[None]:
        return self._suspended()

    def refresh(self) -> None:
        self.refreshes += 1

    def call_from_thread(  # pragma: no cover
        self, callback: Callable[..., Any], *args: Any
    ) -> None:
        callback(*args)

    def call_later(  # pragma: no cover
        self, callback: Callable[..., None], *args: Any
    ) -> None:
        callback(*args)

    def progress(self, label: str) -> contextlib.AbstractContextManager[None]:  # pragma: no cover
        return contextlib.nullcontext()

    def is_current_screen(self, screen: Any) -> bool:  # pragma: no cover
        return True

    def screen_depth(self) -> int:
        return self.depth


class Harness:
    def __init__(
        self,
        *,
        audit: AuditLog | None = None,
        readonly: bool = False,
        uid_ok: bool = True,
        process_result: tuple[int | None, str | None] = (0, None),
        default_image: str | None = None,
        images: dict[str, str] | None = None,
    ) -> None:
        self.readonly = readonly
        self.audit_log = audit
        self._uid_ok = uid_ok
        self._process_result = process_result
        self.ui = FakeUi()
        self.processes: list[list[str]] = []
        self.reruns: list[tuple[str, str, str | None, str | None, str]] = []
        self.confirmations: list[tuple[str, str]] = []
        self.settings = DebugSettings(kube_context=None, default_image=default_image, images=images)
        self.controller = DebugController(
            ui=self.ui,
            audit=lambda: self.audit_log,
            readonly=lambda: self.readonly,
            settings=lambda: self.settings,
            pod_uid_unchanged=self._uid_unchanged,
            confirm_screen=self._confirm_screen,
            run_debug=lambda: self._rerun,
        )
        self.controller.run_process = self._run_process  # type: ignore[method-assign]  # unit tests stub the subprocess engine

    @property
    def notifications(self) -> list[tuple[str, str]]:
        return self.ui.notifications

    @property
    def suspends(self) -> int:
        return self.ui.suspends

    @property
    def refreshes(self) -> int:
        return self.ui.refreshes

    def _notify(self, message: str, *, severity: str = "information") -> None:
        self.ui.notifications.append((message, severity))

    def _confirm_screen(self, title: str, operation: str, **kwargs: Any) -> ConfirmScreen:
        screen = ConfirmScreen(title, operation, **kwargs)
        self.confirmations.append((title, operation))
        return screen

    async def _rerun(
        self,
        namespace: str,
        name: str,
        container: str | None,
        approved_uid: str | None,
        image: str,
    ) -> None:
        self.reruns.append((namespace, name, container, approved_uid, image))

    async def _uid_unchanged(self, namespace: str, name: str, uid: str, *, action: str) -> bool:
        if not self._uid_ok:
            self._notify(f"{action} cancelled - pod {name} was replaced", severity="warning")
        return self._uid_ok

    async def approve(self, confirmed: bool | None = True) -> None:
        """Answer the retry dialog the way the user would, then let the
        worker the approval scheduled run."""
        self.ui.callbacks[-1](confirmed)
        await self.ui.drain()

    def _run_process(
        self,
        argv: list[str],
        banner: str,
        namespace: str,
        name: str,
        image: str,
        approved_uid: str | None,
    ) -> tuple[int | None, str | None]:
        self.processes.append(argv)
        return self._process_result


def _entries(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


async def test_readonly_blocks_without_any_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), readonly=True)
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert h.notifications == [
        ("Read-only mode: cluster writes are disabled", "warning"),
    ]
    assert not h.processes
    assert not audit_path.exists()


async def test_missing_audit_log_blocks_the_debug() -> None:
    h = Harness(audit=None)
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert h.notifications == [
        ("Writes disabled: no audit log configured", "warning"),
    ]
    assert not h.processes


async def test_replaced_pod_cancels_before_the_intent_audit(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), uid_ok=False)
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert not h.processes
    assert not audit_path.exists()


async def test_intent_audit_failure_blocks_the_run(tmp_path: Path) -> None:
    class ExplodingAudit(AuditLog):
        def append(self, **kwargs: Any) -> None:
            raise OSError("disk full")

    h = Harness(audit=ExplodingAudit(tmp_path / "audit.jsonl"))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert ("Write blocked: audit log unavailable", "error") in h.notifications
    assert not h.processes


async def test_successful_run_audits_intent_then_success(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path))
    await h.controller.run("default", "api-1", "app", "u1", "busybox:1.36")
    assert h.suspends == 1
    assert h.refreshes == 1
    assert len(h.processes) == 1
    assert "--image=busybox:1.36" in " ".join(h.processes[0])
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "success"]
    assert not h.ui.screens


async def test_pull_failure_audits_error_and_offers_the_retry(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), process_result=(1, "ErrImagePull"))
    await h.controller.run("default", "api-1", None, "u1", "koolkits:jvm")
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "error: image pull failed (ErrImagePull)"]
    assert len(h.ui.screens) == 1
    assert "koolkits:jvm" in h.confirmations[0][0]


async def test_replaced_pod_at_attach_time_audits_and_warns(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), process_result=(None, None))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "error: pod replaced before attach"]
    assert any("was replaced since the prompt" in msg for msg, _sev in h.notifications)
    assert not h.ui.screens


async def test_nonzero_exit_audits_error_and_hints_at_rbac(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=AuditLog(audit_path), process_result=(42, None))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    outcomes = [e["outcome"] for e in _entries(audit_path)]
    assert outcomes == ["intent", "error: exit 42"]
    assert any("check RBAC" in msg for msg, _sev in h.notifications)


async def test_outcome_audit_failure_only_warns(tmp_path: Path) -> None:
    """The mutation already happened: a failed outcome append must warn, not
    raise out of the worker."""

    class FailAfterIntent(AuditLog):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.appends = 0

        def append(self, **kwargs: Any) -> None:
            self.appends += 1
            if self.appends > 1:
                raise OSError("disk full")
            super().append(**kwargs)

    audit_path = tmp_path / "audit.jsonl"
    h = Harness(audit=FailAfterIntent(audit_path))
    await h.controller.run("default", "api-1", None, "u1", "busybox:1.36")
    assert ("Audit write failed for the executed debug", "warning") in h.notifications
    assert [e["outcome"] for e in _entries(audit_path)] == ["intent"]


async def test_intent_audit_runs_off_the_event_loop(tmp_path: Path) -> None:
    """Audit appends fsync; a blocking append must not stall the loop."""
    blocked = asyncio.Event()

    class SlowAudit(AuditLog):
        def append(self, **kwargs: Any) -> None:
            time.sleep(0.05)  # simulates the fsync stall (worker thread)
            super().append(**kwargs)

    h = Harness(audit=SlowAudit(tmp_path / "audit.jsonl"))

    async def _heartbeat() -> None:
        blocked.set()

    task = asyncio.create_task(h.controller.run("default", "api-1", None, "u1", "busybox:1.36"))
    heartbeat = asyncio.create_task(_heartbeat())
    await asyncio.wait_for(blocked.wait(), timeout=1.0)  # loop stayed responsive
    await task
    await heartbeat
    assert h.processes


# ---------------------------------------------------------------------------
# The image-pull retry offer (moved off the app in Deep Task 10)
# ---------------------------------------------------------------------------


async def test_the_retry_offer_proposes_the_configured_fallback_image() -> None:
    h = Harness(default_image="registry.local/busybox:1.36")
    h.controller.offer_pull_retry("team", "api-1", "app", "u1", "koolkits:jvm", "ErrImagePull")
    title, operation = h.confirmations[0]
    assert title == "Image pull failed for koolkits:jvm (ErrImagePull)"
    assert "registry.local/busybox:1.36" in operation
    assert "api-1/app" in operation


async def test_an_approved_retry_reruns_the_debug_with_the_fallback() -> None:
    h = Harness(default_image="registry.local/busybox:1.36")
    h.controller.offer_pull_retry("team", "api-1", "app", "u1", "koolkits:jvm", "ErrImagePull")
    await h.approve()
    assert h.reruns == [("team", "api-1", "app", "u1", "registry.local/busybox:1.36")]


async def test_a_declined_retry_never_reruns_the_debug() -> None:
    h = Harness(default_image="registry.local/busybox:1.36")
    h.controller.offer_pull_retry("team", "api-1", "app", "u1", "koolkits:jvm", "ErrImagePull")
    await h.approve(False)
    assert h.reruns == []


async def test_a_dismissed_retry_never_reruns_the_debug() -> None:
    h = Harness(default_image="registry.local/busybox:1.36")
    h.controller.offer_pull_retry("team", "api-1", "app", "u1", "koolkits:jvm", "ErrImagePull")
    await h.approve(None)
    assert h.reruns == []


async def test_an_air_gapped_image_map_offers_no_public_fallback() -> None:
    """`debug.images` configured without a `debug.default_image` means the
    cluster cannot pull from Docker Hub: notify, never offer busybox."""
    h = Harness(images={"java": "registry.local/koolkits:jvm"})
    h.controller.offer_pull_retry("team", "api-1", None, "u1", "koolkits:jvm", "ErrImagePull")
    assert h.ui.screens == []
    assert h.notifications == [
        ("kubectl debug: image pull failed for koolkits:jvm (ErrImagePull)", "error")
    ]


async def test_an_equivalent_fallback_reference_is_not_offered() -> None:
    """Retrying the very image that just failed would only add another
    ephemeral container entry that can never be removed."""
    h = Harness(default_image="busybox:1.36")
    h.controller.offer_pull_retry("team", "api-1", None, "u1", "busybox:1.36", "ErrImagePull")
    assert h.ui.screens == []
    assert h.notifications[-1][1] == "error"


async def test_no_retry_dialog_opens_over_another_screen() -> None:
    h = Harness(default_image="registry.local/busybox:1.36")
    h.ui.depth = 2  # a modal is already open
    h.controller.offer_pull_retry("team", "api-1", None, "u1", "koolkits:jvm", "ErrImagePull")
    assert h.ui.screens == []
    assert h.notifications[-1][1] == "error"


async def test_the_default_fallback_is_offered_without_any_debug_config() -> None:
    h = Harness()
    h.controller.offer_pull_retry("team", "api-1", None, "u1", "koolkits:jvm", "ErrImagePull")
    assert len(h.ui.screens) == 1
    assert "busybox:1.36" in h.confirmations[0][1]
