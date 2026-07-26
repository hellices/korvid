"""Unit tests for Secret value decoding + masking helpers (issue #39)."""

from __future__ import annotations

import base64
import hashlib

from korvid.core.secrets import (
    MASK_PLACEHOLDER,
    mask_secret_manifest,
    reveal_value,
    secret_keys,
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class TestRevealValue:
    def test_decodes_utf8_base64(self) -> None:
        result = reveal_value(_b64("s3cr3t-password"))
        assert result.text == "s3cr3t-password"
        assert result.binary is False

    def test_binary_payload_becomes_size_and_sha256(self) -> None:
        payload = bytes(range(256))
        result = reveal_value(base64.b64encode(payload).decode())
        assert result.binary is True
        assert f"{len(payload)} bytes" in result.text
        assert hashlib.sha256(payload).hexdigest() in result.text
        # No raw bytes leak into the rendered text.
        assert "\x00" not in result.text

    def test_invalid_base64_is_graceful(self) -> None:
        result = reveal_value("!!! not base64 !!!")
        assert result.binary is True
        assert "invalid base64" in result.text

    def test_stringdata_passthrough(self) -> None:
        result = reveal_value("plain-text-value", encoded=False)
        assert result.text == "plain-text-value"
        assert result.binary is False

    def test_multiline_text_decodes_as_text(self) -> None:
        config = "line1\nline2\n"
        result = reveal_value(_b64(config))
        assert result.text == config
        assert result.binary is False


class TestMaskSecretManifest:
    def _manifest(self) -> dict[str, object]:
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": "db-creds",
                "namespace": "default",
                "annotations": {
                    "kubectl.kubernetes.io/last-applied-configuration": '{"data":{"password":"aHVudGVyMg=="}}',
                    "keep-me": "yes",
                },
            },
            "data": {"password": _b64("hunter2"), "username": _b64("admin")},
            "stringData": {"note": "plaintext"},
        }

    def test_masks_data_and_stringdata(self) -> None:
        masked = mask_secret_manifest(self._manifest())
        data = masked["data"]
        string_data = masked["stringData"]
        assert isinstance(data, dict)
        assert isinstance(string_data, dict)
        assert data["password"] == MASK_PLACEHOLDER
        assert data["username"] == MASK_PLACEHOLDER
        assert string_data["note"] == MASK_PLACEHOLDER

    def test_strips_last_applied_annotation(self) -> None:
        masked = mask_secret_manifest(self._manifest())
        meta = masked["metadata"]
        assert isinstance(meta, dict)
        annotations = meta["annotations"]
        assert isinstance(annotations, dict)
        assert "kubectl.kubernetes.io/last-applied-configuration" not in annotations
        assert annotations["keep-me"] == "yes"

    def test_does_not_mutate_input(self) -> None:
        original = self._manifest()
        password_b64 = _b64("hunter2")
        mask_secret_manifest(original)
        data = original["data"]
        assert isinstance(data, dict)
        assert data["password"] == password_b64
        meta = original["metadata"]
        assert isinstance(meta, dict)
        annotations = meta["annotations"]
        assert isinstance(annotations, dict)
        assert "kubectl.kubernetes.io/last-applied-configuration" in annotations

    def test_no_secret_material_in_masked_yaml(self) -> None:
        import yaml

        masked = mask_secret_manifest(self._manifest())
        dumped = yaml.safe_dump(masked)
        assert "hunter2" not in dumped
        assert _b64("hunter2") not in dumped
        assert "aHVudGVyMg" not in dumped


class TestSecretKeys:
    def test_data_then_stringdata_sorted(self) -> None:
        manifest: dict[str, object] = {
            "kind": "Secret",
            "data": {"zeta": "eg==", "alpha": "eg=="},
            "stringData": {"note": "x"},
        }
        assert secret_keys(manifest) == [
            ("alpha", "data"),
            ("zeta", "data"),
            ("note", "stringData"),
        ]

    def test_missing_sections(self) -> None:
        assert secret_keys({"kind": "Secret"}) == []
