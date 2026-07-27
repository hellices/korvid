"""Custom table columns from config (issue #45).

Pure extraction: a `CustomColumn` names a value source inside a resource
manifest — a label, an annotation, or a minimal JSONPath subset — and
`evaluate` turns it into a display string. Missing values render `<none>`;
anything unexpected renders `<err>` so a bad expression can never crash the
render loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

#: Value sources a column may declare in config.
SOURCES = ("label", "annotation", "jsonpath")

MISSING = "<none>"
ERROR = "<err>"


@dataclass(frozen=True)
class CustomColumn:
    """One user-declared column: header name + where its value comes from."""

    name: str
    source: str  # one of SOURCES
    expr: str  # label/annotation key, or a JSONPath expression


@lru_cache(maxsize=256)
def parse_jsonpath(expr: str) -> tuple[str | int, ...]:
    """Compile the supported JSONPath subset to path segments.

    Supported: leading-dot key paths with optional non-negative integer
    indexes — `.spec.containers[0].image`. Filters, wildcards, slices, and
    quoted keys are not (labels/annotations have their own sources).

    Cached: `evaluate` runs per row and per watch event, and the distinct
    expressions are bounded by config — recompiling would burn CPU on the
    watch fan-out hot path.

    Raises:
        ValueError: If the expression falls outside the subset.
    """
    if not expr or expr == ".":
        raise ValueError("jsonpath is empty")
    if not expr.startswith("."):
        raise ValueError(f"jsonpath must start with '.': {expr!r}")
    segments: list[str | int] = []
    for part in expr[1:].split("."):
        key, _, rest = part.partition("[")
        if not key:
            raise ValueError(f"jsonpath has an empty segment: {expr!r}")
        segments.append(key)
        while rest:
            index, bracket, rest = rest.partition("]")
            if not bracket or not index.isdigit():
                raise ValueError(f"jsonpath has a malformed index: {expr!r}")
            segments.append(int(index))
            if rest:
                if not rest.startswith("["):
                    raise ValueError(f"jsonpath has a malformed index: {expr!r}")
                rest = rest[1:]
    return tuple(segments)


def _walk(manifest: dict[str, Any], segments: tuple[str | int, ...]) -> Any:
    """The value at *segments*, or None when any step is missing/mismatched."""
    node: Any = manifest
    for segment in segments:
        if isinstance(segment, int):
            if not isinstance(node, list) or not 0 <= segment < len(node):
                return None
            node = node[segment]
        else:
            if not isinstance(node, dict):
                return None
            node = node.get(segment)
    return node


def _render(value: Any) -> str:
    """Display string for an extracted value; `<none>` for null/missing."""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "true" if value else "false"  # YAML/JSON style, not Python's
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(", ", ": "), default=str)
    return str(value)


def evaluate(column: CustomColumn, manifest: dict[str, Any]) -> str:
    """The display value of *column* for *manifest*; never raises."""
    try:
        if column.source == "label":
            meta = manifest.get("metadata") or {}
            return _render((meta.get("labels") or {}).get(column.expr))
        if column.source == "annotation":
            meta = manifest.get("metadata") or {}
            return _render((meta.get("annotations") or {}).get(column.expr))
        if column.source == "jsonpath":
            return _render(_walk(manifest, parse_jsonpath(column.expr)))
        logger.debug("unknown custom column source %r", column.source)
        return ERROR
    except Exception:
        logger.debug("custom column %s failed to evaluate", column.name, exc_info=True)
        return ERROR


def evaluate_all(columns: tuple[CustomColumn, ...], manifest: dict[str, Any]) -> tuple[str, ...]:
    """All column values for *manifest*, in declaration order."""
    return tuple(evaluate(column, manifest) for column in columns)
