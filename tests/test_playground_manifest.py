"""Contracts for the throwaway-cluster workloads that Getting Started applies.

The guide tells a newcomer to run a remote manifest against a cluster, so
what that manifest creates is a promise: only the `playground` namespace and
its three small workloads, nothing that needs the host or the Kubernetes API,
and exactly the healthy and broken states the guide's table asks the reader
to find.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "deploy" / "playground" / "workloads.yaml"
GUIDE = ROOT / "docs" / "getting-started.md"
RAW_URL = "https://raw.githubusercontent.com/hellices/korvid/main/deploy/playground/workloads.yaml"
NAMESPACE = "playground"
CLUSTER = "korvid-playground"


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


def _guide_section() -> str:
    guide = GUIDE.read_text(encoding="utf-8")
    assert "## Practice on a throwaway cluster" in guide
    return guide.split("## Practice on a throwaway cluster", 1)[1].split("\n## ", 1)[0]


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
        if document["kind"] != "Namespace":
            assert document["metadata"]["namespace"] == NAMESPACE


def test_workloads_need_no_host_access_privilege_or_api_token() -> None:
    for name in _deployments():
        spec = _pod_spec(name)
        assert spec["automountServiceAccountToken"] is False, name
        assert not {"hostNetwork", "hostPID", "hostIPC", "volumes"} & spec.keys(), name
        for container in spec["containers"]:
            assert "securityContext" not in container, name
            assert "volumeMounts" not in container, name


def test_each_workload_shows_the_state_the_guide_promises() -> None:
    images = {name: _pod_spec(name)["containers"][0]["image"] for name in _deployments()}

    assert images == {
        "web": "nginx:1.27",
        "checkout": "busybox:1.36",
        "payments": "nginx:1.27-alpne",
    }, "payments must keep its deliberately misspelled tag; the others must stay pullable"
    command = " ".join(_pod_spec("checkout")["containers"][0]["command"])
    assert "DATABASE_URL" in command
    assert command.rstrip().endswith("exit 1"), "checkout must keep crashing"
    service = next(document for document in _documents() if document["kind"] == "Service")
    assert service["spec"]["selector"] == _deployments()["web"]["spec"]["selector"]["matchLabels"]


def test_the_guide_applies_this_manifest_only_to_the_throwaway_context() -> None:
    section = _guide_section()

    assert RAW_URL.endswith(MANIFEST.relative_to(ROOT).as_posix())
    assert RAW_URL in section
    assert f"kind create cluster --name {CLUSTER}" in section
    assert f"kubectl --context kind-{CLUSTER} apply" in section
    assert f"korvid -n {NAMESPACE}" in section
    assert f"kind delete cluster --name {CLUSTER}" in section
    for name in ("web", "checkout", "payments"):
        assert f"`{name}`" in section, f"the guide must say what to look for in {name}"
