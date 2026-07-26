"""Secret value decoding + masking helpers (issue #39, spec §5 #9, §7).

Pure functions used by the Secret viewer widget and by the describe paths
that must never surface raw or decoded secret material.
"""

from __future__ import annotations

import base64
import copy
import hashlib
from dataclasses import dataclass

#: Placeholder rendered instead of a secret value until the user reveals it.
MASK_PLACEHOLDER = "••••••"

#: kubectl's client-side apply stores the full original manifest — including
#: unmasked data/stringData — in this annotation.
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


@dataclass(frozen=True)
class RevealedValue:
    """A decoded secret value ready for display.

    `text` is either the decoded UTF-8 payload or a safe summary
    (binary digest / decode-error message); `binary` marks the latter
    two so callers never treat the summary as the real value.
    """

    text: str
    binary: bool = False


def reveal_value(raw: str, *, encoded: bool = True) -> RevealedValue:
    """Decode one secret value for display.

    Args:
        raw: The stored value — base64 for `data`, plaintext for `stringData`.
        encoded: False for `stringData` entries, which are not base64.

    Returns:
        The decoded text, or a size + sha256 summary for binary payloads,
        or an error note for invalid base64. Never raises.
    """
    if not encoded:
        return RevealedValue(text=raw, binary=False)
    try:
        payload = base64.b64decode(raw, validate=True)
    except (ValueError, TypeError):
        return RevealedValue(text="<invalid base64>", binary=True)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return _binary_summary(payload)
    # Non-printable characters (C0/C1 controls, DEL, …) other than common
    # whitespace mean the payload is not text — render the digest summary
    # instead of garbage.
    if any(ch not in "\t\n\r" and not ch.isprintable() for ch in text):
        return _binary_summary(payload)
    return RevealedValue(text=text, binary=False)


def _binary_summary(payload: bytes) -> RevealedValue:
    digest = hashlib.sha256(payload).hexdigest()
    return RevealedValue(text=f"<binary {len(payload)} bytes, sha256={digest}>", binary=True)


def secret_keys(manifest: dict[str, object]) -> list[tuple[str, str]]:
    """List a Secret's entries as `(key, section)` pairs.

    `data` keys come first, then `stringData`, each sorted by key so the
    viewer's row order is stable across refreshes.
    """
    result: list[tuple[str, str]] = []
    for section in ("data", "stringData"):
        entries = manifest.get(section)
        if isinstance(entries, dict):
            result.extend((str(key), section) for key in sorted(entries))
    return result


def mask_secret_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Return a copy of a Secret manifest safe to render or share.

    Every `data`/`stringData` value becomes `MASK_PLACEHOLDER` and the
    last-applied-configuration annotation (which embeds the unmasked
    manifest) is stripped. The input is not mutated.
    """
    result = copy.deepcopy(manifest)
    meta = result.get("metadata")
    if isinstance(meta, dict):
        annotations = meta.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop(_LAST_APPLIED, None)
    for section in ("data", "stringData"):
        entries = result.get(section)
        if isinstance(entries, dict):
            for key in entries:
                entries[key] = MASK_PLACEHOLDER
    return result
