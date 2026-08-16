"""Security invariants around the advisory impact preview (issues #283, #295).

The preview adds text to an existing dialog. It must not become a new way to
approve, execute, reserve, or unblock a cluster write, and a graph failure
must not take a legitimate confirmation away from the user. Every test here
drives the real `Ctrl-D` / `r` / `S` flow through the Task 4 harness.

The scale-down flow (#295) is the one that reaches the snapshot from a
*modal callback* rather than from the binding coroutine: `S` opens the
replica prompt and the confirmation runs on an app-owned worker started by
that prompt's result callback. So its security tests cancel the worker the
real callback created instead of a task a test made up - a cancellation
path the delete tests cannot exercise.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from textual import events
from textual.worker import Worker, WorkerState

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.models import GenericSummary, PodSummary
from korvid.k8s.relationship_facts import (
    FactConfidence,
    ReferenceFact,
    RelationKind,
    RelationshipFacts,
    TargetReference,
)
from korvid.ui.app import KorvidApp
from korvid.ui.widgets.confirm_screen import ConfirmScreen, ReplicasPrompt

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


def _plain_replicaset() -> GenericSummary:
    """A ReplicaSet owned by `prod/web`, so the Pod below is inside the
    affected set of a `web` delete."""
    return GenericSummary(
        name="web-abc",
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


def _pod_with_a_bidi_dangling_reference() -> PodSummary:
    """A Pod in the affected set whose dangling ConfigMap reference carries
    bidi overrides and isolates in both the target name and the evidence
    field path.

    U+202E (RIGHT-TO-LEFT OVERRIDE) and U+2066..U+2069 (directional
    isolates) reorder everything after them when a terminal renders the
    line: unflattened, a cluster could make `.../delete-me` read as
    `.../em-eteled`, or make an evidence path appear to point at a field it
    does not. An approval dialog is exactly where that must not happen.
    """
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
                    relation=RelationKind.OWNED_BY,
                    target=TargetReference(
                        group="apps",
                        kind="ReplicaSet",
                        namespace="prod",
                        name="web-abc",
                        uid="rs-1",
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="metadata.ownerReferences[0]",
                ),
                ReferenceFact(
                    relation=RelationKind.USES_CONFIG,
                    target=TargetReference(
                        group="",
                        kind="ConfigMap",
                        namespace="prod",
                        name="app\u202econfig\u2066rogue\u2069",
                    ),
                    confidence=FactConfidence.DECLARED,
                    field="spec.volumes[0]\u0085.configMap\u202e",
                ),
            )
        ),
    )


async def test_bidi_controls_in_a_dangling_reference_never_reach_the_dialog(
    tmp_path: Path,
) -> None:
    """A dangling reference is cluster-controlled text on its most exposed
    path: name, resolution and evidence field all reach the dialog. No
    Unicode control or format character may survive the render - the line
    must read left to right exactly as it was composed."""
    rows: dict[str, list[Any]] = {
        "deployments": [
            GenericSummary(
                name="web", namespace="prod", kind="Deployment", created="", desired=1, uid="d-1"
            )
        ],
        "replicasets": [_plain_replicaset()],
        "pods": [_pod_with_a_bidi_dangling_reference()],
    }
    env = ImpactEnv(tmp_path / "audit.jsonl", rows=rows)
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        text = impact_text(env.app)
        assert "unresolved references in the affected set: 1" in text
        assert not any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in text.replace("\n", ""))
        # Flattened, not dropped: the identity and the field path still read
        # as bounded fragments rather than silently losing characters.
        assert "ConfigMap/prod/app config rogue  (missing)" in text
        assert "spec.volumes[0] .configMap" in text


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


async def test_a_uid_less_row_still_confirms_and_writes_with_no_snapshot_read(
    tmp_path: Path,
) -> None:
    """Omitting the section must cost the user nothing but the section.

    A row whose summary type carries no uid gets no impact preview (there is
    no identity to match a snapshot node against, and korvid never
    name-resolves one), but the approval gate, the keystroke confirmation
    and the write itself are untouched - and the snapshot is never loaded,
    so the omission also costs no LIST fan-out.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(
        audit_path,
        rows={
            "deployments": [
                GenericSummary(
                    name="web", namespace="prod", kind="Deployment", created="", desired=1, uid=""
                )
            ],
            "replicasets": [_plain_replicaset()],
        },
    )
    async with env.app.run_test() as pilot:
        await open_delete_dialog(env, pilot, "deploy", expect="web")
        assert not env.app.screen.query(".confirm-impact")
        assert env.ops.calls == []
        await pilot.press("y")
        await until(pilot, lambda: env.ops.calls, label="approved uid-less delete ran")
        assert env.ops.calls == [("delete", "deployments", "prod", "web", None)]
        assert env.lister.calls == []
        assert audit_path.exists()


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
            "Pod/prod/web-abc-1 via uses_config (declared) at"
            " Pod/prod/web-abc-1: spec.volumes[0].secret.secretName" in text
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


