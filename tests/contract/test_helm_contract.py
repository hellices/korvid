"""Helm lifecycle contract: dry-run previews leave no trace, writes move the
release history exactly one revision at a time.

Release state is asserted through the API server (helm's release Secrets)
rather than helm's own output, so the test proves what the cluster stored.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.k8s.helmcli import HelmCLI

from .conftest import RUN_ID

pytestmark = pytest.mark.contract

SECRET = ResourceMeta(kind="Secret", plural="secrets", group="", version="v1", namespaced=True)
RELEASE = f"korvid-contract-{RUN_ID}"[:53].rstrip("-").lower()


@pytest.fixture(scope="module")
def helm_binary() -> str:
    binary = shutil.which("helm")
    if binary is None:
        pytest.skip("helm binary not installed")
    return binary


@pytest.fixture
def chart_dir(helm_binary: str, tmp_path: Path) -> str:
    subprocess.run(
        [helm_binary, "create", "contractchart"], cwd=tmp_path, check=True, capture_output=True
    )
    return str(tmp_path / "contractchart")


async def _revision_exists(client: KubeClient, namespace: str, revision: int) -> bool:
    try:
        await client.get_object(SECRET, namespace, f"sh.helm.release.v1.{RELEASE}.v{revision}")
    except ApiStatusError as exc:
        if exc.status == 404:
            return False
        raise
    return True


async def test_helm_release_lifecycle(
    client: KubeClient, namespace: str, helm_binary: str, chart_dir: str
) -> None:
    helm = HelmCLI(helm_binary)

    # Preview: dry-run install renders manifests but stores no release.
    rendered = await helm.dry_run_install(RELEASE, chart_dir, namespace)
    assert "kind: Deployment" in rendered
    assert not await _revision_exists(client, namespace, 1), "dry-run must not store a release"

    # Execute: install stores exactly revision 1.
    await helm.install(RELEASE, chart_dir, namespace)
    assert await _revision_exists(client, namespace, 1)
    assert not await _revision_exists(client, namespace, 2)

    # Preview: dry-run upgrade leaves history at revision 1.
    await helm.dry_run_upgrade(RELEASE, chart_dir, namespace)
    assert not await _revision_exists(client, namespace, 2), (
        "dry-run upgrade must not add a revision"
    )

    # Execute: upgrade adds exactly revision 2.
    await helm.upgrade(RELEASE, chart_dir, namespace)
    assert await _revision_exists(client, namespace, 2)
    assert not await _revision_exists(client, namespace, 3)

    # Execute: rollback to 1 is itself a new revision (3), preserving history.
    await helm.rollback(RELEASE, 1, namespace)
    assert await _revision_exists(client, namespace, 3)

    # Preview: dry-run uninstall keeps every revision in place.
    await helm.dry_run_uninstall(RELEASE, namespace)
    assert await _revision_exists(client, namespace, 1)
    assert await _revision_exists(client, namespace, 3)

    # Execute: uninstall removes the whole release history.
    await helm.uninstall(RELEASE, namespace)
    for revision in (1, 2, 3):
        assert not await _revision_exists(client, namespace, revision)
