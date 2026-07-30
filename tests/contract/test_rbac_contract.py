"""RBAC contract: short-lived TokenRequest tokens act with exactly the
permissions their Role grants — allowed in the bound namespace, 403
elsewhere, and SelfSubjectAccessReview agrees with observed behaviour.

ServiceAccount tokens are authorised by native Kubernetes RBAC even on an
Azure-RBAC-enabled cluster, so this exercises the same authz path korvid's
permission probes rely on.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from kubernetes_asyncio import client as k8s
from kubernetes_asyncio import config as k8s_config
from kubernetes_asyncio.client.exceptions import ApiException

from korvid.k8s.client import KubeClient
from korvid.k8s.discovery import ResourceMeta

from .conftest import CONFIGMAP, configmap_manifest, run_labels

pytestmark = pytest.mark.contract

SERVICE_ACCOUNT = ResourceMeta(
    kind="ServiceAccount", plural="serviceaccounts", group="", version="v1", namespaced=True
)
ROLE = ResourceMeta(
    kind="Role", plural="roles", group="rbac.authorization.k8s.io", version="v1", namespaced=True
)
ROLE_BINDING = ResourceMeta(
    kind="RoleBinding",
    plural="rolebindings",
    group="rbac.authorization.k8s.io",
    version="v1",
    namespaced=True,
)

SA_NAME = "korvid-contract-reader"


def _role_manifest() -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "configmap-reader", "labels": run_labels()},
        "rules": [
            {"apiGroups": [""], "resources": ["configmaps"], "verbs": ["get", "list"]},
        ],
    }


def _binding_manifest(namespace: str) -> dict[str, Any]:
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "configmap-reader", "labels": run_labels()},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "configmap-reader",
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": SA_NAME, "namespace": namespace},
        ],
    }


@pytest.fixture
async def sa_api(client: KubeClient, namespace: str) -> AsyncIterator[k8s.ApiClient]:
    """ApiClient authenticated as the namespace-scoped reader ServiceAccount."""
    sa_manifest = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": SA_NAME, "labels": run_labels()},
    }
    await client.create_object(SERVICE_ACCOUNT, namespace, sa_manifest)
    await client.create_object(ROLE, namespace, _role_manifest())
    await client.create_object(ROLE_BINDING, namespace, _binding_manifest(namespace))

    bootstrap = k8s.Configuration()
    await k8s_config.load_kube_config(client_configuration=bootstrap)
    async with k8s.ApiClient(configuration=bootstrap) as boot_api:
        token_request = k8s.AuthenticationV1TokenRequest(
            spec=k8s.V1TokenRequestSpec(audiences=[], expiration_seconds=600)
        )
        response = await k8s.CoreV1Api(boot_api).create_namespaced_service_account_token(
            SA_NAME, namespace, token_request
        )
    token = response.status.token
    assert token, "TokenRequest must return a short-lived token"

    sa_conf = k8s.Configuration(host=bootstrap.host)
    sa_conf.ssl_ca_cert = bootstrap.ssl_ca_cert
    sa_conf.api_key = {"BearerToken": token}
    sa_conf.api_key_prefix = {"BearerToken": "Bearer"}
    async with k8s.ApiClient(configuration=sa_conf) as api:
        yield api
    # Namespace teardown removes SA/Role/RoleBinding with everything else.


async def test_token_grants_bound_namespace_only(
    client: KubeClient, namespace: str, sa_api: k8s.ApiClient
) -> None:
    await client.create_object(CONFIGMAP, namespace, configmap_manifest("rbac-visible"))
    core = k8s.CoreV1Api(sa_api)

    # Allowed: the Role grants get/list on configmaps in this namespace.
    listed = await core.list_namespaced_config_map(namespace)
    assert any(item.metadata.name == "rbac-visible" for item in listed.items)

    # Denied elsewhere: identical read in another namespace is 403.
    with pytest.raises(ApiException, match="403"):
        await core.list_namespaced_config_map("default")

    # Denied verb: the Role has no create.
    with pytest.raises(ApiException, match="403"):
        await core.create_namespaced_config_map(
            namespace, k8s.V1ConfigMap(metadata=k8s.V1ObjectMeta(name="rbac-forbidden"))
        )


async def test_self_subject_access_review_matches_observed(
    namespace: str, sa_api: k8s.ApiClient
) -> None:
    authz = k8s.AuthorizationV1Api(sa_api)

    async def review(verb: str, ns: str) -> bool:
        ssar = k8s.V1SelfSubjectAccessReview(
            spec=k8s.V1SelfSubjectAccessReviewSpec(
                resource_attributes=k8s.V1ResourceAttributes(
                    namespace=ns, verb=verb, resource="configmaps"
                )
            )
        )
        result = await authz.create_self_subject_access_review(ssar)
        return bool(result.status.allowed)

    assert await review("list", namespace) is True
    assert await review("list", "default") is False
    assert await review("create", namespace) is False
