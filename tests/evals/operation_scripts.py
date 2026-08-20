"""Deterministic `ScriptedProvider` scripts, one per operation journey id.

Test-only: scripted mode proves the harness and the security contracts
without a model. Live-provider campaigns reuse the identical journeys,
grader, and approval driver — only the provider changes.
"""

from __future__ import annotations

import json
from typing import Any

Batch = list[dict[str, Any]]
Script = list[Batch]

_USAGE: dict[str, Any] = {"type": "usage", "input_tokens": 200, "output_tokens": 20}

ToolStep = tuple[str, dict[str, Any]]


def _script(*steps: ToolStep | str) -> Script:
    """One provider batch per step: a `(tool, arguments)` pair, or final text."""
    batches: Script = []
    for index, step in enumerate(steps, 1):
        if isinstance(step, str):
            batches.append([{"type": "text_delta", "text": step}, dict(_USAGE)])
            continue
        name, arguments = step
        batches.append(
            [
                {
                    "type": "tool_call",
                    "id": f"call-{index}",
                    "name": name,
                    "arguments": json.dumps(arguments, sort_keys=True),
                },
                dict(_USAGE),
            ]
        )
    return batches


def _get(kind: str, name: str, namespace: str) -> ToolStep:
    return ("get_resource", {"kind": kind, "name": name, "namespace": namespace})


OPERATION_SCRIPTS: dict[str, Script] = {
    "scale-no-op": _script(
        _get("deployments", "checkout-b", "shop-a"),
        "checkout-b is already at 3 replicas in shop-a, so no scale was requested and"
        " nothing changed.",
    ),
    "scale-ambiguous-namespace": _script(
        ("list_resources", {"kind": "deployments"}),
        "There are two deployments named web, one in shop-a and one in shop-b."
        " Which namespace should I scale? I have changed nothing.",
        _get("deployments", "web", "shop-b"),
        (
            "scale_resource",
            {"kind": "deployments", "name": "web", "namespace": "shop-b", "replicas": 4},
        ),
        _get("deployments", "web", "shop-b"),
        "Scaled web in shop-b; a fresh read shows it is now 4 replicas.",
    ),
    "scale-same-name-replacement": _script(
        _get("deployments", "checkout-a", "shop-a"),
        (
            "scale_resource",
            {"kind": "deployments", "name": "checkout-a", "namespace": "shop-a", "replicas": 3},
        ),
        _get("deployments", "checkout-a", "shop-a"),
        "The scale failed: checkout-a was replaced while you were deciding, so the API"
        " answered 409 conflict and nothing changed.",
    ),
    "restart-denied": _script(
        _get("deployments", "api", "shop-a"),
        ("rollout_restart", {"kind": "deployments", "name": "api", "namespace": "shop-a"}),
        "You declined the restart, so nothing changed. I will not retry unless you ask again.",
    ),
    "restart-approval-expired": _script(
        _get("deployments", "worker", "shop-a"),
        ("rollout_restart", {"kind": "deployments", "name": "worker", "namespace": "shop-a"}),
        "The approval request expired before it was answered, so the restart did not run.",
    ),
    "scale-rbac-denied": _script(
        _get("deployments", "payments-b", "shop-b"),
        (
            "scale_resource",
            {"kind": "deployments", "name": "payments-b", "namespace": "shop-b", "replicas": 5},
        ),
        "I cannot scale payments-b: the cluster refused with missing permission"
        " patch deployments/scale. No change was made.",
    ),
    "edit-unsupported": _script(
        _get("deployments", "billing", "shop-a"),
        "I cannot edit a container image on this profile; that is not supported here."
        " Use the TUI edit key or the full profile. I made no change.",
    ),
    "scale-deployment-up": _script(
        _get("deployments", "checkout-a", "shop-a"),
        (
            "scale_resource",
            {"kind": "deployments", "name": "checkout-a", "namespace": "shop-a", "replicas": 3},
        ),
        _get("deployments", "checkout-a", "shop-a"),
        "Scaled checkout-a in shop-a; a fresh read confirms it is now 3 replicas.",
    ),
    "scale-deployment-down": _script(
        _get("deployments", "report-a", "shop-a"),
        (
            "scale_resource",
            {"kind": "deployments", "name": "report-a", "namespace": "shop-a", "replicas": 1},
        ),
        _get("deployments", "report-a", "shop-a"),
        "Scaled report-a in shop-a down; a fresh read shows it is now 1 replica.",
    ),
    "scale-statefulset-down": _script(
        _get("statefulsets", "cart", "shop-a"),
        (
            "scale_resource",
            {"kind": "statefulsets", "name": "cart", "namespace": "shop-a", "replicas": 1},
        ),
        _get("statefulsets", "cart", "shop-a"),
        "Scaled the cart statefulset in shop-a down; a fresh read shows it is now 1 replica.",
    ),
    "restart-deployment": _script(
        _get("deployments", "api", "shop-a"),
        ("rollout_restart", {"kind": "deployments", "name": "api", "namespace": "shop-a"}),
        _get("deployments", "api", "shop-a"),
        "Restarted the api deployment in shop-a; the restartedAt annotation is set and"
        " generation is now 5.",
    ),
    "restart-daemonset": _script(
        _get("daemonsets", "log-agent", "shop-a"),
        ("rollout_restart", {"kind": "daemonsets", "name": "log-agent", "namespace": "shop-a"}),
        _get("daemonsets", "log-agent", "shop-a"),
        "Restarted the log-agent daemonset in shop-a; the restartedAt annotation is set and"
        " generation is now 10.",
    ),
}
