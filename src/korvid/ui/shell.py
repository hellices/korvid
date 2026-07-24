"""Shell-in helper: builds the kubectl exec argv for dropping into a pod shell."""

from __future__ import annotations


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
