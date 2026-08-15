"""Security invariants around the advisory impact preview (issue #283).

The preview adds text to an existing dialog. It must not become a new way to
approve, execute, reserve, or unblock a cluster write, and a graph failure
must not take a legitimate confirmation away from the user. Every test here
drives the real `Ctrl-D` / `r` flow through the Task 4 harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from textual import events

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    TargetReference,
)
from korvid.ui.widgets.confirm_screen import ConfirmScreen

from .test_impact_flow import CATALOG_ALIASES, ImpactEnv, impact_text, open_delete_dialog, to_view
from .waits import until


def _markup_replicaset() -> GenericSummary:
    """A ReplicaSet whose name is Rich markup: cluster-controlled text must
    never be interpreted as styling in an approval dialog."""
    return GenericSummary(
        name="[bold red]web-abc[/]",
        namespace="prod",
        kind="ReplicaSet",
        created="",
        uid="rs-1",
        relationships=RelationshipFacts(
            api_group="apps",
            references=(
                ReferenceFact(
                    relation=RelationKind.OWNED_BY,
                    target=TargetReference(
                        group="apps", kind="Deployment", namespace="prod", name="web", uid="d-1"
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="metadata.ownerReferences[0]",
                ),
            ),
        ),
    )


def _secret_row() -> GenericSummary:
    """A Secret summary carries identity only - never `data`/`stringData`."""
    return GenericSummary(name="db", namespace="prod", kind="Secret", created="", uid="secret-1")


def _pod_using_secret() -> PodSummary:
    return PodSummary(
        name="web-abc-1",
        namespace="prod",
        phase="Running",
        ready="1/1",
        restarts=0,
        node=None,
        uid="pod-1",
        relationships=RelationshipFacts(
            references=(
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference(group="", kind="Secret", namespace="prod", name="db"),
                    confidence=FactConfidence.DECLARED,
                    field="spec.volumes[0].secret.secretName",
                ),
            )
        ),
    )


async def test_declined_delete_with_an_impact_section_runs_no_operation(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "known direct dependents" in impact_text(env.app)
        await pilot.press("n")
        await pilot.pause()
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_keystroke_buffered_during_the_impact_load_cannot_approve(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        stale = events.Key("y", "y")  # created before the dialog existed
        await pilot.press("ctrl+d")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="confirm dialog"
        )
        env.app.screen.post_message(stale)
        await pilot.pause()
        assert env.ops.calls == []
        assert not audit_path.exists()
        await pilot.press("y")
        await until(pilot, lambda: env.ops.calls, label="approved delete ran")
        assert env.ops.calls == [("delete", "deployments", "prod", "web", "deploy-1")]


async def test_the_impact_load_never_writes_reserves_or_audits(tmp_path: Path) -> None:
    """Loading a snapshot is a read: it must take no write reservation (which
    would block `:ctx`), run no operation, and write no audit record.

    Sampled *during* the first LIST (via `on_first_call`), not only after the
    dialog has opened: a reservation taken and released before the dialog
    appears would pass a post-load-only assertion while still blocking `:ctx`
    for the moment it mattered.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    reservations_during_load: list[int] = []
    env.lister.on_first_call = lambda: reservations_during_load.append(
        env.app._active_cluster_writes
    )
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert env.lister.calls != []
        assert reservations_during_load == [0]
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_audit_failure_still_blocks_the_operation_factory(tmp_path: Path) -> None:
    """Fail-closed auditing is unchanged: an unwritable audit log blocks the
    write even though the dialog showed an impact summary.

    Waits on the app's own blocked notification rather than on a fixed
    sleep: an empty `env.ops.calls` after an arbitrary pause is also what a
    write that simply had not started yet looks like, so the assertion has
    to be anchored to the fail-closed outcome actually being reached. The
    error toast is emitted on the audit-failure branch only (a successful or
    a failed mutation produces different text), and the write reservation is
    released when the app-owned worker returns - so once both are observed,
    an empty `calls` means the operation factory was never invoked.
    """
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # a directory at the log path makes appends fail
    env = ImpactEnv(audit_path)
    blocked = "delete deployments/web blocked: audit log unavailable"
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "known direct dependents" in impact_text(env.app)
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(str(note.message) == blocked for note in env.app._notifications),
            label="fail-closed block notification",
        )
        await until(
            pilot,
            lambda: env.app._active_cluster_writes == 0,
            label="write worker finished",
        )
        assert [
            note.severity for note in env.app._notifications if str(note.message) == blocked
        ] == ["error"]
        assert env.ops.calls == []
        assert list(audit_path.iterdir()) == []  # the log path is still the empty directory


