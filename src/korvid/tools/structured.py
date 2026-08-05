"""Size-bounding for structured (YAML) tool results.

A byte-level cut is fine for logs and reports, but it turns a manifest
into text that is no longer YAML: the model can no longer parse it, and
neither can the outbound policy, whose recursive redaction needs a
document (a manifest that arrives as wreckage is blocked, not sent).

Oversized structured results are therefore shrunk *structurally* — long
scalars clamped, long lists and mappings elided, deep subtrees replaced
by a marker — so the result is always a smaller but still valid document
that says where content was dropped.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml

#: Placed where a subtree was dropped entirely.
ELISION = "… [elided — result budget]"

#: Key that carries the "n more keys elided" note inside a shrunk mapping.
_ELIDED_KEY = "… elided"

#: Progressive reduction ladder — (scalar chars, container entries, depth).
#: Each step is tried in order and the first one that fits is used, so a
#: result only loses as much detail as its size actually requires.
_REDUCTION_STEPS: tuple[tuple[int, int, int], ...] = (
    (4_096, 200, 24),
    (1_024, 40, 16),
    (256, 12, 10),
    (80, 6, 6),
    (40, 3, 4),
)

_TRAILING_ELLIPSIS = "…"

#: Prefix the executor puts in front of a failed tool result. The outbound
#: boundary uses it to tell an executor error from a document, so a real
#: document must never be able to start with it.
ERROR_PREFIX = "ERROR:"


def dump_yaml(document: Any) -> str:
    """Serialize a document with the canonical structured-result options.

    Keys are sorted, so a document whose first key sorts before
    `apiVersion` (`ERROR`, on a CRD) would otherwise serialize into
    something indistinguishable from an executor error and skip the
    structured redaction pipeline. Such a document is emitted with an
    explicit `---` document start: same parse, unambiguous prefix.
    """
    text = yaml.safe_dump(
        document,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=True,
    ).rstrip()
    if text.startswith(ERROR_PREFIX):
        return f"---\n{text}"
    return text


def dump_bounded_yaml(document: Any, limit: int) -> str:
    """Serialize `document` as YAML that stays parseable within `limit`.

    The unmodified document is used whenever it fits. Otherwise the
    reduction ladder is applied until one step fits; if even the smallest
    step is too large, only the object's identity plus an elision note is
    returned.

    Args:
        document: Any YAML-serializable value (normally a manifest).
        limit: Character budget for the serialized result.

    Returns:
        Valid YAML text, at most `limit` characters unless the minimal
        identity notice itself is longer (documents are never returned
        as unparsable fragments to satisfy a byte bound).
    """
    text = dump_yaml(document)
    if len(text) <= limit:
        return text
    for scalars, entries, depth in _REDUCTION_STEPS:
        text = dump_yaml(_shrink(document, scalars=scalars, entries=entries, depth=depth))
        if len(text) <= limit:
            return text
    return dump_yaml(_identity_summary(document))


def _clamp(value: str, scalars: int) -> str:
    if len(value) <= scalars:
        return value
    return value[: max(scalars - 1, 0)] + _TRAILING_ELLIPSIS


def _shrink(value: Any, *, scalars: int, entries: int, depth: int) -> Any:
    if isinstance(value, str):
        return _clamp(value, scalars)
    if isinstance(value, Mapping):
        if depth <= 0:
            return ELISION
        return _shrink_mapping(value, scalars=scalars, entries=entries, depth=depth)
    if isinstance(value, list):
        if depth <= 0:
            return ELISION
        kept = [
            _shrink(item, scalars=scalars, entries=entries, depth=depth - 1)
            for item in value[:entries]
        ]
        if len(value) > entries:
            kept.append(f"… {len(value) - entries} more items elided — result budget")
        return kept
    return value


def _shrink_mapping(
    value: Mapping[Any, Any],
    *,
    scalars: int,
    entries: int,
    depth: int,
) -> dict[Any, Any]:
    items = list(value.items())
    result: dict[Any, Any] = {
        key: _shrink(item, scalars=scalars, entries=entries, depth=depth - 1)
        for key, item in items[:entries]
    }
    if len(items) > entries:
        result[_ELIDED_KEY] = f"{len(items) - entries} more keys elided — result budget"
    return result


def _identity_summary(document: Any) -> dict[str, Any]:
    """Last resort: what the object *is*, plus a note that the body is gone."""
    summary: dict[str, Any] = {}
    if isinstance(document, Mapping):
        for key in ("apiVersion", "kind"):
            item = document.get(key)
            if isinstance(item, str):
                summary[key] = _clamp(item, 253)
        metadata = document.get("metadata")
        if isinstance(metadata, Mapping):
            identity = {
                key: _clamp(metadata[key], 253)
                for key in ("name", "namespace")
                if isinstance(metadata.get(key), str)
            }
            if identity:
                summary["metadata"] = identity
    summary["truncated"] = ELISION
    return summary
