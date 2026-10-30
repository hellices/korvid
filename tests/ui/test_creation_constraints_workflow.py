"""`:blocked` end to end: a Deployment row, its recorded failures, and their ReplicaSet (#411)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import PODS_META, ResourceMeta, build_alias_map
from korvid.k8s.discovery_snapshot import (
    GroupVersionOutcome,
    RawDiscovery,
    RootOutcome,
    ServedResource,
)
from korvid.k8s.evidence_requests import EvidenceClient, EvidencePage, EvidenceTarget
from korvid.k8s.models import GenericSummary
from korvid.ui.widgets.creation_constraints_screen import CreationConstraintsScreen
from tests.app_factory import build_test_app
from tests.ui.waits import screen_ready, until

DEPLOYMENTS = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
REPLICA_SETS = ResourceMeta("ReplicaSet", "replicasets", "apps", "v1", True)
EXCEEDED = 'pods "api-7d9f-x" is forbidden: exceeded quota: compute, requested: pods=1'
_SERVED = {
    ("", "v1"): ("resourcequotas", "limitranges", "events"),
    ("apps", "v1"): ("deployments", "replicasets"),
}


class Client(EvidenceClient):
    """Serves one Deployment whose ReplicaSet records a quota rejection."""

    def __init__(self) -> None:
        self.reads: list[tuple[str, str, str]] = []

    @property
    def session_generation(self) -> int:
        return 1

    async def discovery_snapshot(self) -> RawDiscovery:
        outcomes = tuple(
            GroupVersionOutcome(
                group, version, tuple(ServedResource(r, frozenset({"get", "list"})) for r in names)
            )
            for (group, version), names in _SERVED.items()
        )
        return RawDiscovery(
            1,
            datetime.now(UTC),
            0.0,
            RootOutcome("/api", (("", "v1"),)),
            RootOutcome("/apis", (("apps", "v1"),)),
            outcomes,
        )

    async def list_evidence_page(
        self, target: EvidenceTarget, *, limit: int, continuation: str, max_bytes: int
    ) -> EvidencePage:
        self.reads.append(("list", target.resource, target.namespace or ""))
        if target.resource == "replicasets":
            owner = [{"uid": "uid-api", "controller": True}]
            rs = {
                "metadata": {
                    "name": "api-7d9f",
                    "namespace": "default",
                    "uid": "uid-rs",
                    "ownerReferences": owner,
                }
            }
            return EvidencePage((rs,))
        if target.resource == "events":
            event = {
                "metadata": {"name": "e", "namespace": "default", "uid": "uid-e"},
                "reason": "FailedCreate",
                "involvedObject": {"kind": "ReplicaSet", "name": "api-7d9f", "uid": "uid-rs"},
                "message": EXCEEDED,
            }
            return EvidencePage((event,))
        if target.resource == "resourcequotas":
            quota = {
                "metadata": {"name": "compute", "namespace": "default", "uid": "uid-q"},
                "spec": {"hard": {"pods": "0"}},
                "status": {"hard": {"pods": "0"}, "used": {"pods": "0"}},
            }
            return EvidencePage((quota,))
        return EvidencePage(())

    async def get_evidence_object(
        self, target: EvidenceTarget, name: str, *, max_bytes: int
    ) -> dict[str, Any]:
        self.reads.append(("get", target.resource, name))
        template = {
            "spec": {
                "containers": [{"name": "app", "env": [{"name": "T", "value": "s3cr3t-literal"}]}]
            }
        }
        return {
            "metadata": {"name": name, "namespace": "default", "uid": "uid-api"},
            "spec": {"replicas": 1, "template": template},
            "status": {},
        }


async def _source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
    if kind == "deployments":
        yield ("ADDED", GenericSummary("api", "default", "Deployment", "", uid="uid-api"))
    if kind == "replicasets":
        yield ("ADDED", GenericSummary("api-7d9f", "default", "ReplicaSet", "", uid="uid-rs"))
    await asyncio.Event().wait()


async def _command(pilot: Any, text: str) -> None:
    await pilot.press("colon", *text, "enter")


def _app(client: EvidenceClient) -> Any:
    store = ResourceStore()
    return build_test_app(
        config=KorvidConfig(namespace="default"),
        store=store,
        watch_manager=WatchManager(store, _source),
        aliases=build_alias_map([PODS_META, DEPLOYMENTS, REPLICA_SETS]),
        agent_available=False,
        evidence_client=client,
    )


async def test_a_deployments_failures_lead_to_their_replica_set() -> None:
    client = Client()
    app = _app(client)
    async with app.run_test() as pilot:
        await _command(pilot, "deployments")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "api"),
            label="Deployment selected",
        )

        await _command(pilot, "blocked")
        await until(pilot, lambda: screen_ready(app, CreationConstraintsScreen), label="view open")
        screen = app.screen
        assert isinstance(screen, CreationConstraintsScreen)
        (failure,) = screen.report.failures
        assert failure.rejection.quotas == ("compute",)
        assert "s3cr3t-literal" not in repr(screen.report)
        assert sorted(client.reads) == [
            ("get", "deployments", "api"),
            ("list", "events", "default"),
            ("list", "limitranges", "default"),
            ("list", "replicasets", "default"),
            ("list", "resourcequotas", "default"),
        ]

        await pilot.press("enter")
        await until(pilot, lambda: app.current_kind == "replicasets", label="ReplicaSet view")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "api-7d9f"),
            label="ReplicaSet selected",
        )


async def test_blocked_on_another_view_says_what_to_open() -> None:
    app = _app(Client())
    async with app.run_test() as pilot:
        await _command(pilot, "blocked")
        await until(
            pilot,
            lambda: any("Open Deployments, ReplicaSets" in n.message for n in app._notifications),
            label="reason shown",
        )
        assert not isinstance(app.screen, CreationConstraintsScreen)


class BrokenClient(Client):
    async def discovery_snapshot(self) -> RawDiscovery:
        raise RuntimeError("discovery exploded")


async def test_a_failed_run_is_reported_instead_of_vanishing() -> None:
    app = _app(BrokenClient())
    async with app.run_test() as pilot:
        await _command(pilot, "deployments")
        await until(
            pilot,
            lambda: app._view.selected_ns_name(notify=False) == ("default", "api"),
            label="Deployment selected",
        )
        await _command(pilot, "blocked")
        await until(
            pilot,
            lambda: any(
                "Workload creation failure explanation failed" in n.message
                and "discovery exploded" in n.message
                for n in app._notifications
            ),
            label="failure reported",
        )
        assert app.is_running
