"""Search-first helm chart picker (issue #106).

`helm search repo` across every configured repository is slow: the screen
opens instantly and fetches charts per keyword instead, with a
LoadingIndicator while the search subprocess runs. The search itself is an
injected async callable so the screen stays testable without a helm binary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, LoadingIndicator, OptionList, Static

from korvid.k8s.helmcli import ChartHit, HelmError
from korvid.ui.widgets.confirm_screen import FreshKeysInput

SearchFn = Callable[[str], Awaitable[list[ChartHit]]]


class HelmChartSearchScreen(ModalScreen["ChartHit | None"]):
    """Keyword search over configured helm repos; picking a chart dismisses
    with its `ChartHit`, Esc dismisses with None."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+r", "manage_repos", "Repositories", show=True),
    ]

    DEFAULT_CSS = """
    HelmChartSearchScreen {
        align: center middle;
    }
    HelmChartSearchScreen VerticalScroll {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    HelmChartSearchScreen #chart-search-title {
        padding-bottom: 1;
        text-style: bold;
    }
    HelmChartSearchScreen #chart-loading {
        height: 1;
    }
    HelmChartSearchScreen #chart-results {
        height: auto;
        max-height: 14;
    }
    HelmChartSearchScreen #chart-status {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        search: SearchFn,
        *,
        title: str = "Install helm chart",
        initial: str = "",
        on_manage_repos: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._search = search
        self._title = title
        self._initial = initial
        self._on_manage_repos = on_manage_repos
        self._hits: list[ChartHit] = []
        self._created_time = Message().time
        #: generation counter: an exclusive resubmit cancels the previous
        #: search worker, whose cleanup must not touch the spinner or the
        #: results the replacement owns.
        self._search_seq = 0

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(self._title, id="chart-search-title", markup=False)
            yield FreshKeysInput(
                self._created_time,
                value=self._initial,
                placeholder="keyword — Enter searches configured repos",
                id="chart-keyword",
            )
            yield LoadingIndicator(id="chart-loading")
            yield OptionList(id="chart-results")
            yield Static(
                "Enter: search / pick — Ctrl-R: repositories — Esc: cancel",
                id="chart-status",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#chart-loading", LoadingIndicator).display = False
        self.query_one("#chart-keyword", Input).focus()
        if self._initial:
            self._start_search(self._initial)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self._start_search(event.value.strip())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if 0 <= event.option_index < len(self._hits):
            self.dismiss(self._hits[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_manage_repos(self) -> None:
        if self._on_manage_repos is not None:
            self._on_manage_repos()

    def browse_repo(self, repo: str) -> None:
        """Scope the search to one repository (issue #137): the `repo/`
        prefix convention, typed for you, searched immediately.

        `helm search repo` substring-matches, so `stable/` would also
        surface `my-stable/...` charts: the results are filtered to the
        exact name prefix. A manual re-search is unfiltered again.
        """
        keyword = f"{repo}/"
        self.query_one("#chart-keyword", Input).value = keyword
        self._start_search(keyword, repo_scope=repo)

    def _start_search(self, keyword: str, *, repo_scope: str | None = None) -> None:
        self._search_seq += 1
        self.run_worker(
            self._run_search(keyword, self._search_seq, repo_scope),
            exclusive=True,
            group="helm-chart-search",
        )

    async def _run_search(self, keyword: str, seq: int, repo_scope: str | None = None) -> None:
        loading = self.query_one("#chart-loading", LoadingIndicator)
        status = self.query_one("#chart-status", Static)
        results = self.query_one("#chart-results", OptionList)
        loading.display = True
        status.update(f"Searching charts for {keyword!r}…" if keyword else "Fetching charts…")
        results.clear_options()
        self._hits = []
        try:
            hits = await self._search(keyword)
        except HelmError as exc:
            if seq == self._search_seq:
                status.update(f"helm search failed: {exc}")
            return
        finally:
            # Only the live search may clear the spinner: a cancelled
            # predecessor's cleanup can run after its replacement showed it.
            if seq == self._search_seq:
                loading.display = False
        if seq != self._search_seq:
            return
        if repo_scope is not None:
            hits = [hit for hit in hits if hit.name.startswith(f"{repo_scope}/")]
        if not hits:
            status.update(
                f"no charts matched {keyword!r} — try another keyword or add a repository (Ctrl-R)"
            )
            return
        self._hits = list(hits)
        results.add_options([f"{hit.name}  {hit.version}" for hit in self._hits])
        results.highlighted = 0
        results.focus()
        status.update(f"{len(self._hits)} charts — Enter picks, Esc cancels")
