"""Component references for the hierarchy tree (issue #120).

Three cluster-side sources describe what a helm release or an OLM operator
installed, and all of them reduce to (kind, name, namespace) rows:

- helm: the decoded release payload's rendered ``manifest`` (multi-doc YAML)
- OLM Operator objects: ``status.components.refs[]`` object references
- OLM InstallPlan objects: ``status.plan[]`` step resources

Every input is cluster-controlled, so the parsers are fail-soft: malformed
documents, entries, and steps are skipped (never raised), input size and
entry counts are capped, and duplicates are folded.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import yaml

#: Upper bound on the documents/entries/steps *inspected* per source. Real
#: charts and operators install tens of objects; bounding the input (not the
#: accepted rows) keeps a hostile payload of malformed or duplicate entries
#: from buying unbounded parse work.
MAX_COMPONENT_DOCS = 500

#: Upper bound on the rendered-manifest text handed to the YAML parser. The
#: helm payload is already capped at decompression (``MAX_PAYLOAD_BYTES``);
#: this narrower cap bounds parse work for the one field the tree reads.
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class ComponentRef:
    """One installed object as declared by its manager.

    ``namespace`` is exactly what the source declared - charts routinely
    omit ``metadata.namespace`` for objects installed into the release
    namespace, and cluster-scoped kinds never carry one, so the empty
    string means "unspecified" and the caller resolves it against
    discovery data.
    """

    kind: str
    name: str
    api_version: str = ""
    namespace: str = ""


def _scalar(value: Any) -> str:
    """The value as a string when it is a YAML scalar, else ''."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return str(value)
    return ""


def _append(refs: list[ComponentRef], seen: set[ComponentRef], ref: ComponentRef) -> None:
    if ref not in seen:
        seen.add(ref)
        refs.append(ref)


def manifest_components(manifest: Any) -> list[ComponentRef]:
    """Component refs from a helm rendered ``manifest`` payload field.

    Documents are split on ``---`` separator lines and parsed one by one so
    a single malformed document cannot hide the rest. Docs without a kind
    or ``metadata.name`` are skipped; oversize input yields nothing.
    """
    if (
        not isinstance(manifest, str)
        or len(manifest.encode("utf-8", "replace")) > MAX_MANIFEST_BYTES
    ):
        return []
    refs: list[ComponentRef] = []
    seen: set[ComponentRef] = set()
    for chunk in _split_docs(manifest, MAX_COMPONENT_DOCS):
        try:
            doc = yaml.safe_load(chunk)
        except (yaml.YAMLError, RecursionError):
            # RecursionError: pathologically nested flow collections blow the
            # interpreter stack inside the parser - skip like any bad doc.
            continue
        if not isinstance(doc, dict):
            continue
        kind = _scalar(doc.get("kind"))
        meta = doc.get("metadata")
        if not isinstance(meta, dict):
            continue
        name = _scalar(meta.get("name"))
        if not kind or not name:
            continue
        ref = ComponentRef(
            kind=kind,
            name=name,
            api_version=_scalar(doc.get("apiVersion")),
            namespace=_scalar(meta.get("namespace")),
        )
        _append(refs, seen, ref)
    return refs


def _doc_start_rest(line: str) -> str | None:
    """None when *line* is not a ``---`` document marker; else the content
    following the marker ('' for the bare and comment-trailed forms). Any
    remainder is the next document's first content per YAML."""
    stripped = line.strip()
    if stripped == "---":
        return ""
    if stripped.startswith("--- "):
        rest = stripped[4:].lstrip()
        return "" if rest.startswith("#") else rest
    return None


def _split_docs(manifest: str, limit: int) -> Iterator[str]:
    """Lazily split a multi-doc YAML string on ``---`` separator lines,
    yielding at most *limit* documents - a separator-only payload must not
    materialize documents past the inspection cap."""
    produced = 0
    current: list[str] = []
    for line in io.StringIO(manifest):
        rest = _doc_start_rest(line.rstrip("\n"))
        if rest is not None:
            yield "\n".join(current)
            produced += 1
            if produced >= limit:
                return
            current = [rest] if rest else []
        else:
            current.append(line.rstrip("\n"))
    yield "\n".join(current)


def reference_components(entries: Any) -> list[ComponentRef]:
    """Component refs from a list of object references (Operator
    ``status.components.refs[]`` shape: apiVersion/kind/name/namespace)."""
    if not isinstance(entries, list):
        return []
    refs: list[ComponentRef] = []
    seen: set[ComponentRef] = set()
    for entry in entries[:MAX_COMPONENT_DOCS]:
        if not isinstance(entry, dict):
            continue
        kind = _scalar(entry.get("kind"))
        name = _scalar(entry.get("name"))
        if not kind or not name:
            continue
        ref = ComponentRef(
            kind=kind,
            name=name,
            api_version=_scalar(entry.get("apiVersion")),
            namespace=_scalar(entry.get("namespace")),
        )
        _append(refs, seen, ref)
    return refs


def installplan_components(steps: Any) -> list[ComponentRef]:
    """Component refs from InstallPlan ``status.plan[]`` steps (each step's
    ``resource`` carries group/version/kind/name)."""
    if not isinstance(steps, list):
        return []
    refs: list[ComponentRef] = []
    seen: set[ComponentRef] = set()
    for step in steps[:MAX_COMPONENT_DOCS]:
        if not isinstance(step, dict):
            continue
        resource = step.get("resource")
        if not isinstance(resource, dict):
            continue
        kind = _scalar(resource.get("kind"))
        name = _scalar(resource.get("name"))
        if not kind or not name:
            continue
        group = _scalar(resource.get("group"))
        version = _scalar(resource.get("version"))
        api_version = (f"{group}/{version}" if group else version) if version else ""
        ref = ComponentRef(
            kind=kind,
            name=name,
            api_version=api_version,
            namespace=_scalar(step.get("namespace") or resource.get("namespace")),
        )
        _append(refs, seen, ref)
    return refs
