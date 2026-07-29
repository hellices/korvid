"""HelmRepoScreen: list/add/update helm chart repositories (issue #106).

`helm repo add` writes local helm config only (no cluster mutation), so the
screen uses an explicit typed form rather than the write-approval gate.
"""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input, OptionList, Static

from korvid.k8s.helmcli import HelmError, HelmRepo
from korvid.ui.widgets.helm_repos import HelmRepoScreen

from .waits import until

_BITNAMI = HelmRepo(name="bitnami", url="https://charts.bitnami.com/bitnami")
_JETSTACK = HelmRepo(name="jetstack", url="https://charts.jetstack.io")


class HostApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    def compose(self) -> ComposeResult:
        yield Static("host")


class FakeRepoOps:
    def __init__(self, repos: list[HelmRepo] | None = None) -> None:
        self.repos = repos if repos is not None else [_BITNAMI]
        self.add_error: str | None = None
        self.added: list[tuple[str, str]] = []
        self.updates = 0
        self.gate: asyncio.Event | None = None
        self.add_gate: asyncio.Event | None = None
        self.add_started = 0

    async def repo_list(self) -> list[HelmRepo]:
        if self.gate is not None:
            await self.gate.wait()
        return list(self.repos)

    async def repo_add(self, name: str, url: str) -> str:
        self.add_started += 1
        if self.add_gate is not None:
            await self.add_gate.wait()
        if self.add_error is not None:
            raise HelmError(self.add_error)
        self.added.append((name, url))
        self.repos.append(HelmRepo(name=name, url=url))
        return f'"{name}" has been added to your repositories'

    async def repo_update(self) -> str:
        self.updates += 1
        return "Update Complete."


async def _open(app: HostApp, ops: FakeRepoOps) -> HelmRepoScreen:
    screen = HelmRepoScreen(
        repo_list=ops.repo_list, repo_add=ops.repo_add, repo_update=ops.repo_update
    )

    def _done(_: object) -> None:
        app.closed = True

    await app.push_screen(screen, _done)
    return screen


async def _listed(app: HostApp, pilot: object, n: int) -> None:
    await until(
        pilot,
        lambda: app.screen.query_one("#repo-list", OptionList).option_count == n,
        label=f"{n} repos listed",
    )


async def test_lists_repos_on_mount() -> None:
    app = HostApp()
    ops = FakeRepoOps([_BITNAMI, _JETSTACK])
    async with app.run_test() as pilot:
        await _open(app, ops)
        await _listed(app, pilot, 2)
        first = app.screen.query_one("#repo-list", OptionList).get_option_at_index(0)
        assert "bitnami" in str(first.prompt)
        assert "https://charts.bitnami.com/bitnami" in str(first.prompt)


async def test_no_repos_hint() -> None:
    app = HostApp()
    ops = FakeRepoOps([])
    async with app.run_test() as pilot:
        await _open(app, ops)
        await until(
            pilot,
            lambda: (
                "no repositories configured"
                in str(app.screen.query_one("#repo-status", Static).render())
            ),
            label="empty hint shown",
        )


async def test_add_repo_calls_helm_and_refreshes_list() -> None:
    app = HostApp()
    ops = FakeRepoOps([_BITNAMI])
    async with app.run_test() as pilot:
        screen = await _open(app, ops)
        await _listed(app, pilot, 1)
        screen.query_one("#repo-name", Input).value = "jetstack"
        screen.query_one("#repo-url", Input).value = "https://charts.jetstack.io"
        screen.query_one("#repo-url", Input).focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: ops.added == [("jetstack", "https://charts.jetstack.io")],
            label="repo added",
        )
        await _listed(app, pilot, 2)


async def test_add_requires_name_and_url() -> None:
    app = HostApp()
    ops = FakeRepoOps()
    async with app.run_test() as pilot:
        screen = await _open(app, ops)
        await _listed(app, pilot, 1)
        screen.query_one("#repo-url", Input).focus()
        await pilot.press("enter")  # both fields empty
        await until(
            pilot,
            lambda: "name and URL" in str(app.screen.query_one("#repo-status", Static).render()),
            label="validation message",
        )
        assert ops.added == []


async def test_add_failure_reports_and_keeps_screen() -> None:
    app = HostApp()
    ops = FakeRepoOps()
    ops.add_error = "repo add failed: looks like a bad URL"
    async with app.run_test() as pilot:
        screen = await _open(app, ops)
        await _listed(app, pilot, 1)
        screen.query_one("#repo-name", Input).value = "broken"
        screen.query_one("#repo-url", Input).value = "https://nope.invalid"
        screen.query_one("#repo-url", Input).focus()
        await pilot.press("enter")
        await until(
            pilot,
            lambda: "bad URL" in str(app.screen.query_one("#repo-status", Static).render()),
            label="add error surfaced",
        )
        assert isinstance(app.screen, HelmRepoScreen)


async def test_ctrl_r_updates_repo_indexes() -> None:
    app = HostApp()
    ops = FakeRepoOps()
    async with app.run_test() as pilot:
        await _open(app, ops)
        await _listed(app, pilot, 1)
        await pilot.press("ctrl+r")
        await until(pilot, lambda: ops.updates == 1, label="repo update ran")
        await until(
            pilot,
            lambda: "Update Complete" in str(app.screen.query_one("#repo-status", Static).render()),
            label="update result shown",
        )


async def test_escape_closes() -> None:
    app = HostApp()
    async with app.run_test() as pilot:
        await _open(app, FakeRepoOps())
        await _listed(app, pilot, 1)
        await pilot.press("escape")
        await until(pilot, lambda: app.closed, label="screen closed")


async def test_actions_are_rejected_while_a_repo_mutation_is_pending() -> None:
    """`helm repo add` mutates local config: a queued Ctrl-R/Enter must not
    cancel it mid-flight — new actions are rejected until it finishes."""
    app = HostApp()
    ops = FakeRepoOps()
    ops.add_gate = asyncio.Event()
    async with app.run_test() as pilot:
        screen = await _open(app, ops)
        await _listed(app, pilot, 1)
        screen.query_one("#repo-name", Input).value = "jetstack"
        screen.query_one("#repo-url", Input).value = "https://charts.jetstack.io"
        screen.query_one("#repo-url", Input).focus()
        await pilot.press("enter")  # add: pending on the gate
        await until(pilot, lambda: ops.add_started == 1, label="add in flight")
        await pilot.press("ctrl+r")  # must be rejected, not cancel the add
        await pilot.pause()
        ops.add_gate.set()
        await until(
            pilot,
            lambda: ops.added == [("jetstack", "https://charts.jetstack.io")],
            label="add completed despite the queued update",
        )
        assert ops.updates == 0
