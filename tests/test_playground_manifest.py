"""Contracts for the throwaway-cluster workloads that Getting Started applies.

The guide tells a newcomer to apply a downloaded manifest to a cluster, so what
that manifest creates is a promise: only the `playground` namespace and its
three small workloads, nothing that needs the host or the Kubernetes API, and
exactly the healthy and broken states the guide's table asks the reader to
find. The guide's commands are a promise too: they must not be able to reach
any cluster other than the throwaway one.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from tests.platforms import posix_only

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "deploy" / "playground" / "workloads.yaml"
GUIDE = ROOT / "docs" / "getting-started.md"
RAW_URL = "https://raw.githubusercontent.com/hellices/korvid/main/deploy/playground/workloads.yaml"
NAMESPACE = "playground"
CLUSTER = "korvid-playground"
CONTEXT = f"kind-{CLUSTER}"
DEDICATED_KUBECONFIG = f'export KUBECONFIG="$PWD/{CLUSTER}.kubeconfig"'

#: The only fields the workloads may set. An allowlist rather than a denylist,
#: so a new field that grants host access (`hostPort`, an init container, a
#: pod `securityContext`, a Service `type`) fails here instead of slipping by.
ALLOWED_POD_FIELDS = {"automountServiceAccountToken", "containers"}
ALLOWED_CONTAINER_FIELDS = {"name", "image", "ports", "command"}
ALLOWED_SERVICE_FIELDS = {"selector", "ports"}


def _documents() -> list[dict[str, Any]]:
    loaded = list(yaml.safe_load_all(MANIFEST.read_text(encoding="utf-8")))
    documents = [document for document in loaded if document is not None]
    assert all(isinstance(document, dict) for document in documents)
    return documents


def _deployments() -> dict[str, dict[str, Any]]:
    return {
        document["metadata"]["name"]: document
        for document in _documents()
        if document["kind"] == "Deployment"
    }


def _pod_spec(name: str) -> dict[str, Any]:
    spec = _deployments()[name]["spec"]["template"]["spec"]
    assert isinstance(spec, dict)
    return spec


def _checkout_script() -> str:
    command = _pod_spec("checkout")["containers"][0]["command"]
    assert command[:2] == ["sh", "-c"], "checkout must run one shell script"
    script = command[2]
    assert isinstance(script, str)
    return script


def _guide_section() -> str:
    guide = GUIDE.read_text(encoding="utf-8")
    assert "## Practice on a throwaway cluster" in guide
    return guide.split("## Practice on a throwaway cluster", 1)[1].split("\n## ", 1)[0]


def _shell_blocks(section: str) -> list[list[str]]:
    blocks = re.findall(r"```sh\n(.*?)```", section, flags=re.DOTALL)
    return [[line.strip() for line in block.splitlines() if line.strip()] for block in blocks]


def test_manifest_creates_only_the_playground_namespace_and_its_workloads() -> None:
    created = sorted((document["kind"], document["metadata"]["name"]) for document in _documents())

    assert created == [
        ("Deployment", "checkout"),
        ("Deployment", "payments"),
        ("Deployment", "web"),
        ("Namespace", NAMESPACE),
        ("Service", "web"),
    ]
    for document in _documents():
        if document["kind"] == "Namespace":
            assert "spec" not in document
        else:
            assert document["metadata"]["namespace"] == NAMESPACE


def test_workloads_set_only_reviewed_fields() -> None:
    for name, deployment in _deployments().items():
        spec = deployment["spec"]
        assert set(spec) == {"replicas", "selector", "template"}, name
        selector = spec["selector"]["matchLabels"]
        assert selector.items() <= spec["template"]["metadata"]["labels"].items(), name
        pod = _pod_spec(name)
        assert set(pod) == ALLOWED_POD_FIELDS, name
        assert pod["automountServiceAccountToken"] is False, name
        assert len(pod["containers"]) == 1, name
        container = pod["containers"][0]
        assert set(container) <= ALLOWED_CONTAINER_FIELDS, name
        for port in container.get("ports", []):
            assert set(port) == {"containerPort"}, name

    service = next(document for document in _documents() if document["kind"] == "Service")
    assert set(service["spec"]) == ALLOWED_SERVICE_FIELDS, "the Service stays a plain ClusterIP"


def test_each_workload_shows_the_state_the_guide_promises() -> None:
    images = {name: _pod_spec(name)["containers"][0]["image"] for name in _deployments()}

    assert images == {
        "web": "nginx:1.27",
        "checkout": "busybox:1.36",
        "payments": "nginx:1.27-alpne",
    }, "payments must keep its deliberately misspelled tag; the others must stay pullable"
    service = next(document for document in _documents() if document["kind"] == "Service")
    assert service["spec"]["selector"] == _deployments()["web"]["spec"]["selector"]["matchLabels"]


@posix_only("the checkout script is a POSIX shell script")
def test_checkout_crashes_only_while_database_url_is_unset() -> None:
    """The manifest says checkout exits *because* DATABASE_URL is not set.

    A stub `sleep` ends the script's keep-alive loop, so the configured run
    finishes instead of waiting forever and proves it got past the check.
    """
    script = f"sleep() {{ exit 7; }}\n{_checkout_script()}"
    path = {"PATH": os.environ.get("PATH", "")}

    unset = subprocess.run(
        ["sh", "-c", script], env=path, capture_output=True, text=True, timeout=30, check=False
    )
    configured = subprocess.run(
        ["sh", "-c", script],
        env={**path, "DATABASE_URL": "postgres://orders"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert unset.returncode == 1
    assert "FATAL: DATABASE_URL is not set" in unset.stderr
    assert configured.returncode == 7, "with DATABASE_URL set, checkout must keep running"
    assert "FATAL" not in configured.stderr


def test_the_guide_keeps_the_playground_in_its_own_kubeconfig() -> None:
    section = _guide_section()
    blocks = _shell_blocks(section)
    assert len(blocks) == 2, "the guide has one setup block and one cleanup block"
    setup, cleanup = blocks

    assert setup[0] == DEDICATED_KUBECONFIG, "isolate the kubeconfig before any other command"
    assert setup[1:] == [
        f"kind create cluster --name {CLUSTER}",
        f"curl -fsSLO {RAW_URL}",
        f"kubectl --context {CONTEXT} apply -f workloads.yaml",
        f"korvid -n {NAMESPACE}",
    ]
    assert RAW_URL.endswith(MANIFEST.relative_to(ROOT).as_posix())
    kubectl = [line for block in blocks for line in block if line.startswith("kubectl")]
    assert kubectl
    assert all(f"--context {CONTEXT}" in line for line in kubectl)
    assert cleanup[0] == f"kind delete cluster --name {CLUSTER}"
    assert "unset KUBECONFIG" in section
    for name in ("web", "checkout", "payments"):
        assert f"`{name}`" in section, f"the guide must say what to look for in {name}"
