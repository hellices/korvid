"""What the user is currently looking at, as a read-only boundary (#187).

Controllers need to answer "which kind, which namespace, which row is
selected, what does this alias resolve to" - and nothing more. Naming that
set makes the read-only-ness structural: there are no setters here, so a
controller cannot navigate the app as a side effect of doing its own job.

Every method is a *live* read. A `:ctx` switch retargets the alias table and
the store, and the user moves the selection constantly, so a controller that
cached any of this would act on a stale view.

`AppViewState` on `KorvidApp` is the single implementation, an adapter for
the same reason `AppUIBridge` and `AppWriteGate` are - Textual's `App`
metaclass conflicts with `ABCMeta`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from korvid.core.store import Summary
from korvid.k8s.discovery import ResourceMeta


class ViewState(ABC):
    """Read-only access to the focused pane and the resources behind it."""

    @abstractmethod
    def current_kind(self) -> str:
        """Resource kind of the focused pane, as the user typed it."""

    @abstractmethod
    def current_scope(self) -> str:
        """Namespace the focused pane is scoped to, or the all-namespaces marker."""

    @abstractmethod
    def current_namespace(self) -> str:
        """Alias of `current_scope`, kept because both names are in use."""

    @abstractmethod
    def canonical_kind(self, kind: str) -> str:
        """Resolve an alias to the view kind that watching and writes agree on.

        Not always the bare plural: when a same-plural CRD from another group
        won the alias collision, the qualified alias *is* canonical, because
        it is the one that names the meta the user meant.
        """

    @abstractmethod
    def aliases(self) -> Mapping[str, ResourceMeta]:
        """The discovered alias table. Rebuilt by a `:ctx` switch.

        A `Mapping` view, not the table itself: rewriting an alias would
        retarget every later write the app makes, which is the app's
        decision to take on a `:ctx` switch and no controller's.
        """

    @abstractmethod
    def resources(self, kind: str, scope: str) -> list[Summary]:
        """Objects the watch has loaded for (kind, scope), sorted.

        A query rather than the `ResourceStore`, whose `clear`, `clear_all`
        and `apply_event` would let a controller erase or fabricate the
        view the user is looking at.
        """

    @abstractmethod
    def readonly(self) -> bool:
        """Whether the session refuses every write.

        Narrower than handing out `KorvidConfig`, which is only shallowly
        frozen: its `keybindings` and `agent_options` dicts are mutable, and
        no controller has business reading the agent configuration.
        """

    @abstractmethod
    def default_namespace(self) -> str | None:
        """Session default namespace, or None when the session set none.

        None is distinct from `"default"`: callers decide the fallback,
        because the right one differs between a write and a read.
        """

    @abstractmethod
    def selected_ns_name(self) -> tuple[str | None, str | None]:
        """(namespace, name) of the selected row, or (None, None) with a warning."""

    @abstractmethod
    def selected_uid(self, namespace: str | None, name: str) -> str | None:
        """UID of the selected object, for pinning a write to one incarnation."""

    @abstractmethod
    def gvr_label(self, meta: ResourceMeta) -> str:
        """Group-qualified plural, so messages disambiguate same-plural kinds."""

    @abstractmethod
    def write_locus(self, namespace: str | None) -> str:
        """Human phrasing for where a write lands ('in ns/x' or cluster-wide)."""
