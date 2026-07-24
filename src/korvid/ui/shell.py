"""Shell-in helper: builds kubectl exec/debug argv for dropping into a pod shell.

All builders accept ``context`` to pin the kubectl subprocess to the kubeconfig
context korvid connected with (kubeconfig parity).  Without it kubectl reads
``current-context`` at invocation time, so switching contexts in another
terminal would silently retarget the shell at a different cluster.  Note this
pins the context *name* only — rewriting that context entry in kubeconfig while
korvid runs is not defended (existing TUIs share this limitation).
"""

from __future__ import annotations

DEBUG_IMAGE = "busybox:1.36"


def _context_args(context: str | None) -> list[str]:
    return ["--context", context] if context else []


def build_exec_argv(
    namespace: str,
    pod: str,
    container: str | None = None,
    context: str | None = None,
) -> list[str]:
    """Return argv for `kubectl exec -it` that prefers bash and falls back to sh."""
    return [
        "kubectl",
        "exec",
        *_context_args(context),
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
    context: str | None = None,
) -> list[str]:
    """Return argv for a non-interactive probe that checks whether sh exists.

    Used after an interactive shell exits non-zero to tell "no shell in image"
    (probe fails -> offer kubectl debug) apart from "user's last command failed
    or Ctrl+C" (probe succeeds -> nothing to do).
    """
    return [
        "kubectl",
        "exec",
        *_context_args(context),
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
    context: str | None = None,
) -> list[str]:
    """Return argv for `kubectl debug` attaching an ephemeral busybox container.

    This is the escape hatch for distroless images that ship no sh/bash.
    ``--target`` shares the target container's process namespace when given.
    """
    return [
        "kubectl",
        "debug",
        *_context_args(context),
        "-it",
        "-n",
        namespace,
        pod,
        f"--image={DEBUG_IMAGE}",
        *([f"--target={container}"] if container else []),
        "--",
        "sh",
    ]
