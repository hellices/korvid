"""Model search screen — Task 10.

Search-first: a free-text box over the full catalog replaces a vendor list.
A provider name is a label and a search term, never a gate.
"""

from __future__ import annotations

import re
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from korvid.agent.model_profiles import ModelCatalog, ModelEntry, split_reference

#: Maximum capability suffix length guard — rendered only for known facts.
_CONTEXT_THRESHOLD = 1000  # tokens below this are probably miscoded

_PROVIDER_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _capability_suffix(entry: ModelEntry) -> str:
    """Compact badge string built only from tri-state facts that are True.

    An unknown capability (`None`) renders nothing at all rather than "no" —
    the distinction is the whole point of the tri-state.
    """
    parts: list[str] = []
    if entry.context_window_tokens and entry.context_window_tokens >= _CONTEXT_THRESHOLD:
        k = entry.context_window_tokens // 1000
        parts.append(f"{k}k ctx")
    if entry.supports_tools is True:
        parts.append("tools")
    if entry.supports_reasoning is True:
        parts.append("reasoning")
    return (" · " + " · ".join(parts)) if parts else ""


def _format_row(entry: ModelEntry) -> str:
    display = f"  {entry.display_name}" if entry.display_name else ""
    return f"{entry.reference}{display}{_capability_suffix(entry)}"


def _manual_option_label(reference: str) -> str:
    return f'Use "{reference}" (not in catalog)'


class ModelSearchScreen(ModalScreen["str | None"]):
    """Free-text model search returning a `provider/model` reference.

    Args:
        catalog: Searched on every keystroke. Search is synchronous and
            in-memory, so it is safe on the input handler.
        initial_query: Prefilled when editing an existing profile, so
            editing starts from the current model rather than blank.
        discovered: Entries a prior discovery produced, merged ahead of
            catalog results.
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    ModelSearchScreen {
        align: center middle;
    }
    ModelSearchScreen VerticalScroll {
        width: 76;
        max-width: 92%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    ModelSearchScreen #model-query {
        margin-bottom: 1;
    }
    ModelSearchScreen #model-results {
        height: auto;
        max-height: 16;
    }
    ModelSearchScreen #search-status {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        catalog: ModelCatalog,
        *,
        initial_query: str = "",
        discovered: tuple[ModelEntry, ...] = (),
    ) -> None:
        super().__init__()
        self._catalog = catalog
        self._initial_query = initial_query
        self._discovered = discovered
        #: Entries currently displayed, parallel to OptionList options.
        #: The final entry is ``None`` when the synthetic manual option is shown.
        self._shown_entries: tuple[ModelEntry, ...] = ()
        #: Whether the last option in the list is the synthetic manual entry.
        self._has_manual_option: bool = False

    # ------------------------------------------------------------------
    # Compose / mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Input(
                value=self._initial_query,
                placeholder="type to search — provider/model for manual entry",
                id="model-query",
            )
            yield OptionList(id="model-results")
            yield Static(
                "Type to search · Enter submits · Esc cancels",
                id="search-status",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#model-query", Input).focus()
        if self._initial_query:
            self._run_search(self._initial_query)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._run_search(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        query = event.value.strip()
        if not query:
            return
        self._submit_manual_reference(query)

    def _submit_manual_reference(self, query: str) -> None:
        reason = self._validate_manual_reference(query)
        if reason:
            self.query_one("#search-status", Static).update(reason)
            return
        self.dismiss(query)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        idx = event.option_index
        if self._has_manual_option and idx == len(self._shown_entries):
            # Synthetic manual option selected
            self._submit_manual_reference(self.query_one("#model-query", Input).value.strip())
            return
        if 0 <= idx < len(self._shown_entries):
            self.dismiss(self._shown_entries[idx].reference)

    def action_cancel(self) -> None:
        self.dismiss(None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_search(self, query: str) -> None:
        """Update the results list from the in-memory catalog. Synchronous."""
        results = self.query_one("#model-results", OptionList)
        status = self.query_one("#search-status", Static)
        results.clear_options()
        self._shown_entries = ()
        self._has_manual_option = False

        q = query.strip()
        if not q:
            status.update("Type to search · Enter submits · Esc cancels")
            return

        # Merge discovered entries ahead of catalog results.
        discovered_refs = {e.reference for e in self._discovered}
        catalog_hits = self._catalog.search(q, limit=50)
        # Filter discovered to only those matching the query.
        disc_matched = tuple(e for e in self._discovered if q.lower() in e.reference.lower())
        catalog_new = tuple(e for e in catalog_hits if e.reference not in discovered_refs)
        merged = disc_matched + catalog_new
        merged = merged[:50]

        self._shown_entries = merged

        options: list[Option] = [Option(_format_row(e)) for e in merged]

        # Synthetic manual option: shown when query contains a slash and
        # no catalog entry has an exact reference match.
        if "/" in q and not any(e.reference == q for e in merged):
            self._has_manual_option = True
            options.append(Option(_manual_option_label(q)))

        if not options:
            status.update(f'No catalog match for "{q}" — use provider/model format to add manually')
            return

        results.add_options(options)
        results.highlighted = 0

        count = len(merged)
        manual_note = " + manual entry" if self._has_manual_option else ""
        status.update(f"{count} result{'s' if count != 1 else ''}{manual_note}")

    @staticmethod
    def _validate_manual_reference(query: str) -> str:
        """Return an error reason string if ``query`` is not a valid reference."""
        if not query:
            return "Enter a model reference (provider/model)."
        if " " in query or "\t" in query:
            return "Reference must not contain whitespace — use provider/model format."
        if "/" not in query:
            return "Use provider/model format — e.g. openai/gpt-4o."
        provider, _ = split_reference(query)
        if not _PROVIDER_PREFIX_RE.match(provider):
            return (
                f"Provider prefix {provider!r} is invalid — "
                "use provider/model format with a lowercase alphanumeric provider."
            )
        return ""
