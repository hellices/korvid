"""The live connection's cluster identity names its saved namespace slots (issue #406)."""

from __future__ import annotations

import pytest
from kubernetes_asyncio import client as k8s_client

from korvid.k8s import cluster_identity


def _connected_to(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    configuration = k8s_client.Configuration()
    configuration.host = host
    monkeypatch.setattr(
        k8s_client.Configuration, "get_default_copy", classmethod(lambda _cls: configuration)
    )


def test_an_explicit_context_pairs_with_the_connected_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connected_to(monkeypatch, "https://dev.example:6443")

    def unexpected_kubeconfig_read(_config_file: str | None = None) -> tuple[list[str], str]:
        raise AssertionError("an explicit context needs no kubeconfig lookup")

    monkeypatch.setattr(cluster_identity, "list_context_names", unexpected_kubeconfig_read)

    assert cluster_identity.current_cluster_identity("dev") == ("dev", "https://dev.example:6443")


def test_the_kubeconfig_current_context_names_a_default_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _connected_to(monkeypatch, "https://prod.example:6443")
    monkeypatch.setattr(
        cluster_identity, "list_context_names", lambda _file=None: (["dev", "prod"], "prod")
    )

    assert cluster_identity.current_cluster_identity(None) == (
        "prod",
        "https://prod.example:6443",
    )


def test_an_unresolvable_context_has_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _connected_to(monkeypatch, "https://prod.example:6443")
    monkeypatch.setattr(cluster_identity, "list_context_names", lambda _file=None: ([], None))

    assert cluster_identity.current_cluster_identity(None) is None


def test_a_connection_without_a_server_has_no_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _connected_to(monkeypatch, "")

    assert cluster_identity.current_cluster_identity("dev") is None
