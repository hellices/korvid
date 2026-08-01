"""Helm chart repository management screen (issue #106).

Lists configured repositories, adds new ones, and refreshes their indexes.
`helm repo add`/`update` mutate only the local helm configuration — never
the cluster — so an explicit typed form (not the write-approval gate)
is the right confirmation shape here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, LoadingIndicator, OptionList, Static

from korvid.k8s.helmcli import HelmError, HelmRepo
from korvid.ui.widgets.confirm_screen import FreshKeysInput

RepoListFn = Callable[[], Awaitable[list[HelmRepo]]]
RepoAddFn = Callable[[str, str], Awaitable[str]]
RepoUpdateFn = Callable[[], Awaitable[str]]


class HelmRepoScreen(ModalScreen[str | None]):
    """List / add / update helm chart repositories.

    Enter on a repository row dismisses with that repo's name — the chart
    picker underneath scopes its search to the repo (issue #137). Esc
    dismisses with None (management only, nothing picked).
    """

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("ctrl+r", "update_repos", "Update indexes", show=True),
    ]

    DEFAULT_CSS = """
    HelmRepoScreen {
        align: center middle;
    }
    HelmRepoScreen > VerticalScroll {
        width: 72;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    HelmRepoScreen #repo-title {
        padding-bottom: 1;
        text-style: bold;
    }
    HelmRepoScreen #repo-list {
        height: auto;
        max-height: 10;
    }
    HelmRepoScreen #repo-loading {
        height: 1;
    }
    HelmRepoScreen Horizontal {
        height: auto;
    }
    HelmRepoScreen #repo-name {
        width: 24;
    }
    HelmRepoScreen #repo-url {
        width: 1fr;
    }
    HelmRepoScreen #repo-status {
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        repo_list: RepoListFn,
        repo_add: RepoAddFn,
        repo_update: RepoUpdateFn,
    ) -> None:
        super().__init__()
        self._repo_list = repo_list
        self._repo_add = repo_add
        self._repo_update = repo_update
        self._created_time = Message().time
        #: one repo operation at a time: `helm repo add/update` mutate local
        #: helm config and must never be cancelled mid-flight by a newer
        #: action — new work is rejected while one is pending.
        self._busy = False
        #: the pending operation mutates helm config: dismissing the screen
        #: (which cancels its workers and kills the subprocess) is rejected
        #: until it finishes. A read-only list may be abandoned freely.
        self._mutating = False
        #: rows currently shown in #repo-list, in display order — maps a
        #: selected option index back to its repository.
        self._repos: list[HelmRepo] = []

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static("Helm chart repositories", id="repo-title", markup=False)
            yield OptionList(id="repo-list")
            yield LoadingIndicator(id="repo-loading")
            with Horizontal():
                yield FreshKeysInput(self._created_time, placeholder="name", id="repo-name")
                yield FreshKeysInput(
                    self._created_time,
                    placeholder="https://… — Enter adds the repository",
                    id="repo-url",
                )
            yield Static(
                "Enter on a repo: browse its charts — Enter: add — Ctrl-R: update — Esc: close",
                id="repo-status",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#repo-name", Input).focus()
        self._start(self._refresh_list(), mutating=False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = self.query_one("#repo-name", Input).value.strip()
        url = self.query_one("#repo-url", Input).value.strip()
        if not name or not url:
            self._status("both a repository name and URL are required")
            return
        self._start(self._add_repo(name, url))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Enter on a repo row: hand the repo to the chart picker below
        (issue #137) — browsing is read-only, but a pending mutation still
        owns the screen until it finishes."""
        event.stop()
        if self._mutating:
            self._status("still working — wait for the current operation to finish")
            return
        if 0 <= event.option_index < len(self._repos):
            self.dismiss(self._repos[event.option_index].name)

    def action_close(self) -> None:
        if self._mutating:
            self._status("still working — wait for the current operation to finish")
            return
        self.dismiss(None)

    def action_update_repos(self) -> None:
        self._start(self._update_repos())

    def _start(self, work: Coroutine[Any, Any, None], *, mutating: bool = True) -> None:
        if self._busy:
            work.close()
            self._status("still working — wait for the current operation to finish")
            return
        self._busy = True
        self._mutating = mutating
        self.run_worker(self._guarded(work), group="helm-repos")

    async def _guarded(self, work: Coroutine[Any, Any, None]) -> None:
        try:
            await work
        finally:
            self._busy = False
            self._mutating = False

    def _status(self, text: str) -> None:
        self.query_one("#repo-status", Static).update(text)

    def _loading(self, on: bool) -> None:
        self.query_one("#repo-loading", LoadingIndicator).display = on

    async def _refresh_list(self) -> None:
        self._loading(True)
        try:
            repos = await self._repo_list()
        except HelmError as exc:
            self._status(f"helm repo list failed: {exc}")
            return
        finally:
            self._loading(False)
        listing = self.query_one("#repo-list", OptionList)
        listing.clear_options()
        self._repos = list(repos)
        if not repos:
            self._status("no repositories configured — add one below")
            return
        listing.add_options([f"{repo.name}  {repo.url}" for repo in repos])
        self._status(f"{len(repos)} repositories")

    async def _add_repo(self, name: str, url: str) -> None:
        self._loading(True)
        self._status(f"Adding repository {name!r}…")
        try:
            result = await self._repo_add(name, url)
        except HelmError as exc:
            self._status(str(exc))
            return
        finally:
            self._loading(False)
        self.query_one("#repo-name", Input).value = ""
        self.query_one("#repo-url", Input).value = ""
        self._status(result.strip() or f"repository {name!r} added")
        await self._refresh_list()

    async def _update_repos(self) -> None:
        self._loading(True)
        self._status("Updating repository indexes…")
        try:
            result = await self._repo_update()
        except HelmError as exc:
            self._status(f"helm repo update failed: {exc}")
            return
        finally:
            self._loading(False)
        tail = result.strip().splitlines()[-1] if result.strip() else "indexes updated"
        self._status(tail)
