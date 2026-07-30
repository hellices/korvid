"""Hierarchy tree for helm releases and operators (issue #120).

`build_hierarchy` turns the component refs extracted from a release manifest
(or an operator's `status.components.refs`) into a display tree, expanding
workload components into their live runtime descendants (Deployment →
ReplicaSets → Pods) via ownerReferences against the resource store.

`HierarchyScreen` renders that tree read-only: Enter on a navigable node
resolves to ``("goto", kind, namespace, name)`` (the app jumps to that
object's view), ``d`` to ``("describe", ...)``, Escape to ``None``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, Tree
from textual.widgets.tree import TreeNode

from korvid.k8s.components import ComponentRef

#: Runtime ownership chain expanded under manifest components. Only the
#: workload kinds whose children the store already watches; anything else
#: renders as a leaf.
_RUNTIME_CHILDREN: dict[str, tuple[str, str]] = {
    "deployments": ("replicasets", "ReplicaSet"),
    "replicasets": ("pods", "Pod"),
    "statefulsets": ("pods", "Pod"),
    "daemonsets": ("pods", "Pod"),
    "jobs": ("pods", "Pod"),
}


@dataclass
class HierarchyNode:
    """One tree row. ``kind`` is the canonical plural view name; empty when
    the object has no discovered view (not navigable, display-only)."""

    label: str
    kind: str = ""
    namespace: str = ""
    name: str = ""
    children: list[HierarchyNode] = field(default_factory=list)


def _live_index(
    lookup: Callable[[str, str], list[Any] | None],
    cache: dict[tuple[str, str], dict[str, Any] | None],
    view: str,
    namespace: str,
) -> dict[str, Any] | None:
    """name -> live object for one (view, namespace), or None when the view
    is not watched. Built once per tree so many same-kind refs don't rescan
    (and re-sort) the same bucket."""
    key = (view, namespace)
    if key not in cache:
        bucket = lookup(view, namespace)
        cache[key] = (
            None
            if bucket is None
            else {
                str(getattr(obj, "name", "")): obj
                for obj in bucket
                if str(getattr(obj, "namespace", "") or "") == namespace
            }
        )
    return cache[key]


def _owner_index(
    lookup: Callable[[str, str], list[Any] | None],
    cache: dict[tuple[str, str], dict[tuple[str, str], list[Any]]],
    view: str,
    namespace: str,
) -> dict[tuple[str, str], list[Any]]:
    """(namespace, owner uid) -> children for one view, built once per tree."""
    key = (view, namespace)
    index = cache.get(key)
    if index is None:
        index = {}
        for obj in lookup(view, namespace) or []:
            ns = str(getattr(obj, "namespace", "") or "")
            for owner_uid in getattr(obj, "owner_uids", ()) or ():
                index.setdefault((ns, str(owner_uid)), []).append(obj)
        cache[key] = index
    return index


def _runtime_children(
    view: str,
    parent_uid: str,
    namespace: str,
    lookup: Callable[[str, str], list[Any] | None],
    cache: dict[tuple[str, str], dict[tuple[str, str], list[Any]]],
) -> list[HierarchyNode]:
    """Live descendants of one object via ownerReferences, recursively."""
    step = _RUNTIME_CHILDREN.get(view)
    if step is None or not parent_uid:
        return []
    child_view, child_kind = step
    nodes: list[HierarchyNode] = []
    index = _owner_index(lookup, cache, child_view, namespace)
    for obj in index.get((namespace, parent_uid), []):
        name = str(getattr(obj, "name", ""))
        nodes.append(
            HierarchyNode(
                label=f"{child_kind}/{name}",
                kind=child_view,
                namespace=namespace,
                name=name,
                children=_runtime_children(
                    child_view, str(getattr(obj, "uid", "")), namespace, lookup, cache
                ),
            )
        )
    return nodes


def build_hierarchy(
    root_label: str,
    refs: Sequence[ComponentRef],
    *,
    namespace: str,
    resolve: Callable[[ComponentRef], tuple[str, bool] | None],
    lookup: Callable[[str, str], list[Any] | None],
) -> HierarchyNode:
    """Component refs → display tree rooted at *root_label*.

    ``resolve`` maps a component ref to its canonical plural view name plus
    whether the kind is namespaced (None when undiscovered — the node then
    renders display-only). ``lookup(view, namespace)`` returns the live
    summaries from a watch covering that namespace, or None when no watch
    feeds the view right now; a component missing from a *watched* view is
    marked "(missing)" but stays navigable so describe can surface the 404
    explanation.
    """
    root = HierarchyNode(label=root_label)
    owner_cache: dict[tuple[str, str], dict[tuple[str, str], list[Any]]] = {}
    live_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    for ref in refs:
        resolved = resolve(ref)
        if resolved is None:
            root.children.append(HierarchyNode(label=f"{ref.kind}/{ref.name}"))
            continue
        view, namespaced = resolved
        # Cluster-scoped kinds never inherit the release namespace: they
        # live at "", and defaulting would break the live match and goto.
        ns = (ref.namespace or namespace) if namespaced else ""
        index = _live_index(lookup, live_cache, view, ns)
        live = None if index is None else index.get(ref.name)
        # None means the view is not watched - absence is only meaningful
        # where a live watch actually feeds the store for the kind.
        marker = "" if live is not None or index is None else " (missing)"
        uid = str(getattr(live, "uid", "") or "")
        root.children.append(
            HierarchyNode(
                label=f"{ref.kind}/{ref.name}{marker}",
                kind=view,
                namespace=ns,
                name=ref.name,
                children=_runtime_children(view, uid, ns, lookup, owner_cache),
            )
        )
    return root


class HierarchyScreen(ModalScreen[tuple[str, str, str, str] | None]):
    """Read-only component tree; resolves to (action, kind, ns, name) or None."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("d", "pick_describe", "Describe", show=True),
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    HierarchyScreen {
        layout: vertical;
        background: $background;
    }
    HierarchyScreen #hierarchy-title {
        padding: 0 1;
    }
    HierarchyScreen Tree {
        height: 1fr;
    }
    """

    def __init__(self, title: str, root: HierarchyNode) -> None:
        super().__init__()
        self._title = title
        self._root = root

    def compose(self) -> ComposeResult:
        yield Footer()
        yield Static(f"Hierarchy: {self._title}", id="hierarchy-title", markup=False)
        yield Tree[HierarchyNode](self._root.label, data=self._root)

    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        self._populate(tree.root, self._root.children)
        tree.root.expand_all()
        tree.focus()

    def _populate(self, parent: TreeNode[HierarchyNode], nodes: list[HierarchyNode]) -> None:
        for node in nodes:
            if node.children:
                branch = parent.add(node.label, data=node)
                self._populate(branch, node.children)
            else:
                parent.add_leaf(node.label, data=node)

    def update_tree(self, root: HierarchyNode) -> None:
        """Rebuild from a fresh snapshot (store update while open). The
        cursor follows the previously selected *object* - a rebuild that
        inserts or removes rows above it must not change what Enter/`d`
        act on; only when it disappeared does a bounded line stand in."""
        self._root = root
        tree = self.query_one(Tree)
        target = self._cursor_identity(tree)
        cursor_line = tree.cursor_line
        tree.root.remove_children()
        tree.root.set_label(root.label)
        tree.root.data = root
        self._populate(tree.root, root.children)
        tree.root.expand_all()
        line = -1 if target is None else self._dfs_find(tree.root, target, 0)[0]
        tree.cursor_line = line if line >= 0 else min(cursor_line, tree.last_line)

    @staticmethod
    def _cursor_identity(tree: Tree[HierarchyNode]) -> HierarchyNode | None:
        cursor: TreeNode[HierarchyNode] | None = tree.cursor_node
        return None if cursor is None else cursor.data

    def _dfs_find(
        self, parent: TreeNode[HierarchyNode], target: HierarchyNode, counter: int
    ) -> tuple[int, int]:
        """(line of the node matching *target*'s identity or -1, lines seen).

        Counts nodes in display (preorder) sequence - everything is expanded
        after a rebuild, so the count is the cursor line. Navigable nodes
        match on (kind, namespace, name); display-only ones on label."""
        for child in parent.children:
            counter += 1
            data = child.data
            if data is not None and self._same_object(data, target):
                return counter, counter
            found, counter = self._dfs_find(child, target, counter)
            if found >= 0:
                return found, counter
        return -1, counter

    @staticmethod
    def _same_object(data: HierarchyNode, target: HierarchyNode) -> bool:
        if target.kind:
            return (data.kind, data.namespace, data.name) == (
                target.kind,
                target.namespace,
                target.name,
            )
        return data.label == target.label

    def _cursor_target(self) -> HierarchyNode | None:
        tree = self.query_one(Tree)
        cursor: TreeNode[HierarchyNode] | None = tree.cursor_node
        if cursor is None or cursor.data is None or not cursor.data.kind:
            return None
        return cursor.data

    def on_tree_node_selected(self, event: Tree.NodeSelected[HierarchyNode]) -> None:
        """Enter: jump to the selected object's view; display-only nodes
        keep the default expand/collapse toggle behaviour."""
        node = event.node.data
        if node is None or not node.kind:
            return
        event.stop()
        self.dismiss(("goto", node.kind, node.namespace, node.name))

    def action_pick_describe(self) -> None:
        node = self._cursor_target()
        if node is not None:
            self.dismiss(("describe", node.kind, node.namespace, node.name))

    def action_close(self) -> None:
        self.dismiss(None)
