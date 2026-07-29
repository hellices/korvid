"""Helm chart repository management screen (issue #106).

Lists configured repositories, adds new ones, and refreshes their indexes.
`helm repo add`/`update` mutate only the local helm configuration — never
the cluster — so an explicit typed form (not the write-approval gate)
is the right confirmation shape here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, LoadingIndicator, OptionList, Static

from korvid.k8s.helmcli import HelmError, HelmRepo
from korvid.ui.widgets.confirm_screen import FreshKeysInput

RepoListFn = Callable[[], Awaitable[list[HelmRepo]]]
RepoAddFn = Callable[[str, str], Awaitable[str]]
RepoUpdateFn = Callable[[], Awaitable[str]]


class HelmRepoScreen(ModalScreen[None]):
    """List / add / update helm chart repositories."""

    BINDINGS: ClassVar[list[Binding | tuple[str, str] | tuple[str, str, str]]] = [
        Binding("escape", "close", "Close", show=True),
        Binding("ctrl+r", "update_repos", "Update indexes", show=True),
    ]

    DEFAULT_CSS = """
    HelmRepoScreen {
        align: center middle;
    }
    HelmRepoScreen > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
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

    def compose(self) -> ComposeResult:
        with Vertical():
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
                "Enter: add — Ctrl-R: update indexes — Esc: close",
                id="repo-status",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#repo-name", Input).focus()
        self._start(self._refresh_list())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        name = self.query_one("#repo-name", Input).value.strip()
        url = self.query_one("#repo-url", Input).value.strip()
        if not name or not url:
            self._status("both a repository name and URL are required")
            return
        self._start(self._add_repo(name, url))

    def action_close(self) -> None:
        self.dismiss(None)

    def action_update_repos(self) -> None:
        self._start(self._update_repos())

    def _start(self, work: Awaitable[None]) -> None:
        self.run_worker(work, exclusive=True, group="helm-repos")

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
