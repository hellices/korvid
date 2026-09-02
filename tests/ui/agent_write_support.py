"""Shared support for agent write UI tests."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from korvid.agent.session import AgentSession
from korvid.core.audit import AuditLog
from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore, Summary
from korvid.core.watch import WatchManager
from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary
from korvid.k8s.writes import WriteOps
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.agent_panel import AgentPanel

_DEPLOY_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True, ("deploy",))
_ALIASES = {"deployments": _DEPLOY_META, "deploy": _DEPLOY_META}


def _expand_panel(app: KorvidApp) -> None:
    # Approval dialogs only surface while the panel is expanded (spec 6.1);
    # tests that reach the dialog must open the panel first.
    app.query_one(AgentPanel).display = True


class Recorder(WriteOps):
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.uids: list[str | None] = []

    async def delete_object(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.uids.append(uid)
        self.calls.append(("delete", meta.plural, namespace, name))

    async def scale_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        replicas: int,
        *,
        uid: str | None = None,
    ) -> None:
        self.uids.append(uid)
        self.calls.append(("scale", meta.plural, namespace, name, replicas))

    async def rollout_restart(
        self, meta: ResourceMeta, namespace: str | None, name: str, *, uid: str | None = None
    ) -> None:
        self.uids.append(uid)
        self.calls.append(("restart", meta.plural, namespace, name))

    async def replace_object(
        self,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        manifest: dict[str, Any],
        *,
        uid: str | None = None,
    ) -> None:
        self.uids.append(uid)
        self.calls.append(("replace", meta.plural, namespace, name, manifest))


def make_app(
    recorder: Recorder,
    audit_path: Path,
    *,
    readonly: bool = False,
    permitted: bool | None = None,
    get_manifest: Callable[[str, str | None, str], Awaitable[dict[str, Any]]] | None = None,
    approval_timeout_seconds: float | None = None,
    agent_session: AgentSession | None = None,
    agent_model_name: str | None = None,
) -> KorvidApp:
    store = ResourceStore()
    deploys = [GenericSummary(name="web", namespace="default", kind="Deployment", created="")]

    async def source(kind: str, scope: str) -> AsyncIterator[tuple[str, Summary]]:
        for obj in deploys if kind == "deployments" else []:
            yield ("ADDED", obj)
        while True:
            await asyncio.sleep(0.01)

    async def check_permission(
        verb: str, resource: str, sub: str, ns: str | None, group: str, name: str
    ) -> bool:
        assert permitted is not None
        return permitted

    async def default_manifest(kind: str, namespace: str | None, name: str) -> dict[str, Any]:
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": "deploy-uid-1",
                "resourceVersion": "1",
            },
        }

    return KorvidApp(
        config=KorvidConfig(namespace="default", readonly=readonly),
        store=store,
        watch_manager=WatchManager(store, source),
        aliases=dict(_ALIASES),
        get_manifest=get_manifest or default_manifest,
        agent_session=agent_session,
        agent_model_name=agent_model_name,
        write_ops=recorder,
        audit=AuditLog(audit_path),
        check_permission=None if permitted is None else check_permission,
        approval_timeout_seconds=approval_timeout_seconds,
    )
