"""Recursive redaction of cluster data that leaves korvid (issue #189).

Cluster data is recognized as sensitive by its *structure*: `kind:
Secret` is what says `data` holds credentials, and an env entry's `name`
is what says its sibling `value` does. Anything that drops or shortens
those classifiers — a size bound that elides mapping entries, a clamp
that cuts a long name, a projection — destroys the only evidence a later
filter could use. Redaction therefore has to run where a document is
produced, before any other transformation, which is why it lives here as
a pure primitive instead of inside one consumer.

Every path that hands cluster data to something outside korvid shares
this one implementation: the agent tool executor (and the MCP server
that dispatches through it) redact documents as they are produced, and
the outbound provider policy redacts again on the way out, so a
misconfigured or third-party producer cannot become a hole.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from korvid.core.secrets import MASK_PLACEHOLDER, mask_secret_manifest

#: kubectl's client-side apply stores the full original manifest —
#: including unmasked data/stringData — in this annotation.
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

_SENSITIVE_NAMES = frozenset(
    {
        "password",
        "token",
        "apikey",
        "authorization",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "credentials",
    }
)
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
#: Longest sensitive name in words (`access` + `token`) plus one word of
#: slack — bounds the window scan over a hostile, very long key.
_MAX_NAME_WINDOW = 3
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_DOUBLE_QUOTED_VALUE = r'"(?:\\.|[^"\\\r\n])*"'
_SINGLE_QUOTED_VALUE = r"'(?:\\.|[^'\\\r\n])*'"
_AUTHORIZATION_RE = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9])"
    r"(?P<auth_key_quote>[\"']?)authorization(?P=auth_key_quote)\s*[:=]\s*)"
    rf"(?P<value>{_DOUBLE_QUOTED_VALUE}|{_SINGLE_QUOTED_VALUE}|"
    r"(?:(?:bearer|basic)\s+)?[^\s,;}\]]+)"
)
_CREDENTIAL_RE = re.compile(
    r"(?im)(?P<prefix>(?<![A-Za-z0-9])(?P<credential_key_quote>[\"']?)(?:"
    r"password|api[\s_-]?key|client[\s_-]?secret|access[\s_-]?token|"
    r"refresh[\s_-]?token|credentials|token"
    r")(?P=credential_key_quote)\s*[:=]\s*)"
    rf"(?P<value>{_DOUBLE_QUOTED_VALUE}|{_SINGLE_QUOTED_VALUE}|[^\s,;}}\]]+)"
)


class RedactionError(ValueError):
    """Data could not be redacted safely, so it must not be handed on.

    Raised for shapes the redactor cannot reason about: non-string
    mapping keys, cycles, unsupported types. Callers fail closed on it —
    unredactable data is blocked, never forwarded as it arrived.
    """


@dataclass(frozen=True)
class RedactionRecord:
    """One deterministic change made while redacting."""

    path: str
    reason: str


def normalize_name(value: str) -> str:
    """Fold a name to its comparable form: casefolded, alphanumerics only."""
    return "".join(character for character in value.casefold() if character.isalnum())


def denotes_secret(value: str) -> bool:
    """True when consecutive words of `value` spell a credential name.

    Kubernetes names are compounds (`DB_PASSWORD`, `dbPassword`,
    `github-access-token`), so exact normalization alone never recognizes
    them; splitting into words and scanning short windows does, without
    matching unrelated names that merely start the same (`TOKENIZER_PATH`).
    """
    words = tuple(word.casefold() for word in _WORD_RE.findall(value))
    return any(
        "".join(words[start : start + size]) in _SENSITIVE_NAMES
        for start in range(len(words))
        for size in range(1, min(_MAX_NAME_WINDOW, len(words) - start) + 1)
    )


def key_path(path: str, key: str) -> str:
    """Extend a redaction path with one mapping key."""
    if key.isidentifier():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key, ensure_ascii=False)}]"


def record(records: list[RedactionRecord], path: str, reason: str) -> None:
    """Append one redaction record."""
    records.append(RedactionRecord(path=path, reason=reason))


def strip_control_characters(text: str, path: str, records: list[RedactionRecord]) -> str:
    """Replace control characters, recording the change when there was one."""
    if not _CONTROL_RE.search(text):
        return text
    record(records, path, "control-character")
    return _CONTROL_RE.sub("\N{REPLACEMENT CHARACTER}", text)


def sanitize_mapping_key(key: str, path: str, records: list[RedactionRecord]) -> str:
    """Normalize a mapping key so it cannot smuggle control characters."""
    return strip_control_characters(key, path, records)


def _replace_match(
    match: re.Match[str],
    *,
    path: str,
    records: list[RedactionRecord],
    reason: str,
) -> str:
    record(records, path, reason)
    value = match.group("value")
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        replacement = f"{value[0]}{MASK_PLACEHOLDER}{value[-1]}"
    else:
        replacement = MASK_PLACEHOLDER
    return f"{match.group('prefix')}{replacement}"


def redact_text(text: str, path: str, records: list[RedactionRecord]) -> str:
    """Redact credential assignments embedded in free-form text."""
    text = strip_control_characters(text, path, records)
    text = _AUTHORIZATION_RE.sub(
        lambda match: _replace_match(
            match,
            path=path,
            records=records,
            reason="authorization-value",
        ),
        text,
    )
    return _CREDENTIAL_RE.sub(
        lambda match: _replace_match(
            match,
            path=path,
            records=records,
            reason="credential-assignment",
        ),
        text,
    )


def _secret_redactions(
    value: Mapping[str, Any],
    path: str,
    records: list[RedactionRecord],
) -> dict[str, Any]:
    try:
        masked = mask_secret_manifest(dict(value))
    except ValueError as exc:
        raise RedactionError(str(exc)) from exc
    for section in ("data", "stringData"):
        entries = value.get(section)
        if isinstance(entries, Mapping):
            for key in entries:
                record(records, key_path(key_path(path, section), str(key)), "secret-value")
    metadata = value.get("metadata")
    if isinstance(metadata, Mapping):
        annotations = metadata.get("annotations")
        if isinstance(annotations, Mapping) and LAST_APPLIED in annotations:
            record(
                records,
                key_path(key_path(key_path(path, "metadata"), "annotations"), LAST_APPLIED),
                "last-applied-configuration",
            )
    return masked


def _names_a_secret_sibling(value: Mapping[str, Any]) -> bool:
    """True for a `{"name": "DB_PASSWORD", "value": ...}` pair.

    Kubernetes carries container environment variables (and several
    similar list shapes) as sibling `name`/`value` keys, so the credential
    word lives in a *value*, not a key — a key-name rule alone never sees
    it and the secret ships in the sibling.
    """
    name = value.get("name")
    return isinstance(name, str) and "value" in value and denotes_secret(name)


def _mask_reason(key: str, item: Any, *, secret_sibling: bool) -> str | None:
    """Why this entry must be masked, or None to redact it normally."""
    if secret_sibling and key == "value":
        # The name is the classifier, so the whole sibling goes — the API
        # types `value` as a string, and a mapping or list here is
        # malformed or hostile: descending into it would ship the parts
        # under keys that say nothing about what they hold.
        return "sensitive-env-value"
    if normalize_name(key) in _SENSITIVE_NAMES:
        return "sensitive-key"
    # Compound keys (`dbPassword`, `admin-api-key`) name the credential
    # their value holds, so the value goes whatever type it arrived as: a
    # mapping or list would otherwise be descended into and shipped under
    # inner keys that name nothing, and a number can be a PIN. The one
    # exception is a bool, which carries a single bit and no secret — a
    # flag like `automountServiceAccountToken: true` names a credential
    # without holding one, and masking it would lose real information.
    if not isinstance(item, bool) and denotes_secret(key):
        return "sensitive-key"
    return None


def _redact_mapping(
    value: Mapping[Any, Any],
    path: str,
    records: list[RedactionRecord],
    active: set[int],
) -> dict[str, Any]:
    for key in value:
        if not isinstance(key, str):
            raise RedactionError("mapping keys must be strings")
    source: Mapping[str, Any] = value
    kind = source.get("kind")
    if isinstance(kind, str) and normalize_name(kind) == "secret":
        source = _secret_redactions(source, path, records)
    secret_sibling = _names_a_secret_sibling(source)

    result: dict[str, Any] = {}
    for key, item in source.items():
        item_path = key_path(path, key)
        if key == LAST_APPLIED:
            record(records, item_path, "last-applied-configuration")
            continue
        output_key = sanitize_mapping_key(key, item_path, records)
        if output_key in result:
            raise RedactionError("redacted mapping keys must remain unique")
        reason = _mask_reason(key, item, secret_sibling=secret_sibling)
        if reason is not None:
            result[output_key] = MASK_PLACEHOLDER
            record(records, item_path, reason)
            continue
        result[output_key] = _redact_value(item, item_path, records, active)
    return result


def _redact_value(
    value: Any,
    path: str,
    records: list[RedactionRecord],
    active: set[int],
) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RedactionError("non-finite numbers are not allowed")
        return value
    if isinstance(value, str):
        return redact_text(value, path, records)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise RedactionError("recursive data structures are not allowed")
        active.add(identity)
        try:
            return _redact_mapping(value, path, records, active)
        finally:
            active.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in active:
            raise RedactionError("recursive data structures are not allowed")
        active.add(identity)
        try:
            return [
                _redact_value(item, f"{path}[{index}]", records, active)
                for index, item in enumerate(value)
            ]
        finally:
            active.remove(identity)
    raise RedactionError("unsupported outbound data type")


def redact_value(
    value: Any,
    path: str,
    records: list[RedactionRecord],
    active: set[int] | None = None,
) -> Any:
    """Return a redacted copy of one JSON/YAML-shaped value.

    Args:
        value: Any document node — mapping, list, string, or number.
        path: Path prefix used in the redaction records.
        records: Accumulator the records are appended to.
        active: Identities of the containers currently being redacted,
            for cycle detection. Defaults to a fresh set.

    Raises:
        RedactionError: for a cycle, a non-string mapping key, a
            non-finite number, or a type the redactor cannot inspect.
    """
    return _redact_value(value, path, records, set() if active is None else active)


def redact_document(document: Any, *, path: str = "document") -> tuple[Any, list[RedactionRecord]]:
    """Redact a whole document and return it with its redaction records."""
    records: list[RedactionRecord] = []
    return redact_value(document, path, records), records


def redact_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Redact a Kubernetes manifest, discarding the record trail.

    The producer-side entry point. A manifest must arrive here *before*
    it is summarized or size-bounded: those steps remove the `kind` and
    `name` classifiers this function needs to recognize what is secret.

    Raises:
        RedactionError: if the manifest cannot be redacted safely.
    """
    redacted, _ = redact_document(manifest, path="manifest")
    if not isinstance(redacted, dict):
        raise RedactionError("a manifest must redact to a mapping")
    return redacted
