"""Shell-in helper: builds kubectl exec/debug argv for dropping into a pod shell.

All builders accept ``context`` to pin the kubectl subprocess to the kubeconfig
context korvid connected with (kubeconfig parity).  Without it kubectl reads
``current-context`` at invocation time, so switching contexts in another
terminal would silently retarget the shell at a different cluster.  Note this
pins the context *name* only — rewriting that context entry in kubeconfig while
korvid runs is not defended (existing TUIs share this limitation).
"""

from __future__ import annotations

import re

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


def build_node_debug_create_argv(
    node: str,
    namespace: str,
    context: str | None = None,
    image: str = DEBUG_IMAGE,
) -> list[str]:
    """Return argv creating (without attaching) the node-debugger pod.

    `kubectl debug node/<node>` creates a `node-debugger-…` pod pinned to
    the node with the host filesystem mounted at `/host` (issue #46).
    `--attach=false` detaches creation from the interactive session;
    `kubectl debug` has no `-o/--output`, so the created pod's name is
    parsed from its `Creating debugging pod …` message
    (`parse_debug_pod_name`) and the uid fetched with an exact
    `kubectl get pod` — cleanup then deletes precisely that pod (uid
    precondition) instead of diffing namespace listings, which could catch
    a debugger another operator started meanwhile.
    The namespace is always pinned explicitly. `-it` keeps stdin open and
    allocates a TTY on the container for the later `kubectl attach`.
    `--profile=sysadmin` (kubectl 1.30+) makes the pod actually privileged —
    the default profile mounts the host filesystem but denies the privileged
    security context the approval dialog states, so tools like
    `chroot /host` would fail.
    """
    return [
        "kubectl",
        "debug",
        *_context_args(context),
        "-it",
        "--attach=false",
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


_CREATE_MESSAGE = re.compile(r"^Creating debugging pod (\S+) with container ", re.MULTILINE)


def parse_debug_pod_name(output: str) -> str | None:
    """Extract the created pod's name from `kubectl debug node/` output.

    kubectl prints `Creating debugging pod <name> with container <c> on
    node <n>.` on stdout (kubectl has printed this exact shape since the
    node debug path was introduced; there is no machine-readable output
    mode for `kubectl debug`). Returns None when the message is absent —
    the caller must then treat the pod as unidentifiable rather than guess.
    """
    match = _CREATE_MESSAGE.search(output)
    return match.group(1) if match else None


def build_pod_wait_argv(
    namespace: str,
    pod: str,
    context: str | None = None,
    timeout: str = "60s",
) -> list[str]:
    """Return argv waiting for the debugger pod to become Ready, so the
    interactive attach doesn't race the container start."""
    return [
        "kubectl",
        "wait",
        *_context_args(context),
        "-n",
        namespace,
        f"pod/{pod}",
        "--for=condition=Ready",
        f"--timeout={timeout}",
    ]


def build_pod_attach_argv(namespace: str, pod: str, context: str | None = None) -> list[str]:
    """Return argv attaching interactively to the node-debugger pod's shell."""
    return [
        "kubectl",
        "attach",
        *_context_args(context),
        "-it",
        "-n",
        namespace,
        pod,
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
