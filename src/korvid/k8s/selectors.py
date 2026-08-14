"""Shared Kubernetes label selector semantics (issue #281).

This module provides immutable selector data-classes and pure functions that
implement the Kubernetes ``LabelSelector`` API-machinery semantics used across
the codebase (drain planning, operational relationship graph, etc.).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectorExpression:
    """One entry in ``matchExpressions``."""

    key: str
    operator: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LabelSelector:
    """Parsed representation of a Kubernetes ``LabelSelector``.

    Attributes:
        match_labels: Sorted key-value pairs from ``matchLabels``.
        match_expressions: Parsed ``matchExpressions`` entries.
        present: ``True`` when the selector was explicitly present in the
            manifest (even if empty); ``False`` when the field was absent
            (``None`` / non-mapping input).  Callers use this to distinguish
            an absent selector from an explicitly empty one.
    """

    match_labels: tuple[tuple[str, str], ...] = ()
    match_expressions: tuple[SelectorExpression, ...] = ()
    present: bool = False


def parse_label_selector(raw: object) -> LabelSelector:
    """Parse a raw ``LabelSelector`` mapping into a :class:`LabelSelector`.

    Args:
        raw: The value of a ``spec.selector`` field from a Kubernetes manifest
            (typically ``dict[str, Any]``).  A non-mapping value (including
            ``None``) is treated as an absent selector (``present=False``).

    Returns:
        A :class:`LabelSelector` instance.
    """
    if not isinstance(raw, Mapping):
        return LabelSelector()
    raw_labels = raw.get("matchLabels")
    labels: tuple[tuple[str, str], ...] = (
        tuple(sorted((str(key), str(value)) for key, value in raw_labels.items()))
        if isinstance(raw_labels, Mapping)
        else ()
    )
    raw_expressions = raw.get("matchExpressions")
    expressions: list[SelectorExpression] = []
    if isinstance(raw_expressions, list):
        for item in raw_expressions:
            if not isinstance(item, Mapping):
                continue
            key = item.get("key")
            operator = item.get("operator")
            values = item.get("values")
            if not isinstance(key, str) or not isinstance(operator, str):
                continue
            expressions.append(
                SelectorExpression(
                    key,
                    operator,
                    tuple(str(v) for v in values) if isinstance(values, list) else (),
                )
            )
    return LabelSelector(labels, tuple(expressions), present=True)


def _expression_matches(expr: SelectorExpression, labels: Mapping[str, str]) -> bool:
    """Evaluate one ``matchExpressions`` entry against a pod's labels.

    Args:
        expr: The parsed selector expression.
        labels: The pod's label set.

    Returns:
        ``True`` if the expression matches according to Kubernetes semantics.
        Unknown operators return ``False`` (fail-safe for the graph; callers
        that want over-warning behaviour must handle that themselves).
    """
    if expr.operator == "In":
        return labels.get(expr.key) in expr.values
    if expr.operator == "NotIn":
        # apimachinery semantics: a pod *without* the key matches NotIn.
        return labels.get(expr.key) not in expr.values
    if expr.operator == "Exists":
        return expr.key in labels
    if expr.operator == "DoesNotExist":
        return expr.key not in labels
    return False


def matches_selector(
    selector: LabelSelector,
    labels: Mapping[str, str],
    *,
    empty_matches: bool,
) -> bool:
    """Test whether *labels* are selected by *selector*.

    Args:
        selector: A parsed :class:`LabelSelector`.
        labels: The pod's (or resource's) label set.
        empty_matches: What an explicitly empty selector (``present=True``
            with no ``matchLabels`` / ``matchExpressions``) returns.  Pass
            ``True`` for ``policy/v1`` PDB semantics (empty selector selects
            every pod in the namespace) and ``False`` for ``policy/v1beta1``
            semantics (empty selector selects no pods).

    Returns:
        ``False`` for an absent selector (``present=False``) regardless of
        *empty_matches*.  For a present selector, returns ``True`` only when
        every ``matchLabels`` entry and every ``matchExpressions`` entry
        matches.  An empty present selector returns *empty_matches*.
    """
    if not selector.present:
        return False
    if not selector.match_labels and not selector.match_expressions:
        return empty_matches
    for key, value in selector.match_labels:
        if labels.get(key) != value:
            return False
    return all(_expression_matches(expr, labels) for expr in selector.match_expressions)
