"""Unit tests for the shared recursive redaction primitive (issue #189)."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from korvid.core.redaction import (
    LAST_APPLIED,
    RedactionError,
    RedactionRecord,
    denotes_secret,
    merge_records,
    rebase,
    redact_document,
    redact_manifest,
    redact_text,
    redact_value,
)
from korvid.core.secrets import MASK_PLACEHOLDER


def test_nested_secret_data_is_masked_at_any_depth() -> None:
    """The classifier is `kind`, not the position in the document."""
    manifest = {
        "kind": "CompositeApp",
        "spec": {
            "templates": [
                {"kind": "Secret", "data": {"kubeconfig": "c3VwZXItc2VjcmV0"}},
            ]
        },
    }
    redacted = redact_manifest(manifest)
    template = redacted["spec"]["templates"][0]
    assert template["data"]["kubeconfig"] == MASK_PLACEHOLDER


def test_nested_last_applied_annotation_is_dropped() -> None:
    manifest = {
        "kind": "List",
        "items": [
            {
                "kind": "Secret",
                "metadata": {"annotations": {LAST_APPLIED: '{"data":{"p":"aGk="}}'}},
                "data": {"p": "aGk="},
            }
        ],
    }
    redacted = redact_manifest(manifest)
    assert LAST_APPLIED not in redacted["items"][0]["metadata"]["annotations"]
    assert "aGk=" not in str(redacted)


def test_env_entry_masks_the_sibling_of_a_credential_name() -> None:
    document = {"env": [{"name": "DB_PASSWORD", "value": "hunter2"}]}
    redacted = redact_manifest(document)
    assert redacted["env"][0] == {"name": "DB_PASSWORD", "value": MASK_PLACEHOLDER}


@pytest.mark.parametrize(
    "value",
    [
        {"raw": "hunter2"},
        ["hunter2"],
        [{"chunk": "hunter2"}],
        {"nested": {"deeper": ["hunter2"]}},
        True,
        7,
    ],
    ids=["mapping", "list", "list-of-mappings", "deep-mapping", "bool", "int"],
)
def test_a_credential_name_masks_its_sibling_of_any_type(value: Any) -> None:
    """`value` is a string in the API; any other shape is malformed or
    adversarial, and must be masked whole rather than walked into."""
    document = {"env": [{"name": "DB_PASSWORD", "value": value}]}
    redacted = redact_manifest(document)
    assert redacted["env"][0] == {"name": "DB_PASSWORD", "value": MASK_PLACEHOLDER}
    assert "hunter2" not in str(redacted)


def test_a_non_sensitive_name_keeps_its_structured_sibling() -> None:
    document = {"env": [{"name": "FEATURE_FLAGS", "value": {"beta": ["a", "b"]}}]}
    redacted = redact_manifest(document)
    assert redacted["env"][0]["value"] == {"beta": ["a", "b"]}


def test_ordinary_values_survive_redaction() -> None:
    document = {
        "kind": "Pod",
        "spec": {
            "automountServiceAccountToken": True,
            "containers": [
                {
                    "image": "example/api:1.0",
                    "env": [
                        {"name": "LOG_LEVEL", "value": "debug"},
                        {"name": "TOKENIZER_PATH", "value": "/models/tok"},
                        {"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db"}}},
                    ],
                }
            ],
        },
    }
    redacted = redact_manifest(document)
    container = redacted["spec"]["containers"][0]
    assert redacted["spec"]["automountServiceAccountToken"] is True
    assert container["image"] == "example/api:1.0"
    assert container["env"][0]["value"] == "debug"
    assert container["env"][1]["value"] == "/models/tok"
    assert container["env"][2]["valueFrom"] == {"secretKeyRef": {"name": "db"}}


def test_records_describe_every_change() -> None:
    document = {"kind": "Secret", "data": {"token": "aGk="}}
    _, records = redact_document(document, path="doc")
    assert RedactionRecord(path="doc.data.token", reason="secret-value") in records


def test_free_text_credential_assignments_are_masked() -> None:
    records: list[RedactionRecord] = []
    text = redact_text("connect with password=hunter2 now", "text", records)
    assert "hunter2" not in text
    assert records[0].reason == "credential-assignment"


def test_unredactable_shapes_fail_closed() -> None:
    with pytest.raises(RedactionError, match="mapping keys must be strings"):
        redact_value({1: "x"}, "doc", [])
    with pytest.raises(RedactionError, match="unsupported outbound data type"):
        redact_value({"at": object()}, "doc", [])


def test_cycles_fail_closed() -> None:
    document: dict[str, Any] = {"kind": "Pod"}
    document["self"] = document
    with pytest.raises(RedactionError, match="recursive data structures"):
        redact_manifest(document)


def test_the_input_document_is_never_mutated() -> None:
    manifest = {"kind": "Secret", "data": {"password": "aGk="}}
    redact_manifest(manifest)
    assert manifest["data"] == {"password": "aGk="}


@pytest.mark.parametrize(
    "name",
    ["DB_PASSWORD", "dbPassword", "github-access-token", "CLIENT_SECRET", "apiKey"],
)
def test_credential_names_are_recognized(name: str) -> None:
    assert denotes_secret(name)


@pytest.mark.parametrize("name", ["TOKENIZER_PATH", "LOG_LEVEL", "passwordless_mode_note"])
def test_unrelated_names_are_not_credentials(name: str) -> None:
    assert not denotes_secret(name)


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ({"raw": "hunter2-sentinel"}, "mapping"),
        (["hunter2-sentinel"], "list"),
        ({"outer": {"inner": ["hunter2-sentinel"]}}, "nested"),
        ([{"name": "primary", "secret": "hunter2-sentinel"}], "list-of-mappings"),
    ],
)
def test_a_compound_credential_key_masks_a_structured_value(value: Any, label: str) -> None:
    """`dbPassword: {raw: ...}` is malformed or hostile, never a flag.

    Descending into it ships the credential under keys (`raw`, `0`) that
    say nothing about what they hold, so nothing downstream can recognize
    it. The key already said it — the whole value goes.
    """
    redacted = redact_manifest({"kind": "MyDatabase", "spec": {"dbPassword": value}})
    assert redacted["spec"]["dbPassword"] == MASK_PLACEHOLDER, label


@pytest.mark.parametrize("value", [1234567890123456, -1, 3.5])
def test_a_compound_credential_key_masks_a_numeric_value(value: float) -> None:
    """A numeric PIN or long integer token is a credential like any other."""
    redacted = redact_manifest({"spec": {"admin-api-key": value}})
    assert redacted["spec"]["admin-api-key"] == MASK_PLACEHOLDER


def test_a_compound_credential_key_masks_an_absent_value() -> None:
    """Consistent with an exactly-named key: the type never opens a hole."""
    redacted = redact_manifest({"spec": {"adminApiKey": None}})
    assert redacted["spec"]["adminApiKey"] == MASK_PLACEHOLDER


def test_a_compound_credential_key_keeps_a_boolean_flag() -> None:
    """A bool carries one bit and no secret; masking it loses real information.

    `automountServiceAccountToken` names a credential without holding one,
    and whether a pod mounts its token is exactly what a diagnosis needs.
    """
    document = {
        "spec": {
            "automountServiceAccountToken": True,
            "enableApiKeyAuth": False,
            "hostNetwork": True,
        }
    }
    redacted = redact_manifest(document)
    assert redacted["spec"]["automountServiceAccountToken"] is True
    assert redacted["spec"]["enableApiKeyAuth"] is False
    assert redacted["spec"]["hostNetwork"] is True


def test_non_credential_keys_keep_their_structured_values() -> None:
    """The tightened type rule must not swallow ordinary nested config."""
    document = {
        "spec": {
            "tokenizerConfig": {"path": "/models/tok", "layers": [1, 2]},
            "replicas": 3,
            "secretName": "db-credentials",
            "resources": {"limits": {"cpu": "500m"}},
        }
    }
    redacted = redact_manifest(document)
    assert redacted["spec"]["tokenizerConfig"] == {"path": "/models/tok", "layers": [1, 2]}
    assert redacted["spec"]["replicas"] == 3
    assert redacted["spec"]["secretName"] == "db-credentials"
    assert redacted["spec"]["resources"] == {"limits": {"cpu": "500m"}}


@pytest.mark.parametrize(
    "name",
    [
        "AWS_SECRET_ACCESS_KEY",
        "aws_secret_access_key",
        "awsSecretAccessKey",
        "secret-access-key",
    ],
)
def test_the_aws_secret_access_key_is_a_credential_name(name: str) -> None:
    """The single most-copied cloud credential name in the world.

    Its words are `secret access key` — three of them, and none of the
    shorter combinations (`secret`, `accesskey`) is a name on its own, so
    without the full compound in the vocabulary nothing matches and the
    value ships.
    """
    assert denotes_secret(name)


@pytest.mark.parametrize(
    "name",
    ["secretKeyRef", "SECRET_NAME", "AWS_REGION", "AWS_DEFAULT_REGION", "accessModes"],
)
def test_neighbouring_aws_and_secret_names_stay_readable(name: str) -> None:
    """The new compound must not swallow the names next to it.

    `secretKeyRef` is the *pointer* the docs promise to preserve, and a
    region or access mode is ordinary configuration.
    """
    assert not denotes_secret(name)


def test_an_aws_credential_env_entry_is_masked() -> None:
    document = {
        "kind": "Pod",
        "spec": {
            "containers": [
                {
                    "env": [
                        {"name": "AWS_SECRET_ACCESS_KEY", "value": "aws-env-sentinel"},
                        {"name": "AWS_ACCESS_KEY_ID", "value": "AKIAEXAMPLE"},
                        {"name": "AWS_REGION", "value": "eu-west-1"},
                        {
                            "name": "AWS_SESSION_TOKEN",
                            "valueFrom": {"secretKeyRef": {"name": "aws", "key": "session"}},
                        },
                    ]
                }
            ]
        },
    }
    entries = redact_manifest(document)["spec"]["containers"][0]["env"]
    assert entries[0]["value"] == MASK_PLACEHOLDER
    assert entries[1]["value"] == "AKIAEXAMPLE"
    assert entries[2]["value"] == "eu-west-1"
    assert entries[3]["valueFrom"] == {"secretKeyRef": {"name": "aws", "key": "session"}}


def test_an_aws_credential_mapping_key_is_masked() -> None:
    document = {"spec": {"awsSecretAccessKey": "key-sentinel", "awsRegion": "eu-west-1"}}
    redacted = redact_manifest(document)
    assert redacted["spec"]["awsSecretAccessKey"] == MASK_PLACEHOLDER
    assert redacted["spec"]["awsRegion"] == "eu-west-1"


def test_an_aws_credential_assignment_in_free_form_text_is_masked() -> None:
    """The text vocabulary has to agree with the structured one.

    A log line or diagnosis is masked by pattern, not by structure; if
    the two lists disagree, the same credential name is caught in a
    manifest and printed in an event.
    """
    records: list[RedactionRecord] = []
    text = redact_text("env AWS_SECRET_ACCESS_KEY=aws-text-sentinel loaded", "doc", records)
    assert "aws-text-sentinel" not in text
    assert MASK_PLACEHOLDER in text
    assert records


def test_a_secret_key_reference_in_free_form_text_stays_readable() -> None:
    records: list[RedactionRecord] = []
    text = redact_text("mounted secretKeyRef=db-credentials-key", "doc", records)
    assert "db-credentials-key" in text


# --- Carrying records across a boundary (issue #189, review round 3) ---------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tool_result", "messages[3].content"),
        ("tool_result.data.password", "messages[3].content.data.password"),
        (
            "tool_result.spec.containers[0].env[1].value",
            "messages[3].content.spec.containers[0].env[1].value",
        ),
        ('tool_result["odd key"].x', 'messages[3].content["odd key"].x'),
    ],
)
def test_rebase_replaces_only_the_root_of_a_path(path: str, expected: str) -> None:
    rebased = rebase(RedactionRecord(path=path, reason="secret-value"), "messages[3].content")
    assert rebased.path == expected
    assert rebased.reason == "secret-value"


def test_merge_reports_a_redaction_both_passes_saw_only_once() -> None:
    seen_twice = RedactionRecord(path="messages[1].content", reason="credential-assignment")
    merged = merge_records([seen_twice], [seen_twice])
    assert merged == [seen_twice]


def test_merge_keeps_a_redaction_only_the_carried_pass_saw() -> None:
    derived = RedactionRecord(path="messages[1].content", reason="credential-assignment")
    carried = RedactionRecord(path="messages[1].content", reason="control-character")
    merged = merge_records([derived], [carried])
    assert merged == [derived, carried]


def test_merge_keeps_the_larger_count_of_a_repeated_redaction() -> None:
    item = RedactionRecord(path="messages[1].content", reason="credential-assignment")
    assert merge_records([item], [item, item]) == [item, item]
    assert merge_records([item, item], [item]) == [item, item]


def test_merge_of_nothing_carried_is_the_derived_inventory() -> None:
    item = RedactionRecord(path="messages[1].content", reason="control-character")
    assert merge_records([item], []) == [item]
    assert merge_records([], [item]) == [item]


# --- Malformed Secret metadata is fail-closed (issue #189, review round 4) ---

#: A serialized Secret manifest with unmasked data, as `kubectl apply`
#: stores it in the last-applied annotation. Must never survive redaction.
SERIALIZED_SECRET = '{"kind":"Secret","data":{"tls.key":"UkFXLVNFQ1JFVA=="}}'
SERIALIZED_SECRET_SENTINEL = "UkFXLVNFQ1JFVA=="


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param([{"annotations": {LAST_APPLIED: SERIALIZED_SECRET}}], id="list"),
        pytest.param(f"annotations:\n  {LAST_APPLIED}: '{SERIALIZED_SECRET}'", id="string"),
        pytest.param(0, id="number"),
        pytest.param(True, id="bool"),
    ],
)
def test_a_secret_with_non_mapping_metadata_is_rejected(metadata: Any) -> None:
    manifest = {"kind": "Secret", "metadata": metadata, "data": {"a": "Yg=="}}

    with pytest.raises(RedactionError, match="metadata"):
        redact_manifest(manifest)


@pytest.mark.parametrize(
    "annotations",
    [
        pytest.param([SERIALIZED_SECRET], id="list"),
        pytest.param(SERIALIZED_SECRET, id="string"),
        pytest.param(f"{LAST_APPLIED}: {SERIALIZED_SECRET}", id="string-with-last-applied"),
        pytest.param(7, id="number"),
    ],
)
def test_a_secret_with_non_mapping_annotations_is_rejected(annotations: Any) -> None:
    manifest = {
        "kind": "Secret",
        "metadata": {"name": "db", "annotations": annotations},
        "data": {"a": "Yg=="},
    }

    with pytest.raises(RedactionError, match="annotations"):
        redact_manifest(manifest)


def test_the_rejection_message_never_quotes_the_malformed_value() -> None:
    manifest = {
        "kind": "Secret",
        "metadata": {"annotations": SERIALIZED_SECRET},
        "data": {"a": "Yg=="},
    }

    with pytest.raises(RedactionError) as excinfo:
        redact_manifest(manifest)

    assert SERIALIZED_SECRET_SENTINEL not in str(excinfo.value)


def test_a_nested_secret_with_malformed_metadata_is_rejected() -> None:
    """The rule follows `kind: Secret` wherever it appears, not only at the root."""
    manifest = {
        "kind": "CompositeApp",
        "spec": {
            "secretTemplate": {
                "kind": "Secret",
                "metadata": {"annotations": SERIALIZED_SECRET},
                "data": {"a": "Yg=="},
            }
        },
    }

    with pytest.raises(RedactionError, match="annotations"):
        redact_manifest(manifest)


def test_a_secret_with_well_formed_metadata_still_redacts() -> None:
    manifest = {
        "kind": "Secret",
        "metadata": {"name": "db", "annotations": {LAST_APPLIED: SERIALIZED_SECRET, "team": "sre"}},
        "data": {"password": "Yg=="},
    }

    redacted = redact_manifest(manifest)

    assert redacted["metadata"]["annotations"] == {"team": "sre"}
    assert redacted["data"]["password"] == MASK_PLACEHOLDER
    assert SERIALIZED_SECRET_SENTINEL not in str(redacted)


def test_a_secret_without_metadata_is_unaffected() -> None:
    redacted = redact_manifest({"kind": "Secret", "data": {"password": "Yg=="}})

    assert redacted["data"]["password"] == MASK_PLACEHOLDER


def test_a_non_secret_with_odd_metadata_is_still_allowed() -> None:
    """The strict rule is scoped to Secrets; ordinary objects stay readable."""
    redacted = redact_manifest({"kind": "ConfigMap", "metadata": ["odd", "but", "harmless"]})

    assert redacted["metadata"] == ["odd", "but", "harmless"]


# --- Key paths name the sanitized key (issue #189, review round 4) -----------


def test_a_control_character_key_records_the_path_that_is_in_the_payload() -> None:
    """The path names the key the reader will actually see.

    Recording the pre-sanitized spelling pointed the inventory at a key
    that is not in the payload, and carried the raw key material into a
    report whose purpose is to show that nothing raw left (PR #197
    review round 4).
    """
    redacted, records = redact_document({"we\x07ird": "value"}, path="doc")

    assert list(redacted) == ["we\ufffdird"]
    assert [(r.path, r.reason) for r in records] == [('doc["we\ufffdird"]', "control-character")]


def test_a_control_character_key_path_never_carries_the_raw_spelling() -> None:
    _, records = redact_document({"tab\x1b[2Jkey": {"inner": 1}}, path="doc")

    assert records
    for item in records:
        assert "\x1b" not in item.path
        assert "\\u001b" not in item.path
        assert "\\u0007" not in item.path


def test_a_nested_control_character_key_records_a_reachable_path() -> None:
    redacted, records = redact_document({"outer": {"in\x00ner": "v"}}, path="doc")

    assert redacted == {"outer": {"in\ufffdner": "v"}}
    assert ('doc.outer["in\ufffdner"]', "control-character") in [
        (r.path, r.reason) for r in records
    ]


def test_a_masked_value_under_a_control_character_key_uses_one_path() -> None:
    """Both records name the same place, so the inventory can be joined."""
    redacted, records = redact_document({"pass\x07word": "raw"}, path="doc")

    assert redacted == {"pass\ufffdword": MASK_PLACEHOLDER}
    assert {r.path for r in records} == {'doc["pass\ufffdword"]'}
    assert {r.reason for r in records} == {"control-character", "sensitive-key"}


def test_every_record_path_names_a_key_present_in_the_redacted_document() -> None:
    """The property behind the three tests above, stated directly."""
    document = {
        "clean": {"nes\x07ted": {"pass\x01word": "raw"}},
        "list": [{"ke\x02y": "v"}],
    }

    redacted, records = redact_document(document, path="doc")

    rendered = json.dumps(redacted, ensure_ascii=False)
    for item in records:
        match = re.search(r'\["((?:[^"\\]|\\.)*)"\]$|\.([^.\[\]]+)$', item.path)
        assert match is not None, item.path
        key = json.loads(f'"{match.group(1)}"') if match.group(1) is not None else match.group(2)
        assert key in rendered, f"{item.path} names a key absent from the payload"