async def test_cancelling_the_delete_flow_during_the_impact_load_writes_nothing(
    tmp_path: Path,
) -> None:
    """A cancelled snapshot cancels the *write flow*, it does not open a dialog.

    Cancellation here is not hypothetical: `:ctx` tears the API client down
    under whatever is awaiting it, and the impact load is an awaited read
    inside the delete flow like any other. `_impact_preview` re-raises
    `asyncio.CancelledError` instead of folding it into the fail-open
    "impact unavailable" advisory, and this pins what that means end to end
    - the cancellation reaches the caller, so the flow never runs on to push
    a confirmation describing a snapshot it never got, and the whole
    approval path stops where it was: no dialog, no keystroke gate to
    answer, no operation, no write reservation (which would block `:ctx`
    itself), no audit record.

    Distinct from the timeout case, which is a *bounded* failure the user
    keeps their approval through: there the dialog opens with the static
    advisory. Here nothing survives the cancellation at all.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    #: Blocks every LIST until the flow is cancelled; far beyond
    #: `_IMPACT_TIMEOUT`, so a timeout could not reach the dialog first and
    #: quietly turn this into the already-covered fail-open case.
    env.lister.delay = 60.0
    reservations_during_load: list[int] = []
    env.lister.on_first_call = lambda: reservations_during_load.append(
        env.app._active_cluster_writes
    )
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        flow = asyncio.create_task(env.app.action_delete_resource())
        await until(pilot, lambda: env.lister.calls != [], label="impact snapshot listing")
        flow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flow
        # `cancelled()` is the propagation assertion: a flow that swallowed
        # the cancellation and returned would complete normally instead.
        assert flow.cancelled()
        await pilot.pause()
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert len(env.app.screen_stack) == 1
        assert reservations_during_load == [0]
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_a_cancelled_snapshot_read_is_not_downgraded_to_the_unavailable_advisory(
    tmp_path: Path,
) -> None:
    """The same invariant from the other side: the LIST itself is cancelled.

    A client closed mid-read raises `asyncio.CancelledError` out of the
    lister rather than an API error. The fail-open branch must not treat it
    as one: converting it to the static advisory would resurrect a write
    flow whose cluster connection is already gone, and would open an
    approval dialog for a target nothing re-validated.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    env.lister.errors["deployments"] = asyncio.CancelledError()
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        with pytest.raises(asyncio.CancelledError):
            await env.app.action_delete_resource()
        await pilot.pause()
        assert env.lister.calls != []
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert len(env.app.screen_stack) == 1
        assert not env.app.screen.query(".confirm-impact")
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()


async def _open_scale_down(pilot: Any, replicas: int = 1) -> None:
    """Drive the real `S` keybinding down to the replica count.

    Deliberately not a `_confirm_scale` call: the scale flow's approval path
    runs on a worker started from the replica prompt's *result callback*, and
    a test that invoked the coroutine directly would bypass exactly the hop
    these tests are here to constrain. The default row (`prod/web`, three
    desired replicas) makes one replica a known decrease, so the snapshot is
    loaded and the impact section is attached.
    """
    await to_view(pilot, "deploy", expect="web")
    await pilot.press("S")
    await until(
        pilot,
        lambda: isinstance(pilot.app.screen, ReplicasPrompt),
        label="replicas prompt",
    )
    for char in str(replicas):
        await pilot.press(char)
    await pilot.press("enter")


