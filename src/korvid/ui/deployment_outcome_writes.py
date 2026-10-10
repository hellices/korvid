"""Deployment-only observer attachment for generic workload writes."""

from __future__ import annotations

from typing import Protocol

from korvid.k8s.discovery import ResourceMeta
from korvid.ui.deployment_outcome_controller import DeploymentOutcomeController
from korvid.ui.write_gate import AcceptedWriteObserver


class OutcomeWriteTarget(Protocol):
    """Captured fields needed to bind an accepted write to one Deployment."""

    @property
    def meta(self) -> ResourceMeta: ...

    @property
    def epoch(self) -> int: ...

    @property
    def namespace(self) -> str | None: ...

    @property
    def name(self) -> str: ...

    @property
    def uid(self) -> str | None: ...


def restart_observer(
    controller: DeploymentOutcomeController,
    target: OutcomeWriteTarget,
    restarted_at: str,
) -> AcceptedWriteObserver | None:
    """Attach observation only to an apps/Deployment restart."""

    if (target.meta.group, target.meta.plural) != ("apps", "deployments"):
        return None
    return controller.restart_observer(
        epoch=target.epoch,
        namespace=target.namespace,
        name=target.name,
        uid=target.uid,
        restarted_at=restarted_at,
    )


def scale_observer(
    controller: DeploymentOutcomeController,
    target: OutcomeWriteTarget,
    replicas: int,
) -> AcceptedWriteObserver | None:
    """Attach observation only to an apps/Deployment scale."""

    if (target.meta.group, target.meta.plural) != ("apps", "deployments"):
        return None
    return controller.scale_observer(
        epoch=target.epoch,
        namespace=target.namespace,
        name=target.name,
        uid=target.uid,
        replicas=replicas,
    )
