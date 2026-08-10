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

from korvid.core.config import KorvidConfig
from korvid.core.store import ResourceStore
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
    def aliases(self) -> dict[str, ResourceMeta]:
        """The discovered alias table. Rebuilt by a `:ctx` switch."""

    @abstractmethod
    def store(self) -> ResourceStore:
        """The resource store backing the current cluster."""

    @abstractmethod
    def config(self) -> KorvidConfig:
        """Session configuration - read-only mode, default namespace, context."""

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
