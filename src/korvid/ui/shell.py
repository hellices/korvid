"""Shell-in helper: builds kubectl exec/debug argv for dropping into a pod shell.

All builders accept ``context`` to pin the kubectl subprocess to the kubeconfig
context korvid connected with (k9s parity).  Without it kubectl reads
``current-context`` at invocation time, so switching contexts in another
terminal would silently retarget the shell at a different cluster.  Note this
pins the context *name* only — rewriting that context entry in kubeconfig while
korvid runs is not defended (k9s has the same limitation).
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
    image: str = DEBUG_IMAGE,
) -> list[str]:
    """Return argv for `kubectl debug` attaching an ephemeral debug container.

    This is the escape hatch for distroless images that ship no sh/bash.
    ``--target`` shares the target container's process namespace when given.
    ``image`` defaults to busybox; the runtime-aware recommendation
    (issue #52) passes a toolkit image instead.
    """
    return [
        "kubectl",
        "debug",
        *_context_args(context),
        "-it",
        "-n",
        namespace,
        pod,
        f"--image={image}",
        *([f"--target={container}"] if container else []),
        "--",
        "sh",
    ]


def build_node_debug_argv(
    node: str,
    namespace: str,
    context: str | None = None,
    image: str = DEBUG_IMAGE,
) -> list[str]:
    """Return argv for `kubectl debug node/<node>` opening a node shell.

    kubectl creates a `node-debugger-…` pod pinned to the node with the
    host filesystem mounted at `/host` (issue #46). The namespace is always
    pinned explicitly so korvid knows where to clean the pod up afterwards.
    `--profile=sysadmin` (kubectl 1.27+) makes the pod actually privileged —
    the default profile mounts the host filesystem but denies the privileged
    security context the approval dialog states, so tools like
    `chroot /host` would fail.
    """
    return [
        "kubectl",
        "debug",
        *_context_args(context),
        "-it",
        "-n",
        namespace,
        f"node/{node}",
        f"--image={image}",
        "--profile=sysadmin",
        "--",
        "sh",
        "-c",
        "command -v bash >/dev/null 2>&1 && exec bash || exec sh",
    ]


def build_pod_list_argv(namespace: str, context: str | None = None) -> list[str]:
    """Return argv listing a namespace's pods as JSON, used to find the
    `node-debugger-…` pod a node shell created so it can be deleted."""
    return [
        "kubectl",
        "get",
        "pods",
        *_context_args(context),
        "-n",
        namespace,
        "-o",
        "json",
    ]


def build_pod_get_argv(namespace: str, pod: str, context: str | None = None) -> list[str]:
    """Return argv fetching a pod's JSON, used to poll ephemeralContainerStatuses.

    The pull-failure watch (issue #52) runs while kubectl debug is attached and
    the TUI is suspended, so it shells out instead of using the async client.
    """
    return [
        "kubectl",
        "get",
        "pod",
        *_context_args(context),
        "-n",
        namespace,
        pod,
        "-o",
        "json",
    ]