@contextlib.contextmanager
def _recording_workers(app: KorvidApp, started: list[Worker[Any]]) -> Iterator[None]:
    """Record every worker the app starts while the block is open.

    The scale flow hands `_confirm_scale` to `run_worker` from a screen
    callback, so the only handle on that coroutine is the `Worker` object
    the app itself created; a test cannot cancel it without capturing it
    here.

    Kept open only until that one worker has been recorded: everything the
    app does afterwards (a refresh, a notification timer) then runs through
    its real `run_worker`, so nothing outside the flow under test can end up
    in `started`.
    """
    original_run_worker = app.run_worker

    def record_worker(work: Any, *args: Any, **kwargs: Any) -> Worker[Any]:
        worker: Worker[Any] = original_run_worker(work, *args, **kwargs)
        started.append(worker)
        return worker

    with mock.patch.object(app, "run_worker", side_effect=record_worker):
        yield


async def _scale_worker(pilot: Any, app: KorvidApp) -> Worker[Any]:
    """Drive `S` to the replica count and return the worker it started.

    The patch is released as soon as the confirmation worker exists, which
    is well before anything the tests below await on it - the wait for the
    first LIST, the cancellation, the worker's final state - so narrowing it
    costs no determinism.
    """
    started: list[Worker[Any]] = []
    with _recording_workers(app, started):
        await _open_scale_down(pilot)
        await until(pilot, lambda: len(started) == 1, label="scale confirmation worker started")
    return started[0]


async def test_declined_scale_down_with_impact_runs_no_operation(tmp_path: Path) -> None:
    """Declining is still a decline: the impact section is text on the
    dialog, never a second path to the operation factory."""
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    async with env.app.run_test() as pilot:
        await _open_scale_down(pilot)
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="scale-down confirm"
        )
        # The scale-down summary itself, not merely "some impact section":
        # a dialog carrying a delete's or another row's summary would
        # satisfy the selector and prove nothing about this flow.
        assert "scale down apps/Deployment/prod/web" in impact_text(env.app)
        await pilot.press("n")
        await until(pilot, lambda: len(env.app.screen_stack) == 1, label="dialog dismissed")
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_scale_down_audit_failure_blocks_operation_factory(
    tmp_path: Path,
) -> None:
    """Fail-closed auditing is unchanged by the new section: an unwritable
    audit log blocks the scale even though the dialog showed an impact
    summary and the user approved it.

    Anchored to the app's own blocked notification and to the released write
    reservation rather than to a fixed pause: an empty `calls` after an
    arbitrary sleep is also what a write that had not started yet looks
    like. The toast text is emitted on the audit-failure branch only.
    """
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()  # a directory at the log path makes appends fail
    env = ImpactEnv(audit_path)
    blocked = "scale deployments/web blocked: audit log unavailable"
    async with env.app.run_test() as pilot:
        await _open_scale_down(pilot)
        await until(
            pilot, lambda: isinstance(env.app.screen, ConfirmScreen), label="scale-down confirm"
        )
        assert "scale down apps/Deployment/prod/web" in impact_text(env.app)
        await pilot.press("y")
        await until(
            pilot,
            lambda: any(str(note.message) == blocked for note in env.app._notifications),
            label="scale-down audit refusal",
        )
        await until(
            pilot,
            lambda: env.app._active_cluster_writes == 0,
            label="scale-down write worker finished",
        )
        assert [
            note.severity for note in env.app._notifications if str(note.message) == blocked
        ] == ["error"]
        assert env.ops.calls == []
        assert list(audit_path.iterdir()) == []  # still the empty directory


