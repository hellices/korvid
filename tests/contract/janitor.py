"""Pre-run janitor for the live contract cluster (issue #109).

Run as ``python -m tests.contract.janitor`` before the suite starts. It
restores the cluster to a clean baseline no matter how the previous run
died:

- deletes every namespace labelled ``app.kubernetes.io/managed-by=korvid-contract``
- uncordons any node a crashed node-ops test left unschedulable

Cleanup is keyed purely on labels, so it never touches anything a human
created by hand on the cluster.
"""

from __future__ import annotations

import asyncio
import sys

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANAGED_BY_VALUE = "korvid-contract"

NAMESPACE = ResourceMeta(
    kind="Namespace", plural="namespaces", group="", version="v1", namespaced=False
)
NODE = ResourceMeta(kind="Node", plural="nodes", group="", version="v1", namespaced=False)


async def _sweep_namespaces(client: KubeClient) -> list[str]:
    deleted: list[str] = []
    for summary in await client.list_objects(NAMESPACE, None):
        if dict(summary.labels).get(MANAGED_BY_LABEL) != MANAGED_BY_VALUE:
            continue
        try:
            await client.delete_object(NAMESPACE, None, summary.name)
        except ApiStatusError as exc:
            if exc.status != 404:  # already terminating/gone is success
                raise
        deleted.append(summary.name)
    return deleted


async def _sweep_cordons(client: KubeClient) -> list[str]:
    uncordoned: list[str] = []
    for summary in await client.list_objects(NODE, None):
        manifest = await client.get_object(NODE, None, summary.name)
        labels = manifest["metadata"].get("labels") or {}
        if labels.get("korvid.dev/disposable") != "true":
            continue
        if manifest["spec"].get("unschedulable"):
            await client.cordon_node(summary.name, False)
            uncordoned.append(summary.name)
    return uncordoned


async def main() -> int:
    client = KubeClient()
    await client.connect()
    try:
        namespaces = await _sweep_namespaces(client)
        nodes = await _sweep_cordons(client)
    finally:
        await client.close()
    print(f"janitor: deleted {len(namespaces)} stale namespace(s) {namespaces}")
    print(f"janitor: uncordoned {len(nodes)} node(s) {nodes}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