async def test_graph_failure_does_not_block_a_legitimate_confirmation(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    env.lister.errors["deployments"] = RuntimeError("parser exploded")
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "impact unavailable; approval remains available" in text
        # The lister's exception message must never reach the dialog: it can
        # embed cluster-controlled or sensitive text (issue #283's fail-open
        # path renders only the static IMPACT_UNAVAILABLE_LINES).
        assert "parser exploded" not in text
        await pilot.press("y")
        await until(
            pilot,
            lambda: audit_path.exists() and "success" in audit_path.read_text(),
            label="write audited",
        )
        assert env.ops.calls == [("delete", "deployments", "prod", "web", "deploy-1")]
        entries = [json.loads(line) for line in audit_path.read_text().splitlines()]
        assert entries[0]["outcome"] == "intent"
        assert entries[-1]["outcome"] == "success"


async def test_rich_markup_in_a_resource_name_renders_literally(tmp_path: Path) -> None:
    rows: dict[str, list[Any]] = {
        "deployments": [
            GenericSummary(
                name="web", namespace="prod", kind="Deployment", created="", desired=1, uid="d-1"
            )
        ],
        "replicasets": [_markup_replicaset()],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "apps/ReplicaSet/prod/[bold red]web-abc[/]" in impact_text(env.app)


async def test_impact_preview_works_with_the_agent_disabled(tmp_path: Path) -> None:
    """No LLM, no provider: the summary is a deterministic graph query."""
    env = ImpactEnv(tmp_path / "audit.jsonl")
    async with env.app.run_test() as pilot:
        assert env.app.config.agent_enabled is False
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert "known direct dependents (may be affected): 1" in impact_text(env.app)


async def test_no_secret_value_or_manifest_content_reaches_the_dialog(tmp_path: Path) -> None:
    rows: dict[str, list[Any]] = {
        "secrets": [_secret_row()],
        "pods": [_pod_using_secret()],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "secrets", expect="db")
        text = impact_text(env.app)
        assert "delete Secret/prod/db" in text
        assert (
            "Pod/prod/web-abc-1 via uses_config (declared) at spec.volumes[0].secret.secretName"
            in text
        )
        for leak in ("stringData", "data:", "apiVersion", "kind: Secret"):
            assert leak not in text


async def test_rollout_restart_declined_with_an_impact_section_runs_no_operation(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("r")
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="restart dialog"
        )
        assert "rollout restart apps/Deployment/prod/web" in impact_text(env.app)
        await pilot.press("n")
        await pilot.pause()
        assert env.ops.calls == []
        assert not audit_path.exists()


def test_the_catalog_aliases_resolve_every_kind_the_integrated_flows_exercise() -> None:
    """A guard on the harness itself: every write-flow view name this module
    and `test_impact_flow` drive through `open_delete_dialog`/`to_view`
    (`deploy`, `pods`, `secrets`, `nodes`) resolves to a real discovered
    kind, not a synthetic view."""
    exercised = {
        "deployments": ("Deployment", "apps"),
        "pods": ("Pod", ""),
        "secrets": ("Secret", ""),
        "nodes": ("Node", ""),
    }
    for plural, (kind, group) in exercised.items():
        meta = CATALOG_ALIASES[plural]
        assert isinstance(meta, ResourceMeta)
        assert meta.kind == kind
        assert meta.group == group
        assert meta.synthetic is False
