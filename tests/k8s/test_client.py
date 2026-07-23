from typing import Any
from unittest.mock import AsyncMock, patch

from korvid.k8s.client import KubeClient


def _pod(name: str, ns: str = "default") -> dict[str, Any]:
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": {},
        "status": {"phase": "Running", "containerStatuses": [{"ready": True, "restartCount": 0}]},
    }


async def test_list_pods_parses_summaries() -> None:
    client = KubeClient()
    fake_v1 = AsyncMock()
    fake_v1.list_namespaced_pod.return_value = {"items": [_pod("a"), _pod("b")]}
    with patch.object(client, "_core_v1", fake_v1):
        pods = await client.list_pods("default")
    assert [p.name for p in pods] == ["a", "b"]
    fake_v1.list_namespaced_pod.assert_awaited_once_with("default", _preload_content=False)
