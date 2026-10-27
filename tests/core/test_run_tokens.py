"""Late diagnostic results are dropped by their run token (#409, slice S4)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from kubernetes_asyncio import client as k8s_client

from korvid.core.capability import RunToken
from korvid.core.run_tokens import RunTracker
from korvid.k8s import client as client_module
from korvid.k8s.client import KubeClient


class Cluster:
    """The context and session a controller reads through, changed by the tests."""

    def __init__(self) -> None:
        self.epoch = 3
        self.session_generation = 7
        self.switching = False

    def tracker(self) -> RunTracker:
        return RunTracker(
            epoch=lambda: self.epoch,
            session_generation=lambda: self.session_generation,
            switching=lambda: self.switching,
        )


def test_a_run_s_token_is_the_context_session_and_run_it_started_under() -> None:
    cluster = Cluster()
    runs = cluster.tracker()

    assert runs.next_run() == RunToken(3, 7, 1)
    assert runs.next_run() == RunToken(3, 7, 2)


def test_a_result_of_the_current_run_is_applied() -> None:
    runs = Cluster().tracker()
    token = runs.next_run()
    applied: list[str] = []

    assert runs.apply_if_current(token, lambda: applied.append("result"))
    assert applied == ["result"]


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda cluster, runs: setattr(cluster, "epoch", 4), id="context-switch"),
        pytest.param(
            lambda cluster, runs: setattr(cluster, "switching", True), id="switch-under-way"
        ),
        pytest.param(
            lambda cluster, runs: setattr(cluster, "session_generation", 8),
            id="credential-replacement",
        ),
        pytest.param(lambda cluster, runs: runs.next_run(), id="newer-run-or-selection"),
    ],
)
def test_a_result_started_before_a_change_is_dropped(change: Any) -> None:
    cluster = Cluster()
    runs = cluster.tracker()
    token = runs.next_run()
    applied: list[str] = []

    change(cluster, runs)

    assert not runs.is_current(token)
    assert not runs.apply_if_current(token, lambda: applied.append("late"))
    assert applied == []


def test_a_result_with_no_token_is_never_applied() -> None:
    runs = Cluster().tracker()
    runs.next_run()
    applied: list[str] = []

    assert not runs.apply_if_current(None, lambda: applied.append("untracked"))
    assert applied == []


def test_no_result_is_current_before_a_run_starts() -> None:
    runs = Cluster().tracker()

    assert not runs.is_current(RunToken(3, 7, 0))


@pytest.fixture
def offline_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    class Api:
        def __init__(self, configuration: Any) -> None:
            self.configuration = configuration

        async def close(self) -> None:
            return None

    monkeypatch.setattr(client_module, "load_refreshable_kube_config", AsyncMock())
    monkeypatch.setattr(k8s_client, "ApiClient", Api)
    monkeypatch.setattr(k8s_client, "CoreV1Api", lambda api: api)
    monkeypatch.setattr(k8s_client.Configuration, "set_default", lambda configuration: None)


@pytest.mark.usefixtures("offline_kubeconfig")
async def test_reloading_the_kubeconfig_drops_a_result_read_before_it() -> None:
    kube = KubeClient()
    await kube.connect("ctx-a")
    runs = RunTracker(epoch=lambda: 0, session_generation=lambda: kube.session_generation)
    token = runs.next_run()

    await kube.switch_context(None)

    assert not runs.apply_if_current(token, lambda: None)
