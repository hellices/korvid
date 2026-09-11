"""Child programs used by the native Windows ConPTY smoke test."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import sys
import threading
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any

from textual.app import App

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import PodSummary
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.filter_bar import FilterBar
from korvid.ui.widgets.help_screen import HelpScreen
from korvid.ui.widgets.resource_table import ResourceTable

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))
_ALIASES = {"pods": _PODS_META, "po": _PODS_META, "pod": _PODS_META}
_KERNEL32: Any | None = None


def _platform_attribute(module: object, name: str) -> Any:
    return getattr(module, name)


def _witness_root() -> Path:
    value = os.environ.get("KORVID_SMOKE_WITNESS_DIR")
    if value is None:
        raise RuntimeError("KORVID_SMOKE_WITNESS_DIR is required")
    return Path(value)


def _emit_witness(name: str, payload: dict[str, Any]) -> None:
    root = _witness_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{name}.json"
    staging = root / f".{name}-{os.getpid()}.json"
    staging.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(staging, target)


def _process_handle_count() -> int:
    global _KERNEL32
    if _KERNEL32 is not None:
        kernel32 = _KERNEL32
    else:
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            raise RuntimeError("ctypes.WinDLL is unavailable")
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        _KERNEL32 = kernel32
    count = wintypes.DWORD()
    if not kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count)):
        code = int(_platform_attribute(ctypes, "get_last_error")())
        raise OSError(code, f"GetProcessHandleCount failed with Windows error {code}")
    return int(count.value)


def _textual_threads() -> dict[str, int]:
    counts = Counter(
        thread.name
        for thread in threading.enumerate()
        if thread.name in {"textual-input", "textual-output"}
    )
    return dict(counts)


def _snapshot(app: KorvidApp) -> dict[str, Any]:
    driver = app._driver
    return {
        "driver": (
            None
            if driver is None
            else f"{driver.__class__.__module__}.{driver.__class__.__qualname__}"
        ),
        "parent_pid": os.getppid(),
        "pid": os.getpid(),
        "process_handles": _process_handle_count(),
        "textual_threads": _textual_threads(),
    }


def _resources_are_visible(rows: int, table_visible: bool, workspace_visible: bool) -> bool:
    return rows == 2 and table_visible and workspace_visible


class _ObservedKorvidApp(KorvidApp):
    """Production app with read-only phase witnesses for the native test."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._witnessed: set[str] = set()
        self._help_seen = False
        self._resume_seen = False
        self._post_resume_filter_applied = False
        self._post_resume_filter_seen = False

    def _emit_once(self, name: str, payload: dict[str, Any]) -> None:
        if name in self._witnessed:
            return
        self._witnessed.add(name)
        _emit_witness(name, payload)

    async def on_mount(self) -> None:
        await super().on_mount()
        driver = self._driver
        if driver is None:
            raise RuntimeError("native app mounted without a Textual driver")
        self.app_suspend_signal.subscribe(self, self._on_native_suspend, immediate=True)
        self.app_resume_signal.subscribe(self, self._on_native_resume, immediate=True)
        self._emit_once(
            "mounted",
            {
                **_snapshot(self),
                "can_suspend": driver.can_suspend,
                "headless": driver.is_headless,
                "stdin_tty": sys.stdin.isatty(),
                "stdout_tty": sys.stdout.isatty(),
            },
        )
        self.set_interval(0.05, self._observe_state)

    def _on_native_suspend(self, app: App[None]) -> None:
        self._emit_once("suspended", _snapshot(self))

    def _on_native_resume(self, app: App[None]) -> None:
        self._resume_seen = True
        self._emit_once("resumed", _snapshot(self))

    def _observe_state(self) -> None:
        table = self.query_one(ResourceTable)
        workspace = self.query_one("#workspace")
        filter_bar = self.query_one(FilterBar)
        rows = table.row_count
        pattern = self.filter_pattern
        table_visible = bool(table.display)
        workspace_visible = bool(workspace.display)
        state = {
            **_snapshot(self),
            "filter": pattern,
            "filter_focused": self.focused is filter_bar,
            "filter_open": bool(filter_bar.display),
            "rows": rows,
            "screen": self.screen.__class__.__qualname__,
            "table_visible": table_visible,
            "workspace_visible": workspace_visible,
        }
        if pattern == "" and _resources_are_visible(rows, table_visible, workspace_visible):
            self._emit_once("resources-ready", state)
        screen = self.screen
        if isinstance(screen, HelpScreen):
            self._help_seen = True
            self._emit_once("help-open", {**state, "body": screen.body_text()})
        elif self._help_seen:
            self._emit_once("help-closed", state)
        if not self._resume_seen and bool(filter_bar.display) and self.focused is filter_bar:
            self._emit_once("filter-focused", state)
        if (
            pattern == "api"
            and rows == 1
            and not bool(filter_bar.display)
            and self.focused is not filter_bar
        ):
            self._emit_once("filter-applied", state)
        if (
            self._resume_seen
            and not self._post_resume_filter_applied
            and bool(filter_bar.display)
            and self.focused is filter_bar
        ):
            self._emit_once("post-resume-filter-focused", state)
        if (
            self._resume_seen
            and pattern == "worker"
            and rows == 1
            and not bool(filter_bar.display)
            and self.focused is not filter_bar
        ):
            self._post_resume_filter_applied = True
            self._emit_once("post-resume-filter-applied", state)
        if (
            self._post_resume_filter_applied
            and bool(filter_bar.display)
            and self.focused is filter_bar
        ):
            self._post_resume_filter_seen = True
            self._emit_once("post-resume-filter-open", state)
        if (
            self._post_resume_filter_seen
            and not bool(filter_bar.display)
            and pattern == ""
            and rows == 2
        ):
            self._emit_once("post-resume-input", state)


