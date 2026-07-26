"""Tests for the kubectl port-forward argv builder (issue #38)."""

from __future__ import annotations

from korvid.k8s.portforward import build_port_forward_argv


def test_pod_forward_argv() -> None:
    argv = build_port_forward_argv("pods", "default", "api-1", local_port=8080, remote_port=80)
    assert argv == [
        "kubectl",
        "port-forward",
        "--address",
        "127.0.0.1",
        "-n",
        "default",
        "pod/api-1",
        "8080:80",
    ]


def test_service_forward_argv() -> None:
    argv = build_port_forward_argv("services", "prod", "api", local_port=5432, remote_port=5432)
    assert argv == [
        "kubectl",
        "port-forward",
        "--address",
        "127.0.0.1",
        "-n",
        "prod",
        "service/api",
        "5432:5432",
    ]


def test_context_is_pinned_when_given() -> None:
    argv = build_port_forward_argv(
        "pods", "default", "api-1", local_port=8080, remote_port=80, context="staging"
    )
    assert "--context" in argv
    assert argv[argv.index("--context") + 1] == "staging"


def test_no_context_args_without_context() -> None:
    argv = build_port_forward_argv("pods", "default", "api-1", local_port=1, remote_port=2)
    assert "--context" not in argv
