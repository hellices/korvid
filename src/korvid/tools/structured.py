"""Reading and size-bounding structured (YAML) tool results.

A byte-level cut is fine for logs and reports, but it turns a manifest
into text that is no longer YAML: the model can no longer parse it, and
neither can the outbound policy, whose recursive redaction needs a
document (a manifest that arrives as wreckage is blocked, not sent).

Oversized structured results are therefore shrunk *structurally* — long
scalars clamped, long lists and mappings elided, deep subtrees replaced
by a marker — so the result is always a smaller but still valid document
that says where content was dropped.

A shrunk document still has to say what it is: `apiVersion`, `kind`,
`metadata` and the `name`/`namespace` that identify an object are kept
ahead of arbitrary entries at every step, so what survives is a smaller
view of a nameable object rather than an anonymous pile of fields.

Reading one back has the mirror-image requirement. Redaction decides
what is secret from what the document *says* — `kind: Secret`, an env
entry's `name` — so a document that can be read two ways is a document
whose classifiers can be erased. `load_structured_document` is the one
reader used wherever a structured result is parsed on its way out, and
it refuses the constructs that give a document a second reading.
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

#: Keys that say what an object *is*, kept ahead of arbitrary entries at
#: every rung of the ladder and in this order. Nothing requires a manifest
#: to list them first — a converted or patched object can carry extension
#: fields ahead of its own identity — and a reduction that keeps whichever
#: entries came first would then return an anonymous pile of fields, at a
#: size that fits, so the identity fallback never runs (PR #197 review).
#: `name` and `namespace` cover `metadata` and the list shapes (containers,
#: env, ports) whose entries are identified the same way.
_IDENTITY_KEYS: tuple[str, ...] = ("apiVersion", "kind", "metadata", "name", "namespace")

#: Prefix the executor puts in front of a failed tool result. The outbound
#: boundary uses it to tell an executor error from a document, so a real
#: document must never be able to start with it.
ERROR_PREFIX = "ERROR:"

#: Refusal messages. Constants, and deliberately vague about *which* key
#: or anchor: a refusal that quoted the document would carry the content
#: the reader exists to withhold into an error the model gets to see.
_REPEATED_KEY = "a document must not repeat a mapping key"
_ANCHOR_REFERENCE = "a document must not reference an anchor"


class StructuredParseError(yaml.YAMLError):
    """A document could not be read as saying exactly one thing.

    A `yaml.YAMLError` so that every existing handler already fails
    closed on it: a caller that only knows "this text is not a document"
    treats an ambiguous one the same way, and a caller that wants the
    distinction catches this type first.
    """


class _UnambiguousLoader(yaml.SafeLoader):
    """`SafeLoader` that refuses documents with more than one reading."""

    def __init__(self, stream: str) -> None:
        self._composed: set[yaml.Node] = set()
        super().__init__(stream)

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node | None:
        node = super().compose_node(parent, index)
        # Every node is composed exactly once; an alias returns a node
        # that was composed before, which is how the reference shows up
        # here without reaching into the event stream.
        if node is not None:
            if node in self._composed:
                raise StructuredParseError(_ANCHOR_REFERENCE)
            self._composed.add(node)
        return node

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        mapping = super().construct_mapping(node, deep=deep)
        # `node.value` holds every entry the document wrote, with `<<`
        # merges already flattened into it by `SafeConstructor`, so a
        # shorter mapping means two of them landed on one key.
        if len(mapping) != len(node.value):
            raise StructuredParseError(_REPEATED_KEY)
        return mapping


def load_structured_document(text: str) -> Any:
    """Read one structured document, refusing any second reading of it.

    `yaml.safe_load` resolves a repeated mapping key to the last one
    written, and an alias to the node it names — both silently. Either
    erases what redaction reads: `kind: Secret` followed by `kind:
    ConfigMap` loads as a ConfigMap whose `data` still holds the
    credentials, and a second `name` in an env entry frees its sibling
    `value` (PR #197 review). An alias is refused for the same reason
    from the other end — one node reachable at many paths is copied at
    each of them, so a few hundred characters of nested aliases expand
    into millions of nodes before anything is sent.

    Anchors nobody references are fine; it is the reference that makes a
    document say something other than what is written where it is read.

    Args:
        text: The document as it arrived.

    Returns:
        Whatever the document parses to — including `None` for an empty
        one. Which shapes are acceptable is the caller's rule.

    Raises:
        StructuredParseError: for a key repeated at any depth (including
            keys YAML reads as one value, like `yes` and `true`, or a
            `<<` merge that overrides an entry) or an anchor reference.
        yaml.YAMLError: for text that is not one valid YAML document.
    """
    loader = _UnambiguousLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]  # PyYAML leaves Reader.dispose unannotated


class _NoAliasDumper(yaml.SafeDumper):
    """`SafeDumper` that writes a shared node out at each place it appears.

    Serializing a subtree that appears twice as an anchor and an alias is
    smaller, but `load_structured_document` refuses an alias — so what
    korvid produces would be a document korvid's own boundary blocks.
    Redaction copies every node it walks, so this only ever costs size on
    a document that was not redacted.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_yaml(document: Any) -> str:
    """Serialize a document with the canonical structured-result options.

    Keys are sorted, so a document whose first key sorts before
    `apiVersion` (`ERROR`, on a CRD) would otherwise serialize into
    something indistinguishable from an executor error and skip the
    structured redaction pipeline. Such a document is emitted with an
    explicit `---` document start: same parse, unambiguous prefix.
    """
    text = yaml.dump(
        document,
        Dumper=_NoAliasDumper,
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


def _identity_first(value: Mapping[Any, Any]) -> list[tuple[Any, Any]]:
    """The mapping's entries, identity first, everything else in order.

    Deterministic: the reserved keys keep their canonical order and the
    rest keep the document's.
    """
    reserved = [(key, value[key]) for key in _IDENTITY_KEYS if key in value]
    rest = [(key, item) for key, item in value.items() if key not in _IDENTITY_KEYS]
    return reserved + rest


def _shrink_mapping(
    value: Mapping[Any, Any],
    *,
    scalars: int,
    entries: int,
    depth: int,
) -> dict[Any, Any]:
    items = _identity_first(value)
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