def _pod(name: str) -> PodSummary:
    return PodSummary(
        name=name,
        namespace="default",
        phase="Running",
        ready="1/1",
        restarts=0,
        node="smoke-node",
        containers=("main",),
        uid=f"uid-{name}",
    )


def _make_app() -> _ObservedKorvidApp:
    """Build the real app around an in-memory watch; no kubeconfig or network is read."""
    pods = (_pod("api-1"), _pod("worker-2"))
    store = ResourceStore()

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        if kind == "pods":
            for pod in pods:
                yield "ADDED", pod
        while True:
            await asyncio.sleep(60)

    async def list_namespaces() -> list[str]:
        return ["default"]

    return _ObservedKorvidApp(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, source),
        list_namespaces=list_namespaces,
        aliases=dict(_ALIASES),
        agent_available=False,
    )


def _run_app_instance(app: KorvidApp, on_exit: Callable[[int], None]) -> int:
    """Run the native app and propagate Textual's handled-error return code."""
    app.run(headless=False, mouse=False)
    return_code = app.return_code
    exit_code = 1 if return_code is None else return_code
    on_exit(exit_code)
    return exit_code


def _run_app() -> int:
    if os.name != "nt":
        raise RuntimeError("native app smoke child must run on Windows")
    app = _make_app()
    return _run_app_instance(
        app,
        lambda exit_code: _emit_witness(
            "app-exited",
            {**_snapshot(app), "return_code": exit_code},
        ),
    )


def _run_fake_kubectl(argv: Sequence[str]) -> int:
    _emit_witness(
        "shell-child-started",
        {
            "argv": list(argv),
            "executable": sys.executable,
            "parent_pid": os.getppid(),
            "pid": os.getpid(),
            "stdin_tty": sys.stdin.isatty(),
            "stdout_tty": sys.stdout.isatty(),
        },
    )
    print("KORVID_SMOKE_SHELL_READY", flush=True)
    for _ in range(8):
        line = sys.stdin.readline()
        if line == "":
            return 3
        text = line.rstrip("\r\n")
        if text == "exit":
            _emit_witness("shell-child-exited", {"pid": os.getpid()})
            return 0
        _emit_witness("shell-input", {"pid": os.getpid(), "text": text})
        print(f"KORVID_SMOKE_SHELL_INPUT:{text}", flush=True)
    return 4


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--app", action="store_true")
    mode.add_argument("--fake-kubectl", action="store_true")
    args, remainder = parser.parse_known_args(argv)
    args.remainder = remainder
    return args


def main(argv: Sequence[str] | None = None) -> int:
    """Run the native app or its controlled external kubectl child."""
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.app:
        return _run_app()
    remainder = args.remainder
    assert isinstance(remainder, list)
    return _run_fake_kubectl(remainder)


if __name__ == "__main__":
    raise SystemExit(main())
