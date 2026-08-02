"""Optional telepresence integration, phase 1 (issue #159).

Detection + read-only status/intercept queries over the `telepresence`
binary, following the `helmcli` discipline: `shutil.which` detection, fixed
argv, no shell, bounded timeout. Absent binary = the feature simply does
not exist; nothing here runs at startup.

One empirically-verified caveat drives the call policy: `telepresence
status` is *not* side-effect-free — it spawns the local user daemon when
absent (observed on v2.30.1). Queries therefore run only on an explicit
user action (opening the status panel), never on a background poll.

JSON shapes verified against the OSS v2.30 source
(pkg/client/cli/cmd/status.go, list.go; snake_case proto tags) and a live
binary; every parser degrades field-by-field instead of raising.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Traffic-manager's conventional server-side home (used by the install
#: hint's cluster-side probe).
TRAFFIC_MANAGER_NAMESPACE = "ambassador"
TRAFFIC_MANAGER_NAME = "traffic-manager"

_STDERR_TAIL_LINES = 3


def find_telepresence() -> str | None:
    """Absolute path of the `telepresence` binary on PATH, or None."""
    return shutil.which("telepresence")


class TelepresenceError(Exception):
    """A telepresence invocation failed (non-zero exit, timeout, bad output)."""


@dataclass(frozen=True)
class TelepresenceStatus:
    """Connection state from `telepresence status` (display only)."""

    connected: bool
    user_running: bool
    root_running: bool
    version: str = ""
    kubernetes_context: str = ""
    traffic_manager_version: str = ""
    #: CLI-reported failure line ({"error": …} / {"cmd", "err"} shapes); ""
    #: when the status parsed cleanly.
    error: str = ""


@dataclass(frozen=True)
class ActiveIntercept:
    """One active intercept row from `telepresence list`."""

    workload: str
    namespace: str
    kind: str = ""
    name: str = ""
    client: str = ""
    port: str = ""


def _str_of(mapping: dict[str, Any], key: str) -> str:
    value = mapping.get(key)
    return value if isinstance(value, str) else ""


def _dict_of(payload: Any, key: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def parse_status(payload: Any) -> TelepresenceStatus:
    """`telepresence status --format json` → TelepresenceStatus.

    Never raises: the shape varies by daemon/connection state and version
    ({"error": …} and {"cmd", "err"} failure forms both observed live), so
    every field degrades independently. Three source-emitted layouts are
    normalized (status.go): the flat user_daemon/root_daemon form, the
    containerized combined `daemon` form, and the multi-daemon
    `{"connections": […]}` wrapper (the first connected entry wins).
    """
    if isinstance(payload, dict) and "connections" in payload:
        return _pick_connection(payload.get("connections"))
    error = ""
    if isinstance(payload, dict):
        error = _str_of(payload, "error") or _str_of(payload, "err")
    user = _dict_of(payload, "user_daemon")
    root = _dict_of(payload, "root_daemon")
    combined = _dict_of(payload, "daemon")
    if combined:
        # Docker-hosted daemons: one object serves both roles.
        user = combined
        root = combined
    manager = _dict_of(payload, "traffic_manager")
    return TelepresenceStatus(
        # The user daemon reports an explicit "Connected" status string;
        # anything else (missing, "Not connected") is not a session.
        connected=_str_of(user, "status") == "Connected",
        user_running=user.get("running") is True,
        root_running=root.get("running") is True,
        version=_str_of(user, "version"),
        kubernetes_context=_str_of(user, "kubernetes_context"),
        traffic_manager_version=_str_of(manager, "version"),
        error=error,
    )


def _pick_connection(connections: Any) -> TelepresenceStatus:
    """The connected entry of a multi-daemon status, else the first one."""
    if not isinstance(connections, list) or not connections:
        return TelepresenceStatus(connected=False, user_running=False, root_running=False)
    parsed = [parse_status(entry) for entry in connections]
    return next((s for s in parsed if s.connected), parsed[0])


def _intercept_port(spec: dict[str, Any]) -> str:
    for key in ("service_port", "container_port", "target_port"):
        value = spec.get(key)
        if isinstance(value, int) and value:
            return str(value)
    return _str_of(spec, "port_identifier")


def parse_intercepts(payload: Any) -> list[ActiveIntercept]:
    """`telepresence list --format json` → active intercept rows.

    The output is a bare array of WorkloadInfo objects; only workloads with
    `intercept_info` entries produce rows. Junk shapes yield nothing.
    """
    if not isinstance(payload, list):
        return []
    rows: list[ActiveIntercept] = []
    for workload in payload:
        if not isinstance(workload, dict):
            continue
        infos = workload.get("intercept_info")
        if not isinstance(infos, list):
            continue
        for info in infos:
            if not isinstance(info, dict):
                continue
            spec = _dict_of(info, "spec")
            rows.append(
                ActiveIntercept(
                    workload=_str_of(workload, "name"),
                    namespace=_str_of(workload, "namespace"),
                    kind=_str_of(workload, "workload_resource_type"),
                    name=_str_of(spec, "name"),
                    client=_str_of(spec, "client"),
                    port=_intercept_port(spec),
                )
            )
    return rows


async def _execute(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run one subprocess to completion: (exit code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise TelepresenceError(f"failed to start telepresence: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TelepresenceError(f"telepresence timed out after {timeout:.0f}s") from None
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class TelepresenceCLI:
    """Typed read-only queries over one telepresence binary."""

    def __init__(self, binary: str, *, timeout: float = 10.0) -> None:
        self._binary = binary
        self._timeout = timeout

    async def _run_json(self, *args: str) -> Any:
        argv = [self._binary, *args, "--format", "json"]
        code, stdout, stderr = await _execute(argv, self._timeout)
        if code != 0:
            tail = "\n".join(stderr.strip().splitlines()[-_STDERR_TAIL_LINES:]).strip()
            raise TelepresenceError(tail or f"telepresence exited with code {code}")
        try:
            return json.loads(stdout or "{}")
        except json.JSONDecodeError as exc:
            raise TelepresenceError(f"unexpected telepresence output: {exc}") from exc

    async def status(self) -> TelepresenceStatus:
        """Connection state. Note: telepresence itself may start its local
        user daemon to answer — call on explicit user action only."""
        return parse_status(await self._run_json("status"))

    async def list_intercepts(self) -> list[ActiveIntercept]:
        """Active intercepts (requires a connected session)."""
        return parse_intercepts(await self._run_json("list"))
