"""Executor security and redaction tests."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
import yaml

import korvid.tools.executor as executor_module
from korvid.core.redaction import RedactionError, RedactionRecord
from korvid.core.secrets import MASK_PLACEHOLDER
from korvid.k8s.discovery import PODS_META
from korvid.k8s.logs import LogLine
from korvid.tools.executor import MAX_RESULT_CHARS, ToolExecutor, ToolResultBlocked
from korvid.tools.structured import ERROR_PREFIX, load_structured_document
from tests.tools.executor_fakes import (
    _LOG_SECRET,
    LONG_NAME_ENV_SENTINEL,
    NESTED_SECRET_SENTINEL,
    PARENT_SECRET,
    FakeEventKube,
    FakeKube,
    FakeLogKube,
    ParentCredentialKube,
    _ambiguous_key_manifest,
    _credential_log_kube,
    _diagnose_executor,
    identity_last_crd,
    make_executor,
    oversized_crd_with_nested_credentials,
)


async def test_get_resource_masks_secret_data() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {"name": "s", "managedFields": [{"x": 1}]},
        "data": {"password": "aGVsbG8="},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )
    assert "aGVsbG8=" not in out
    assert MASK_PLACEHOLDER in out
    assert "managedFields" not in out


async def test_get_resource_masks_private_key_fields_before_bounding() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "ConfigMap",
        "metadata": {"name": "client-config"},
        "data": {
            "privateKey": "private-key-sentinel",
            "publicKeyId": "public-key-id",
        },
    }

    outcome = await make_executor(kube).execute_recorded(
        "get_resource", {"kind": "pods", "name": "client-config", "namespace": "default"}
    )
    loaded = yaml.safe_load(outcome.text)

    assert loaded["data"] == {
        "privateKey": MASK_PLACEHOLDER,
        "publicKeyId": "public-key-id",
    }
    assert outcome.redactions == (
        RedactionRecord(path="manifest.data.privateKey", reason="sensitive-key"),
    )


async def test_get_resource_masks_secret_string_data() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {"name": "s"},
        "stringData": {"token": "super-secret"},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )
    assert "super-secret" not in out
    assert MASK_PLACEHOLDER in out


async def test_get_resource_strips_last_applied_annotation_on_secret() -> None:
    """Client-side apply stores the unmasked manifest in this annotation."""
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {
            "name": "s",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": (
                    '{"kind":"Secret","data":{"password":"aGVsbG8="}}'
                ),
                "other": "kept",
            },
        },
        "data": {"password": "aGVsbG8="},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )
    assert "aGVsbG8=" not in out
    assert "last-applied-configuration" not in out
    assert "kept" in out


async def test_get_resource_strips_managed_fields_for_non_secret() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Pod",
        "metadata": {"name": "p", "managedFields": [{"manager": "kubectl"}]},
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "p", "namespace": "default"}
    )
    assert "managedFields" not in out


async def test_get_resource_masks_an_aws_credential_env_value() -> None:
    """`AWS_SECRET_ACCESS_KEY` is the name people actually paste into a pod."""
    kube = FakeKube()
    kube.manifest = {
        "kind": "Pod",
        "metadata": {"name": "p", "namespace": "d"},
        "spec": {
            "containers": [
                {
                    "name": "main",
                    "env": [
                        {"name": "AWS_SECRET_ACCESS_KEY", "value": "aws-producer-sentinel"},
                        {"name": "AWS_REGION", "value": "eu-west-1"},
                    ],
                }
            ]
        },
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "p", "namespace": "d"}
    )
    assert "aws-producer-sentinel" not in out
    assert "eu-west-1" in out


async def test_get_resource_redacts_nested_credentials_before_size_reduction() -> None:
    """Size reduction must never precede redaction (PR #197 review).

    Elision drops the nested `kind: Secret`, and clamping cuts the
    credential word off a long env `name`; a document reduced first
    arrives at the central policy with the values still in it and no
    remaining evidence that they are secrets.
    """
    kube = FakeKube()
    kube.manifest = oversized_crd_with_nested_credentials()
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )
    assert NESTED_SECRET_SENTINEL not in out
    assert LONG_NAME_ENV_SENTINEL not in out
    # Still a bounded, parseable document that identifies its object.
    assert len(out) <= MAX_RESULT_CHARS
    loaded = yaml.safe_load(out)
    assert loaded["kind"] == "CompositeApp"
    assert loaded["metadata"]["name"] == "composite-0"


async def test_get_resource_keeps_non_sensitive_content_readable() -> None:
    """Redacting before bounding must not blank out ordinary manifests."""
    kube = FakeKube()
    kube.manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "api-0", "namespace": "prod"},
        "spec": {
            "containers": [
                {
                    "name": "api",
                    "image": "example/api:1.2.3",
                    "env": [
                        {"name": "LOG_LEVEL", "value": "debug"},
                        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db"}}},
                    ],
                }
            ]
        },
    }
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "api-0", "namespace": "prod"}
    )
    loaded = yaml.safe_load(out)
    container = loaded["spec"]["containers"][0]
    assert container["image"] == "example/api:1.2.3"
    assert container["env"][0] == {"name": "LOG_LEVEL", "value": "debug"}
    assert container["env"][1]["valueFrom"] == {"secretKeyRef": {"name": "db"}}


async def test_get_resource_fails_closed_on_an_unredactable_manifest() -> None:
    """Data the redactor cannot reason about is refused, not forwarded."""
    kube = FakeKube()
    kube.manifest = {"kind": "Pod", "metadata": {"name": "p"}, "spec": {1: "unmasked-value"}}
    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "p", "namespace": "d"}
    )
    assert out.startswith("ERROR:")
    assert "unmasked-value" not in out


# --- Malformed Secret metadata is fail-closed (issue #189, review round 4) ---

#: A serialized Secret with unmasked `data`, as `kubectl apply` stores it.
MALFORMED_SECRET_SENTINEL = "UkFXLVNFQ1JFVA=="
_SERIALIZED_SECRET = f'{{"kind":"Secret","data":{{"tls.key":"{MALFORMED_SECRET_SENTINEL}"}}}}'


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"annotations": _SERIALIZED_SECRET}, id="annotations-string"),
        pytest.param({"annotations": [_SERIALIZED_SECRET]}, id="annotations-list"),
        pytest.param(_SERIALIZED_SECRET, id="metadata-string"),
        pytest.param([{"annotations": {"x": _SERIALIZED_SECRET}}], id="metadata-list"),
    ],
)
async def test_get_resource_refuses_a_secret_with_malformed_metadata(
    metadata: Any,
) -> None:
    """A shape the redactor cannot search is refused, not walked.

    `kubectl apply` puts the whole pre-apply manifest in a metadata
    annotation. The removal rule reaches it through mappings only, so a
    non-mapping `metadata`/`annotations` on a Secret shipped a serialized
    Secret verbatim (PR #197 review round 4).
    """
    kube = FakeKube()
    kube.manifest = {"kind": "Secret", "metadata": metadata, "data": {"a": "Yg=="}}

    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "db", "namespace": "prod"}
    )

    assert out.startswith("ERROR:")
    assert MALFORMED_SECRET_SENTINEL not in out


async def test_get_resource_still_returns_a_well_formed_secret() -> None:
    kube = FakeKube()
    kube.manifest = {
        "kind": "Secret",
        "metadata": {"name": "db", "annotations": {"team": "sre"}},
        "data": {"password": "Yg=="},
    }

    out = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "db", "namespace": "prod"}
    )

    loaded = yaml.safe_load(out)
    assert loaded["metadata"]["annotations"] == {"team": "sre"}
    assert loaded["data"]["password"] == MASK_PLACEHOLDER


async def test_get_logs_redacts_full_text_and_preserves_container() -> None:
    class CredentialLogs(FakeLogKube):
        async def stream_logs(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            follow: bool = True,
            tail_lines: int = 200,
        ) -> Any:
            yield LogLine(
                pod=pod,
                container=container,
                text="password=log-password-sentinel",
            )
            yield LogLine(
                pod=pod,
                container=container,
                text="token=log-token-sentinel",
            )
            yield LogLine(
                pod=pod,
                container=container,
                text="Authorization: log-auth-sentinel",
            )

    outcome = await make_executor(CredentialLogs()).execute_recorded(
        "get_logs",
        {"pod": "web", "namespace": "default"},
    )

    assert "log-password-sentinel" not in outcome.text
    assert "log-token-sentinel" not in outcome.text
    assert "log-auth-sentinel" not in outcome.text
    assert outcome.text.count(MASK_PLACEHOLDER) == 3
    assert outcome.redactions == (
        RedactionRecord(path="logs", reason="authorization-value"),
        RedactionRecord(path="logs", reason="credential-assignment"),
        RedactionRecord(path="logs", reason="credential-assignment"),
    )
    assert outcome.container == "app"


async def test_get_events_redacts_text_and_preserves_incarnation() -> None:
    class CredentialEvents(FakeEventKube):
        async def list_events_for(
            self,
            namespace: str,
            name: str,
            *,
            kind: str | None = None,
            uid: str | None = None,
        ) -> list[dict[str, Any]]:
            self.event_calls.append(
                {"namespace": namespace, "name": name, "kind": kind, "uid": uid}
            )
            return [
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "count": 3,
                    "message": "token=event-token-sentinel",
                }
            ]

    outcome = await make_executor(CredentialEvents()).execute_recorded(
        "get_events",
        {"kind": "pods", "namespace": "default", "name": "web"},
    )

    assert outcome.text == f"Warning BackOff (3x): token={MASK_PLACEHOLDER}"
    assert outcome.redactions == (RedactionRecord(path="events", reason="credential-assignment"),)
    assert outcome.incarnation == "abc-123"


async def test_get_logs_redacts_before_the_final_result_cap() -> None:
    padding = MAX_RESULT_CHARS - len(" token=1234") - 1

    class LongCredentialLogs(FakeLogKube):
        async def stream_logs(
            self,
            namespace: str,
            pod: str,
            container: str,
            *,
            follow: bool = True,
            tail_lines: int = 200,
        ) -> Any:
            text = "x" * padding + " token=1234"
            yield LogLine(pod=pod, container=container, text=text)

    outcome = await make_executor(LongCredentialLogs()).execute_recorded(
        "get_logs",
        {"pod": "web", "namespace": "default"},
    )

    assert len(outcome.text) == MAX_RESULT_CHARS
    assert outcome.text.endswith(executor_module._TRUNCATION_SUFFIX)
    assert "1234" not in outcome.text
    assert outcome.redactions == (RedactionRecord(path="logs", reason="credential-assignment"),)


async def test_log_redaction_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_text(
        text: str,
        path: str,
        records: list[RedactionRecord],
    ) -> str:
        raise RedactionError(f"unsafe text shape: {text}")

    monkeypatch.setattr(executor_module, "redact_text", reject_text)

    with pytest.raises(ToolResultBlocked, match="could not redact the result") as caught:
        await make_executor(FakeLogKube()).execute_recorded(
            "get_logs",
            {"pod": "web", "namespace": "default"},
        )

    assert "line-1" not in str(caught.value)


async def test_event_redaction_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_text(
        text: str,
        path: str,
        records: list[RedactionRecord],
    ) -> str:
        raise RedactionError(f"unsafe text shape: {text}")

    monkeypatch.setattr(executor_module, "redact_text", reject_text)

    with pytest.raises(ToolResultBlocked, match="could not redact the result") as caught:
        await make_executor(FakeEventKube()).execute_recorded(
            "get_events",
            {"kind": "pods", "namespace": "default", "name": "web"},
        )

    assert "restarting" not in str(caught.value)


# --- A redaction failure is not an ordinary tool error (round 6) ------------
#
# `redact_document` refuses shapes it cannot reason about — a `kind:
# Secret` whose metadata is not a mapping, a cycle, a non-string key.
# Collapsing that into an `ERROR: ...` string made it indistinguishable
# from "the API said no", so the agent kept the turn going.

_UNREDACTABLE_SECRET = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": "not-a-mapping",
    "data": {"password": "cmF3LXNlY3JldA=="},
}


async def test_execute_recorded_raises_when_a_result_cannot_be_redacted() -> None:
    kube = FakeKube()
    kube.manifest = _UNREDACTABLE_SECRET

    with pytest.raises(ToolResultBlocked, match="could not redact the result"):
        await make_executor(kube).execute_recorded(
            "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
        )


async def test_a_blocked_result_carries_no_raw_data() -> None:
    kube = FakeKube()
    kube.manifest = _UNREDACTABLE_SECRET

    with pytest.raises(ToolResultBlocked) as caught:
        await make_executor(kube).execute_recorded(
            "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
        )

    assert "cmF3LXNlY3JldA==" not in str(caught.value)


async def test_execute_still_returns_a_safe_error_string_when_redaction_fails() -> None:
    """MCP and the eval runner take strings; they must not start raising."""
    kube = FakeKube()
    kube.manifest = _UNREDACTABLE_SECRET

    result = await make_executor(kube).execute(
        "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
    )

    assert type(result) is str
    assert result.startswith(ERROR_PREFIX)
    assert "cmF3LXNlY3JldA==" not in result


# --- Shaped text is redacted before it is cut (round 9) --------------------


@pytest.mark.parametrize(
    "assignment",
    [
        f"api_key={_LOG_SECRET}",
        f"password={_LOG_SECRET}",
        f"AWS_SECRET_ACCESS_KEY={_LOG_SECRET}",
    ],
    ids=["api_key", "password", "aws"],
)
async def test_a_shaped_report_is_redacted_before_it_is_compacted(assignment: str) -> None:
    """Head+tail compaction cuts at a byte offset, so an assignment that
    straddles the cut loses the keyword that classifies it and strands the
    value in the tail. Redaction has to run first (PR #197 review)."""
    outcome = await _diagnose_executor(_credential_log_kube(assignment)).execute_recorded(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert _LOG_SECRET not in outcome.text
    assert MASK_PLACEHOLDER in outcome.text
    assert outcome.redactions
    assert not outcome.error


async def test_a_compacted_report_keeps_its_evidence_and_its_bound() -> None:
    kube = _credential_log_kube(f"api_key={_LOG_SECRET}")

    out = await _diagnose_executor(kube).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert "WORKLOAD — Deployment default/api" in out
    assert "POD DIAGNOSIS — default/api-1" in out
    # The marker is model-facing: it names the budget the model is
    # actually under, which is its tier's, not a retired profile's.
    assert "middle truncated — tier result budget" in out
    assert len(out) <= MAX_RESULT_CHARS


async def test_a_report_without_credentials_is_left_alone() -> None:
    kube = _credential_log_kube("level=error image pull failed")

    outcome = await _diagnose_executor(kube).execute_recorded(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert MASK_PLACEHOLDER not in outcome.text
    assert not outcome.redactions
    assert "image pull failed" in outcome.text


async def test_the_string_api_reports_the_same_redacted_report() -> None:
    """`execute()` is what the MCP host and the eval grader take; the
    producer's redaction is on that path too, not only the recorded one."""
    out = await _diagnose_executor(_credential_log_kube(f"api_key={_LOG_SECRET}")).execute(
        "diagnose_workload",
        {"kind": "deployments", "name": "api", "namespace": "default"},
    )

    assert _LOG_SECRET not in out
    assert MASK_PLACEHOLDER in out


# --- The parent sections are redacted too (round 10 final review) ----------
#
# Round 9 redacted the per-pod blocks, which is where a log excerpt lands.
# The rest of the compound report — the workload's own conditions, its
# Warning events, the owned-ReplicaSet lines, the child-LIST error — is
# assembled from cluster strings just as attacker-influenced as a log
# line, and went out unredacted.

_WORKLOAD_ARGS = {"kind": "deployments", "name": "api", "namespace": "default"}


async def test_a_workload_condition_credential_never_leaves_the_producer() -> None:
    """A Deployment condition message is cluster text like any other."""
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET} at startup"
    )

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert MASK_PLACEHOLDER in outcome.text
    assert [record.reason for record in outcome.redactions] == ["credential-assignment"]
    assert "MinimumReplicasUnavailable" in outcome.text
    assert not outcome.error


async def test_a_workload_warning_event_credential_never_leaves_the_producer() -> None:
    kube = ParentCredentialKube(event_message=f"registry auth failed password={PARENT_SECRET}")

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert "FailedCreate (3x" in outcome.text
    assert [record.reason for record in outcome.redactions] == ["credential-assignment"]


async def test_a_failed_child_list_credential_never_leaves_the_producer() -> None:
    """The LIST error is interpolated straight from the API exception."""
    kube = ParentCredentialKube(list_error=f"denied for AWS_SECRET_ACCESS_KEY={PARENT_SECRET}")

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert MASK_PLACEHOLDER in outcome.text
    assert "POD DIAGNOSES" in outcome.text
    assert outcome.redactions


async def test_the_parent_report_is_redacted_exactly_once() -> None:
    """Two assignments, two records: the parent must not be passed through
    redaction twice, which would inflate the inventory the inspector shows."""
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET}",
        event_message=f"registry auth failed password={PARENT_SECRET}",
    )

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert [record.reason for record in outcome.redactions] == ["credential-assignment"] * 2
    assert {record.path for record in outcome.redactions} == {"report"}


async def test_parent_and_pod_redactions_share_one_record_trail() -> None:
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET}",
        pod_log_line=f"level=error api_key={_LOG_SECRET} retry",
    )

    outcome = await _diagnose_executor(kube).execute_recorded("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in outcome.text
    assert _LOG_SECRET not in outcome.text
    # One record from the parent condition, one from the single expanded
    # pod block: two passes, one trail, nothing counted twice.
    assert [record.reason for record in outcome.redactions] == ["credential-assignment"] * 2


async def test_the_masked_parent_report_keeps_its_sections_and_its_bound() -> None:
    kube = ParentCredentialKube(
        condition_message=f"probe rejected api_key={PARENT_SECRET}",
        event_message=f"registry auth failed password={PARENT_SECRET}",
    )

    out = await _diagnose_executor(kube).execute("diagnose_workload", _WORKLOAD_ARGS)

    assert PARENT_SECRET not in out
    for title in (
        "WORKLOAD — Deployment default/api",
        "SELECTED NON-READY PODS",
        "WORKLOAD CONDITIONS (failing first)",
        "WORKLOAD WARNING EVENTS (newest first)",
        "OWNED REPLICASETS",
    ):
        assert title in out
    assert out.index("WORKLOAD CONDITIONS") < out.index("WORKLOAD WARNING EVENTS")
    assert out.index("OWNED REPLICASETS") < out.index("\nPOD DIAGNOSIS — default/api-1\n")
    assert "MinimumReplicasUnavailable" in out
    assert len(out) <= MAX_RESULT_CHARS


async def test_a_parent_report_without_credentials_is_left_alone() -> None:
    outcome = await _diagnose_executor(ParentCredentialKube()).execute_recorded(
        "diagnose_workload", _WORKLOAD_ARGS
    )

    assert MASK_PLACEHOLDER not in outcome.text
    assert not outcome.redactions
    assert "replicas are unavailable" in outcome.text


# --- Recursion exhaustion is a refusal, not a result (round 9) -------------


def _deeply_nested_secret(depth: int = 1500) -> dict[str, Any]:
    """A CRD burying a `Secret` deeper than the interpreter can recurse."""
    document: Any = {
        "kind": "Secret",
        "metadata": {"name": "db"},
        "data": {"password": "cmF3LXNlY3JldA=="},
    }
    for _ in range(depth):
        document = {"spec": {"nested": document}}
    return {"apiVersion": "v1", "kind": "CompositeApp", **document}


class _DeepKube:
    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document

    async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
        return self._document


async def test_a_manifest_too_deep_to_redact_is_blocked_not_reported() -> None:
    """Running out of stack means the redactor never finished, so it can
    promise nothing about the document (PR #197 review)."""
    executor = ToolExecutor(_DeepKube(_deeply_nested_secret()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    with pytest.raises(ToolResultBlocked, match="too deeply nested"):
        await executor.execute_recorded(
            "get_resource", {"kind": "pods", "name": "a", "namespace": "b"}
        )


async def test_a_manifest_too_deep_to_serialize_is_blocked_not_reported() -> None:
    """The redacted document still has to be written out, and that walk
    is just as recursive."""
    document = _deeply_nested_secret()
    executor = ToolExecutor(_DeepKube(document), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    with (
        mock.patch.object(executor_module, "_mask_manifest", return_value=(document, [])),
        pytest.raises(ToolResultBlocked, match="too deeply nested"),
    ):
        await executor.execute_recorded(
            "get_resource", {"kind": "pods", "name": "a", "namespace": "b"}
        )


async def test_the_string_api_reports_a_deep_manifest_as_a_safe_error() -> None:
    """MCP hosts have no turn to stop, so they get the same safe string
    every other refusal produces — naming the shape, never the document."""
    executor = ToolExecutor(_DeepKube(_deeply_nested_secret()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    out = await executor.execute("get_resource", {"kind": "pods", "name": "a", "namespace": "b"})

    assert out.startswith(ERROR_PREFIX)
    assert "too deeply nested" in out
    assert "cmF3LXNlY3JldA==" not in out
    assert "recursion" not in out


async def test_an_unrelated_recursion_failure_stays_an_ordinary_error() -> None:
    """Only the redaction and serialization walk is normalized; a handler
    bug elsewhere must not be reported as a redaction refusal."""

    class _RecursingKube:
        async def get_object(self, meta: Any, namespace: str | None, name: str) -> dict[str, Any]:
            def spin(n: int) -> int:
                return spin(n + 1)

            return {"kind": "Pod", "depth": spin(0)}

    executor = ToolExecutor(_RecursingKube(), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "a", "namespace": "b"}
    )

    assert outcome.error
    assert outcome.text.startswith(ERROR_PREFIX)


async def test_a_bounded_manifest_still_names_its_object() -> None:
    """A result the model cannot identify is not evidence, and the
    reduction used to drop identity whenever the document listed it last
    (PR #197 review)."""
    executor = ToolExecutor(_DeepKube(identity_last_crd()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )

    manifest = yaml.safe_load(outcome.text)
    assert len(outcome.text) <= MAX_RESULT_CHARS
    assert manifest["kind"] == "CompositeApp"
    assert manifest["apiVersion"] == "example.com/v1"
    assert manifest["metadata"]["name"] == "composite-0"
    assert manifest["metadata"]["namespace"] == "prod"


# --- What the producer writes, the boundary can read (round 13) -----------


async def test_a_produced_manifest_survives_the_strict_reader() -> None:
    """The boundary re-reads every structured result, so a document the
    producer writes must never look ambiguous when it is read back."""
    executor = ToolExecutor(_DeepKube(_ambiguous_key_manifest()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "flags", "namespace": "prod"}
    )
    loaded = load_structured_document(outcome.text)

    assert loaded == _ambiguous_key_manifest()
    assert len(loaded["metadata"]["annotations"]) == 7


async def test_a_bounded_produced_manifest_survives_the_strict_reader() -> None:
    """Reduction rewrites the document; what it emits has to stay readable."""
    executor = ToolExecutor(_DeepKube(identity_last_crd()), {"pods": PODS_META})  # type: ignore[arg-type]  # test double for ReadOps

    outcome = await executor.execute_recorded(
        "get_resource", {"kind": "pods", "name": "composite-0", "namespace": "prod"}
    )
    loaded = load_structured_document(outcome.text)

    assert loaded["kind"] == "CompositeApp"


async def test_a_malformed_manifest_still_blocks_rather_than_crashing() -> None:
    """Identity extraction must not pre-empt the redaction refusal.

    A document whose `metadata` is not a mapping made the UID lookup raise
    before `_mask_manifest` ran, turning a `ToolResultBlocked` refusal
    into an ordinary error - the credential path this repo treats as
    fail-closed (#250 review).
    """
    kube = FakeKube()
    kube.manifest = _UNREDACTABLE_SECRET

    with pytest.raises(ToolResultBlocked, match="could not redact the result"):
        await make_executor(kube).execute_recorded(
            "get_resource", {"kind": "pods", "name": "s", "namespace": "d"}
        )
