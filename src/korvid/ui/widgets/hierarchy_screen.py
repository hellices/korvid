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
from korvid.k8s.relations import owned_by

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


def _runtime_children(
    view: str,
    parent_uid: str,
    namespace: str,
    lookup: Callable[[str], list[Any]],
) -> list[HierarchyNode]:
    """Live descendants of one object via ownerReferences, recursively."""
    step = _RUNTIME_CHILDREN.get(view)
    if step is None or not parent_uid:
        return []
    child_view, child_kind = step
    nodes: list[HierarchyNode] = []
    for obj in lookup(child_view):
        if getattr(obj, "namespace", "") != namespace or not owned_by(obj, parent_uid):
            continue
        name = str(getattr(obj, "name", ""))
        nodes.append(
            HierarchyNode(
                label=f"{child_kind}/{name}",
                kind=child_view,
                namespace=namespace,
                name=name,
                children=_runtime_children(
                    child_view, str(getattr(obj, "uid", "")), namespace, lookup
                ),
            )
        )
    return nodes


def build_hierarchy(
    root_label: str,
    refs: Sequence[ComponentRef],
    *,
    namespace: str,
    resolve: Callable[[str], str | None],
    lookup: Callable[[str], list[Any]],
) -> HierarchyNode:
    """Component refs → display tree rooted at *root_label*.

    ``resolve`` maps a manifest Kind to its canonical plural view name (None
    when undiscovered — the node then renders display-only). ``lookup``
    returns the live summaries for a view; a resolvable component with no
    live match is marked "(missing)" but stays navigable so describe can
    surface the 404 explanation.
    """
    root = HierarchyNode(label=root_label)
    for ref in refs:
        ns = ref.namespace or namespace
        view = resolve(ref.kind)
        if view is None:
            root.children.append(HierarchyNode(label=f"{ref.kind}/{ref.name}"))
            continue
        bucket = lookup(view)
        live = next(
            (
                obj
                for obj in bucket
                if getattr(obj, "name", None) == ref.name and getattr(obj, "namespace", None) == ns
            ),
            None,
        )
        # An empty bucket means the view is not watched right now - absence
        # is only meaningful where the store actually has data for the kind.
        marker = "" if live is not None or not bucket else " (missing)"
        uid = str(getattr(live, "uid", "") or "")
        root.children.append(
            HierarchyNode(
                label=f"{ref.kind}/{ref.name}{marker}",
                kind=view,
                namespace=ns,
                name=ref.name,
                children=_runtime_children(view, uid, ns, lookup),
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
