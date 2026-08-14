"""Read-only session timeline view (issue #282, Task 4).

`SessionTimelineScreen` renders one `SessionTimeline`'s already-bounded,
in-memory `snapshot()` as a keyboard-navigable table. It performs no I/O
and holds no state beyond the toggled filters (epoch/source/resource) —
the caller (the app) owns the `SessionTimeline` and reopens this screen
with a fresh `current_epoch`/`resource_toggle` on every `T` press.

Navigation never parses rendered display strings: each row's goto target
(`kind_alias`, namespace, name) is read straight from the snapshot entry's
own `TimelineResourceRef` into a `RowKey`-keyed mapping populated at render
time, and Enter resolves through that mapping. `occurred_at`, Warning-event
`reason`/`note`, context names, and resource identifiers are all
cluster-controlled — every one of them is wrapped in a markup-disabled
`rich.text.Text` (or rendered through a `markup=False` `Static`) before it
becomes visible, so a literal ``[style]...[/]`` sequence in any of them can
never be misread as Rich markup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Static

from korvid.core.session_timeline import (
    ContextSwitchPayload,
    SessionTimeline,
    TimelineEntry,
    TimelineResourceRef,
    TimelineSource,
    WarningEventPayload,
    WatchDeltaPayload,
)

#: `("goto", kind_alias, namespace, name)` — the dismissed navigation target.
#: `kind_alias` is a discovered-view alias `KorvidApp._jump_to_object`
#: already accepts directly — unlike the relationship graph's
#: `(group, kind)` pair, the timeline's own resource refs carry the alias.
TimelineGotoResult = tuple[str, str, str, str]

#: Deterministic `s` cycle: all -> watch -> event -> context -> write -> all.
_SOURCE_CYCLE: tuple[TimelineSource | None, ...] = (
    None,
    TimelineSource.WATCH,
    TimelineSource.EVENT,
    TimelineSource.CONTEXT,
    TimelineSource.WRITE,
)

_COLUMNS = ("SEQ", "TIME", "EPOCH", "SOURCE", "RESOURCE", "DETAIL")

_IDLE_STATUS = "Enter: navigate a resource row · e: epoch · s: source · r: resource"

_NO_TOGGLE_STATUS = "No resource was selected when the timeline opened - nothing to toggle"

_NO_TARGET_STATUS = "Selected row has no navigable resource"


@dataclass(frozen=True, slots=True)
class _RowTarget:
    """What Enter on one data row resolves to."""

    kind_alias: str | None
    namespace: str
    name: str


def _resource_label(resource: TimelineResourceRef) -> str:
    """A readable "kind/namespace/name" label, blank parts dropped."""
    parts = (resource.kind_alias or resource.display_kind, resource.namespace, resource.name)
    return "/".join(part for part in parts if part)


class SessionTimelineScreen(ModalScreen[TimelineGotoResult | None]):
    """Bounded, filterable view of one session's watch/event/context/write
    history.

    Dismisses with `("goto", kind_alias, namespace, name)` when Enter is
    pressed on a row whose entry carries a resource, or `None` on Escape.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
        Binding("e", "toggle_epoch", "Epoch", show=True),
        Binding("s", "cycle_source", "Source", show=True),
        Binding("r", "toggle_resource", "Resource", show=True),
    ]

    DEFAULT_CSS = """
    SessionTimelineScreen {
        layout: vertical;
        background: $background;
    }
    SessionTimelineScreen #timeline-title {
        padding: 0 1;
        text-style: bold;
    }
    SessionTimelineScreen #timeline-banner {
        padding: 0 1;
        color: $warning;
    }
    SessionTimelineScreen #timeline-status {
        padding: 0 1;
        color: $text-muted;
    }
    SessionTimelineScreen DataTable {
        height: 1fr;
    }
    """

    def __init__(
        self,
        timeline: SessionTimeline,
        *,
        current_epoch: int,
        resource_toggle: TimelineResourceRef | None,
    ) -> None:
        super().__init__()
        self._timeline = timeline
        self._current_epoch = current_epoch
        #: Default filter (spec): current epoch, every source, every
        #: resource — set once here and only ever toggled, never re-derived.
        self._epoch_filter: int | None = current_epoch
        self._source_filter: TimelineSource | None = None
        #: The exact resource captured when the modal opened (issue #282);
        #: `r` toggles between it and "all resources" — it is never
        #: re-read from the underlying table while this screen is open.
        self._resource_toggle = resource_toggle
        self._resource_filter: TimelineResourceRef | None = None
        self._targets: dict[str, _RowTarget] = {}

    def compose(self) -> ComposeResult:
        yield Footer()
        yield Static("Session timeline", id="timeline-title", markup=False)
        yield Static("", id="timeline-banner", markup=False)
        yield Static(_IDLE_STATUS, id="timeline-status", markup=False)
        yield DataTable[str | Text](id="timeline-table")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        self._render_table()
        table.focus()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_toggle_epoch(self) -> None:
        self._epoch_filter = None if self._epoch_filter is not None else self._current_epoch
        self._render_table()

    def action_cycle_source(self) -> None:
        index = _SOURCE_CYCLE.index(self._source_filter)
        self._source_filter = _SOURCE_CYCLE[(index + 1) % len(_SOURCE_CYCLE)]
        self._render_table()

    def action_toggle_resource(self) -> None:
        if self._resource_toggle is None:
            self.query_one("#timeline-status", Static).update(_NO_TOGGLE_STATUS)
            return
        self._resource_filter = None if self._resource_filter is not None else self._resource_toggle
        self._render_table()

    def _render_table(self) -> None:
        snapshot = self._timeline.snapshot(
            epoch=self._epoch_filter, source=self._source_filter, resource=self._resource_filter
        )
        table = self.query_one(DataTable)
        table.clear()
        self._targets = {}
        for entry in reversed(snapshot.entries):
            row_key = f"row-{entry.sequence}"
            resource_label = "-"
            if entry.resource is not None:
                resource_label = _resource_label(entry.resource)
                self._targets[row_key] = _RowTarget(
                    entry.resource.kind_alias, entry.resource.namespace, entry.resource.name
                )
            table.add_row(
                str(entry.sequence),
                Text(entry.occurred_at),
                str(entry.epoch),
                entry.source.value,
                Text(resource_label),
                Text(self._detail(entry)),
                key=row_key,
            )
        stats = snapshot.stats
        self.query_one("#timeline-banner", Static).update(
            f"stored={stats.entry_count} entries · bytes={stats.encoded_bytes} · "
            f"evicted={stats.evicted} · refused={stats.refused}"
        )
        self.query_one("#timeline-status", Static).update(self._filter_status())

    def _filter_status(self) -> str:
        epoch_text = "current" if self._epoch_filter is not None else "all"
        source_text = self._source_filter.value if self._source_filter is not None else "all"
        resource_text = "selected" if self._resource_filter is not None else "all"
        return (
            f"Filters: epoch={epoch_text} · source={source_text} · resource={resource_text} · "
            "Enter: navigate"
        )

    @staticmethod
    def _detail(entry: TimelineEntry) -> str:
        payload = entry.payload
        if isinstance(payload, WatchDeltaPayload):
            return payload.verb
        if isinstance(payload, WarningEventPayload):
            return f"{payload.reason} x{payload.count}: {payload.note}"
        if isinstance(payload, ContextSwitchPayload):
            arrow = f"{payload.from_context or '(default)'} -> {payload.to_context or '(default)'}"
            return f"{payload.phase}: {arrow} {payload.note}".strip()
        # payload: TimelinePayload is a closed 4-member union and the three
        # preceding branches are exhaustive over the others, so this is
        # always a WriteAuditPayload - no runtime assertion needed.
        return f"{payload.action}: {payload.outcome}"

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        target = self._targets.get(str(event.row_key.value or ""))
        if target is None or target.kind_alias is None:
            self.query_one("#timeline-status", Static).update(_NO_TARGET_STATUS)
            return
        self.dismiss(("goto", target.kind_alias, target.namespace, target.name))
