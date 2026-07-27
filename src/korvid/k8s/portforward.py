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

#: Workload kinds kubectl port-forward resolves to a live pod itself — used
#: when a re-attach follows a replaced pod to its owning workload (issue #38).
WORKLOAD_KINDS: dict[str, str] = {
    "deployments": "deployment",
    "replicasets": "replicaset",
    "replicationcontrollers": "replicationcontroller",
    "statefulsets": "statefulset",
    "daemonsets": "daemonset",
    "jobs": "job",
}


#: API groups for forwardable kinds outside the core group.
_TARGET_GROUPS: dict[str, str] = {
    "deployments": "apps",
    "replicasets": "apps",
    "statefulsets": "apps",
    "daemonsets": "apps",
    "jobs": "batch",
}


def forward_target_gvr(kind: str) -> tuple[str, str]:
    """The (group, version) of a forwardable kind, for full-GVR audit entries.

    Core-group kinds (pods, services, replicationcontrollers) yield an empty
    group; workload kinds yield their apps/batch group. All forwardable kinds
    are v1 in supported clusters.
    """
    return _TARGET_GROUPS.get(kind, ""), "v1"


def build_port_forward_argv(
    kind: str,
    namespace: str,
    name: str,
    *,
    local_port: int,
    remote_port: int,
    context: str | None = None,
) -> list[str]:
    """Return argv for `kubectl port-forward` to a pod, service, or workload.

    Binds explicitly to ``127.0.0.1``: kubectl's default ``localhost`` also
    tries ``::1``, and a forward is a local debugging convenience, never a
    way to expose the cluster on the network.

    Raises:
        ValueError: when ``kind`` is not a forwardable resource kind.
    """
    prefix = FORWARDABLE_KINDS.get(kind) or WORKLOAD_KINDS.get(kind)
    if prefix is None:
        raise ValueError(f"cannot port-forward to {kind!r} (pods, services, and workloads only)")
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
