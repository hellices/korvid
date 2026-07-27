"""kubectl port-forward argv builder (issue #38).

Forwards run as kubectl subprocesses rather than through the async client:
`kubernetes.aio` has no portforward support in the ws client used here, and a
subprocess behaves exactly like the hand-run `kubectl port-forward` users
already know — including exiting when its target disappears. That exit is a
feature, not a gap: the registry's liveness tracking and one-key re-attach
(issue #38) are built around detecting it.

``context`` pins the kubeconfig context korvid connected with (same rationale
as `korvid.ui.shell`): without it kubectl reads ``current-context`` at
invocation time, so switching contexts in another terminal would silently
retarget the forward at a different cluster.
"""

from __future__ import annotations

#: Resource kinds a forward can target, mapped to the kubectl type prefix.
FORWARDABLE_KINDS: dict[str, str] = {"pods": "pod", "services": "service"}


def build_port_forward_argv(
    kind: str,
    namespace: str,
    name: str,
    *,
    local_port: int,
    remote_port: int,
    context: str | None = None,
) -> list[str]:
    """Return argv for `kubectl port-forward` to a pod or service.

    Binds explicitly to ``127.0.0.1``: kubectl's default ``localhost`` also
    tries ``::1``, and a forward is a local debugging convenience, never a
    way to expose the cluster on the network.

    Raises:
        ValueError: when ``kind`` is not a forwardable resource kind.
    """
    prefix = FORWARDABLE_KINDS.get(kind)
    if prefix is None:
        raise ValueError(f"cannot port-forward to {kind!r} (pods and services only)")
    return [
        "kubectl",
        "port-forward",
        "--address",
        "127.0.0.1",
        *(["--context", context] if context else []),
        "-n",
        namespace,
        f"{prefix}/{name}",
        f"{local_port}:{remote_port}",
    ]
