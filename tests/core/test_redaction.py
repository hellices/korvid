"""Unit tests for the shared recursive redaction primitive (issue #189)."""

from __future__ import annotations

from typing import Any

import pytest

from korvid.core.redaction import (
    LAST_APPLIED,
    RedactionError,
    RedactionRecord,
    denotes_secret,
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