async def test_cancelling_scale_down_during_impact_load_writes_nothing(
    tmp_path: Path,
) -> None:
    """Cancelling the worker mid-snapshot cancels the *approval flow*.

    `:ctx` tears the API client down under whatever is awaiting it, and the
    scale-down snapshot is an awaited read inside a worker the app owns. The
    cancellation must reach the caller, so the flow never runs on to push a
    confirmation describing a snapshot it never got: no dialog, no keystroke
    gate to answer, no operation, no write reservation (which would block
    `:ctx` itself), no audit record.

    The reservation is sampled *during* the first LIST, not only afterwards:
    one taken and released before the dialog would pass a post-hoc check
    while still blocking `:ctx` for the moment it mattered.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    #: Blocks every LIST until the worker is cancelled; far beyond
    #: `_IMPACT_TIMEOUT`, so a timeout could not reach the dialog first and
    #: quietly turn this into the already-covered fail-open case.
    env.lister.delay = 60.0
    reservations_during_load: list[int] = []
    env.lister.on_first_call = lambda: reservations_during_load.append(
        env.app._active_cluster_writes
    )
    async with env.app.run_test() as pilot:
        worker = await _scale_worker(pilot, env.app)
        await until(pilot, lambda: env.lister.calls != [], label="scale impact listing")
        worker.cancel()
        await until(
            pilot,
            lambda: worker.is_cancelled and worker.is_finished,
            label="scale worker cancelled",
        )
        assert worker.state is WorkerState.CANCELLED
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert len(env.app.screen_stack) == 1
        assert reservations_during_load == [0]
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_cancelled_scale_snapshot_is_not_an_unavailable_confirmation(
    tmp_path: Path,
) -> None:
    """The same invariant from the other side: the LIST itself is cancelled.

    A client closed mid-read raises `asyncio.CancelledError` out of the
    lister rather than an API error. The fail-open branch must not treat it
    as one: converting it to the static "impact unavailable" advisory would
    resurrect a scale whose cluster connection is already gone, and would
    open an approval dialog for a target nothing re-validated. The worker
    ends in `CANCELLED`, which is how a coroutine that let the cancellation
    propagate is distinguishable from one that swallowed it and returned.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path)
    env.lister.errors["deployments"] = asyncio.CancelledError()
    async with env.app.run_test() as pilot:
        worker = await _scale_worker(pilot, env.app)
        await until(
            pilot,
            lambda: worker.state is WorkerState.CANCELLED,
            label="scale worker observed loader cancellation",
        )
        assert env.lister.calls != []
        assert not isinstance(env.app.screen, ConfirmScreen)
        assert len(env.app.screen_stack) == 1
        assert not env.app.screen.query(".confirm-impact")
        assert env.app._active_cluster_writes == 0
        assert env.ops.calls == []
        assert not audit_path.exists()


async def test_scale_down_rbac_denial_never_loads_or_confirms(tmp_path: Path) -> None:
    """A denied SubjectAccessReview ends the flow before the replica prompt.

    The snapshot fan-out is a read the user was just told they may not make
    this write with; it must not run anyway, and no dialog may appear that a
    stray keystroke could approve.
    """
    audit_path = tmp_path / "audit.jsonl"
    env = ImpactEnv(audit_path, permission=False)
    denied = "missing permission: patch deployments/scale"
    async with env.app.run_test() as pilot:
        await to_view(pilot, "deploy", expect="web")
        await pilot.press("S")
        await until(
            pilot,
            lambda: any(str(note.message) == denied for note in env.app._notifications),
            label="scale RBAC denial",
        )
        assert env.lister.calls == []
        assert env.ops.calls == []
        assert len(env.app.screen_stack) == 1
        assert not audit_path.exists()


def test_the_catalog_aliases_resolve_every_kind_the_integrated_flows_exercise() -> None:
    """A guard on the harness itself: every write-flow view name this module
    and `test_impact_flow` drive through `open_delete_dialog`/`to_view`
    (`deploy`, `pods`, `secrets`, `nodes`, and the scalable `rs`/`sts`)
    resolves to a real discovered kind, not a synthetic view."""
    exercised = {
        "deployments": ("Deployment", "apps"),
        "pods": ("Pod", ""),
        "secrets": ("Secret", ""),
        "nodes": ("Node", ""),
        "replicasets": ("ReplicaSet", "apps"),
        "statefulsets": ("StatefulSet", "apps"),
    }
    for plural, (kind, group) in exercised.items():
        meta = CATALOG_ALIASES[plural]
        assert isinstance(meta, ResourceMeta)
        assert meta.kind == kind
        assert meta.group == group
        assert meta.synthetic is False
