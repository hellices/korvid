"""Shell-in helper: builds kubectl exec/debug argv for dropping into a pod shell."""

from __future__ import annotations

DEBUG_IMAGE = "busybox:1.36"


def build_exec_argv(
    namespace: str,
    pod: str,
    container: str | None = None,
) -> list[str]:
    """Return argv for `kubectl exec -it` that prefers bash and falls back to sh."""
    return [
        "kubectl",
        "exec",
        "-it",
        "-n",
        namespace,
        pod,
        *(["-c", container] if container else []),
        "--",
        "sh",
        "-c",
        "command -v bash >/dev/null 2>&1 && exec bash || exec sh",
    ]


def build_probe_argv(
    namespace: str,
    pod: str,
    container: str | None = None,
) -> list[str]:
    """Return argv for a non-interactive probe that checks whether sh exists.

    Used after an interactive shell exits non-zero to tell "no shell in image"
    (probe fails -> offer kubectl debug) apart from "user's last command failed
    or Ctrl+C" (probe succeeds -> nothing to do).
    """
    return [
        "kubectl",
        "exec",
        "-n",
        namespace,
        pod,
        *(["-c", container] if container else []),
        "--",
        "sh",
        "-c",
        "exit 0",
    ]


def build_debug_argv(
    namespace: str,
    pod: str,
    container: str | None = None,
) -> list[str]:
    """Return argv for `kubectl debug` attaching an ephemeral busybox container.

    This is the escape hatch for distroless images that ship no sh/bash.
    ``--target`` shares the target container's process namespace when given.
    """
    return [
        "kubectl",
        "debug",
        "-it",
        "-n",
        namespace,
        pod,
        f"--image={DEBUG_IMAGE}",
        *([f"--target={container}"] if container else []),
        "--",
        "sh",
    ]
