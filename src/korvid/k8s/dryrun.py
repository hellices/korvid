"""Structural manifest diff for dry-run write previews (issue #19).

``diff_manifests`` compares the live object with the object a server-side
``dryRun=All`` write would produce and renders the changes as compact
``~/+/-`` lines (``~ spec.replicas: 3 -> 5``). Server bookkeeping noise
(``status``, ``metadata.resourceVersion``, ...) is excluded so the preview
shows only user-visible intent. Pure functions: no I/O, no client types.
"""

from __future__ import annotations

import json
from typing import Any

#: Paths whose changes are server bookkeeping, not user-visible intent.
_IGNORED_PATHS = frozenset(
    {
        "status",
        "metadata.resourceVersion",
        "metadata.generation",
        "metadata.managedFields",
        "metadata.creationTimestamp",
    }
)

_MAX_VALUE_CHARS = 60
DEFAULT_MAX_LINES = 8


def _fmt(value: Any) -> str:
    """Compact JSON-ish rendering of a leaf value, truncated for the dialog."""
    try:
        text = json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        text = text[:_MAX_VALUE_CHARS] + "..."
    return text


def _leaves(prefix: str, value: Any, sign: str, out: list[str]) -> None:
    """One line per leaf of an added/removed subtree."""
    if prefix in _IGNORED_PATHS:
        return
    if isinstance(value, dict) and value:
        for key in sorted(value, key=str):
            _leaves(f"{prefix}.{key}", value[key], sign, out)
    else:
        out.append(f"{sign} {prefix}: {_fmt(value)}")


def _leaf_equal(current: Any, proposed: Any) -> bool:
    """Equality with JSON scalar-type semantics: Python conflates bool with
    int (``True == 1``), but a CRD field changed by admission from a boolean
    to an integer is a real change the preview must show. Recurses into
    containers so atomically-compared lists get the same treatment."""
    if isinstance(current, bool) != isinstance(proposed, bool):
        return False
    if isinstance(current, list) and isinstance(proposed, list):
        return len(current) == len(proposed) and all(
            _leaf_equal(a, b) for a, b in zip(current, proposed, strict=True)
        )
    if isinstance(current, dict) and isinstance(proposed, dict):
        return set(current) == set(proposed) and all(
            _leaf_equal(value, proposed[key]) for key, value in current.items()
        )
    return bool(current == proposed)


def _walk(prefix: str, current: Any, proposed: Any, out: list[str]) -> None:
    if prefix in _IGNORED_PATHS:
        return
    if isinstance(current, dict) and isinstance(proposed, dict):
        for key in sorted(set(current) | set(proposed), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in proposed:
                _leaves(path, current[key], "-", out)
            elif key not in current:
                _leaves(path, proposed[key], "+", out)
            else:
                _walk(path, current[key], proposed[key], out)
        return
    if not _leaf_equal(current, proposed):
        # Lists (and type changes) compare atomically: positional list diffs
        # are noisier than helpful in a confirmation dialog.
        out.append(f"~ {prefix}: {_fmt(current)} -> {_fmt(proposed)}")


def diff_manifests(
    current: dict[str, Any],
    proposed: dict[str, Any],
    *,
    max_lines: int = DEFAULT_MAX_LINES,
) -> list[str]:
    """Changed paths between two manifests as ``~/+/-`` lines.

    Empty when the objects are equivalent (after excluding bookkeeping
    paths); truncated to ``max_lines`` with a ``... (+N more changes)``
    summary line so the approval dialog stays readable.
    """
    out: list[str] = []
    _walk("", current, proposed, out)
    if len(out) > max_lines:
        extra = len(out) - max_lines
        out = [*out[:max_lines], f"... (+{extra} more changes)"]
    return out
