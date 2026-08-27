"""Pure unit tests for the fail-closed write orchestration (issue TBD).

`run_approved_write` is a byte-identical extraction of what was
`WriteCoordinator._run_write_inner`'s body: intent audit (fail-closed) ->
mutation -> outcome audit, with `AuditRecorder`/`Notifier` standing in for
`self.audit_write`/`self._ui.notify`. It holds no Textual or `ViewState`
reference at all - exercised here with plain async fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from korvid.k8s.discovery import ResourceMeta
from korvid.k8s.errors import ApiStatusError
from korvid.tools.write_coordinator import (
    WRITE_VERBS,
    gvr_label,
    perm_target,
    run_approved_write,
    write_locus,
)

_PODS_META = ResourceMeta("Pod", "pods", "", "v1", True, ("po",))


class _Recorder:
    """The approved mutation, plus a record of when its factory was called."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.built = 0

    def factory(self) -> Awaitable[None]:
        self.built += 1
        return self._run()

    async def _run(self) -> None:
        if self.error is not None:
            raise self.error


class _AuditSpy:
    """Records outcomes in call order; can be made to fail on demand."""

    def __init__(self, *, fails: bool = False) -> None:
        self.fails = fails
        self.outcomes: list[str] = []

    async def __call__(
        self,
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        if self.fails:
            raise OSError("audit sink unavailable")
        self.outcomes.append(outcome)


class _NotifySpy:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def __call__(self, message: str, *, severity: str) -> None:
        self.messages.append((message, severity))


async def test_audit_runs_before_the_mutation_is_ever_built() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder()

    async def audit_then_check(*args: Any, **kwargs: Any) -> None:
        await audit(*args, **kwargs)
        assert rec.built == 0, "the factory must not exist before intent is audited"

    outcome = await run_approved_write(
        "delete",
        _PODS_META,
        "default",
        "web-1",
        rec.factory,
        "",
        audit=audit_then_check,
        notify=notify,
    )
    assert outcome == "done"
    assert rec.built == 1
    assert audit.outcomes == ["intent", "success"]


async def test_failed_intent_audit_blocks_the_mutation() -> None:
    audit = _AuditSpy(fails=True)
    notify = _NotifySpy()
    rec = _Recorder()
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "", audit=audit, notify=notify
    )
    assert outcome == "blocked: audit log unavailable"
    assert rec.built == 0
    assert ("delete pods/web-1 blocked: audit log unavailable", "error") in notify.messages


async def test_forbidden_mutation_keeps_the_rbac_message_contract() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=ApiStatusError(403, "Forbidden"))
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "", audit=audit, notify=notify
    )
    assert outcome == "failed: missing permission: delete pods"
    assert audit.outcomes[0] == "intent"
    assert audit.outcomes[1].startswith("error:")


async def test_conflicting_mutation_explains_the_uid_precondition() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=ApiStatusError(409, "Conflict"))
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "", audit=audit, notify=notify
    )
    assert outcome == (
        "failed: conflict: the target changed since it was approved - refresh and retry"
    )


async def test_unexpected_mutation_failure_still_audits_the_outcome() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=RuntimeError("boom"))
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "", audit=audit, notify=notify
    )
    assert outcome == "failed: boom"
    assert audit.outcomes == ["intent", "error: boom"]


async def test_outcome_audit_failure_warns_but_keeps_the_executed_write() -> None:
    calls: list[str] = []

    async def flaky(
        action: str,
        meta: ResourceMeta,
        namespace: str | None,
        name: str,
        detail: str,
        outcome: str,
    ) -> None:
        calls.append(outcome)
        if outcome != "intent":
            raise OSError("disk full")

    notify = _NotifySpy()
    rec = _Recorder()
    outcome = await run_approved_write(
        "delete", _PODS_META, "default", "web-1", rec.factory, "", audit=flaky, notify=notify
    )
    assert outcome == "done"
    assert rec.built == 1
    assert ("Audit log write failed (operation already executed)", "warning") in notify.messages


async def test_cancelled_mutation_propagates_without_being_reported_as_failed() -> None:
    audit = _AuditSpy()
    notify = _NotifySpy()
    rec = _Recorder(error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_approved_write(
            "delete", _PODS_META, "default", "web-1", rec.factory, "", audit=audit, notify=notify
        )
    assert audit.outcomes == ["intent"], "no outcome audit or notify for a cancelled mutation"
    assert notify.messages == []


def test_perm_target_matches_the_write_verbs_table() -> None:
    assert perm_target("delete", _PODS_META) == ("delete", "pods")
    assert WRITE_VERBS["delete"] == ("delete", "")


def test_gvr_label_and_write_locus_are_pure_string_helpers() -> None:
    assert gvr_label(_PODS_META) == "pods"
    assert write_locus("default") == " in namespace default"
    assert write_locus(None) == " (cluster-scoped)"


# --- shared action-specific write validation (issue TBD) --------------------
#
# `validate_scale_request`/`validate_restart_request` are the one, shared
# implementation of the request-shape checks a scale/rollout_restart write
# must pass before it may reach an approval policy, an audit intent, or a
# mutation - both `korvid.ui.agent_ui_controller.AgentUiController.build_write_op`
# and `korvid.evals.operation_runner.ScriptedOperationBridge` call these same
# two functions, so a TUI-free eval run and a Textual run reject exactly the
# same malformed requests, worded identically.

_DEPLOYMENT_META = ResourceMeta("Deployment", "deployments", "apps", "v1", True)
_CONFIGMAP_META = ResourceMeta("ConfigMap", "configmaps", "", "v1", True)


def test_validate_scale_request_accepts_a_scalable_kind_and_non_negative_replicas() -> None:
    from korvid.tools.write_coordinator import validate_scale_request

    assert validate_scale_request(_DEPLOYMENT_META, 3) is None
    assert validate_scale_request(_DEPLOYMENT_META, 0) is None


def test_validate_scale_request_rejects_a_non_scalable_kind() -> None:
    from korvid.tools.write_coordinator import validate_scale_request

    assert validate_scale_request(_CONFIGMAP_META, 3) == "ERROR: scale does not apply to configmaps"


def test_validate_scale_request_rejects_missing_or_negative_replicas() -> None:
    from korvid.tools.write_coordinator import validate_scale_request

    assert (
        validate_scale_request(_DEPLOYMENT_META, None)
        == "ERROR: scale requires a 'replicas' argument >= 0"
    )
    assert (
        validate_scale_request(_DEPLOYMENT_META, -1)
        == "ERROR: scale requires a 'replicas' argument >= 0"
    )


def test_validate_restart_request_accepts_a_restartable_kind() -> None:
    from korvid.tools.write_coordinator import validate_restart_request

    assert validate_restart_request(_DEPLOYMENT_META) is None


def test_validate_restart_request_rejects_a_non_restartable_kind() -> None:
    from korvid.tools.write_coordinator import validate_restart_request

    assert (
        validate_restart_request(_CONFIGMAP_META)
        == "ERROR: rollout restart does not apply to configmaps"
    )


def test_scalable_and_restartable_are_published_from_this_module() -> None:
    """`korvid.ui.resource_write_controller` re-exports these two, unchanged,
    so both existing UI import sites and this eval-safe module read the one
    same set of eligible (group, plural) pairs."""
    from korvid.tools.write_coordinator import RESTARTABLE, SCALABLE

    assert ("apps", "deployments") in SCALABLE
    assert ("apps", "deployments") in RESTARTABLE
    assert ("", "configmaps") not in SCALABLE
    assert ("", "configmaps") not in RESTARTABLE
