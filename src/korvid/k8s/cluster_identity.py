"""Which cluster the live connection talks to, for saved namespace slots (issue #406)."""

from __future__ import annotations

from kubernetes_asyncio import client as k8s_client

from korvid.k8s.client import list_context_names


def current_cluster_identity(context: str | None) -> tuple[str, str] | None:
    """The resolved context name and API server URL of the live connection.

    A session started without an explicit context uses the kubeconfig's
    current-context name. The server is read from the connected client
    configuration, so a context name reused for another cluster resolves to a
    different identity. No network request is made, but the kubeconfig may be
    read, so call this off the event loop.

    Args:
        context: The explicitly selected context, or None for the default.

    Returns:
        `(context, server)`, or None when either part cannot be resolved.
    """
    name = context if context is not None else list_context_names()[1]
    server = k8s_client.Configuration.get_default_copy().host
    if not name or not server:
        return None
    return name, str(server)
